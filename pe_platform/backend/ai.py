"""Local transport and scope adapters for the pinned PE assistant/runtime.

Conversation, context/memory selection, policy, tool outcomes, run tracking,
provider loop, streaming and numeric verification are the native implementations.
This boundary supplies workspace credentials and a cash-only proposal surface.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
import asyncio
import json
import math
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import proposals

CASH_TOOLS = frozenset(
    {
        "get_cashflow_snapshot",
        "analyze_cashflow_position",
        "get_cashflow_actuals_review",
        "get_ar_ap_summary",
        "get_weekly_review",
        "update_cashflow_cells",
        "layer_cashflow_schedule",
        "categorize_cashflow_actuals",
        "delete_cashflow_override",
        "refresh_ar_ap_snapshot",
        "refresh_cashflow_forecast",
        "spread_cashflow_balance",
        "get_module_context",
        "teach_peregrine",
    }
)
_BOUND_CLIENT = ContextVar("vesper_pe_workspace_model_client", default=None)
_ACTIVE_RUNS = set()
_ACTIVE_LOCK = threading.Lock()
_PROVIDER_FAILURE = "The selected workspace provider request did not complete"
_CONVERSATION_SCOPE = ContextVar("vesper_pe_finance_conversation_scope", default=None)
_CHAT_MESSAGE_MODELS = set()


def _chat_metadata(scope, base):
    return {
        "local_base_fingerprint": base,
        "local_scope": {
            "workspace_id": scope["id"],
            "revision": scope["revision"],
            "finance_base_fingerprint": base,
        },
    }


def _conversation_for_base(db, scope, base, *, create=False):
    from app.models import ChatConversation

    # Native rows remain the system of record. A complete deterministic digest
    # separates finance epochs without rewriting or deleting historical evidence.
    title = "local-cashflow:" + proposals.digest({"workspace_id": scope["id"], "finance_base": base})
    conversation = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.realm_id == scope["realm_id"],
            ChatConversation.user_id == scope["user_id"],
            ChatConversation.organization_id == scope["organization_id"],
            ChatConversation.organization_company_id == scope["organization_company_id"],
            ChatConversation.title == title,
        )
        .order_by(ChatConversation.id.desc())
        .first()
    )
    if conversation is None and create:
        conversation = ChatConversation(
            realm_id=scope["realm_id"],
            user_id=scope["user_id"],
            organization_id=scope["organization_id"],
            organization_company_id=scope["organization_company_id"],
            title=title,
        )
        db.add(conversation)
        db.flush()
    return conversation


def _install_conversation_scope():
    from app.assistant import main as native
    from app.models import ChatMessage
    from sqlalchemy import event
    from sqlalchemy.orm import object_session

    original = native._get_or_create_conversation
    if not getattr(original, "_local_finance_scope", False):

        @wraps(original)
        def scoped(db, *, realm_id, user, organization, organization_company):
            binding = _CONVERSATION_SCOPE.get()
            if binding is None:
                return original(
                    db,
                    realm_id=realm_id,
                    user=user,
                    organization=organization,
                    organization_company=organization_company,
                )
            runtime, scope, base = binding
            if (
                str(realm_id) != scope["realm_id"]
                or user.id != scope["user_id"]
                or getattr(organization, "id", None) != scope["organization_id"]
                or getattr(organization_company, "id", None) != scope["organization_company_id"]
            ):
                raise HTTPException(403, "Conversation scope differs from the current finance workspace")
            proposals.require_current(runtime, scope, base, db)
            return _conversation_for_base(db, scope, base, create=True)

        scoped._local_finance_scope = True
        native._get_or_create_conversation = scoped
    if ChatMessage not in _CHAT_MESSAGE_MODELS:

        def stamp(_mapper, _connection, row):
            db = object_session(row)
            metadata = db.info.get("local_chat_metadata") if db is not None else None
            if metadata is not None:
                row.metadata_json = {**(row.metadata_json or {}), **metadata}

        event.listen(ChatMessage, "before_insert", stamp)
        _CHAT_MESSAGE_MODELS.add(ChatMessage)


def _provider_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        # A provider may echo an entered credential in arbitrary error prose.
        # Pattern redaction is not an adequate boundary for those exceptions.
        raise RuntimeError(_PROVIDER_FAILURE) from None


class _ProviderStream:
    def __init__(self, target):
        self.target = target

    @property
    def text_stream(self):
        iterator = _provider_call(lambda: iter(self.target.text_stream))
        while True:
            try:
                value = next(iterator)
            except StopIteration:
                return
            except Exception:
                raise RuntimeError(_PROVIDER_FAILURE) from None
            yield value

    def __iter__(self):
        iterator = _provider_call(iter, self.target)
        while True:
            try:
                value = next(iterator)
            except StopIteration:
                return
            except Exception:
                raise RuntimeError(_PROVIDER_FAILURE) from None
            yield value

    def get_final_message(self):
        return _provider_call(self.target.get_final_message)

    def close(self):
        close = getattr(self.target, "close", None)
        if callable(close):
            return _provider_call(close)


class _ProviderStreamManager:
    def __init__(self, target):
        self.target = target

    def __enter__(self):
        try:
            return _ProviderStream(_provider_call(self.target.__enter__))
        except Exception:
            close = getattr(self.target, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise RuntimeError(_PROVIDER_FAILURE) from None

    def __exit__(self, *args):
        return _provider_call(self.target.__exit__, *args)


def connection_bundle(runtime, scope, bundle=None):
    runtime.workspaces.require_current(scope)
    value = dict(bundle if bundle is not None else (runtime.settings.get(scope["id"], "model") or {}))
    if (
        value.get("provider") not in {"openai", "anthropic"}
        or not isinstance(value.get("model"), str)
        or not value["model"].strip()
        or len(value["model"]) > 150
        or any(ord(c) < 32 for c in value["model"])
        or not isinstance(value.get("api_key"), str)
        or not value["api_key"].strip()
        or not value.get("consent")
    ):
        raise HTTPException(
            409, "Save an AI connection and acknowledge its data-flow notice in this workspace first."
        )
    return value


class _ClientProxy:
    """Guard every physical provider call, including native nested analysis lanes."""

    def __init__(self, target, runtime, scope, bundle, base=None, path=()):
        self._target, self._runtime, self._scope = target, runtime, dict(scope)
        self._bundle, self._base, self._path = bundle, base, path

    def __getattr__(self, name):
        member = getattr(self._target, name)
        if name == "close" and callable(member):
            return lambda: _provider_call(member)
        if name in {"create", "stream"} and callable(member):

            def call(*args, **kwargs):
                if proposals.no_provider_calls():
                    raise RuntimeError("Provider calls are unavailable during deterministic review")
                proposals.require_current(self._runtime, self._scope, self._base)
                # No ambient key, proxy, provider fallback or hosted storage opt-in.
                if self._bundle["provider"] == "openai" and "completions" in self._path:
                    kwargs["store"] = False
                    from app.agent.runtime.model_adapters import OpenAIChatAdapter

                    if not OpenAIChatAdapter().supports_temperature(self._bundle["model"]):
                        kwargs.pop("temperature", None)
                        if "max_tokens" in kwargs:
                            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                result = _provider_call(member, *args, **kwargs)
                if name == "stream":
                    return _ProviderStreamManager(result)
                if kwargs.get("stream") is True:
                    return _ProviderStream(result)
                return result

            return call
        if name == "with_options":
            return lambda **kw: _ClientProxy(
                member(**kw), self._runtime, self._scope, self._bundle, self._base, self._path
            )
        if callable(member) or isinstance(member, (str, int, float, bool, type(None))):
            return member
        return _ClientProxy(
            member, self._runtime, self._scope, self._bundle, self._base, self._path + (name,)
        )


def _new_client(runtime, scope, bundle, base=None):
    # A server-only injection hook exercises the actual native loop with fixtures.
    factory = getattr(runtime, "ai_client_factory", None)
    if factory is not None:
        raw = factory(dict(bundle))
    else:
        import httpx

        transport = httpx.Client(trust_env=False)
        if bundle["provider"] == "openai":
            from openai import OpenAI

            raw = OpenAI(
                api_key=bundle["api_key"],
                base_url="https://api.openai.com/v1",
                timeout=60,
                max_retries=0,
                http_client=transport,
            )
        else:
            from anthropic import Anthropic

            raw = Anthropic(
                api_key=bundle["api_key"],
                base_url="https://api.anthropic.com",
                timeout=60,
                max_retries=0,
                http_client=transport,
            )
    return _ClientProxy(raw, runtime, scope, bundle, base)


@contextmanager
def _client_binding(client, bundle):
    token = _BOUND_CLIENT.set((client, bundle))
    try:
        yield
    finally:
        _BOUND_CLIENT.reset(token)


def _bound(provider=None):
    binding = _BOUND_CLIENT.get()
    if proposals.no_provider_calls() or binding is None:
        raise RuntimeError("No authorised workspace model connection is bound to this operation")
    client, bundle = binding
    if provider is not None and bundle["provider"] != provider:
        raise RuntimeError("This workspace has not authorised that provider")
    return client, bundle


def test_connection(runtime, scope, bundle):
    bundle = connection_bundle(runtime, scope, bundle)
    client = _new_client(runtime, scope, bundle)
    try:
        if bundle["provider"] == "openai":
            client.chat.completions.create(
                model=bundle["model"],
                max_completion_tokens=16,
                messages=[{"role": "user", "content": "Reply OK. Connection test only; no financial data."}],
            )
        else:
            client.messages.create(
                model=bundle["model"],
                max_tokens=16,
                messages=[{"role": "user", "content": "Reply OK. Connection test only; no financial data."}],
            )
        runtime.workspaces.require_current(scope)
        return {
            "ok": True,
            "provider": bundle["provider"],
            "model": bundle["model"],
            "message": "Connection test succeeded. A scoped financial assistant response is a separate check.",
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            400,
            "The selected provider/model did not complete the connection test. Check access directly in this workspace.",
        ) from None
    finally:
        client.close()


def _install_lane_transport():
    """Keep native lane routing/budget/usage/context; replace only key transport.

    The source's global-key guards cannot represent per-workspace session keys.
    All local lanes use the exact entered provider/model and have no fallback.
    Optional semantic embeddings stay disabled; native lexical memory persists.
    """
    from app.services import llm

    def lane_config(lane):
        _, bundle = _bound()
        return llm.LaneConfig(
            lane, "claude" if bundle["provider"] == "anthropic" else "openai", bundle["model"], None
        )

    def call_openai(
        self,
        model,
        system_prompt,
        user_prompt,
        temperature,
        json_mode,
        timeout_override=None,
        max_retries_override=None,
        max_output_tokens_override=None,
    ):
        client, bundle = _bound("openai")
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        if max_output_tokens_override is not None:
            kwargs["max_completion_tokens"] = max_output_tokens_override
        tenant = llm.current_tenant_context()
        if tenant is not None:
            kwargs["user"] = tenant.tag
        client = llm._apply_call_overrides(client, timeout_override, max_retries_override)
        result = client.chat.completions.create(
            model=bundle["model"],
            temperature=temperature,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            **kwargs,
        )
        usage = getattr(result, "usage", None)
        counts = (
            None
            if usage is None
            else {
                "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            }
        )
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
        if counts is not None and cached is not None:
            counts["cached_input_tokens"] = int(cached or 0)
        choice = result.choices[0]
        return choice.message.content, counts, getattr(choice, "finish_reason", None) == "length"

    def call_claude(
        self,
        model,
        system_prompt,
        user_prompt,
        temperature,
        json_mode,
        timeout_override=None,
        max_retries_override=None,
        max_output_tokens_override=None,
    ):
        client, bundle = _bound("anthropic")
        if json_mode:
            system_prompt += "\n\nRespond ONLY with valid JSON."
        kwargs = {}
        tenant = llm.current_tenant_context()
        if tenant is not None:
            kwargs["metadata"] = {"user_id": tenant.tag}
        client = llm._apply_call_overrides(client, timeout_override, max_retries_override)
        result = client.messages.create(
            model=bundle["model"],
            system=system_prompt,
            temperature=temperature,
            max_tokens=max_output_tokens_override or 4096,
            messages=[{"role": "user", "content": user_prompt}],
            **kwargs,
        )
        usage = getattr(result, "usage", None)
        counts = (
            None
            if usage is None
            else {
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            }
        )
        if counts is not None:
            for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                if getattr(usage, field, None) is not None:
                    counts[field] = int(getattr(usage, field) or 0)
        return (
            (result.content[0].text if result.content else None),
            counts,
            getattr(result, "stop_reason", None) == "max_tokens",
        )

    def embedding(self, text):
        # Never select a second provider/model implicitly. Native lexical memory
        # retrieval/versioning works without optional semantic vectors.
        return None

    llm._lane_config = lane_config
    llm._get_openai_client = lambda: _bound("openai")[0]
    llm._get_anthropic_client = lambda: _bound("anthropic")[0]
    llm.LaneLLMService._call_openai = call_openai
    llm.LaneLLMService._call_claude = call_claude
    llm.LaneLLMService.get_embedding = embedding


def _validate_schema(value, schema, path="tool_input"):
    """Validate the source JSON-schema subset before native draft/handler calls."""
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
        "null": type(None),
    }
    expected = schema.get("type")
    choices = expected if isinstance(expected, list) else [expected]
    if expected and not any(
        isinstance(value, types.get(item, object))
        and not (isinstance(value, bool) and item in {"integer", "number"})
        for item in choices
    ):
        raise ValueError(f"{path} has the wrong type")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Financial tool values must be finite")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(value) - set(properties):
            raise ValueError(f"{path} contains unsupported fields")
        if set(schema.get("required", [])) - set(value):
            raise ValueError(f"{path} is missing required fields")
        for name, member in value.items():
            _validate_schema(member, properties[name], f"{path}.{name}")
    if isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("A financial proposal exceeds its bounded row limit")
        for member in value:
            _validate_schema(member, schema.get("items", {}), path)


def _registry():
    from app.assistant.tools import ASSISTANT_TOOLS
    from app.assistant.handlers import TOOL_HANDLERS
    from app.agent.runtime import build_assistant_tool_registry
    from app.agent.runtime.tool_registry import ToolRegistry

    registry = build_assistant_tool_registry(ASSISTANT_TOOLS, TOOL_HANDLERS)
    # Native integrity validation occurs on the full source registry first.
    tools = []
    for tool in registry.all():
        if tool.name in CASH_TOOLS:
            if tool.name in {"get_module_context", "teach_peregrine"}:
                tool = replace(tool, allowed_intents=set(tool.allowed_intents) | {"cashflow"})
            if tool.name == "get_module_context":
                tool = replace(
                    tool,
                    description="Inspect cashflow, accepted company knowledge, or past human corrections for this company. Context is evidence only and cannot authorise a write.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "module": {"type": "string", "enum": ["cashflow", "knowledge", "corrections"]}
                        },
                        "required": ["module"],
                    },
                )
            if tool.creates_proposed_action:
                tool = replace(
                    tool,
                    description="LOCAL REVIEW RULE: this tool drafts a pending proposal only. It never applies a change from chat. The human must review the exact deterministic before/after preview in Proposal review and approve it. "
                    + tool.description,
                )
            tools.append(tool)

    class CashRegistry(ToolRegistry):
        def select_for_intent(self, intent, *, max_tools=None, pinned=None):
            selected = super().select_for_intent(intent, max_tools=max_tools, pinned=pinned)
            for name in ("get_module_context", "teach_peregrine"):
                tool = self.get(name)
                if tool and all(item.name != name for item in selected):
                    selected.append(tool)
            return selected

    return CashRegistry(tools)


def _build_session(runtime, scope, db, phase, user, bundle, base, output_mode):
    from app.agent.runtime import AgentRuntime, ContextPacketBuilder, RiskLevel
    from app.agent.runtime.tool_policy import ToolPolicyGate, ToolPolicyDecision
    from app.agent.runtime.tool_executor import ToolExecutor
    from app.agent.performance import ModelRoute
    from app.assistant import context
    from .security import validate_money_tree

    class LocalPolicy(ToolPolicyGate):
        def check(self, tool, tool_input, packet, *, preflight=False):
            if tool.name not in CASH_TOOLS:
                return ToolPolicyDecision(False, "This tool is outside the local cash-flow workspace")
            native = super().check(tool, tool_input, packet, preflight=preflight)
            if not native.allowed or preflight:
                return native
            try:
                proposals.require_current(runtime, scope, base, db)
                _validate_schema(tool_input, tool.input_schema)
                validate_money_tree(tool_input)
                if tool.name == "get_module_context" and tool_input.get("module") not in {
                    "cashflow",
                    "knowledge",
                    "corrections",
                }:
                    raise ValueError("Only cash-flow and correction memory context is available")
            except (ValueError, HTTPException) as exc:
                return ToolPolicyDecision(False, str(exc.detail if isinstance(exc, HTTPException) else exc))
            return native

    class ProposalOnlyExecutor(ToolExecutor):
        def _maybe_auto_execute_routine_draft(self, packet, result):
            return result

    policy = LocalPolicy()
    db.info["local_chat_metadata"] = _chat_metadata(scope, base)
    context.set_current_user_question(phase.user_question)
    native = AgentRuntime(
        db=db,
        registry=_registry(),
        policy_gate=policy,
        context_builder=ContextPacketBuilder(db, intent_classifier=lambda _text: "cashflow"),
    )
    proposals.bind_drafts(db, scope, base)
    session = native.prepare_session(
        request_text=phase.user_question,
        company_id=scope["realm_id"],
        user=user,
        user_role=phase.user_role,
        conversation_id=phase.conversation_id,
        trigger_type="chat_stream" if output_mode.value == "stream" else "chat",
        output_mode=output_mode,
        organization_id=scope["organization_id"],
        organization_company_id=scope["organization_company_id"],
        tenant_id=str(scope["organization_id"]),
        request_id=phase.request_id,
        risk_level=RiskLevel.INTERNAL_WRITE,
        max_tools=30,
        confirmation_tokens=phase.confirmed_tool_names,
        confirmed_action_fingerprints=phase.confirmed_action_fingerprints,
    )
    route = ModelRoute(
        bundle["provider"], bundle["model"], "local", "Explicit active-workspace model; no fallback"
    )
    session.model_route = route
    session.context = replace(
        session.context,
        model_routing_metadata=route.to_dict(),
        assumptions=list(session.context.assumptions)
        + [
            "Local cash-flow workspace only. Draft every financial or memory change for explicit human review; never apply model writes.",
            "Past conversation figures are historical evidence. Fetch current cash-flow evidence for current figures.",
            "Current deterministic finance base: " + base,
        ],
    )
    session.stable_system_prompt = native.prompt_builder.build_stable_policy(
        company_name=session.company_name
    )
    session.volatile_system_prompt = native.prompt_builder.build_volatile_context(session.context)
    session.system_prompt = native.prompt_builder.build_system_prompt(
        session.context, company_name=session.company_name
    )
    session.messages = native.prompt_builder.build_messages(session.context)
    session.executor = ProposalOnlyExecutor(db=db, policy_gate=policy, tracker=session.tracker)
    session.tracker.model_name, session.tracker.model_provider = bundle["model"], bundle["provider"]
    if session.tracker.run is not None:
        session.tracker.run.model_name, session.tracker.run.model_provider = (
            bundle["model"],
            bundle["provider"],
        )
        session.tracker.run.permission_scope = {
            **(session.tracker.run.permission_scope or {}),
            "local_workspace_id": scope["id"],
            "local_workspace_revision": scope["revision"],
            "local_base_fingerprint": base,
        }
        db.commit()
    return session


def _loop(client, provider):
    from app.agent.runtime import AnthropicRuntimeLoop

    class LocalLoop(AnthropicRuntimeLoop):
        @staticmethod
        def _fallback_route(_route):
            return None

    return LocalLoop(client_factory=lambda: client, provider=provider)


@contextmanager
def _one_run(scope):
    key = (scope["id"], scope["revision"])
    with _ACTIVE_LOCK:
        if key in _ACTIVE_RUNS:
            raise HTTPException(409, "An assistant request is already running in this workspace")
        _ACTIVE_RUNS.add(key)
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_RUNS.discard(key)


def _explanation(session, answer, events, usage, error, scope, base):
    from app.assistant.main import _build_message_explanation, _augment_explanation_with_claims

    explanation = _build_message_explanation(
        events,
        session.model_route,
        token_usage=usage,
        final_model_name=session.tracker.model_name,
        final_provider=session.tracker.model_provider,
    )
    explanation = _augment_explanation_with_claims(
        explanation, answer, events, tracker=session.tracker, usage_sink=usage, generation_error=error
    )
    explanation.update(_chat_metadata(scope, base))
    return explanation


def register(app, runtime):
    # Import native entrypoints only AFTER bootstrap selected an isolated DB.
    from app.assistant import main as native
    from app.agent.runtime import OutputMode, AgentStreamState
    from app.assistant.errors import AssistantError
    from app.models import ChatMessage, AgentRun, AgentStep

    _install_lane_transport()
    _install_conversation_scope()
    proposals.register(app, runtime)
    router = APIRouter(tags=["Native PE assistant — local cash-flow scope"])

    def query_body(body):
        from pydantic import ValidationError

        try:
            return native.AssistantQuery.model_validate(body)
        except ValidationError:
            raise HTTPException(422, "Provide a valid question and the current workspace company.") from None

    def phase_for(body, request, scope, base):
        if str(body.realm_id) != scope["realm_id"]:
            raise HTTPException(403, "Assistant company differs from the active workspace")
        if not body.get_message().strip():
            raise HTTPException(400, "Enter a question for this cash-flow workspace")
        with proposals._scoped_session(runtime, scope) as db:
            db.info["local_chat_metadata"] = _chat_metadata(scope, base)
            user = runtime.user_for_scope(db, scope)
            token = _CONVERSATION_SCOPE.set((runtime, scope, base))
            try:
                return native._run_assistant_request_phase(
                    body=body, request=request, db=db, current_user=user
                )
            finally:
                _CONVERSATION_SCOPE.reset(token)

    @router.get("/assistant/history")
    def history(realm_id: str, request: Request):
        scope = runtime.require_scope(request)
        if realm_id != scope["realm_id"]:
            raise HTTPException(403, "Conversation belongs to a different workspace")
        with proposals._scoped_session(runtime, scope) as db:
            native.check_tenant_access(
                "VIEWER", company_id=realm_id, db=db, user=runtime.user_for_scope(db, scope)
            )
            base = proposals.financial_fingerprint(runtime, scope, db)
            conversation = _conversation_for_base(db, scope, base)
            if conversation is None:
                return []
            return [
                {
                    "role": row.role,
                    "content": row.content,
                    "created_at": row.created_at,
                    "metadata": row.metadata_json or None,
                }
                for row in db.query(ChatMessage)
                .filter(ChatMessage.conversation_id == conversation.id)
                .order_by(ChatMessage.id)
                .all()
            ]

    @router.post("/assistant/confirmations")
    def confirmations(body: dict, request: Request):
        scope = runtime.require_scope(request)
        if str(body.get("realm_id")) != scope["realm_id"] or body.get("tool_name") not in CASH_TOOLS:
            raise HTTPException(403, "Confirmation is outside this cash-flow workspace")
        # All local model mutations are proposal-first; no token authorizes them.
        raise HTTPException(
            400, "Review the proposed change and its financial preview in Proposal review instead"
        )

    @router.post("/assistant/query")
    def query(body: dict, request: Request):
        body = query_body(body)
        scope = runtime.require_scope(request)
        bundle = connection_bundle(runtime, scope)
        base = proposals.financial_fingerprint(runtime, scope)
        with _one_run(scope):
            phase = phase_for(body, request, scope, base)
            client = _new_client(runtime, scope, bundle, base)
            session = None
            with proposals._scoped_session(runtime, scope) as db, _client_binding(client, bundle):
                try:
                    session = _build_session(
                        runtime,
                        scope,
                        db,
                        phase,
                        runtime.user_for_scope(db, scope),
                        bundle,
                        base,
                        OutputMode.MARKDOWN,
                    )
                    result = _loop(client, bundle["provider"]).run_completion(
                        session, result_transform=native._prepare_tool_result_for_task_lane
                    )
                    if not result.answer:
                        result.answer = "The selected model returned no assistant answer. Check the connection and try a fresh request."
                        result.error = result.error or "The model returned an empty answer"
                    proposals.require_current(runtime, scope, base, db)
                    explanation = _explanation(
                        session,
                        result.answer,
                        result.tool_events,
                        result.token_usage,
                        result.error,
                        scope,
                        base,
                    )
                    db.add(
                        ChatMessage(
                            conversation_id=phase.conversation_id,
                            role="assistant",
                            content=result.answer,
                            metadata_json=explanation,
                        )
                    )
                    db.commit()
                    native._finalize_stream_run(
                        db,
                        session.tracker,
                        org_id=scope["organization_id"],
                        realm_id=scope["realm_id"],
                        status="failed"
                        if result.error or not result.answer
                        else "partially_completed"
                        if result.token_usage.get("partial")
                        else "succeeded",
                        output_summary=result.answer,
                        token_usage=result.token_usage,
                        error=result.error,
                    )
                    return {
                        "answer": result.answer,
                        "actions": result.tool_events,
                        "explanation": explanation,
                    }
                except AssistantError as exc:
                    native._persist_assistant_error_message(db, phase, exc)
                    raise HTTPException(exc.http_status, exc.user_message) from None
                except HTTPException:
                    raise
                except Exception:
                    db.rollback()
                    raise HTTPException(
                        502,
                        "The workspace assistant could not complete this request. Its proposed changes still require review.",
                    ) from None
                finally:
                    if session is not None:
                        native._finalize_stream_run(
                            db,
                            session.tracker,
                            org_id=scope["organization_id"],
                            realm_id=scope["realm_id"],
                            status="failed",
                            output_summary=None,
                            token_usage=None,
                            error="Request ended before a confirmed terminal result",
                        )
                    client.close()

    @router.post("/assistant/stream")
    def stream(body: dict, request: Request):
        body = query_body(body)
        scope = runtime.require_scope(request)
        bundle = connection_bundle(runtime, scope)
        base = proposals.financial_fingerprint(runtime, scope)
        phase = phase_for(body, request, scope, base)

        def event_stream():
            session, state, client = None, AgentStreamState(), None

            def frame(kind, content=None, **extra):
                return (
                    "data: "
                    + json.dumps(
                        {"type": kind, **({"content": content} if content is not None else {}), **extra}
                    )
                    + "\n\n"
                )

            with _one_run(scope), runtime.session_factory() as db:
                try:
                    client = _new_client(runtime, scope, bundle, base)
                    # No tenant ContextVar token remains across a generator yield.
                    # The native after_begin listener reapplies DB scope per txn.
                    with native._stream_run_scope(
                        db, org_id=scope["organization_id"], realm_id=scope["realm_id"]
                    ):
                        from app.tenant_rls import tenant_scope_live

                        def bound():
                            return tenant_scope_live(
                                db, org_id=scope["organization_id"], realm_id=scope["realm_id"]
                            )

                        with bound(), _client_binding(client, bundle):
                            session = _build_session(
                                runtime,
                                scope,
                                db,
                                phase,
                                runtime.user_for_scope(db, scope),
                                bundle,
                                base,
                                OutputMode.STREAM,
                            )
                            iterator = _loop(client, bundle["provider"]).iter_stream(
                                session,
                                state=state,
                                result_transform=native._prepare_tool_result_for_task_lane,
                                status_label_for_tool=lambda name: native._TOOL_STATUS_LABELS.get(
                                    name, "Reviewing cash-flow evidence..."
                                ),
                            )
                        while True:
                            try:
                                with bound(), _client_binding(client, bundle):
                                    proposals.require_current(runtime, scope, base, db)
                                    event = next(iterator)
                            except StopIteration:
                                break
                            yield frame(event.type, event.content)
                        if not state.full_answer:
                            state.error = state.error or "The model returned an empty answer"
                            state.full_answer = "The selected model returned no assistant answer. Check the connection and try a fresh request."
                            yield frame(
                                "error",
                                user_message=state.full_answer,
                                error_code="local_empty_answer",
                                retryable=True,
                            )
                        with bound(), _client_binding(client, bundle):
                            proposals.require_current(runtime, scope, base, db)
                            explanation = _explanation(
                                session,
                                state.full_answer,
                                state.tool_events,
                                state.token_usage,
                                state.error,
                                scope,
                                base,
                            )
                            if state.full_answer:
                                db.add(
                                    ChatMessage(
                                        conversation_id=phase.conversation_id,
                                        role="assistant",
                                        content=state.full_answer,
                                        metadata_json=explanation,
                                    )
                                )
                                db.commit()
                            native._finalize_stream_run(
                                db,
                                session.tracker,
                                org_id=scope["organization_id"],
                                realm_id=scope["realm_id"],
                                status="failed"
                                if state.error
                                else "partially_completed"
                                if state.token_usage.get("partial")
                                else "succeeded",
                                output_summary=state.full_answer,
                                token_usage=state.token_usage,
                                error=state.error,
                            )
                        yield frame("explanation", explanation)
                except GeneratorExit:
                    state.error = "The local client disconnected before completion"
                    raise
                except Exception as exc:
                    db.rollback()
                    if isinstance(exc, AssistantError):
                        from app.tenant_rls import tenant_scope_live

                        with tenant_scope_live(
                            db, org_id=scope["organization_id"], realm_id=scope["realm_id"]
                        ):
                            native._persist_assistant_error_message(db, phase, exc)
                    state.error = "Assistant stopped because workspace, evidence, model access or request validity changed"
                    message = (
                        exc.user_message
                        if isinstance(exc, AssistantError)
                        else exc.detail
                        if isinstance(exc, HTTPException)
                        else state.error
                    )
                    yield frame(
                        "error", user_message=message, error_code="local_assistant_stopped", retryable=False
                    )
                finally:
                    if session is not None:
                        native._finalize_stream_run(
                            db,
                            session.tracker,
                            org_id=scope["organization_id"],
                            realm_id=scope["realm_id"],
                            status="failed",
                            output_summary=state.full_answer,
                            token_usage=state.token_usage,
                            error=state.error or "Stream ended before final evidence persistence",
                        )
                    if client is not None:
                        client.close()
                yield frame("done")

        async def thread_stream():
            # The native loop holds tenant-tag ContextVars across its yields.
            # Keep its complete iterator on one dedicated thread rather than
            # asking Starlette's pool to resume it on arbitrary worker threads.
            loop, queue, cancelled = asyncio.get_running_loop(), asyncio.Queue(), threading.Event()
            sentinel = object()

            def run():
                iterator = event_stream()
                try:
                    for chunk in iterator:
                        if cancelled.is_set():
                            break
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
                finally:
                    iterator.close()
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            worker = threading.Thread(target=run, name="vesper-scoped-assistant", daemon=True)
            worker.start()
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is sentinel:
                        break
                    yield chunk
            finally:
                cancelled.set()

        return StreamingResponse(
            thread_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/agent/runs")
    def runs(request: Request, company_id: str | None = None, limit: int = 25):
        scope = runtime.require_scope(request)
        if company_id and company_id != scope["realm_id"]:
            raise HTTPException(403, "Runs belong to a different workspace")
        with proposals._scoped_session(runtime, scope) as db:
            rows = (
                db.query(AgentRun)
                .filter(
                    AgentRun.company_id == scope["realm_id"],
                    AgentRun.organization_id == scope["organization_id"],
                )
                .order_by(AgentRun.id.desc())
                .limit(max(1, min(limit, 100)))
                .all()
            )
            return [
                {
                    "id": row.id,
                    "company_id": row.company_id,
                    "status": row.status,
                    "trigger_type": row.trigger_type,
                    "input_summary": row.input_summary,
                    "output_summary": row.output_summary,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                    "model_name": row.model_name,
                    "token_usage": row.token_usage,
                    "error": row.error,
                }
                for row in rows
            ]

    @router.get("/agent/runs/{run_id}")
    def run_detail(run_id: int, request: Request):
        scope = runtime.require_scope(request)
        with proposals._scoped_session(runtime, scope) as db:
            row = (
                db.query(AgentRun)
                .filter(
                    AgentRun.id == run_id,
                    AgentRun.company_id == scope["realm_id"],
                    AgentRun.organization_id == scope["organization_id"],
                )
                .first()
            )
            if row is None:
                raise HTTPException(404, "Agent run not found in this workspace")
            steps = db.query(AgentStep).filter(AgentStep.agent_run_id == row.id).order_by(AgentStep.id).all()
            return {
                "id": row.id,
                "status": row.status,
                "token_usage": row.token_usage,
                "steps": [
                    {
                        "id": step.id,
                        "step_type": step.step_type,
                        "tool_name": step.tool_name,
                        "status": step.status,
                        "input_payload": step.input,
                        "output_payload": step.output,
                        "error": step.error,
                    }
                    for step in steps
                ],
            }

    app.include_router(router)

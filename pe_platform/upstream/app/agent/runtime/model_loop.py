from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Iterator, Optional

from ...config import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MODEL_CONTEXT_WINDOWS,
    settings,
)
from ...services.cost_attribution import bind_budget_pressure
from ...services.evidence.facts import capture_facts
from ...services.llm_usage import record_llm_usage
# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B4 — fallback-answer neutralizer (G3)
from ...services.prompt_fence import neutralize_untrusted_text, wrap_untrusted
from ...services.tenant_tag import bind_tenant_tag, tenant_tag
from ..performance import ModelRoute, estimate_model_cost_usd
from .model_adapters import ModelAdapter, OpenAIChatAdapter, adapter_for_provider
# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — terminal-reason + fallback policy
from .loop_termination import (
    ACTION_CONTINUE,
    ACTION_REFUSAL,
    ACTION_TRUNCATED,
    TRUNCATION_DISCLOSURE,
    append_paused_turn,
    fallback_category,
    fallback_eligible,
    refusal_answer,
    resolve_terminal_action,
    stamp_fallback_telemetry,
    stamp_terminal_telemetry,
    truncated_answer,
)
from .runtime import AgentRuntimeSession

logger = logging.getLogger(__name__)

# Marker appended to a tool-result payload that has been trimmed mid-loop to
# keep the resent transcript within budget. Used to make trimming idempotent
# without adding non-API keys to the tool_result block.
_TRIM_MARKER = "\n…[older tool result trimmed to save context]"

# Reused module-level Anthropic client for the agent runtime tool loop
# (audit items Q2/Q4) — one client across calls, with bounded timeout/retries.
import threading as _threading  # noqa: E402

_RUNTIME_CLIENT_LOCK = _threading.Lock()
_RUNTIME_ANTHROPIC_CLIENT: Optional[Any] = None
_RUNTIME_ANTHROPIC_CTOR: Optional[Any] = None
_RUNTIME_OPENAI_CLIENT: Optional[Any] = None
_RUNTIME_OPENAI_CTOR: Optional[Any] = None


def _shared_anthropic_client() -> Any:
    """Return a reused module-level Anthropic client (audit Q2/Q4).

    The cached instance is keyed on the ``anthropic.Anthropic`` constructor
    identity so that swapping the SDK (e.g. monkeypatching in tests) or a
    hot-reload transparently rebuilds the client.
    """
    global _RUNTIME_ANTHROPIC_CLIENT, _RUNTIME_ANTHROPIC_CTOR
    import anthropic

    ctor = anthropic.Anthropic
    if _RUNTIME_ANTHROPIC_CLIENT is None or _RUNTIME_ANTHROPIC_CTOR is not ctor:
        with _RUNTIME_CLIENT_LOCK:
            if _RUNTIME_ANTHROPIC_CLIENT is None or _RUNTIME_ANTHROPIC_CTOR is not ctor:
                _RUNTIME_ANTHROPIC_CLIENT = _construct_anthropic(ctor)
                _RUNTIME_ANTHROPIC_CTOR = ctor
    return _RUNTIME_ANTHROPIC_CLIENT


def _construct_anthropic(ctor: Any) -> Any:
    """Build an Anthropic client with bounded timeout/retries, tolerating
    lightweight test doubles whose __init__ only accepts api_key."""
    try:
        return ctor(
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_request_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    except TypeError:
        return ctor(api_key=settings.anthropic_api_key)


def _shared_openai_client() -> Any:
    """Return a reused module-level OpenAI client with bounded transport settings."""
    global _RUNTIME_OPENAI_CLIENT, _RUNTIME_OPENAI_CTOR
    from openai import OpenAI

    ctor = OpenAI
    if _RUNTIME_OPENAI_CLIENT is None or _RUNTIME_OPENAI_CTOR is not ctor:
        with _RUNTIME_CLIENT_LOCK:
            if _RUNTIME_OPENAI_CLIENT is None or _RUNTIME_OPENAI_CTOR is not ctor:
                _RUNTIME_OPENAI_CLIENT = _construct_openai(ctor)
                _RUNTIME_OPENAI_CTOR = ctor
    return _RUNTIME_OPENAI_CLIENT


def _construct_openai(ctor: Any) -> Any:
    try:
        return ctor(
            api_key=settings.openai_api_key,
            timeout=settings.llm_request_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    except TypeError:
        return ctor(api_key=settings.openai_api_key)


# OpenAI-compatible providers (DeepSeek/Groq/Ollama/vLLM/Together) each get their own
# client keyed by provider name — they are NOT api.openai.com and must never share it.
_RUNTIME_OPENAI_COMPATIBLE_CLIENTS: dict[str, Any] = {}


class ProviderRoutingError(RuntimeError):
    """A configured provider cannot be routed to its own endpoint. Refused, never
    degraded to another vendor — tenant prompt content would leave for the wrong API."""


def _is_openai_family(adapter: ModelAdapter) -> bool:
    """True for OpenAI and every openai_compatible provider. OpenAICompatibleAdapter
    reports its own provider name ("deepseek", "groq", …), so a name check misses it and
    the call falls through to the Anthropic request/client path."""
    return isinstance(adapter, OpenAIChatAdapter)


def _openai_compatible_client(provider: str) -> Any:
    """Return a cached OpenAI-SDK client pointed at THIS provider's own endpoint.

    Config is per provider — ``<PROVIDER>_BASE_URL`` / ``<PROVIDER>_API_KEY`` (e.g.
    DEEPSEEK_BASE_URL), falling back to OPENAI_COMPATIBLE_BASE_URL/_API_KEY. Missing
    config refuses with ProviderRoutingError instead of using another vendor's client.
    """
    key = (provider or "").strip().lower()
    cached = _RUNTIME_OPENAI_COMPATIBLE_CLIENTS.get(key)
    if cached is not None:
        return cached

    env_prefix = key.upper()
    base_url = (os.getenv(f"{env_prefix}_BASE_URL") or os.getenv("OPENAI_COMPATIBLE_BASE_URL") or "").strip()
    api_key = (os.getenv(f"{env_prefix}_API_KEY") or os.getenv("OPENAI_COMPATIBLE_API_KEY") or "").strip()
    missing = [name for name, value in ((f"{env_prefix}_BASE_URL", base_url), (f"{env_prefix}_API_KEY", api_key)) if not value]
    if missing:
        raise ProviderRoutingError(
            f"provider '{provider}' is configured but {' and '.join(missing)} not set "
            "(fallback: OPENAI_COMPATIBLE_BASE_URL / OPENAI_COMPATIBLE_API_KEY) — refusing the call "
            "rather than sending tenant content to a different vendor's API"
        )

    from openai import OpenAI

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=settings.llm_request_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    except TypeError:
        client = OpenAI(api_key=api_key, base_url=base_url)
    with _RUNTIME_CLIENT_LOCK:
        _RUNTIME_OPENAI_COMPATIBLE_CLIENTS[key] = client
    return client


_RUNTIME_GEMINI_CLIENT: Any = None
_RUNTIME_GEMINI_CTOR: Any = None


def _shared_gemini_client() -> Any:
    """Return a reused module-level Google Gemini client or SDK module."""
    global _RUNTIME_GEMINI_CLIENT, _RUNTIME_GEMINI_CTOR
    try:
        import google.generativeai as genai
        ctor = genai
    except Exception:
        ctor = None

    if _RUNTIME_GEMINI_CLIENT is None or _RUNTIME_GEMINI_CTOR is not ctor:
        with _RUNTIME_CLIENT_LOCK:
            if _RUNTIME_GEMINI_CLIENT is None or _RUNTIME_GEMINI_CTOR is not ctor:
                _RUNTIME_GEMINI_CLIENT = _construct_gemini(ctor)
                _RUNTIME_GEMINI_CTOR = ctor
    return _RUNTIME_GEMINI_CLIENT


def _construct_gemini(ctor: Any) -> Any:
    if ctor is None:
        return None
    api_key = getattr(settings, "gemini_api_key", None) or os.getenv("GEMINI_API_KEY")
    if api_key:
        try:
            ctor.configure(api_key=api_key)
        except Exception:
            pass
    return ctor


def reset_runtime_client() -> None:
    """Drop cached runtime provider clients (used by tests)."""
    global _RUNTIME_ANTHROPIC_CLIENT, _RUNTIME_ANTHROPIC_CTOR, _RUNTIME_OPENAI_CLIENT, _RUNTIME_OPENAI_CTOR, _RUNTIME_GEMINI_CLIENT, _RUNTIME_GEMINI_CTOR
    with _RUNTIME_CLIENT_LOCK:
        _RUNTIME_ANTHROPIC_CLIENT = None
        _RUNTIME_ANTHROPIC_CTOR = None
        _RUNTIME_OPENAI_CLIENT = None
        _RUNTIME_OPENAI_CTOR = None
        _RUNTIME_OPENAI_COMPATIBLE_CLIENTS.clear()
        _RUNTIME_GEMINI_CLIENT = None
        _RUNTIME_GEMINI_CTOR = None


ToolResultTransform = Callable[[str, str], str]
ToolStatusLabel = Callable[[str], str]


def _block_text(content: Any) -> str:
    """Best-effort string length view of a tool_result block's content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content or "")


def _approx_transcript_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(_block_text(block.get("content"))) + len(str(block.get("text") or ""))
                else:
                    total += len(str(block))
    return total


def _compact_tool_results(messages: list[dict[str, Any]], *, force: bool = False) -> bool:  # CRAFT-OK: dynamic provider transcript blocks (Anthropic/OpenAI message shapes), pre-existing file-wide annotation
    """Trim older tool-result payloads in place when the resent transcript grows
    past budget (LLM optimization roadmap #1/#6).

    The agent loop resends the full transcript every iteration, so large QBO
    tool results get re-billed N times. Once the cumulative transcript exceeds
    ``agent_loop_tool_result_budget_chars`` we shrink the *oldest* tool results
    (keeping the most recent ``agent_loop_keep_recent_tool_turns`` intact) to a
    short prefix. Recent turns — where the model is actively reasoning — stay
    full-resolution. Mutates ``messages`` and returns True if anything changed.

    ``force=True`` (the C7 token-budget path) skips the char-budget trigger and
    trims regardless — used when the FEEDBACK budget (actual provider input
    tokens) says the next call would overflow the model's context window even
    though the char heuristic has not tripped.

    Only the ``content`` text is trimmed; no non-API keys are added to the
    tool_result block, and tool_use/tool_result pairing is never broken.
    """
    budget = settings.agent_loop_tool_result_budget_chars
    keep_recent = max(0, settings.agent_loop_keep_recent_tool_turns)
    clip = settings.agent_loop_compacted_result_chars

    result_indices = [
        idx
        for idx, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in message["content"])
    ]
    if len(result_indices) <= keep_recent:
        return False
    if not force and _approx_transcript_chars(messages) <= budget:
        return False

    changed = False
    trim_targets = result_indices[: len(result_indices) - keep_recent] if keep_recent else result_indices
    for idx in trim_targets:
        message = messages[idx]
        new_blocks = []
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = _block_text(block.get("content"))
                if len(text) > clip and _TRIM_MARKER not in text:
                    block = {**block, "content": text[:clip] + _TRIM_MARKER}
                    changed = True
            new_blocks.append(block)
        messages[idx] = {**message, "content": new_blocks}
    return changed


# ── C7: token-accurate context budgeting against the routed model's window ───
# Marker appended by the SECOND-stage (digest) compaction. Distinct from
# _TRIM_MARKER so both passes stay independently idempotent.
_DIGEST_MARKER = "\n…[older turn digested to fit the model context window]"

# Floor for the computed budget so a misconfigured headroom/margin can never
# drive the budget to zero and dead-loop the compactor.
_MIN_CONTEXT_BUDGET_TOKENS = 1024


def _parse_context_window_overrides(raw: str) -> dict[str, int]:
    """Parse AGENT_MODEL_CONTEXT_WINDOWS: comma-separated ``substring:tokens``
    (or ``substring=tokens``) pairs. Malformed entries are logged and skipped —
    a bad knob must never take down routing."""
    overrides: dict[str, int] = {}
    for entry in (raw or "").replace("=", ":").split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, value = entry.rpartition(":")
        key = key.strip().lower()
        try:
            tokens = int(value.strip())
        except ValueError:
            logger.warning(
                "[AgentRuntime] AGENT_MODEL_CONTEXT_WINDOWS entry %r is not 'substring:tokens'; skipped",
                entry,
            )
            continue
        if not sep or not key or tokens <= 0:
            logger.warning(
                "[AgentRuntime] AGENT_MODEL_CONTEXT_WINDOWS entry %r is not 'substring:tokens'; skipped",
                entry,
            )
            continue
        overrides[key] = tokens
    return overrides


def context_window_for_model(model: str | None) -> int:
    """Resolve the routed model's context window (tokens).

    Longest-substring-key match, env overrides first, then the conservative
    DEFAULT_MODEL_CONTEXT_WINDOWS table (claude-* 200k-class, gpt-4o* 128k);
    unmatched ids fall back to the conservative DEFAULT_CONTEXT_WINDOW_TOKENS
    rather than an invented precise value.
    """
    name = (model or "").strip().lower()
    for table in (_parse_context_window_overrides(settings.agent_model_context_windows), DEFAULT_MODEL_CONTEXT_WINDOWS):
        for key in sorted(table, key=len, reverse=True):
            if key and key in name:
                return table[key]
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def context_budget_tokens_for_model(model: str | None) -> int:
    """Input-token budget = window − output headroom − estimation margin."""
    budget = (
        context_window_for_model(model)
        - settings.agent_output_headroom_tokens
        - settings.agent_context_budget_margin_tokens
    )
    return max(_MIN_CONTEXT_BUDGET_TOKENS, budget)


def _digest_old_turns(messages: list[dict[str, Any]]) -> bool:  # CRAFT-OK: dynamic provider transcript blocks (Anthropic/OpenAI message shapes), pre-existing file-wide annotation
    """SECOND-stage compaction when forced tool-result trimming is still not
    enough (C7 AC-1 "then summary"): digest OLD turns' payload down to a short
    head so the projected input fits the model window.

    CONTENT-ONLY, structure-preserving by construction: tool_use blocks are
    never touched, tool_result blocks keep their ``tool_use_id`` and only have
    their payload text clipped, and no message is dropped or reordered — the
    tool_use↔tool_result pairing invariant holds. The recent window
    (2·keep_recent_tool_turns + 2 trailing messages) stays full-resolution.
    """
    keep_recent = max(0, settings.agent_loop_keep_recent_tool_turns)
    protected_from = max(0, len(messages) - (2 * keep_recent + 2))
    clip = max(80, int(settings.agent_loop_compacted_result_chars) // 4)
    changed = False
    for idx in range(protected_from):
        message = messages[idx]
        payload = message.get("content")  # SA-OK: bounded-loop dict lookup on the in-process transcript list, not a DB query
        if isinstance(payload, str):
            if len(payload) > clip and _DIGEST_MARKER not in payload:
                messages[idx] = {**message, "content": payload[:clip] + _DIGEST_MARKER}
                changed = True
            continue
        if not isinstance(payload, list):
            continue
        new_blocks = []
        block_changed = False
        for block in payload:
            if isinstance(block, dict) and block.get("type") == "tool_result":  # SA-OK: bounded-loop dict lookup on transcript blocks, not a DB query
                text = _block_text(block.get("content"))  # SA-OK: bounded-loop dict lookup on transcript blocks, not a DB query
                if len(text) > clip and _DIGEST_MARKER not in text:
                    block = {**block, "content": text[:clip] + _DIGEST_MARKER}
                    block_changed = True
            elif isinstance(block, dict) and block.get("type") == "text":  # SA-OK: bounded-loop dict lookup on transcript blocks, not a DB query
                text = str(block.get("text") or "")  # SA-OK: bounded-loop dict lookup on transcript blocks, not a DB query
                if len(text) > clip and _DIGEST_MARKER not in text:
                    block = {**block, "text": text[:clip] + _DIGEST_MARKER}
                    block_changed = True
            new_blocks.append(block)
        if block_changed:
            messages[idx] = {**message, "content": new_blocks}
            changed = True
    return changed


class _ContextWindowBudgeter:
    """FEEDBACK context budgeting for one loop run (C7 AC-1).

    After every provider call the loop records the ACTUAL input size from
    ``response.usage`` (input + cache-read + cache-creation tokens — the full
    prompt counts against the window). Before the next call, the projected
    input = last actual + a chars-per-token estimate of the transcript delta;
    the very first call is estimated purely at
    ``agent_token_estimate_chars_per_token`` (default 4 — no tiktoken by
    design). Over budget → force tool-result compaction, then the digest pass;
    a still-over projection stops locally before provider invocation (structure
    is never sliced to fit).
    """

    def __init__(self, session: AgentRuntimeSession):
        self._session = session
        self._cpt = max(1, int(settings.agent_token_estimate_chars_per_token or 4))
        self._base_chars = len(getattr(session, "system_prompt", "") or "") + sum(
            len(str(schema)) for schema in (getattr(session, "tool_schemas", None) or [])
        )
        self._last_actual_input: int | None = None
        self._last_transcript_chars: int | None = None

    def budget_tokens(self) -> int:
        # Resolved per call: the route can change mid-loop (escalation/fallback).
        return context_budget_tokens_for_model(getattr(self._session.model_route, "model", None))

    def _estimate_tokens(self, chars: int) -> int:
        if chars <= 0:
            return 0
        return -(-chars // self._cpt)  # ceil division

    def projected_input_tokens(self, messages: list[dict[str, Any]]) -> int:  # CRAFT-OK: dynamic provider transcript blocks (Anthropic/OpenAI message shapes), pre-existing file-wide annotation
        chars = _approx_transcript_chars(messages)
        if self._last_actual_input is None or self._last_transcript_chars is None:
            return self._estimate_tokens(self._base_chars + chars)
        delta_chars = chars - self._last_transcript_chars
        if delta_chars >= 0:
            return self._last_actual_input + self._estimate_tokens(delta_chars)
        return max(0, self._last_actual_input - self._estimate_tokens(-delta_chars))

    def fit(self, messages: list[dict[str, Any]]) -> bool:  # CRAFT-OK: dynamic provider transcript blocks (Anthropic/OpenAI message shapes), pre-existing file-wide annotation
        """Compact the transcript until the projected input fits the budget."""
        # Legacy char-budget pass first (unchanged pre-C7 behavior).
        _compact_tool_results(messages)
        budget = self.budget_tokens()
        if self.projected_input_tokens(messages) <= budget:
            return True
        _compact_tool_results(messages, force=True)
        if self.projected_input_tokens(messages) <= budget:
            return True
        _digest_old_turns(messages)
        projected = self.projected_input_tokens(messages)
        if projected > budget:
            logger.error(
                "[AgentRuntime] projected input still exceeds the context budget after forced "
                "compaction + digest (projected=%s budget=%s model=%s) — stopping locally; "
                "tool_use/tool_result pairing is never sliced to fit",
                projected,
                budget,
                getattr(self._session.model_route, "model", None),
            )

        return projected <= budget

    def record_actual(self, messages: list[dict[str, Any]], response: Any) -> None:  # CRAFT-OK: dynamic provider transcript blocks + SDK response object, pre-existing file-wide annotation
        """Record the provider-reported ACTUAL input size for the transcript
        that was just sent (the feedback leg — estimates only ever cover the
        delta since this measurement)."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_tokens = int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        actual = input_tokens + cache_read + cache_creation
        if actual <= 0:
            return
        self._last_actual_input = actual
        self._last_transcript_chars = _approx_transcript_chars(messages)


def _messages_with_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a non-destructive copy of ``messages`` with an ephemeral cache
    breakpoint on the last content block of the last message (LLM optimization
    roadmap #2).

    Anthropic serves the longest cached prefix ending at a ``cache_control``
    marker. Marking the tail of the current message means the whole
    [system + tools + conversation history + prior tool turns] prefix is read
    from cache on the next loop iteration instead of being re-billed at full
    input price. This is safe because that prefix is byte-stable within a run.
    """
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        for i in range(len(blocks) - 1, -1, -1):
            if isinstance(blocks[i], dict):
                blocks[i] = {**blocks[i], "cache_control": {"type": "ephemeral"}}
                break
        last["content"] = blocks
    else:
        return messages
    out[-1] = last
    return out


@dataclass
class AgentLoopResult:
    answer: str
    tool_events: list[dict[str, Any]]
    token_usage: dict[str, int]
    error: Optional[str] = None


@dataclass
class AgentStreamEvent:
    type: str
    content: str


@dataclass
class AgentStreamState:
    full_answer: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    error: Optional[str] = None


class AnthropicRuntimeLoop:
    """Canonical provider-neutral tool loop for Peregrine agent runtime sessions.

    The class name remains for compatibility with existing imports; provider
    behavior is delegated to ``ModelAdapter`` implementations.
    """

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[[], Any]] = None,
        adapter: Optional[ModelAdapter] = None,
        provider: Optional[str] = None,
        fallback_client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.adapter = adapter or adapter_for_provider(provider or settings.task_llm_provider)
        self.client_factory = client_factory or self._default_client_factory
        self.fallback_client_factory = fallback_client_factory

    @staticmethod
    @contextlib.contextmanager
    def _bind_session_tenant(session: AgentRuntimeSession) -> Iterator[None]:
        """Bind the run's tenant identity AND budget pressure so lane LLM calls
        triggered during tool execution carry the same provider tag, stamp the
        same realm triple, and (under soft-budget pressure) bias toward cheaper
        routes. (The agent's own model calls are tagged/routed directly from the
        session, so they do not depend on this binding.)"""
        context = getattr(session, "context", None)
        with bind_tenant_tag(
            organization_id=getattr(context, "organization_id", None),
            organization_company_id=getattr(context, "organization_company_id", None),
            qbo_realm_id=getattr(context, "qbo_realm_id", None),
        ), bind_budget_pressure(getattr(session, "budget_action", None)):
            yield

    def run_completion(
        self,
        session: AgentRuntimeSession,
        *,
        max_iterations: int = 12,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        result_transform: Optional[ToolResultTransform] = None,
    ) -> AgentLoopResult:
        primary_adapter = adapter_for_provider(getattr(session.model_route, "provider", None) or self.adapter.provider)
        original_route = session.model_route
        try:
            return self._run_completion_once(
                session,
                adapter=primary_adapter,
                client_factory=self.client_factory,
                max_iterations=max_iterations,
                max_tokens=max_tokens,
                temperature=temperature,
                result_transform=result_transform,
            )
        except Exception as exc:
            primary_error = primary_adapter.normalize_errors(exc)
            # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — classification-gated
            # fallback (GAP-B3): auth/quota failures surface immediately instead of blindly
            # retrying on a different provider (credentials/billing problems are not routing
            # problems). Eligible classes (rate_limit/timeout/parse/other) keep today's behavior.
            error_class = fallback_category(exc)
            fallback_route = self._fallback_route(original_route) if fallback_eligible(error_class) else None
            if fallback_route is None and fallback_eligible(error_class) is False:
                logger.warning(
                    "[AgentRuntime] primary provider failed with non-fallback class '%s'; surfacing without fallback",
                    error_class,
                )
            if fallback_route is not None:
                fallback_adapter = adapter_for_provider(fallback_route.provider)
                try:
                    logger.warning(
                        "[AgentRuntime] primary provider failed (%s); falling back primary=%s fallback=%s",
                        error_class,
                        getattr(original_route, "provider", None),
                        fallback_route.provider,
                    )
                    session.model_route = fallback_route
                    self._mark_active_provider(session, fallback_route)
                    fallback_result = self._run_completion_once(
                        session,
                        adapter=fallback_adapter,
                        client_factory=self.fallback_client_factory or self._client_factory_for_adapter(fallback_adapter),
                        max_iterations=max_iterations,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        result_transform=result_transform,
                    )
                    stamp_fallback_telemetry(
                        fallback_result.token_usage,
                        fallback_provider=fallback_route.provider,
                        category=error_class,
                    )
                    return fallback_result
                except Exception as fallback_exc:
                    primary_error = (
                        f"{primary_error}; fallback {fallback_route.provider} failed: "
                        f"{fallback_adapter.normalize_errors(fallback_exc)}"
                    )
                finally:
                    session.model_route = original_route

            logger.error("[AgentRuntime] Model loop failed: %s", primary_error)
            answer = self._user_safe_model_error(partial_answer=False)
            if session.tracker.run and session.tracker.run.status == "running":
                session.tracker.finish(
                    status="failed",
                    output_summary=answer,
                    token_usage={"input_tokens": 0, "output_tokens": 0},
                    error=primary_error,
                )
            return AgentLoopResult(
                answer=answer,
                tool_events=[],
                token_usage={"input_tokens": 0, "output_tokens": 0},
                error=primary_error,
            )

    def _run_completion_once(
        self,
        session: AgentRuntimeSession,
        *,
        adapter: ModelAdapter,
        client_factory: Callable[[], Any],
        max_iterations: int,
        max_tokens: int,
        temperature: float,
        result_transform: Optional[ToolResultTransform],
    ) -> AgentLoopResult:
        old_adapter = self.adapter
        self.adapter = adapter
        messages = list(session.messages)
        tool_events: list[dict[str, Any]] = []
        token_usage = {"input_tokens": 0, "output_tokens": 0}
        answer = ""
        try:
            client = None
            # C7 feedback budgeting: projected input (last ACTUAL usage +
            # estimated delta; first call chars/cpt) must fit the routed
            # model's window budget before every call.
            budgeter = _ContextWindowBudgeter(session)
            # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R4 — wall-clock deadline for
            # the WHOLE loop (iteration caps bound count, not time). On expiry: stop calling the
            # model; the honest partial-answer path below composes from the tool results so far.
            loop_deadline = perf_counter() + max(30, settings.agent_loop_deadline_seconds)
            for _ in range(max_iterations):
                if perf_counter() >= loop_deadline:
                    logger.warning(
                        "[AgentRuntime] loop wall-clock deadline (%ss) reached; stopping with partial results",
                        settings.agent_loop_deadline_seconds,
                    )
                    token_usage["deadline_reached"] = True
                    break
                if not budgeter.fit(messages):
                    token_usage.update(context_budget_exceeded=True, partial=True, stop_reason="context_budget_exceeded")
                    answer = self._context_budget_answer(tool_events)
                    break
                if client is None:
                    client = client_factory()
                response = self._create_message(
                    client,
                    session,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    token_usage=token_usage,
                    stream=False,
                )
                budgeter.record_actual(messages, response)

                tool_calls = self.adapter.parse_tool_calls(response)
                if tool_calls:
                    self._apply_tool_calls(
                        session,
                        response,
                        messages,
                        tool_calls,
                        tool_events,
                        result_transform=result_transform,
                    )
                    self._escalate_after_tool(session)
                    if len(messages) >= settings.agent_loop_max_messages:
                        logger.warning(
                            "[AgentRuntime] message ceiling reached (%s); stopping tool loop",
                            len(messages),
                        )
                        break
                    continue

                # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — AP-LAO-02: branch
                # the terminal reason instead of assuming "no tool call == final". max_tokens is
                # TRUNCATED (disclose, flag telemetry); pause_turn is INCOMPLETE (continue);
                # refusal is FILTERED (user-safe refusal, never blind-retried).
                action, reason = resolve_terminal_action(self.adapter, response)
                if action == ACTION_CONTINUE and append_paused_turn(messages, response):
                    logger.info("[AgentRuntime] pause_turn — continuing the paused turn")
                    continue
                answer = self.adapter.final_response(response)  # LLMLOOP-OK: terminal reason IS branched — resolve_terminal_action above routes max_tokens/pause_turn/refusal; this line reads text for the already-classified action
                if action == ACTION_TRUNCATED:
                    logger.warning("[AgentRuntime] final turn truncated by max_tokens — disclosed to user")
                    answer = truncated_answer(answer)
                    stamp_terminal_telemetry(token_usage, stop_reason=reason, truncated=True)
                elif action == ACTION_REFUSAL:
                    logger.warning("[AgentRuntime] provider refusal/content-filter terminal turn")
                    answer = refusal_answer(answer)
                    stamp_terminal_telemetry(token_usage, stop_reason=reason, refusal=True)
                else:
                    stamp_terminal_telemetry(token_usage, stop_reason=reason)
                break

            if not answer and tool_events:
                answer = self._fallback_answer(tool_events)

            self._stamp_observation(session, token_usage)
            return AgentLoopResult(answer=answer, tool_events=tool_events, token_usage=token_usage)
        finally:
            self.adapter = old_adapter

    def iter_stream(
        self,
        session: AgentRuntimeSession,
        *,
        state: AgentStreamState,
        max_iterations: int = 8,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        result_transform: Optional[ToolResultTransform] = None,
        status_label_for_tool: Optional[ToolStatusLabel] = None,
    ) -> Iterator[AgentStreamEvent]:
        primary_adapter = adapter_for_provider(getattr(session.model_route, "provider", None) or self.adapter.provider)
        original_route = session.model_route
        try:
            yield from self._iter_stream_once(
                session,
                state=state,
                adapter=primary_adapter,
                client_factory=self.client_factory,
                max_iterations=max_iterations,
                max_tokens=max_tokens,
                temperature=temperature,
                result_transform=result_transform,
                status_label_for_tool=status_label_for_tool,
            )
            return
        except Exception as exc:
            primary_error = primary_adapter.normalize_errors(exc)
            # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — classification-gated
            # stream fallback (GAP-B3), mirroring run_completion: auth/quota failures surface
            # immediately instead of blindly retrying on a different provider.
            error_class = fallback_category(exc)
            fallback_route = self._fallback_route(original_route) if fallback_eligible(error_class) else None
            can_fallback_before_output = fallback_route is not None and not state.full_answer and not state.tool_events
            if can_fallback_before_output:
                fallback_adapter = adapter_for_provider(fallback_route.provider)
                try:
                    logger.warning(
                        "[AgentRuntime] primary streaming provider failed before output; falling back primary=%s fallback=%s",
                        getattr(original_route, "provider", None),
                        fallback_route.provider,
                    )
                    session.model_route = fallback_route
                    self._mark_active_provider(session, fallback_route)
                    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — persist the
                    # fallback decision dimensions (GAP-C2 partial) into the run token_usage JSON.
                    stamp_fallback_telemetry(
                        state.token_usage,
                        fallback_provider=fallback_route.provider,
                        category=error_class,
                    )
                    yield from self._iter_stream_once(
                        session,
                        state=state,
                        adapter=fallback_adapter,
                        client_factory=self.fallback_client_factory or self._client_factory_for_adapter(fallback_adapter),
                        max_iterations=max_iterations,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        result_transform=result_transform,
                        status_label_for_tool=status_label_for_tool,
                    )
                    return
                except Exception as fallback_exc:
                    primary_error = (
                        f"{primary_error}; fallback {fallback_route.provider} failed: "
                        f"{fallback_adapter.normalize_errors(fallback_exc)}"
                    )
                finally:
                    session.model_route = original_route

            logger.error("[AgentRuntime] Streaming model loop failed: %s", primary_error)
            state.error = primary_error
            safe_answer = self._user_safe_model_error(partial_answer=bool(state.full_answer))
            if not state.full_answer:
                state.full_answer = safe_answer
            if session.tracker.run and session.tracker.run.status == "running":
                session.tracker.finish(
                    status="failed",
                    output_summary=state.full_answer,
                    token_usage=state.token_usage,
                    error=primary_error,
                )
            yield AgentStreamEvent(type="error", content=safe_answer)

    def _iter_stream_once(
        self,
        session: AgentRuntimeSession,
        *,
        state: AgentStreamState,
        adapter: ModelAdapter,
        client_factory: Callable[[], Any],
        max_iterations: int,
        max_tokens: int,
        temperature: float,
        result_transform: Optional[ToolResultTransform],
        status_label_for_tool: Optional[ToolStatusLabel],
    ) -> Iterator[AgentStreamEvent]:
        old_adapter = self.adapter
        self.adapter = adapter
        messages = list(session.messages)
        try:
            client = None
            # C7 feedback budgeting — same contract as the non-streamed loop.
            budgeter = _ContextWindowBudgeter(session)
            # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R4 — wall-clock deadline,
            # stream analog (see run_completion). Honest partial via the fallback path below.
            loop_deadline = perf_counter() + max(30, settings.agent_loop_deadline_seconds)
            for _ in range(max_iterations):
                if perf_counter() >= loop_deadline:
                    logger.warning(
                        "[AgentRuntime] stream loop wall-clock deadline (%ss) reached; stopping with partial results",
                        settings.agent_loop_deadline_seconds,
                    )
                    state.token_usage["deadline_reached"] = True
                    break
                if not budgeter.fit(messages):
                    state.token_usage.update(context_budget_exceeded=True, partial=True, stop_reason="context_budget_exceeded")
                    notice = self._context_budget_answer(state.tool_events)
                    if state.full_answer:
                        notice = "\n\n" + notice
                    state.full_answer += notice
                    yield AgentStreamEvent(type="text", content=notice)
                    break
                # Stream the turn. Text deltas are emitted live so the FINAL
                # answer turn renders incrementally; the SDK still assembles a
                # complete final message (with full tool_use content blocks)
                # which we use to drive the tool loop. Intermediate tool-calling
                # turns therefore keep correct, complete content blocks while
                # only the final no-tool turn produces user-visible text deltas
                # (audit S2).
                if client is None:
                    client = client_factory()
                response = yield from self._stream_turn(
                    client,
                    session,
                    messages,
                    state=state,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                budgeter.record_actual(messages, response)

                tool_calls = self.adapter.parse_tool_calls(response)
                if tool_calls:
                    executed = self._apply_tool_calls(
                        session,
                        response,
                        messages,
                        tool_calls,
                        state.tool_events,
                        result_transform=result_transform,
                    )
                    self._escalate_after_tool(session)
                    for event in executed:
                        label = status_label_for_tool(event["tool"]) if status_label_for_tool else f"Working on {event['tool']}..."
                        yield AgentStreamEvent(type="status", content=label)
                    if len(messages) >= settings.agent_loop_max_messages:
                        logger.warning(
                            "[AgentRuntime] message ceiling reached (%s); stopping stream tool loop",
                            len(messages),
                        )
                        break
                    continue

                # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — AP-LAO-02 stream
                # analog: the deltas already reached the client, so honesty here means DISCLOSING
                # rather than un-sending. max_tokens → append the truncation disclosure as a final
                # text frame (and persist it in full_answer); pause_turn → continue the loop;
                # refusal → emit the user-safe refusal if the turn produced no text.
                action, reason = resolve_terminal_action(self.adapter, response)
                if action == ACTION_CONTINUE and append_paused_turn(messages, response):
                    logger.info("[AgentRuntime] pause_turn (stream) — continuing the paused turn")
                    continue
                if action == ACTION_TRUNCATED:
                    logger.warning("[AgentRuntime] streamed final turn truncated by max_tokens — disclosed to user")
                    stamp_terminal_telemetry(state.token_usage, stop_reason=reason, truncated=True)
                    for chunk in self._chunks(TRUNCATION_DISCLOSURE):
                        state.full_answer += chunk
                        yield AgentStreamEvent(type="text", content=chunk)
                elif action == ACTION_REFUSAL:
                    logger.warning("[AgentRuntime] provider refusal/content-filter terminal turn (stream)")
                    stamp_terminal_telemetry(state.token_usage, stop_reason=reason, refusal=True)
                    if not state.full_answer:
                        for chunk in self._chunks(refusal_answer("")):
                            state.full_answer += chunk
                            yield AgentStreamEvent(type="text", content=chunk)
                else:
                    stamp_terminal_telemetry(state.token_usage, stop_reason=reason)
                break

            if not state.full_answer and state.tool_events:
                fallback = self._fallback_answer(state.tool_events)
                for chunk in self._chunks(fallback):
                    state.full_answer += chunk
                    yield AgentStreamEvent(type="text", content=chunk)
        finally:
            self._stamp_observation(session, state.token_usage)
            self.adapter = old_adapter

    @staticmethod
    def _escalate_after_tool(session: AgentRuntimeSession) -> None:
        """Escalate a cheap "fast" run to the frontier model once the model has
        decided to call a tool (LLM optimization roadmap #3).

        The first turn — pure chat, or the low-stakes decision to call a tool —
        may run on the cheap model. Every subsequent turn (where the model
        reasons over tool results / financial data) runs on the frontier model.
        No-op unless the current route is the "fast" lane, so balanced/reasoning
        runs and provider-neutral (OpenAI) runs are untouched.
        """
        if not settings.agent_escalate_after_tool:
            return
        route = session.model_route
        if getattr(route, "lane", None) != "fast":
            return
        target_model = settings.task_llm_model
        if not target_model or getattr(route, "model", None) == target_model:
            return
        escalated = ModelRoute(
            provider=getattr(route, "provider", None) or settings.task_llm_provider,
            model=target_model,
            lane="balanced",
            reason="escalated_after_tool_call",
        )
        session.model_route = escalated
        AnthropicRuntimeLoop._mark_active_provider(session, escalated)
        logger.info(
            "[AgentRuntime] escalated model after tool call fast=%s -> %s",
            getattr(route, "model", None),
            target_model,
        )

    @staticmethod
    def _cached_system_blocks(session: AgentRuntimeSession) -> list[dict[str, Any]]:
        """Build the Anthropic ``system`` payload as structured content blocks
        with a cache breakpoint on the stable prefix (audit S3).

        Anthropic prompt caching marks the END of a cacheable prefix with a
        ``cache_control`` block. Here the first block is the byte-stable policy
        prefix carrying ``cache_control={"type": "ephemeral"}``; the volatile
        request/realm context follows as a second, uncached block. Combined with
        the cache_control on the LAST tool definition, the cached prefix spans
        [tools + stable policy] while volatile data stays outside it.
        """
        stable = getattr(session, "stable_system_prompt", None) or session.system_prompt
        volatile = getattr(session, "volatile_system_prompt", None) or ""
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": stable,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if volatile:
            blocks.append({"type": "text", "text": volatile})
        return blocks

    @staticmethod
    def _cached_tool_schemas(session: AgentRuntimeSession) -> list[dict[str, Any]]:
        """Return tool schemas in their deterministic registry order with a
        cache breakpoint on the final tool (audit S3).

        Tool definitions are deterministic per intent (tool_registry iterates a
        fixed INTENT_BUNDLES) so the serialized tools block is byte-stable across
        requests. Placing ``cache_control`` on the last tool marks the end of the
        tool prefix for caching; the stable system prefix continues that cached
        span. We copy the dicts so the session's canonical schemas are untouched.
        """
        schemas = [dict(schema) for schema in session.tool_schemas]
        if schemas:
            schemas[-1] = {**schemas[-1], "cache_control": {"type": "ephemeral"}}
        return schemas

    @staticmethod
    def _tenant_tag(session: AgentRuntimeSession) -> Optional[str]:
        """Opaque, PII-free tenant tag for this run, or None if no org context.

        Derived from the session's realm triple — identical to the tag bound on
        the ContextVar for lane calls, so a tenant tags the same across
        providers and lanes.
        """
        context = getattr(session, "context", None)
        org_id = getattr(context, "organization_id", None)
        if org_id is None:
            return None
        return tenant_tag(
            org_id,
            getattr(context, "organization_company_id", None),
            getattr(context, "qbo_realm_id", None),
        )

    @classmethod
    def _tenant_metadata(cls, session: AgentRuntimeSession) -> dict[str, str]:
        """Anthropic ``metadata`` carrying the provider-agnostic tenant tag.

        Empty when there is no org context (the tag would be meaningless); an
        empty dict is a no-op for the Anthropic SDK.
        """
        tag = cls._tenant_tag(session)
        return {"user_id": tag} if tag else {}

    def _temperature_kwargs(self, session: AgentRuntimeSession, temperature: float) -> dict[str, float]:
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — AP-LAO-01 helper: consult
        # the adapter's per-model capability; adapters without the method (legacy test doubles)
        # keep today's send-temperature behavior.
        supports = getattr(self.adapter, "supports_temperature", None)
        model = getattr(session.model_route, "model", "") or ""
        if supports is not None and not supports(model):
            logger.info("[AgentRuntime] omitting temperature for reasoning-class model %s", model)
            return {}
        return {"temperature": temperature}

    def _create_message(
        self,
        client: Any,
        session: AgentRuntimeSession,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
        token_usage: dict[str, int],
        stream: bool,
    ) -> Any:
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-1). Fail-loud
        # close-run budget gate, BEFORE the provider call and OUTSIDE the try
        # below: a no-op unless a task_type="close" context is bound (the
        # interactive chat loop never is), so an over-budget close can never
        # spend through this seam either.
        from ...services.llm_task_context import enforce_close_run_budget

        enforce_close_run_budget()
        with session.tracker.step(
            step_type="model_call",
            input_payload={
                "message_count": len(messages),
                "tool_count": len(session.tool_schemas),
                "stream": stream,
                "model_route": session.model_route.to_dict(),
            },
        ) as model_step:
            started = perf_counter()
            call_error: Optional[Exception] = None
            response = None
            try:
                # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — AP-LAO-01: the
                # adapter owns supports_temperature(model); the param is OMITTED for model
                # classes that reject it (OpenAI o-series / gpt-5 reasoning ids 400 on
                # temperature != 1) instead of being sent unconditionally.
                temperature_kwargs = self._temperature_kwargs(session, temperature)
                if _is_openai_family(self.adapter):
                    request = self.adapter.build_request(
                        context=session.context,
                        system_prompt=session.system_prompt,
                        messages=messages,
                        tools=session.tools,
                    )
                    create_kwargs = {
                        "model": session.model_route.model,
                        "max_tokens": max_tokens,
                        **temperature_kwargs,
                        "messages": request["messages"],
                    }
                    if request.get("tools"):
                        create_kwargs["tools"] = request["tools"]
                    tenant_user = self._tenant_tag(session)
                    if tenant_user:
                        create_kwargs["user"] = tenant_user
                    response = client.chat.completions.create(**create_kwargs)
                elif self.adapter.provider == "google":
                    request = self.adapter.build_request(
                        context=session.context,
                        system_prompt=session.system_prompt,
                        messages=messages,
                        tools=session.tools,
                    )
                    if hasattr(client, "models") and hasattr(client.models, "generate_content"):
                        response = client.models.generate_content(
                            model=session.model_route.model,
                            contents=request.get("contents", []),
                            config={"system_instruction": request.get("system_instruction"), **temperature_kwargs},
                        )
                    elif hasattr(client, "GenerativeModel"):
                        model_obj = client.GenerativeModel(
                            model_name=session.model_route.model,
                            system_instruction=request.get("system_instruction"),
                            tools=request.get("tools"),
                        )
                        response = model_obj.generate_content(
                            contents=request.get("contents", []),
                            generation_config={"max_output_tokens": max_tokens, **temperature_kwargs},
                        )
                    elif hasattr(client, "generate_content"):
                        response = client.generate_content(
                            contents=request.get("contents", []),
                            generation_config={"max_output_tokens": max_tokens, **temperature_kwargs},
                        )
                    elif callable(client):
                        response = client(model=session.model_route.model, request=request)
                    else:
                        response = getattr(client, "chat", lambda **kw: None)(**request)
                else:
                    response = client.messages.create(
                        model=session.model_route.model,
                        max_tokens=max_tokens,
                        **temperature_kwargs,
                        system=self._cached_system_blocks(session),
                        tools=self._cached_tool_schemas(session),
                        messages=_messages_with_cache_breakpoint(messages),
                        metadata=self._tenant_metadata(session),
                    )
            except Exception as exc:
                call_error = exc

            latency_ms = int((perf_counter() - started) * 1000)

            if call_error is not None:
                self._record_step_usage(
                    session,
                    model_step,
                    latency_ms=latency_ms,
                    status="error",
                    error=call_error,
                    usage=None,
                )
                raise call_error

            usage = self.adapter.extract_usage(response)
            token_usage["input_tokens"] += usage["input_tokens"]
            token_usage["output_tokens"] += usage["output_tokens"]
            self._record_step_usage(
                session,
                model_step,
                latency_ms=latency_ms,
                status="ok",
                error=None,
                usage=self._usage_with_cache(response, usage),
            )
            model_step.finish(
                status="succeeded",
                output_payload={
                    "stop_reason": getattr(response, "stop_reason", None),
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                },
            )
            return response

    def _stream_turn(
        self,
        client: Any,
        session: AgentRuntimeSession,
        messages: list[dict[str, Any]],
        *,
        state: AgentStreamState,
        max_tokens: int,
        temperature: float,
    ) -> Iterator[AgentStreamEvent]:
        """Stream one turn with real Anthropic token deltas (audit S2).

        Yields ``AgentStreamEvent(type="text", ...)`` for each text delta as it
        arrives, updating ``state.full_answer`` incrementally. Captures
        ``first_token_latency_ms`` as the wall-clock time to the first text
        delta, distinct from total turn latency. Returns (via generator return)
        the completed Anthropic message so the caller can detect tool_use blocks
        and continue the loop. Tool-calling turns emit no text deltas (the model
        produces only tool_use content), so only the final answer turn streams to
        the user — while every turn still yields a complete message.
        """
        if self.adapter.provider != "anthropic":
            response = self._create_message(
                client,
                session,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                token_usage=state.token_usage,
                stream=False,
            )
            if not self.adapter.parse_tool_calls(response):
                text = self.adapter.final_response(response)
                for chunk in self._chunks(text):
                    state.full_answer += chunk
                    yield AgentStreamEvent(type="text", content=chunk)
            return response

        with session.tracker.step(
            step_type="model_call",
            input_payload={
                "message_count": len(messages),
                "tool_count": len(session.tool_schemas),
                "stream": True,
                "model_route": session.model_route.to_dict(),
            },
        ) as model_step:
            started = perf_counter()
            first_token_at: Optional[float] = None
            call_error: Optional[Exception] = None
            final_message: Any = None
            try:
                # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — AP-LAO-01 stream
                # analog: temperature omitted when the routed model rejects it.
                stream_manager = self.adapter.stream_response(
                    client,
                    model=session.model_route.model,
                    max_tokens=max_tokens,
                    **self._temperature_kwargs(session, temperature),
                    system=self._cached_system_blocks(session),
                    tools=self._cached_tool_schemas(session),
                    messages=_messages_with_cache_breakpoint(messages),
                    metadata=self._tenant_metadata(session),
                )
                turn_text = ""
                with stream_manager as stream:
                    for delta in self.adapter.iter_text_deltas(stream):
                        if first_token_at is None:
                            first_token_at = perf_counter()
                        turn_text += delta
                        state.full_answer += delta
                        # Emit text deltas live as they arrive so the FINAL
                        # answer turn renders incrementally (audit S2).
                        yield AgentStreamEvent(type="text", content=delta)
                    final_message = stream.get_final_message()
                # If this turn actually requested tools, any preamble text it
                # streamed is not the answer — roll it back out of full_answer.
                # The tool loop will continue and a later no-tool turn produces
                # the real answer.
                if turn_text and self.adapter.parse_tool_calls(final_message):
                    if state.full_answer.endswith(turn_text):
                        state.full_answer = state.full_answer[: -len(turn_text)]
            except Exception as exc:
                call_error = exc

            latency_ms = int((perf_counter() - started) * 1000)
            first_token_latency_ms = (
                int((first_token_at - started) * 1000) if first_token_at is not None else None
            )

            if call_error is not None:
                self._record_step_usage(
                    session,
                    model_step,
                    latency_ms=latency_ms,
                    status="error",
                    error=call_error,
                    usage=None,
                )
                raise call_error

            usage = self.adapter.extract_usage(final_message)
            state.token_usage["input_tokens"] += usage["input_tokens"]
            state.token_usage["output_tokens"] += usage["output_tokens"]
            self._record_step_usage(
                session,
                model_step,
                latency_ms=latency_ms,
                status="ok",
                error=None,
                usage=self._usage_with_cache(final_message, usage),
                first_token_latency_ms=first_token_latency_ms,
            )
            model_step.finish(
                status="succeeded",
                output_payload={
                    "stop_reason": getattr(final_message, "stop_reason", None),
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "first_token_latency_ms": first_token_latency_ms,
                },
            )
            return final_message

    @staticmethod
    def _log_cache_efficiency(model: Optional[str], usage: Optional[dict[str, Any]]) -> None:
        """Emit a real-time cache-hit signal per model call (LLM optimization
        roadmap, observability quick win).

        Low cache-hit on a high-token prompt means the cacheable prefix is being
        busted; surfacing it live lets ops catch a cache-busting regression in
        seconds rather than after a retrospective observability query.
        """
        if not usage:
            return
        input_tokens = int(usage.get("input_tokens") or 0)
        if input_tokens < 1000:
            return
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cached_input = int(usage.get("cached_input_tokens") or 0)
        billed = input_tokens + cache_read
        hit_rate = (cache_read + cached_input) / billed if billed else 0.0
        if hit_rate < 0.1:
            logger.warning(
                "[AgentRuntime] low prompt-cache hit rate model=%s input_tokens=%s hit_rate=%.2f",
                model,
                input_tokens,
                hit_rate,
            )
        elif settings.llm_verbose_routing_logs:
            logger.info(
                "[AgentRuntime] prompt-cache hit rate model=%s input_tokens=%s hit_rate=%.2f",
                model,
                input_tokens,
                hit_rate,
            )

    @staticmethod
    def _usage_with_cache(response: Any, usage: dict[str, int]) -> dict[str, Any]:
        """Augment normalized usage with Anthropic prompt-cache token fields."""
        merged: dict[str, Any] = dict(usage)
        raw = getattr(response, "usage", None)
        if raw is not None:
            cache_creation = getattr(raw, "cache_creation_input_tokens", None)
            cache_read = getattr(raw, "cache_read_input_tokens", None)
            if cache_creation is not None:
                merged["cache_creation_input_tokens"] = int(cache_creation or 0)
            if cache_read is not None:
                merged["cache_read_input_tokens"] = int(cache_read or 0)
        return merged

    def _record_step_usage(
        self,
        session: AgentRuntimeSession,
        model_step: Any,
        *,
        latency_ms: int,
        status: str,
        error: Optional[Exception],
        usage: Optional[dict[str, Any]],
        first_token_latency_ms: Optional[int] = None,
    ) -> None:
        """Best-effort LLMUsageEvent for an agent-runtime model call, scoped to
        the run's realm triple. Never raises, never blocks the loop.

        ``first_token_latency_ms`` is supplied explicitly by the streaming path
        (time-to-first-text-delta); for non-streamed calls it defaults to the
        full latency on success (audit S2).
        """
        try:
            run = getattr(session.tracker, "run", None)
            context = getattr(session, "context", None)
            # Read the PKs from the trackers' plain-Python mirrors (captured at
            # flush), NOT the expired-on-commit ORM attributes — an ORM `run.id` /
            # `step.id` read here would re-open an autobegun transaction and pin a
            # pooled connection just to recover an id we already hold (AC-2.1). The
            # ORM-attr branch is only a fallback for a tracker double that carries
            # no mirror; the real trackers always set it, so prod never reloads.
            agent_run_id = getattr(session.tracker, "run_id", None) or getattr(run, "id", None)
            step_obj = getattr(model_step, "step", None)
            agent_step_id = getattr(model_step, "step_id", None) or getattr(step_obj, "id", None)
            route = session.model_route
            if first_token_latency_ms is None and status == "ok":
                first_token_latency_ms = latency_ms
            # Per-step cost attribution (LLM optimization roadmap #4): cost is
            # computed and persisted per model call (keyed to this step / model /
            # lane) instead of only as a run-level rollup, so an expensive outlier
            # turn — or the post-escalation frontier-model turns — are individually
            # attributable.
            model_name = getattr(route, "model", None)
            cost = estimate_model_cost_usd(model_name, usage) if status == "ok" else None
            self._log_cache_efficiency(model_name, usage)
            organization_id = getattr(run, "organization_id", None) or getattr(context, "organization_id", None)
            organization_company_id = getattr(run, "organization_company_id", None) or getattr(
                context, "organization_company_id", None
            )
            qbo_realm_id = getattr(context, "qbo_realm_id", None) or getattr(run, "company_id", None)
            # Dev/staging attribution canary: an agent-run usage row with a hole
            # in the realm triple won't roll up to a tenant. Surface it loudly off
            # production so the gap is caught before it reaches cost reporting.
            if settings.environment != "production" and not (
                organization_id and organization_company_id and qbo_realm_id
            ):
                logger.warning(
                    "[AgentRuntime] usage row with incomplete realm triple "
                    "org=%s company=%s realm=%s (run=%s)",
                    organization_id,
                    organization_company_id,
                    qbo_realm_id,
                    agent_run_id,
                )
            usage_event_id = record_llm_usage(
                provider=getattr(route, "provider", None),
                model=model_name,
                module="agent_runtime",
                route=getattr(route, "lane", None) or getattr(getattr(route, "risk_level", None), "value", None) or "agent_loop",
                status=status,
                latency_ms=latency_ms,
                first_token_latency_ms=first_token_latency_ms,
                token_usage=usage,
                estimated_cost_usd=float(cost) if cost is not None else None,
                error=error,
                organization_id=organization_id,
                organization_company_id=organization_company_id,
                qbo_realm_id=qbo_realm_id,
                user_id=getattr(run, "user_id", None),
                agent_run_id=agent_run_id,
                agent_step_id=agent_step_id,
                tool_call_count=len(session.tool_schemas or []),
                # AC-4 (plan 20260715-fluid-tool-selection): record the classified intent that
                # drove tool selection this turn, so routing is observable. This is the ONLY
                # populate site — the generic service recorder (llm.py) has no AgentIntent in
                # scope and leaves it null. Records routing, does not change it; arms the
                # deferred lever-1 gate (re-open only if telemetry shows a routed intent whose
                # bundle omits a needed READ >10% of chat turns).
                tool_bundle_name=getattr(context, "intent", None),
            )
            if usage_event_id is None:
                self._mark_observation_incomplete(session, "usage_recording_failed")
        except Exception as exc:  # pragma: no cover - defensive
            self._mark_observation_incomplete(session, "usage_recording_failed")
            # FAIL-OPEN but LOUD (W-1): a dropped ledger row is unmetered spend.
            logger.warning("[AgentRuntime] usage recording failed (unmetered call!): %s", exc)

    @staticmethod
    def _mark_observation_incomplete(session, reason: str) -> None:
        tracker = session.tracker
        if callable(getattr(tracker, "mark_degraded", None)):
            tracker.mark_degraded(reason)
        else:
            reasons = list(getattr(session, "observation_reasons", []) or [])
            if reason not in reasons:
                reasons.append(reason)
            session.observation_reasons = reasons

    @staticmethod
    def _stamp_observation(session, usage: dict) -> None:
        reasons = list(getattr(session.tracker, "degraded_reasons", []) or [])
        reasons.extend(getattr(session, "observation_reasons", []) or [])
        if reasons:
            usage["observation_incomplete"] = True
            usage["observation_reasons"] = list(dict.fromkeys(reasons))

    def _apply_tool_calls(
        self,
        session: AgentRuntimeSession,
        response: Any,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        tool_events: list[dict[str, Any]],
        *,
        result_transform: Optional[ToolResultTransform],
    ) -> list[dict[str, Any]]:
        tool_results = []
        executed: list[dict[str, Any]] = []
        content_blocks = list(getattr(response, "content", []) or [])
        # Bind the tenant identity for the synchronous span where tools (and any
        # result-transform compression) run — that is where lane LLM calls are
        # made, so they pick up the provider tag and realm triple. We bind HERE
        # rather than across the whole loop because the streaming loop yields
        # between turns, and a ContextVar token cannot be reset across the copied
        # contexts the stream is driven in. Tool execution never yields, so
        # set/reset stay in one context.
        with self._bind_session_tenant(session):
            for call in tool_calls:
                tool_name = str(call["name"])
                tool_input = call.get("input") or {}
                # Plan 20260712-agentic-finance-os W2 (GAP-2): capture handler-emitted typed
                # facts for this invocation. Facts ride the EVENT dict (loop bookkeeping),
                # NEVER the provider tool_result block (AP-LAO-07 pairing stays untouched).
                captured_facts: list = []
                try:
                    session.executor.db.rollback()
                    with capture_facts() as captured_facts:
                        raw_result = session.execute_tool(tool_name, tool_input)
                    result_text = result_transform(tool_name, raw_result) if result_transform else raw_result
                except Exception as exc:
                    session.executor.db.rollback()
                    captured_facts = []  # a failed tool contributes NO evidence
                    error_detail = self.adapter.normalize_errors(exc)
                    result_text = f"Error executing {tool_name}. Try to fix your parameters and try again. Details: {error_detail}"
                    logger.error("[AgentRuntime] Tool %s failed: %s", tool_name, error_detail)

                event = {"tool": tool_name, "input": tool_input, "result": result_text}
                outcome = getattr(session, "last_tool_outcome", None)
                if outcome is not None:
                    event["outcome"] = {"status": outcome.status, "code": outcome.code, "action": outcome.action}
                if captured_facts:
                    event["facts"] = [fact.to_payload() for fact in captured_facts]
                tool_events.append(event)
                executed.append(event)
                # The tool result is untrusted content re-entering the model (QBO/DB/external
                # strings, or a normalized error) — it is the dominant unfenced injection surface
                # (SoK arXiv 2601.17548). Fence it in a per-result <untrusted source="tool:{name}">
                # envelope via the EXISTING prompt_fence so a spoofed </untrusted><system>… breakout
                # is neutralized, not passed through as a live tag. Only the model-channel `content`
                # is wrapped; the `event` dict above keeps the raw text for telemetry/downstream.
                # `wrap_untrusted` sanitizes the source label (the colon collapses to a hyphen).
                fenced_content = wrap_untrusted(f"tool:{tool_name}", result_text)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": fenced_content,
                    }
                )

        if _is_openai_family(self.adapter):
            messages.append(self._openai_assistant_tool_message(response))
            self.adapter.submit_tool_results(messages, tool_results)
        else:
            messages.append({"role": "assistant", "content": content_blocks})
            messages.append({"role": "user", "content": tool_results})
        return executed

    @staticmethod
    def _openai_assistant_tool_message(response: Any) -> dict[str, Any]:
        choices = getattr(response, "choices", []) or []
        message = getattr(choices[0], "message", None) if choices else None
        tool_calls = []
        for call in getattr(message, "tool_calls", []) or []:
            function = getattr(call, "function", None)
            tool_calls.append(
                {
                    "id": getattr(call, "id", None),
                    "type": getattr(call, "type", "function") or "function",
                    "function": {
                        "name": getattr(function, "name", None),
                        "arguments": getattr(function, "arguments", "{}"),
                    },
                }
            )
        return {
            "role": "assistant",
            "content": getattr(message, "content", None),
            "tool_calls": tool_calls,
        }

    def _context_budget_answer(self, tool_events: list[dict[str, Any]]) -> str:
        notice = "Analysis stopped because the available context is too large. Please narrow the period or question to continue."
        return (self._fallback_answer(tool_events) + "\n\n" if tool_events else "") + notice

    def _fallback_answer(self, tool_events: list[dict[str, Any]]) -> str:
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B4 — fence gap G3: this path
        # previously presented RAW tool text verbatim as Peregrine's own words ("Here's what I
        # found") — adversarial memo/vendor text could render as the assistant's voice, persist
        # as an assistant turn, and re-enter later prompts unfenced. Now: structural tags are
        # neutralized, the snippets are explicitly labeled raw + unverified, and the framing is
        # honest about the run ending before a narrative was composed.
        summaries = []
        for event in tool_events:
            tool = event.get("tool", "unknown")  # SA-OK: dict-key access on the in-memory tool-event dict, not a DB query
            result = neutralize_untrusted_text(str(event.get("result") or ""))  # SA-OK: dict-key access, not a DB query
            if len(result) > 300:
                result = result[:300] + "..."
            summaries.append(f"{tool}: {result}")
        return (
            "I retrieved data but the run ended before I could compose a full answer. "
            "Below are the raw tool outputs (unverified excerpts, not my analysis) — "
            "ask again for a complete interpretation:\n\n" + "\n\n".join(summaries)
        )

    def _chunks(self, text: str, *, size: int = 80) -> Iterator[str]:
        for start in range(0, len(text), size):
            yield text[start:start + size]

    @staticmethod
    def _user_safe_model_error(*, partial_answer: bool) -> str:
        if partial_answer:
            return "The model provider failed after the response started. Please retry if the answer looks incomplete."
        return "I hit a model provider error before I could answer. Please try again."

    def _default_client_factory(self) -> Any:
        return self._client_factory_for_adapter(self.adapter)()

    @staticmethod
    def _mark_active_provider(session: AgentRuntimeSession, route: ModelRoute) -> None:
        tracker = getattr(session, "tracker", None)
        if tracker is not None:
            tracker.model_provider = route.provider
            tracker.model_name = route.model
            run = getattr(tracker, "run", None)
            if run is not None:
                run.model_provider = route.provider
                run.model_name = route.model

    @staticmethod
    def _client_factory_for_adapter(adapter: ModelAdapter) -> Callable[[], Any]:
        if adapter.provider == "openai":
            return _shared_openai_client
        if _is_openai_family(adapter):
            provider = adapter.provider
            return lambda: _openai_compatible_client(provider)
        if adapter.provider == "google":
            return _shared_gemini_client
        return _shared_anthropic_client

    @staticmethod
    def _fallback_route(route: Any) -> Optional[ModelRoute]:
        provider = (settings.task_llm_fallback_provider or "").strip().lower()
        model = (settings.task_llm_fallback_model or "").strip()
        if not provider or not model:
            return None
        if provider in {getattr(route, "provider", None), ""}:
            return None
        return ModelRoute(
            provider=provider,
            model=model,
            lane="tool_runtime_fallback",
            reason=f"primary_provider_failed:{getattr(route, 'provider', 'unknown')}",
        )

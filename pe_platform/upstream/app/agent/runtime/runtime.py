from __future__ import annotations

import secrets
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models import AgentRun, EntityBilling, OrganizationCompany, TenantBilling
from ..performance import downgrade_route, route_chat_model
from ..tracking import AgentRunTracker
from ...config import settings
from ...services.cost_attribution import BUDGET_REACHED_MESSAGE, enforce_tenant_budget
from ...services.tenant_companies import company_is_suspended
from ...services.tenant_usage import summarize_tenant_usage
from ...services.tool_outcomes import ToolExecutionOutcome
from ...assistant.errors import (
    AssistantBudgetUnavailable,
    AssistantBudgetReached,
    AssistantCompanySuspended,
    AssistantDuplicateRun,
    AssistantQuotaExceeded,
    AssistantRequestTooLarge,
)
from .confirmation_tokens import compute_action_fingerprint
from .context_packet import ContextPacketBuilder
from .prompt_builder import PromptBuilder
from .tool_executor import ToolExecutor
from .tool_policy import ToolPolicyGate
from .tool_registry import ToolRegistry
from .types import ContextPacket, OutputMode, RiskLevel, ToolDefinition


@dataclass
class AgentRuntimeSession:
    context: ContextPacket
    system_prompt: str
    messages: list[dict[str, Any]]
    tools: list[ToolDefinition]
    tool_schemas: list[dict[str, Any]]
    tracker: AgentRunTracker
    executor: ToolExecutor
    model_route: Any
    company_name: str
    # Split system prompt for prompt-cache structuring (audit S3). The stable
    # prefix is byte-stable across requests; the volatile suffix carries the
    # request- and realm-specific context. ``system_prompt`` stays available as
    # the concatenation for non-Anthropic lanes / logging.
    stable_system_prompt: str = ""
    volatile_system_prompt: str = ""
    used_confirmation_tools: set[str] = field(default_factory=set)
    # Phase 6: maps a confirmed tool name -> the canonical action fingerprint
    # that was bound to its confirmation token at issue time (or None for a
    # legacy token issued without tool_input). At execution time the fingerprint
    # is recomputed from the trusted ContextPacket scope + the ACTUAL tool input
    # the model emitted; a stored fingerprint that does not match the action
    # being executed blocks the call. This is what stops a token confirmed for
    # action A from authorizing a materially different action B.
    confirmed_action_fingerprints: dict[str, Optional[str]] = field(default_factory=dict)
    # Phase B spend guard: the enforce_tenant_budget verdict for this run
    # ("downgrade" under soft-budget pressure, else None). Bound onto the
    # budget-pressure ContextVar during tool execution so lane calls can bias
    # toward cheaper routes. A "block" verdict raises during build and never
    # produces a session.
    budget_action: Optional[str] = None
    last_tool_outcome: ToolExecutionOutcome | None = None

    def execute_tool(self, tool_name: str, tool_input: Any) -> str:
        self.last_tool_outcome = None
        tool = next((candidate for candidate in self.tools if candidate.name == tool_name), None)
        if not tool:
            with self.tracker.step(
                step_type="tool_call",
                tool_name=tool_name,
                input_payload={
                    "tool_input": tool_input,
                    "allowed": False,
                    "current_user_role": self.context.user_role,
                    "company_id": self.context.company_id,
                },
            ) as step:
                step.finish(
                    status="blocked",
                    output_payload={
                        "allowed": False,
                        "block_reason": f"Tool '{tool_name}' is not available in this run",
                    },
                    error=f"Tool '{tool_name}' is not available in this run",
                )
            return f"Tool call blocked by Vesper Bot policy: Tool '{tool_name}' is not available in this run"
        if tool.requires_confirmation and tool.name in self.used_confirmation_tools:
            with self.tracker.step(
                step_type="tool_call",
                tool_name=tool_name,
                input_payload={
                    "tool_input": tool_input,
                    "allowed": False,
                    "current_user_role": self.context.user_role,
                    "company_id": self.context.company_id,
                    "requires_confirmation": True,
                },
            ) as step:
                step.finish(
                    status="blocked",
                    output_payload={
                        "allowed": False,
                        "block_reason": "Confirmation for this tool has already been consumed in this run",
                    },
                    error="Confirmation for this tool has already been consumed in this run",
                )
            return "Tool call blocked by Vesper Bot policy: Confirmation for this tool has already been consumed in this run"
        if tool.requires_confirmation:
            fingerprint_block = self._verify_action_fingerprint(tool, tool_input)
            if fingerprint_block is not None:
                return fingerprint_block
            self.used_confirmation_tools.add(tool.name)
        # The public chat facade may be specialized by an embedding application.
        # Only its genuine typed execution result can supply authority metadata.
        self.executor.last_outcome = None
        text = self.executor.execute(tool, self.context, tool_input)
        outcome = self.executor.last_outcome
        self.last_tool_outcome = outcome if isinstance(outcome, ToolExecutionOutcome) else None
        return text

    def _verify_action_fingerprint(self, tool: ToolDefinition, tool_input: Any) -> Optional[str]:
        """Phase 6 binding check.

        If the confirmation token consumed for this tool carried a canonical
        action fingerprint, recompute the fingerprint from the trusted
        ContextPacket scope plus the ACTUAL tool input the model emitted and
        require an exact match. A token issued without a fingerprint (legacy
        scope-only confirmation) leaves the entry as None and skips the check,
        preserving backward compatibility. Returns a policy-block string when
        the action does not match, else None to allow.
        """
        expected = self.confirmed_action_fingerprints.get(tool.name)
        if not expected:
            return None
        actual = compute_action_fingerprint(
            tool_name=tool.name,
            user_id=self.context.user_id,
            company_id=self.context.company_id,
            organization_id=self.context.organization_id,
            organization_company_id=self.context.organization_company_id,
            qbo_realm_id=self.context.qbo_realm_id,
            tool_input=tool_input if isinstance(tool_input, dict) else None,
        )
        if secrets.compare_digest(expected, actual):
            return None
        block_reason = (
            "Confirmation token does not match this action — the confirmed "
            "action differs from the action the assistant attempted to execute"
        )
        with self.tracker.step(
            step_type="tool_call",
            tool_name=tool.name,
            input_payload={
                "tool_input": tool_input,
                "allowed": False,
                "current_user_role": self.context.user_role,
                "company_id": self.context.company_id,
                "requires_confirmation": True,
            },
        ) as step:
            step.finish(
                status="blocked",
                output_payload={"allowed": False, "block_reason": block_reason},
                error=block_reason,
            )
        return f"Tool call blocked by Vesper Bot policy: {block_reason}"


class AgentRuntime:
    def __init__(
        self,
        *,
        db: Session,
        registry: ToolRegistry,
        policy_gate: Optional[ToolPolicyGate] = None,
        prompt_builder: Optional[PromptBuilder] = None,
        context_builder: Optional[ContextPacketBuilder] = None,
    ):
        self.db = db
        self.registry = registry
        self.policy_gate = policy_gate or ToolPolicyGate()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.context_builder = context_builder or ContextPacketBuilder(db)

    def prepare_session(
        self,
        *,
        request_text: str,
        company_id: str,
        user: Any,
        user_role: str,
        conversation_id: Optional[int],
        trigger_type: str,
        output_mode: OutputMode,
        organization_id: Optional[int] = None,
        organization_company_id: Optional[int] = None,
        tenant_id: Optional[str] = None,
        request_id: Optional[str] = None,
        max_tools: int | None = None,
        risk_level: RiskLevel = RiskLevel.INTERNAL_WRITE,
        confirmation_tokens: Optional[list[str]] = None,
        confirmed_action_fingerprints: Optional[dict[str, Optional[str]]] = None,
    ) -> AgentRuntimeSession:
        self._enforce_runtime_limits(
            request_text=request_text,
            company_id=company_id,
            user=user,
            conversation_id=conversation_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
        )
        # Phase B spend guard: evaluate the tenant's monthly AI budget alongside
        # the suspend/token-limit checks above. A hard over-budget blocks the run
        # here (before any model call); a soft over-budget biases routing cheaper
        # (applied to model_route below). Unavailable budget evidence refuses spending.
        budget_verdict = enforce_tenant_budget(
            self.db,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
        )
        if budget_verdict["action"] == "unavailable":
            raise AssistantBudgetUnavailable()
        if budget_verdict["action"] == "block":
            raise AssistantBudgetReached(BUDGET_REACHED_MESSAGE)
        budget_action = budget_verdict["action"]  # "allow" or "downgrade"
        # Audit S5: compute the expensive, request-stable context selections
        # (intent classification, conversation history, company memory, financial
        # briefing, company info) exactly once and reuse them across both the
        # provisional and final context builds. These selections are realm-scoped
        # by construction and identical between the two passes, so reusing them
        # removes the duplicated request-path latency without changing behaviour.
        prepared_sources = self.context_builder.prepare_sources(
            request_text=request_text,
            company_id=company_id,
            conversation_id=conversation_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
        )
        provisional_context = self.context_builder.build(
            request_text=request_text,
            company_id=company_id,
            user=user,
            user_role=user_role,
            conversation_id=conversation_id,
            output_mode=output_mode,
            tenant_id=tenant_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            risk_level=risk_level,
            confirmation_tokens=confirmation_tokens,
            prepared_sources=prepared_sources,
        )
        # Tiered routing: when fast-first is enabled, simple intents start on the
        # cheap model and the loop escalates to the frontier model on the first
        # tool call (see AnthropicRuntimeLoop). LLM_FAST_MODEL still wins if an
        # operator has pinned one explicitly.
        fast_model = settings.llm_fast_model or (
            settings.agent_fast_model if settings.agent_fast_first_turn else settings.task_llm_model
        )
        model_route = route_chat_model(
            provider=settings.task_llm_provider,
            default_model=settings.task_llm_model,
            fast_model=fast_model or settings.task_llm_model,
            reasoning_model=settings.llm_reasoning_model or settings.task_llm_model,
            intent=provisional_context.intent,
            requires_tools=True,
        )
        # Soft budget pressure biases the interactive task lane one Claude tier
        # cheaper (Opus→Sonnet→Haiku). It stays on Claude — the tool loop is
        # provider-pinned — and the budget reason is annotated for audit.
        if budget_action == "downgrade":
            model_route = downgrade_route(
                model_route,
                default_model=settings.task_llm_model,
                fast_model=fast_model or settings.task_llm_model,
                reasoning_model=settings.llm_reasoning_model or settings.task_llm_model,
                reason=budget_verdict["reason"],
            )
        # PLAN-EXEMPT: pin confirmed tools through the max_tools read-budget trim (confirmation-flow
        # fix). A requires_confirmation tool the user has explicitly confirmed this turn (its name is
        # carried on provisional_context.confirmation_tokens) MUST survive selection — otherwise it is
        # silently dropped by the read-budget cap BEFORE the policy gate can honor the token, and the
        # confirmed write can never execute (generate_report sat past the 15-tool cap in the
        # reporting_analysis bundle). An un-confirmed confirm-gated tool is still trimmed harmlessly:
        # the policy gate would block it anyway, so pinning ONLY the confirmed set is both necessary
        # and sufficient.
        selected = self.registry.select_for_intent(
            provisional_context.intent,
            max_tools=max_tools,
            pinned=provisional_context.confirmation_tokens,
        )
        allowed, forbidden = self.policy_gate.filter_allowed_tools(selected, provisional_context)
        company_name = prepared_sources.company_name

        tracker = AgentRunTracker(
            self.db,
            company_id=company_id,
            trigger_type=trigger_type,
            input_summary=request_text,
            conversation_id=conversation_id,
            model_provider=model_route.provider,
            model_name=model_route.model,
            prompt_version=self.prompt_builder.prompt_version,
            user_id=getattr(user, "id", None),
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            permission_scope={
                "role": user_role,
                "allowed_tools": [tool.name for tool in allowed],
                "forbidden_tools": forbidden,
                "risk_level": risk_level.value,
                "writeback": "proposal_first_code_policy_controlled",
                "organization_id": organization_id,
                "organization_company_id": organization_company_id,
                "realm_id": company_id,
            },
            request_id=request_id,
        )
        tracker.start()

        context = self.context_builder.build(
            request_text=request_text,
            company_id=company_id,
            user=user,
            user_role=user_role,
            conversation_id=conversation_id,
            output_mode=output_mode,
            run_id=tracker.run_id,
            tenant_id=tenant_id,
            organization_id=organization_id,
            organization_company_id=organization_company_id,
            model_routing_metadata=model_route.to_dict() if hasattr(model_route, "to_dict") else {},
            allowed_tools=[tool.name for tool in allowed],
            forbidden_tools=forbidden,
            risk_level=risk_level,
            confirmation_tokens=confirmation_tokens,
            prepared_sources=prepared_sources,
        )
        stable_system_prompt = self.prompt_builder.build_stable_policy(company_name=company_name)
        volatile_system_prompt = self.prompt_builder.build_volatile_context(context)
        system_prompt = self.prompt_builder.build_system_prompt(context, company_name=company_name)
        messages = self.prompt_builder.build_messages(context)
        executor = ToolExecutor(db=self.db, policy_gate=self.policy_gate, tracker=tracker)
        return AgentRuntimeSession(
            context=context,
            system_prompt=system_prompt,
            stable_system_prompt=stable_system_prompt,
            volatile_system_prompt=volatile_system_prompt,
            messages=messages,
            tools=allowed,
            tool_schemas=[tool.to_model_schema() for tool in allowed],
            tracker=tracker,
            executor=executor,
            model_route=model_route,
            company_name=company_name,
            confirmed_action_fingerprints=dict(confirmed_action_fingerprints or {}),
            budget_action=budget_action,
        )

    def _enforce_runtime_limits(
        self,
        *,
        request_text: str,
        company_id: str,
        user: Any,
        conversation_id: Optional[int],
        organization_id: Optional[int],
        organization_company_id: Optional[int],
    ) -> None:
        if len(request_text or "") > settings.agent_max_request_chars:
            raise AssistantRequestTooLarge("Agent request exceeds configured context limit")

        duplicate_window_start = datetime.utcnow() - timedelta(seconds=settings.agent_duplicate_run_window_seconds)
        duplicate = (
            self.db.query(AgentRun)
            .filter(
                AgentRun.company_id == company_id,
                AgentRun.user_id == getattr(user, "id", None),
                AgentRun.status.in_(["queued", "running"]),
                AgentRun.created_at >= duplicate_window_start,
            )
        )
        if conversation_id is not None:
            duplicate = duplicate.filter(AgentRun.conversation_id == conversation_id)
        if organization_id is not None:
            duplicate = duplicate.filter(or_(AgentRun.organization_id == organization_id, AgentRun.organization_id.is_(None)))
        if duplicate.first():
            raise AssistantDuplicateRun("A matching agent run is already in progress")

        if organization_id is None:
            return

        # Block runs for a company that has been shut off (manual suspend or unpaid billing).
        if organization_company_id is not None:
            org_company = (
                self.db.query(OrganizationCompany)
                .filter(OrganizationCompany.id == organization_company_id)
                .first()
            )
            if company_is_suspended(self.db, organization_company=org_company):
                raise AssistantCompanySuspended("This company is suspended")

        tenant_billing = self.db.query(TenantBilling).filter(TenantBilling.organization_id == organization_id).first()
        entity_billing = None
        if organization_company_id is not None:
            entity_billing = (
                self.db.query(EntityBilling)
                .filter(EntityBilling.organization_company_id == organization_company_id)
                .first()
            )
        # Aggregate usage at the same scope as the limit being enforced: a per-company
        # EntityBilling limit is measured against that company's usage, not the whole org.
        if entity_billing and entity_billing.token_limit_monthly is not None:
            token_limit = entity_billing.token_limit_monthly
            usage = summarize_tenant_usage(self.db, organization_id=organization_id, company_id=company_id)
        else:
            token_limit = tenant_billing.token_limit_monthly if tenant_billing else None
            usage = summarize_tenant_usage(self.db, organization_id=organization_id, company_id=None)
        if token_limit is not None and usage["total_tokens"] >= token_limit:
            raise AssistantQuotaExceeded("Tenant monthly agent token limit has been reached")

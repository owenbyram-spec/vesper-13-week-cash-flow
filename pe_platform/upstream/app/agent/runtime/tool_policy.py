from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ...dependencies import ROLE_WEIGHTS
from .tool_registry import canonicalize_intent
from .types import ContextPacket, OutputMode, RISK_WEIGHTS, RiskLevel, ToolDefinition  # noqa: F401


class ToolPolicyViolation(PermissionError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: Optional[str] = None


class ToolPolicyGate:
    """Authoritative policy check for every model-proposed tool call."""

    def filter_allowed_tools(self, tools: list[ToolDefinition], context: ContextPacket) -> tuple[list[ToolDefinition], list[dict[str, Any]]]:
        allowed: list[ToolDefinition] = []
        forbidden: list[dict[str, Any]] = []
        for tool in tools:
            decision = self.check(tool, {}, context, preflight=True)
            if decision.allowed:
                allowed.append(tool)
            else:
                forbidden.append({"name": tool.name, "reason": decision.reason})
        return allowed, forbidden

    def require_allowed(self, tool: ToolDefinition, tool_input: Any, context: ContextPacket) -> None:
        decision = self.check(tool, tool_input, context, preflight=False)
        if not decision.allowed:
            raise ToolPolicyViolation(decision.reason or "Tool call blocked by policy")

    def check(self, tool: ToolDefinition, tool_input: Any, context: ContextPacket, *, preflight: bool = False) -> ToolPolicyDecision:
        if not tool:
            return ToolPolicyDecision(False, "Unknown tool")

        intent = canonicalize_intent(context.intent)
        from .specialist_specs import SPECIALIST_SPECS
        spec = SPECIALIST_SPECS.get(intent)
        if spec is not None and tool.name in spec.prohibited_tools:
            return ToolPolicyDecision(False, "Tool is prohibited for this domain intent")

        if tool.risk_level == RiskLevel.ADMIN and intent != "admin":
            return ToolPolicyDecision(False, "Admin tools are not available in assistant chat")

        if intent not in tool.allowed_intents and "general" not in tool.allowed_intents:
            return ToolPolicyDecision(False, f"Tool is not allowed for intent '{intent}'")

        if context.output_mode not in tool.allowed_output_modes:
            return ToolPolicyDecision(False, f"Tool is not allowed in {context.output_mode.value} mode")

        user_weight = ROLE_WEIGHTS.get((context.user_role or "VIEWER").upper(), 0)
        required_weight = ROLE_WEIGHTS.get((tool.required_role or "VIEWER").upper(), 10)
        if user_weight < required_weight:
            return ToolPolicyDecision(False, f"Role {tool.required_role} required")

        if RISK_WEIGHTS[tool.risk_level] > RISK_WEIGHTS[context.risk_level]:
            return ToolPolicyDecision(False, f"Tool risk {tool.risk_level.value} exceeds run risk {context.risk_level.value}")

        if tool.mutates_state and user_weight <= ROLE_WEIGHTS["VIEWER"]:
            return ToolPolicyDecision(False, "Read-only users cannot execute mutating tools")

        if tool.external_side_effect:
            return ToolPolicyDecision(False, "External side-effect tools require approval")

        if tool.requires_confirmation and tool.name not in set(context.confirmation_tokens or []):
            reason = "Tool requires explicit backend confirmation before execution"
            if preflight:
                return ToolPolicyDecision(False, reason)
            return ToolPolicyDecision(False, reason)

        if not preflight:
            scope_decision = self._check_scope_override(tool_input, context)
            if not scope_decision.allowed:
                return scope_decision

        return ToolPolicyDecision(True)

    def _check_scope_override(self, tool_input: Any, context: ContextPacket) -> ToolPolicyDecision:
        if not isinstance(tool_input, dict):
            return ToolPolicyDecision(True)

        def normalize(value: Any) -> str:
            return str(value).strip()

        for key in ("realm_id", "company_id", "qbo_realm_id"):
            value = tool_input.get(key)
            if value not in (None, "") and normalize(value) != context.company_id:
                return ToolPolicyDecision(False, f"Tool input attempted to use unauthorized {key}")

        tenant_value = tool_input.get("tenant_id")
        if tenant_value not in (None, "") and context.tenant_id and normalize(tenant_value) != str(context.tenant_id):
            return ToolPolicyDecision(False, "Tool input attempted to override tenant_id")

        org_value = tool_input.get("organization_id")
        if org_value not in (None, "") and context.organization_id and normalize(org_value) != str(context.organization_id):
            return ToolPolicyDecision(False, "Tool input attempted to override organization_id")

        org_company_value = tool_input.get("organization_company_id")
        if (
            org_company_value not in (None, "")
            and context.organization_company_id
            and normalize(org_company_value) != str(context.organization_company_id)
        ):
            return ToolPolicyDecision(False, "Tool input attempted to override organization_company_id")

        return ToolPolicyDecision(True)

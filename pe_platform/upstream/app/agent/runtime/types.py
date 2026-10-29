from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict
from typing import Any, Callable, Optional

from ...services.tool_outcomes import DraftedToolResult


class EpisodicContextBlock(TypedDict):
    """One distilled past-run summary block (LG-4, Wave 3.3), produced by the
    EpisodicMemorySelector and consumed by PromptBuilder._wrapped_blocks. Model-
    generated text — rendered ONLY through the wrap_untrusted fence."""

    type: str      # always "past_investigation" (the fence source label)
    content: str   # the template-assembled honest paragraph
    source: str    # always "AgentEpisode" (provenance)


class ToolFailureBlock(TypedDict):
    """One known-tool-failure caveat projection (LG-5, Wave 3.3), produced by the
    ToolFailureSelector and rendered by PromptBuilder._dynamic_request_context.

    CODE-CONTROLLED fields ONLY (E1/E-D): the caveat renders in the TRUSTED,
    UNFENCED request block, so NO model/exception-derived string may enter it —
    ``last_error_redacted`` is NEVER projected here. ``error_class`` is one of the
    ``classify_error`` closed-vocab constants; ``last_seen`` is a code-generated
    ISO timestamp."""

    tool_name: str       # a registered tool name (code-controlled)
    error_class: str     # a classify_error closed-vocab constant (code-controlled)
    failure_count: int   # a DB counter (code-controlled)
    last_seen: str       # an ISO timestamp (code-controlled) — NEVER an error string


class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    DRAFT_ONLY = "DRAFT_ONLY"
    INTERNAL_WRITE = "INTERNAL_WRITE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    ADMIN = "ADMIN"


RISK_WEIGHTS = {
    RiskLevel.READ_ONLY: 10,
    RiskLevel.ANALYSIS_ONLY: 20,
    RiskLevel.DRAFT_ONLY: 30,
    RiskLevel.INTERNAL_WRITE: 40,
    RiskLevel.EXTERNAL_WRITE: 50,
    RiskLevel.ADMIN: 100,
}


class OutputMode(str, Enum):
    MARKDOWN = "markdown"
    PLAIN_TEXT = "plain_text"
    STREAM = "stream"
    JSON = "json"


class AgentIntent(str, Enum):
    GENERAL = "general"
    REPORTING_ANALYSIS = "reporting_analysis"
    CASHFLOW = "cashflow"
    BUDGETING = "budgeting"
    CLOSE_MANAGEMENT = "close_management"
    DOCUMENTS = "documents"
    MEMORY = "memory"
    BILLING = "billing"
    ADMIN = "admin"
    ASC_GUIDANCE = "asc_guidance"
    RECEIVABLES = "receivables"
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Decision 11: the AP payment-run
    # intent — bill-payment batches route here, not to the cashflow liquidity lens.
    PAYABLES = "payables"
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Decision 1 (E3-corrected): the
    # bank-reconciliation intent — reconcile / reconciliation / bank rec /
    # unmatched-transactions phrasing routes here (migrated OUT of
    # close_management), backed by its own INTENT_BUNDLES entry + specialist spec.
    RECONCILIATION = "reconciliation"
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Decision 1 (E1-corrected): the
    # amortization-schedules intent — schedule-row edits (add/update/month-values/
    # delete), get_schedule_data, dispose_schedule, and the ASC-842 lease lifecycle
    # (leases ARE amortization schedules) route here, backed by its own
    # INTENT_BUNDLES entry. This is the SH-6 reachability fix: the schedule-row
    # writes were close_management members trimmed past the max_tools=15 offer at
    # HEAD (dead from chat — /confirmations refuses requires_confirmation=False
    # tools, so the pinned tier could never rescue them).
    SCHEDULES = "schedules"
    # Legacy/intermediate labels kept for backward compatibility. Context
    # builders should map user prompts to the canonical values above before
    # tool selection.
    CONVERSATIONAL = "conversational"
    DEEP_ANALYSIS = "deep_analysis"
    ALERT_ESCALATION = "alert_escalation"


ToolHandler = Callable[..., str | DraftedToolResult]


@dataclass(frozen=True)
class SourceMetadata:
    source_type: str
    source_id: Optional[str] = None
    label: Optional[str] = None
    period: Optional[str] = None
    accounting_basis: Optional[str] = None
    sync_timestamp: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextPacket:
    run_id: Optional[int]
    conversation_id: Optional[int]
    user_id: Optional[int]
    user_email: Optional[str]
    tenant_id: Optional[str]
    company_id: str
    qbo_realm_id: str
    user_role: str
    request_text: str
    intent: str
    risk_level: RiskLevel
    output_mode: OutputMode
    # LG-4 episodic recall (Wave 3.3): model-generated summaries of past runs. Rendered
    # ONLY through the wrap_untrusted fence (non-authoritative; never authorizes a write).
    relevant_episodic_context: list[EpisodicContextBlock] = field(default_factory=list)
    # LG-5 tool-failure caveat (Wave 3.3): the realm's OPEN (unresolved, unexpired) tool
    # failures projected to code-controlled fields ONLY — rendered as an always-on,
    # allowed-tools-filtered NEVER-DROP caveat in the TRUSTED request block. It NEVER
    # deselects a tool (informs, never gates selection — AWB-06). last_error_redacted is
    # NEVER carried here (E1/E-D).
    known_tool_failures: list[ToolFailureBlock] = field(default_factory=list)
    selected_conversation_history: list[dict[str, Any]] = field(default_factory=list)
    selected_company_memory: list[dict[str, Any]] = field(default_factory=list)
    relevant_financial_context: list[dict[str, Any]] = field(default_factory=list)
    relevant_documents_context: list[dict[str, Any]] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    reporting_period: Optional[str] = None
    source_metadata: list[SourceMetadata] = field(default_factory=list)
    model_routing_metadata: dict[str, Any] = field(default_factory=dict)
    organization_id: Optional[int] = None
    organization_company_id: Optional[int] = None
    confirmation_tokens: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    namespace: str
    description: str
    input_schema: dict[str, Any]
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening C4 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 5: output_schema moved to the
    # defaulted tail of this dataclass (default None); see below send_to_model.
    handler: Optional[ToolHandler]
    risk_level: RiskLevel
    required_role: str
    mutates_state: bool
    external_side_effect: bool
    requires_confirmation: bool
    creates_proposed_action: bool
    allowed_intents: set[str]
    source_requirements: list[str]
    audit_category: str
    pii_sensitivity: str
    log_redaction_policy: str
    idempotency_key_requirement: str
    allowed_output_modes: set[OutputMode] = field(
        default_factory=lambda: {OutputMode.MARKDOWN, OutputMode.PLAIN_TEXT, OutputMode.STREAM, OutputMode.JSON}
    )
    send_to_model: bool = True
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening C4 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 5: the decorative shared
    # default output schema is dead; output_schema defaults None and is set ONLY
    # for the tools whose results are typed in tool_results.py (the tool_metadata
    # no-consumer doctrine). Lives at the defaulted tail because dataclass fields
    # with defaults must follow the non-defaulted ones; every construction site
    # passes keywords.
    output_schema: dict[str, Any] | None = None  # CRAFT-OK: reproduces the field's pre-existing JSON-schema dict[str, Any] annotation, now optional

    def to_model_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

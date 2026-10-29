"""
app/assistant/main.py
─────────────────────
FastAPI endpoints for the Peregrine AI Assistant.
Prepares canonical agent-runtime sessions and persists chat history.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import event as _sa_event

from ..db import SessionLocal, get_db
from ..rate_limit import limiter
from ..tenant_rls import TenantScope, emit_tenant_guc
from ..models import (
    AuditEntry, ChatConversation, ChatMessage, Organization, OrganizationCompany, User
)
from ..auth import get_current_user_from_token

from .tools import ASSISTANT_TOOLS
from .handlers import TOOL_HANDLERS
from ..dependencies import check_tenant_access
# PLAN-EXEMPT: 20260703-qa-batch4 AC-1 — app.agent.runtime is imported LAZILY
# (at point of use, not module load) to break a worker-order import cycle: in
# the Celery worker, app.tasks.agent:273 imports app.agent.runtime.close_orchestration
# FIRST, which runs this package's __init__ -> runtime.py:19 `from
# ...assistant.errors import ...` -> app/assistant/__init__.py:7 `from .main
# import router` -> re-enters the still-initializing app.agent.runtime package
# via this file's (former) top-level absolute import -> ImportError: cannot
# import name 'AgentRuntime' from partially initialized module. The web
# process never hit this (it imports app.assistant before app.agent.runtime
# ever starts initializing), so the bug was worker-only. TYPE_CHECKING import
# below keeps static analysis / IDEs typed; `from __future__ import
# annotations` (top of file) makes every annotation a lazily-evaluated string
# so the TYPE_CHECKING-only names are never evaluated at runtime import time.
if TYPE_CHECKING:
    from ..agent.runtime import (
        AgentRuntimeSession,
        OutputMode,
    )
from ..agent.performance import estimate_model_cost_usd
from ..agent.tracking import AgentRunTracker
from ..agent.runtime.confirmation_tokens import consume_agent_confirmation_token, issue_agent_confirmation_token
from ..services.errors import user_safe_http_exception
from ..services.llm import build_context_artifact
from ..services.tenant_companies import organization_company_for_realm, organization_for_company
from . import context as _ctx
from .errors import AssistantError
from .context import _classify_intent, count_correction_exemplars

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/assistant", tags=["Peregrine CFO Assistant"])

_TOOL_EXPLANATION_LABELS = {
    "search_finance_records": "QuickBooks search index",
    "get_financial_data": "QuickBooks financial data",
    "run_qbo_report": "QuickBooks report",
    "compare_periods": "Period comparison",
    "get_budget_data": "Budget model",
    "search_documents": "Uploaded documents",
    "search_agreements": "Material agreements",
    "get_agreement_context": "Agreement context",
    "get_module_context": "Peregrine module state",
    "generate_financial_report": "Generated report pack",
    "check_close_status": "Close checklist",
}
_PERIOD_INPUT_KEYS = (
    "period",
    "comparison",
    "start_date",
    "end_date",
    "as_of_date",
    "from_period",
    "to_period",
    "date",
)
_FORMULA_INPUT_KEYS = (
    "formula",
    "custom_formula",
    "formula_json",
    "formula_config",
    "driver_formula",
)
_FINANCIAL_TOOLS = {"search_finance_records", "get_financial_data", "run_qbo_report", "compare_periods", "get_budget_data"}
_TOOL_RESULT_COMPRESSION_THRESHOLD = 4000
_TOOL_STATUS_LABELS = {
    "search_finance_records": "Searching QuickBooks records...",
    "get_financial_data": "Pulling financial data...",
    "search_documents": "Searching documents...",
    "search_agreements": "Searching agreements...",
    "get_agreement_context": "Reading agreement context...",
    "get_module_context": "Checking module context...",
    "get_budget_data": "Checking budget data...",
    "compare_periods": "Comparing periods...",
    "run_qbo_report": "Running QBO report...",
}


def _request_id_from_request(request: Optional[Request]) -> Optional[str]:
    if not request:
        return None
    request_id = getattr(request.state, "request_id", None)
    return request_id or request.headers.get("x-request-id") or request.headers.get("x-correlation-id")


def _resolve_assistant_scope(
    db: Session,
    realm_id: str,
) -> tuple[Optional[Organization], Optional[OrganizationCompany]]:
    organization = organization_for_company(db, realm_id)
    if not organization:
        return None, None

    organization_company = organization_company_for_realm(
        db,
        organization_id=organization.id,
        realm_id=realm_id,
    )
    if not organization_company and organization.primary_realm_id == realm_id:
        organization_company = OrganizationCompany(
            organization_id=organization.id,
            realm_id=realm_id,
            is_primary=True,
        )
        db.add(organization_company)
        db.flush()
    if not organization_company:
        raise HTTPException(status_code=403, detail="Company does not belong to validated organization")
    if (organization_company.status or "active").lower() != "active":
        raise HTTPException(status_code=403, detail="Company is not active")
    return organization, organization_company


def _claim_or_reject_scoped_row(
    row: Any,
    *,
    organization: Optional[Organization],
    organization_company: Optional[OrganizationCompany],
    label: str,
) -> Any:
    if not row or not organization:
        return row
    if getattr(row, "organization_id", None) not in (None, organization.id):
        raise HTTPException(status_code=403, detail=f"{label} belongs to another organization")
    if organization_company and getattr(row, "organization_company_id", None) not in (None, organization_company.id):
        raise HTTPException(status_code=403, detail=f"{label} belongs to another entity")
    if getattr(row, "organization_id", None) is None:
        row.organization_id = organization.id
    if organization_company and getattr(row, "organization_company_id", None) is None:
        row.organization_company_id = organization_company.id
    return row


def _get_or_create_conversation(
    db: Session,
    *,
    realm_id: str,
    user: User,
    organization: Optional[Organization],
    organization_company: Optional[OrganizationCompany],
) -> ChatConversation:
    query = db.query(ChatConversation).filter(
        ChatConversation.realm_id == realm_id,
        ChatConversation.user_id == user.id,
    )
    if organization:
        conversation = query.filter(ChatConversation.organization_id == organization.id).first()
        if not conversation:
            conversation = query.filter(ChatConversation.organization_id.is_(None)).first()
            if conversation:
                _claim_or_reject_scoped_row(
                    conversation,
                    organization=organization,
                    organization_company=organization_company,
                    label="Chat conversation",
                )
    else:
        conversation = query.filter(ChatConversation.organization_id.is_(None)).first()

    if not conversation:
        conversation = ChatConversation(
            realm_id=realm_id,
            user_id=user.id,
            organization_id=organization.id if organization else None,
            organization_company_id=organization_company.id if organization_company else None,
        )
        db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _prepare_tool_result_for_task_lane(tool_name: str, result_text: str) -> str:
    if not result_text or len(result_text) <= _TOOL_RESULT_COMPRESSION_THRESHOLD:
        return result_text
    try:
        artifact = build_context_artifact(
            result_text,
            purpose=f"Compress {tool_name} output before returning it to the task lane.",
        )
        compressed = artifact.get("compressed_text") or artifact.get("summary")
        if compressed:
            logger.info(
                "[Assistant] Compressed tool result via context lane tool=%s original_chars=%s compressed_chars=%s",
                tool_name,
                len(result_text),
                len(str(compressed)),
            )
            return f"[Context lane compressed tool output]\n{compressed}"
    except Exception as exc:
        logger.warning("[Assistant] Context-lane compression failed for %s: %s", tool_name, exc)
    return result_text[:_TOOL_RESULT_COMPRESSION_THRESHOLD] + "\n... (tool output truncated after context-lane failure)"


def _clean_tool_payload(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key).startswith("_"):
                continue
            cleaned[str(key)] = _clean_tool_payload(item)
        return cleaned
    if isinstance(value, list):
        return [_clean_tool_payload(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _collect_period_values(payload: Dict[str, Any]) -> List[str]:
    periods: List[str] = []
    for key in _PERIOD_INPUT_KEYS:
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, list):
            periods.extend(str(item) for item in value if item not in (None, ""))
        else:
            periods.append(str(value))
    return periods


def _collect_formula_notes(payload: Dict[str, Any], result_text: str) -> List[str]:
    notes: List[str] = []
    for key in _FORMULA_INPUT_KEYS:
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            notes.append(value.strip())
        else:
            notes.append(json.dumps(value, sort_keys=True))
    if not notes:
        for match in re.finditer(r"(?im)^(?:formula|calculation)\s*:\s*(.+)$", result_text or ""):
            formula = match.group(1).strip()
            if formula:
                notes.append(formula)
    deduped: List[str] = []
    seen = set()
    for note in notes:
        if note not in seen:
            deduped.append(note)
            seen.add(note)
    return deduped[:3]


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A5 — claim-grain evidence (INV-CLAIM,
# SYNTHESIS GAP-C1): every material numeric claim (currency/percent) in the FINAL answer is
# deterministically cross-checked against the run's actual tool evidence; unverified claims
# add an explicit user-visible caveat, and the counts persist in the explanation payload
# (metadata_json) — the first real producer for the eval lane's numeric_claim_count signal.
def _augment_explanation_with_claims(
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R2 — two persistence lanes for the
    # counts: `tracker` (query path — the run already finished; post-finish JSON annotation) and
    # `usage_sink` (stream path — the loop's token_usage dict, persisted by the LATER
    # _finalize_stream_run finish, so annotating the run row here would be overwritten).
    explanation: dict, answer: str, tool_events: list, tracker=None, usage_sink: dict | None = None, generation_error: str | None = None  # CRAFT-OK: explanation/tool-event dicts are the established metadata_json + loop telemetry shapes; tracker duck-typed for test doubles
) -> dict:
    # Plan 20260712-agentic-finance-os W2 (GAP-2/2a): the semantic lane subsumes the
    # presence lane per claim (D-6 precedence — semantic where handler-emitted typed
    # facts exist, presence otherwise). The claims block is a SUPERSET of the presence
    # shape; evidence_confidence is observables-only, never model self-report.
    from ..services.evidence import (
        evidence_confidence,
        semantic_caveats,
        semantic_claims_block,
        verify_semantic_answer,
    )

    from ..services.evidence.semantic import verification_status

    try:
        verification = verify_semantic_answer(answer or "", tool_events or [])
    except Exception:
        explanation["verification"] = {"status": "not_checked", "scope": "numeric_claims", "provisional": False}
        explanation["critical_warnings"] = ["Numeric checks could not be completed. Verify figures before relying on them."]
        explanation["observation"] = {"status": "incomplete", "reasons": ["verification_failed"]}
        return _apply_observation(explanation, tracker=tracker, usage=usage_sink)
    status = verification_status(verification)
    if generation_error and status != "conflict":
        status = "not_checked"
    if (usage_sink or {}).get("partial") and status == "checked_numeric_claims":
        status = "limited"
    explanation["verification"] = {"status": status, "scope": "numeric_claims", "provisional": False}
    explanation["claims"] = semantic_claims_block(verification)
    explanation["evidence_confidence"] = evidence_confidence(verification)
    new_caveats = semantic_caveats(verification)
    if generation_error:
        new_caveats.append("The response was interrupted. Its numeric checks are incomplete; verify figures before relying on them.")
        explanation["observation"] = {"status": "incomplete", "reasons": ["generation_failed"]}
    if status == "limited" and not new_caveats:
        new_caveats.append("Numeric matching has limited coverage; these figures have not all been checked against typed financial evidence.")
    explanation["critical_warnings"] = new_caveats
    if new_caveats:
        caveats = list(explanation.get("caveats") or [])
        caveats.extend(new_caveats)
        explanation["caveats"] = list(dict.fromkeys(caveats))
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R2 — persist the counts onto the
    # run's token_usage JSON so the eval-run exporter feeds a REAL numeric_claim_count into
    # eval records (the scorer penalizes unverified claims; verified claims are good).
    if usage_sink is not None:
        usage_sink["numeric_claim_count"] = verification.numeric_claim_count
        usage_sink["unverified_numeric_claims"] = len(verification.unverified)
    if tracker is not None:
        from ..agent.tracking import annotate_run_usage

        annotate_run_usage(
            tracker,
            numeric_claim_count=verification.numeric_claim_count,
            unverified_numeric_claims=len(verification.unverified),
        )
    return _apply_observation(explanation, tracker=tracker, usage=usage_sink)


def _apply_observation(explanation: dict, *, tracker=None, usage=None) -> dict:
    reasons = list((usage or {}).get("observation_reasons") or [])
    reasons.extend(getattr(tracker, "degraded_reasons", []) or [])
    previous = explanation.get("observation") or {}
    reasons.extend(previous.get("reasons") or [])
    incomplete = bool(reasons or (usage or {}).get("observation_incomplete") or previous.get("status") == "incomplete")
    explanation["observation"] = {"status": "incomplete" if incomplete else "complete", "reasons": list(dict.fromkeys(reasons))}
    return explanation


def _build_message_explanation(
    tool_events: List[Dict[str, Any]],
    model_route: Any,
    token_usage: Optional[Dict[str, Any]] = None,
    *,
    final_model_name: Optional[str] = None,
    final_provider: Optional[str] = None,
) -> Dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    source_refs: List[Dict[str, Any]] = []
    periods: List[str] = []
    formula_notes: List[str] = []
    caveats: List[str] = []
    used_live_tools = False
    # Exemplar provenance (U5 closed loop): how many past human corrections were
    # ACTUALLY injected into this answer. The only injection path is the
    # get_module_context("corrections") tool result (context.py builder), so the
    # count is derived from the emitted tool result itself — a failed retrieval,
    # an empty block, or a compressed rewrite all count 0 (no false chip).
    corrections_applied = 0

    for event in tool_events:
        tool_name = str(event.get("tool") or "unknown")
        cleaned_input = _clean_tool_payload(event.get("input") or {})
        result_text = str(event.get("result") or "")
        if tool_name == "get_module_context":
            raw_input = event.get("input") or {}
            module = str(raw_input.get("module") or "") if isinstance(raw_input, dict) else ""
            if module.strip().lower().replace("-", "_") == "corrections":
                corrections_applied += count_correction_exemplars(result_text)
        period_values = _collect_period_values(cleaned_input if isinstance(cleaned_input, dict) else {})
        source_ref: Dict[str, Any] = {
            "type": "tool",
            "tool": tool_name,
            "label": _TOOL_EXPLANATION_LABELS.get(tool_name, tool_name.replace("_", " ").title()),
        }
        if period_values:
            source_ref["period"] = period_values[0]
            periods.extend(period_values)
        source_refs.append(source_ref)

        if tool_name in _FINANCIAL_TOOLS or tool_name in {"search_documents", "search_agreements", "get_agreement_context", "get_module_context"}:
            used_live_tools = True

        formula_notes.extend(_collect_formula_notes(cleaned_input if isinstance(cleaned_input, dict) else {}, result_text))

        if result_text.lower().startswith("error executing") or result_text.lower().startswith("unknown tool"):
            caveats.append(f"{source_ref['label']} returned an execution error during this answer.")

    if not source_refs:
        caveats.append("No live tools were called for this answer, so verify time-sensitive facts before acting.")
    elif not used_live_tools:
        caveats.append("This answer relied on module context more than fresh QBO pulls.")

    if not formula_notes and any((event.get("tool") in _FINANCIAL_TOOLS) for event in tool_events):
        formula_notes.append("Amounts are based on direct tool output from connected finance data; no extra custom formula was captured.")

    unique_periods = list(dict.fromkeys(periods))[:4]
    unique_formula_notes = list(dict.fromkeys(formula_notes))[:3]
    unique_caveats = list(dict.fromkeys(caveats))

    # Per-question task-lane spend. token_usage is the input/output sum the
    # runtime loop already accumulates across EVERY model call in this answer
    # (including any fast->frontier escalation or provider-fallback turns).
    usage = dict(token_usage or {})
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))

    # Prefer the final/active model the run actually settled on (restamped by
    # _mark_active_provider after escalation/fallback); fall back to the route
    # chosen before the loop ran.
    route_model = model_route.model if hasattr(model_route, "model") else None
    route_provider = model_route.provider if hasattr(model_route, "provider") else None
    model_name = final_model_name or route_model
    provider = final_provider or route_provider
    lane = model_route.lane if hasattr(model_route, "lane") else None
    escalated = bool(model_name and route_model and model_name != route_model)
    cost = estimate_model_cost_usd(model_name, usage) if model_name else None

    return {
        "verification_schema_version": 1,
        "verification": {"status": "not_checked", "scope": "numeric_claims", "provisional": True},
        "critical_warnings": [],
        "observation": {"status": "incomplete" if usage.get("observation_incomplete") else "complete", "reasons": list(usage.get("observation_reasons") or [])},
        "generated_at": generated_at,
        "source_refs": source_refs[:4],
        "periods": unique_periods,
        "formula_notes": unique_formula_notes,
        "caveats": unique_caveats,
        "data_freshness_at": None,
        "used_live_tools": used_live_tools,
        "corrections_applied": corrections_applied,
        "model_route": model_route.to_dict() if hasattr(model_route, "to_dict") else None,
        "model": {
            "name": model_name,
            "provider": provider,
            "lane": lane,
            "escalated": escalated,
        },
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": float(cost) if cost is not None else None,
        },
    }


class AssistantQuery(BaseModel):
    message: str = ""
    question: str = ""  # legacy alias
    realm_id: str
    confirmation_token: Optional[str] = None
    confirmation_tokens: List[str] = Field(default_factory=list)

    def get_message(self) -> str:
        return self.message or self.question

    def get_confirmation_tokens(self) -> List[str]:
        tokens = list(self.confirmation_tokens or [])
        if self.confirmation_token:
            tokens.append(self.confirmation_token)
        return tokens


class AssistantConfirmationRequest(BaseModel):
    realm_id: str
    tool_name: str
    # Phase 6: the concrete tool input for the action being confirmed. When
    # provided, the backend computes and stores the canonical action fingerprint
    # server-side, binding the issued token to this exact action. The legacy
    # ``action_fingerprint`` field is retained for backward compatibility but is
    # ignored whenever ``tool_input`` is supplied (the server never trusts a
    # client-provided fingerprint over one it computes itself).
    tool_input: Optional[Dict[str, Any]] = None
    action_fingerprint: Optional[str] = None


@dataclass
class _AssistantRequestPhase:
    """The request-phase result of an assistant call.

    Everything that produced this was committed on the REQUEST ``db`` under live
    request scope (the confirmation-token burn + the user ``ChatMessage``). The
    run-loop phase (``_build_runtime_session_on``) is then executed against a
    SEPARATE session the caller scopes for the streaming generator's whole life —
    so this carries only the plain values the run-loop needs to re-home onto that
    session, never a request-``db``-bound ORM row.
    """
    user_question: str
    realm_id: str
    user_role: str
    conversation_id: int
    organization_id: int | None
    organization_company_id: int | None
    tenant_id: str | None
    request_id: str | None
    confirmed_tool_names: list[str]
    confirmed_action_fingerprints: dict[str, str | None]


def _run_assistant_request_phase(
    *,
    body: AssistantQuery,
    request: Request,
    db: Session,
    current_user: User,
) -> _AssistantRequestPhase:
    """Request-phase writes — stay on the request ``db`` under live request scope.

    The confirmation-token consume and the user ``ChatMessage`` are correct today
    on the request session; moving them onto the post-teardown generator session
    would fail them closed under FORCE RLS (CRIT-EVAL AC-4 #1). This function owns
    them; the run-loop (a separate, generator-owned scoped session) is built in
    ``_build_runtime_session_on``.
    """
    realm_id = str(body.realm_id)
    user_question = body.get_message()

    access = check_tenant_access("VIEWER", company_id=realm_id, db=db, user=current_user)
    organization, organization_company = _resolve_assistant_scope(db, realm_id)
    conversation = _get_or_create_conversation(
        db,
        realm_id=realm_id,
        user=current_user,
        organization=organization,
        organization_company=organization_company,
    )

    db.add(ChatMessage(conversation_id=conversation.id, role="user", content=user_question))
    db.commit()

    confirmed_tool_names: list[str] = []
    # Phase 6: carry the fingerprint each token was bound to (None for legacy
    # tokens issued without tool_input) so the runtime can verify, at tool
    # execution time, that the action the model emits matches the action the
    # user confirmed.
    confirmed_action_fingerprints: dict[str, str | None] = {}
    for token in body.get_confirmation_tokens():
        try:
            token_row = consume_agent_confirmation_token(
                db,
                token,
                user_id=current_user.id,
                company_id=realm_id,
                tenant_id=str(organization.id) if organization else None,
                organization_id=organization.id if organization else None,
                organization_company_id=organization_company.id if organization_company else None,
                # Two-phase: burn + scope-validate here; the action-fingerprint
                # binding is enforced at tool-execution time (the runtime
                # recomputes it from the real tool input), since the concrete
                # tool arguments do not exist yet at session-prepare time.
                enforce_fingerprint=False,
            )
        except ValueError as exc:
            http_exc = user_safe_http_exception(
                status_code=400,
                error_code="assistant_confirmation_token_invalid",
                user_message="The confirmation token is invalid, expired, or not scoped to this request.",
                exc=exc,
                retryable=False,
                log_context="assistant_confirmation_token_invalid",
            )
            raise http_exc from exc
        confirmed_tool_names.append(token_row.tool_name)
        confirmed_action_fingerprints[token_row.tool_name] = token_row.action_fingerprint
        db.add(AuditEntry(
            company_id=realm_id,
            user_email=current_user.email,
            action="CONSUME_AGENT_CONFIRMATION_TOKEN",
            entity_type="agent_confirmation_token",
            entity_id=str(token_row.id),
            details={
                "tool_name": token_row.tool_name,
                "action_fingerprint": token_row.action_fingerprint,
            },
        ))
        db.commit()

    return _AssistantRequestPhase(
        user_question=user_question,
        realm_id=realm_id,
        user_role=str(access.get("role") or "VIEWER"),
        conversation_id=conversation.id,
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        tenant_id=str(organization.id) if organization else None,
        request_id=_request_id_from_request(request),
        confirmed_tool_names=confirmed_tool_names,
        confirmed_action_fingerprints=confirmed_action_fingerprints,
    )


def _persist_assistant_error_message(
    session_db: Session, phase: _AssistantRequestPhase, exc: AssistantError
) -> None:
    """Persist a guarded assistant error reply so a prep failure never orphans the user message.

    Best-effort: a failure to persist must NOT mask the original error — log + roll back and move
    on. The content is the PUBLIC ``user_message`` (never the raw exception, a token, or PII).
    """
    try:
        session_db.add(
            ChatMessage(
                conversation_id=phase.conversation_id,
                role="assistant",
                content=exc.user_message,
                metadata_json={
                    "kind": "assistant_error",
                    "error_code": exc.error_code,
                    "retryable": exc.retryable,
                },
            )
        )
        session_db.commit()
    except Exception:  # WRITEBACK-OK: best-effort error-reply persist; must never mask the original prep error
        session_db.rollback()
        logger.warning(
            "[Assistant] failed to persist prep-error reply for conversation %s",
            phase.conversation_id,
            exc_info=True,
        )


def _build_runtime_session_on(
    session_db: Session,
    *,
    phase: _AssistantRequestPhase,
    current_user: User,
    output_mode: OutputMode,
    trigger_type: str,
) -> "AgentRuntimeSession":
    """Build the agent runtime session on ``session_db`` — the run-loop's session.

    ``session_db`` is the session the caller has scoped (via ``tenant_scope_live``)
    for the run-loop's whole life. The runtime, its tracker, and its tool executor
    are all homed onto it, so EVERY transaction the loop opens (the ``AgentRun``
    insert, every ``AgentStep``, the terminal finalize) re-emits the tenant GUC and
    runs under live scope — never the post-teardown request scope that fails closed
    under FORCE RLS (the ``ObjectDeletedError`` + stuck-``running`` 429).
    """
    # Local import (not module-level): see the PLAN-EXEMPT note on the
    # TYPE_CHECKING block above — importing app.agent.runtime at module load
    # re-enters the still-initializing package when the Celery worker's FIRST
    # agent-runtime import is via app.tasks.agent's close-orchestration path.
    from ..agent.runtime import AgentRuntime, ContextPacketBuilder, RiskLevel, build_assistant_tool_registry

    _ctx.set_current_user_question(phase.user_question)
    runtime = AgentRuntime(
        db=session_db,
        registry=build_assistant_tool_registry(ASSISTANT_TOOLS, TOOL_HANDLERS),
        context_builder=ContextPacketBuilder(session_db, intent_classifier=_classify_intent),
    )
    try:
        return runtime.prepare_session(
            request_text=phase.user_question,
            company_id=phase.realm_id,
            user=current_user,
            user_role=phase.user_role,
            conversation_id=phase.conversation_id,
            trigger_type=trigger_type,
            output_mode=output_mode,
            organization_id=phase.organization_id,
            organization_company_id=phase.organization_company_id,
            tenant_id=phase.tenant_id,
            request_id=phase.request_id,
            risk_level=RiskLevel.INTERNAL_WRITE,
            confirmation_tokens=phase.confirmed_tool_names,
            confirmed_action_fingerprints=phase.confirmed_action_fingerprints,
        )
    except AssistantError as exc:
        # Typed runtime-limit condition → its OWN HTTP status (413/409/403/429), not the blanket
        # 429 below. This arm MUST precede `except ValueError` because AssistantError subclasses it.
        # Also: persist a guarded assistant error reply in the SAME conversation so the user
        # message is never orphaned (a persist failure must never mask the original error).
        _persist_assistant_error_message(session_db, phase, exc)
        mapped = user_safe_http_exception(
            status_code=exc.http_status,
            error_code=exc.error_code,
            user_message=exc.user_message,
            exc=exc,
            retryable=exc.retryable,
            log_context="assistant_prep_error",
        )
        raise mapped from exc
    except ValueError as exc:
        rate_limited = user_safe_http_exception(
            status_code=429,
            error_code="assistant_session_rate_limited",
            user_message="The assistant is temporarily rate limited. Please try again shortly.",
            exc=exc,
            retryable=True,
            log_context="assistant_session_rate_limited",
        )
        raise rate_limited from exc


@router.post("/confirmations")
def create_assistant_confirmation(
    body: AssistantConfirmationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    # Local import — see the PLAN-EXEMPT note near the TYPE_CHECKING block above.
    from ..agent.runtime import build_assistant_tool_registry

    realm_id = str(body.realm_id)
    organization, organization_company = _resolve_assistant_scope(db, realm_id)
    registry = build_assistant_tool_registry(ASSISTANT_TOOLS, TOOL_HANDLERS)
    tool = registry.get(body.tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Assistant tool not found")
    if not tool.requires_confirmation:
        raise HTTPException(status_code=400, detail="This tool does not require confirmation")
    if tool.external_side_effect:
        raise HTTPException(status_code=400, detail="External side-effect tools require ProposedAction approval")

    check_tenant_access(tool.required_role, company_id=realm_id, db=db, user=current_user)
    token, token_row = issue_agent_confirmation_token(
        db,
        user_id=current_user.id,
        company_id=realm_id,
        tenant_id=str(organization.id) if organization else None,
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        qbo_realm_id=realm_id,
        tool_name=tool.name,
        tool_input=body.tool_input,
        action_fingerprint=body.action_fingerprint,
    )
    db.commit()
    db.refresh(token_row)
    db.add(AuditEntry(
        company_id=realm_id,
        user_email=current_user.email,
        action="ISSUE_AGENT_CONFIRMATION_TOKEN",
        entity_type="agent_confirmation_token",
        entity_id=str(token_row.id),
        details={
            "tool_name": tool.name,
            "action_fingerprint": token_row.action_fingerprint,
            "expires_at": token_row.expires_at.isoformat() if token_row.expires_at else None,
        },
    ))
    db.commit()
    return {
        "confirmation_token": token,
        "tool_name": tool.name,
        "company_id": realm_id,
        "action_fingerprint": token_row.action_fingerprint,
        "expires_at": token_row.expires_at.isoformat() if token_row.expires_at else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main AI Endpoint — canonical runtime loop
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/query")
@limiter.limit("10/minute")  # expensive LLM/tool loop ($ cost) — per-user/per-tenant key (_rate_limit_key)
def ask_peregrine(
    body: AssistantQuery,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Main AI endpoint. Delegates model/tool orchestration to the canonical agent runtime.
    """
    # Local import — see the PLAN-EXEMPT note near the TYPE_CHECKING block above.
    from ..agent.runtime import AnthropicRuntimeLoop, OutputMode

    # /query is synchronous and runs entirely within the live request scope, so
    # both the request-phase writes AND the run loop execute on the request `db`.
    # (The streaming endpoint must split these — see stream_peregrine.)
    phase = _run_assistant_request_phase(
        body=body,
        request=request,
        db=db,
        current_user=current_user,
    )
    runtime_session = _build_runtime_session_on(
        db,
        phase=phase,
        current_user=current_user,
        output_mode=OutputMode.MARKDOWN,
        trigger_type="chat",
    )
    conversation_id = phase.conversation_id
    model_route = runtime_session.model_route
    result = AnthropicRuntimeLoop().run_completion(
        runtime_session,
        max_iterations=12,
        max_tokens=4096,
        result_transform=_prepare_tool_result_for_task_lane,
    )
    answer = result.answer
    actions_taken = result.tool_events
    token_usage = result.token_usage

    if runtime_session.tracker.run and runtime_session.tracker.run.status == "running":
        runtime_session.tracker.finish(
            status="failed" if result.error or not answer else "partially_completed" if token_usage.get("partial") else "succeeded",
            output_summary=answer,
            token_usage=token_usage,
            error=result.error if result.error else None if answer else "No assistant answer was generated.",
        )

    # 8) Save assistant message. Report the final/active model the run settled
    # on (the tracker is restamped on escalation/fallback), not the pre-loop route.
    explanation = _build_message_explanation(
        actions_taken,
        model_route,
        token_usage=token_usage,
        final_model_name=runtime_session.tracker.model_name,
        final_provider=runtime_session.tracker.model_provider,
    )
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A5/R2 — claim-grain evidence wiring
    explanation = _augment_explanation_with_claims(
        explanation, answer, actions_taken, tracker=runtime_session.tracker, usage_sink=token_usage, generation_error=result.error
    )
    asst_msg = ChatMessage(
        conversation_id=conversation_id,
        role="assistant",
        content=answer,
        metadata_json=explanation,
    )
    db.add(asst_msg)
    db.commit()

    return {
        "answer": answer,
        "actions": actions_taken,
        "explanation": explanation,
    }


@router.get("/history")
def get_chat_history(realm_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user_from_token)):
    # Security Check
    check_tenant_access("VIEWER", company_id=str(realm_id), db=db, user=current_user)
    organization, _organization_company = _resolve_assistant_scope(db, str(realm_id))
    
    conversation_query = db.query(ChatConversation).filter_by(realm_id=str(realm_id), user_id=current_user.id)
    if organization:
        conversation = conversation_query.filter(ChatConversation.organization_id == organization.id).first()
        if not conversation:
            conversation = conversation_query.filter(ChatConversation.organization_id.is_(None)).first()
            if conversation:
                _claim_or_reject_scoped_row(
                    conversation,
                    organization=organization,
                    organization_company=_organization_company,
                    label="Chat conversation",
                )
    else:
        conversation = conversation_query.filter(ChatConversation.organization_id.is_(None)).first()
    if not conversation:
        return []

    messages = (
        db.query(ChatMessage)
        .filter_by(conversation_id=conversation.id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return [
        {
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at,
            "metadata": m.metadata_json or None,
        }
        for m in messages
    ]


@router.post("/refresh-cache")
def refresh_assistant_cache(
    body: AssistantQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Manually trigger a cache refresh for a company's executive summary.
    Call this after initial setup or when you want fresh context.
    """
    from ..services.context_service import ContextCacheService
    realm_id = str(body.realm_id)
    check_tenant_access("ANALYST", company_id=realm_id, db=db, user=current_user)
    _resolve_assistant_scope(db, realm_id)
    service = ContextCacheService(db)
    summary = service.refresh_executive_summary(realm_id)
    return {"status": "refreshed", "realm_id": realm_id, "summary_length": len(summary)}


# ─────────────────────────────────────────────────────────────────────────────
# Streaming Endpoint (SSE)
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def _stream_run_scope(
    session_db: Session,
    *,
    org_id: int | None,
    realm_id: str | None,
) -> Iterator[None]:
    """Pin tenant scope to ``session_db`` for the streaming run-loop's whole life.

    The streaming generator is pumped by Starlette via ``iterate_in_threadpool``,
    which copies the contextvar Context per ``next()`` call — so a contextvar set
    inside the generator (the basis of ``tenant_scope_live``) is NOT visible to
    the engine-level ``begin`` listener on the next transaction, and unwinding its
    token across contexts raises ``ValueError: Token created in a different
    Context``. Instead, bind the scope to THIS session by closure via an
    ``after_begin`` listener (contextvar-independent), so EVERY transaction the
    run-loop opens on ``session_db`` re-emits the org/realm GUC under FORCE RLS —
    and emit once now for a transaction that may already be open. Removed on exit.
    """
    scope = TenantScope(org_id=org_id, realm_id=realm_id, bypass=False)

    def _after_begin(_session, _transaction, connection) -> None:
        emit_tenant_guc(connection, scope)

    _sa_event.listen(session_db, "after_begin", _after_begin)
    try:
        # Emit onto the already-open transaction (if any) immediately, so an
        # in-flight unit of work picks up the scope without waiting for its next
        # commit (mirrors tenant_scope_live's live-emit leg).
        emit_tenant_guc(session_db.connection(), scope)
        yield
    finally:
        _sa_event.remove(session_db, "after_begin", _after_begin)


def _finalize_stream_run(
    run_db: Session,
    tracker: AgentRunTracker | None,
    *,
    org_id: int | None,
    realm_id: str | None,
    status: str,
    output_summary: str | None,
    token_usage: dict[str, object] | None,
    error: str | None,
) -> None:
    """Terminally finalize the streamed AgentRun UNDER the live generator scope.

    The ordering here is load-bearing and is the single point the regression test
    (tests/pg/test_stream_run_scope_rls.py) exercises:

    - The ``tracker.run`` None/truthiness check is a pure-Python guard (no DB hit)
      and stays OUTSIDE the scope.
    - ``tracker.run.status`` is an EXPIRED ORM attribute under the default
      ``expire_on_commit=True`` (``start()`` / per-step commits expired it). Reading
      it triggers an ORM refresh SELECT on a NEW transaction. That read AND
      ``tracker.finish()`` (which refreshes ``run.id`` + queries ``AgentStep`` +
      UPDATEs the run) MUST run INSIDE the re-entered ``_stream_run_scope``: the
      OUTER scope already removed its ``after_begin`` listener, so a refresh outside
      the re-entered scope opens a no-GUC transaction → FORCE-RLS zero rows →
      ``ObjectDeletedError``, ``finish()`` never runs, the run stays ``running``, the
      next request 429s. Re-enter scope FIRST, then touch the ORM.
    """
    if tracker is None or not tracker.run:
        return
    with _stream_run_scope(run_db, org_id=org_id, realm_id=realm_id):
        if tracker.run.status == "running":
            tracker.finish(
                status=status,
                output_summary=output_summary,
                token_usage=token_usage,
                error=error,
            )


@router.post("/stream")
@limiter.limit("10/minute")  # expensive streaming LLM/tool loop ($ cost) — per-user/per-tenant key (_rate_limit_key)
def stream_peregrine(body: AssistantQuery, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user_from_token)) -> StreamingResponse:
    """
    Streaming version of /query. Returns Server-Sent Events (SSE) so the
    frontend can display text as it's generated.
    """
    # Request-phase writes (the confirmation-token burn + the user ChatMessage)
    # stay on the request `db` under live request scope — they are correct today
    # and must NOT move into the generator (CRIT-EVAL AC-4 #1).
    phase = _run_assistant_request_phase(
        body=body,
        request=request,
        db=db,
        current_user=current_user,
    )
    # Capture the run's OWN org/realm LIVE in the handler (while request scope is
    # alive) so the generator can re-pin them. Never `bypass` for the run body
    # (AC-4): the run is scoped to exactly its own tenant.
    run_org_id = phase.organization_id
    run_realm_id = phase.realm_id
    conversation_id = phase.conversation_id

    def event_stream() -> Iterator[str]:
        # The generator is iterated by Starlette AFTER the request handler returns
        # and `get_db` has torn down (clear_tenant_scope). It therefore CANNOT rely
        # on the request session/scope — under FORCE RLS an org-keyed `agent_runs`
        # read/write with no GUC hits zero rows (the ObjectDeletedError + the
        # stuck-`running` 429). So the generator owns its OWN session, scoped to the
        # run's tenant for its WHOLE life, and finalizes the run UNDER that live
        # scope on any exit.
        # Local import — see the PLAN-EXEMPT note near the TYPE_CHECKING block above.
        from ..agent.runtime import AgentStreamState, AnthropicRuntimeLoop, OutputMode

        state = AgentStreamState()
        run_db = SessionLocal()  # SA-OK: closed-elsewhere try/finally run_db.close() below guarantees close
        runtime_session = None
        try:
            with _stream_run_scope(run_db, org_id=run_org_id, realm_id=run_realm_id):
                # Build the runtime/tracker/executor on the generator-owned, scoped
                # session — every transaction the loop opens re-emits the GUC.
                try:
                    runtime_session = _build_runtime_session_on(
                        run_db,
                        phase=phase,
                        current_user=current_user,
                        output_mode=OutputMode.STREAM,
                        trigger_type="chat_stream",
                    )
                except HTTPException as http_exc:
                    # Prep failed: the typed AssistantError was mapped to its HTTP status AND the
                    # orphan-fix error reply was already persisted inside _build_runtime_session_on.
                    # A stream can't change its HTTP status mid-flight, so surface the SAME
                    # error_code/user_message the non-streaming path returns, as an SSE `error`
                    # frame. No AgentRun was created → the finally-finalize no-ops.
                    detail = http_exc.detail if isinstance(http_exc.detail, dict) else {}
                    error_frame = {
                        "type": "error",
                        "error_code": detail.get("error_code", "assistant_error"),
                        "user_message": detail.get(
                            "user_message", "The assistant could not start this request."
                        ),
                        "retryable": bool(detail.get("retryable", False)),
                    }
                    yield f"data: {json.dumps(error_frame)}\n\n"
                    yield "data: {\"type\": \"done\"}\n\n"
                    return
                model_route = runtime_session.model_route
                for event in AnthropicRuntimeLoop().iter_stream(
                    runtime_session,
                    state=state,
                    max_iterations=8,
                    max_tokens=2048,
                    result_transform=_prepare_tool_result_for_task_lane,
                    status_label_for_tool=lambda tool_name: _TOOL_STATUS_LABELS.get(tool_name, "Working on it..."),
                ):
                    yield f"data: {{\"type\": {json.dumps(event.type)}, \"content\": {json.dumps(event.content)}}}\n\n"

                # Save assistant message on the generator-owned scoped session
                # (writing on the torn-down request `db` would fail closed under RLS).
                try:
                    if state.full_answer:
                        _finalize_stream_run(
                            run_db, runtime_session.tracker, org_id=run_org_id, realm_id=run_realm_id,
                            status="failed" if state.error else "partially_completed" if state.token_usage.get("partial") else "succeeded",
                            output_summary=state.full_answer, token_usage=state.token_usage, error=state.error,
                        )
                        explanation = _build_message_explanation(
                            state.tool_events,
                            model_route,
                            token_usage=state.token_usage,
                            final_model_name=runtime_session.tracker.model_name,
                            final_provider=runtime_session.tracker.model_provider,
                        )
                        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A5/R2 — claim wiring.
                        # Stream lane: counts go into state.token_usage (the finally-finalize
                        # persists it); the request-db tracker is torn down here under RLS.
                        explanation = _augment_explanation_with_claims(
                            explanation, state.full_answer, state.tool_events,
                            usage_sink=state.token_usage, tracker=runtime_session.tracker, generation_error=state.error,
                        )
                        asst_msg = ChatMessage(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=state.full_answer,
                            metadata_json=explanation,
                        )
                        run_db.add(asst_msg)
                        run_db.commit()
                        yield f"data: {{\"type\": \"explanation\", \"content\": {json.dumps(explanation)}}}\n\n"
                except Exception:
                    run_db.rollback()
                    logger.warning("[AssistantStream] Failed to persist assistant stream response", exc_info=True)
                    failed_evidence = {"verification_schema_version": 1, "verification": {"status": "not_checked", "scope": "numeric_claims", "provisional": False}, "critical_warnings": ["The answer evidence could not be saved. Verify figures before relying on them."], "observation": {"status": "incomplete", "reasons": ["response_persistence_failed"]}, "data_freshness_at": None}
                    yield f"data: {json.dumps({"type": "explanation", "content": failed_evidence})}\n\n"
        finally:
            # Guaranteed terminal finalize on ANY exit (success, model error, or a
            # client disconnect that closes the generator) — UNDER the live scope
            # so the UPDATE matches its own row instead of an RLS-blind zero-row
            # no-op that strands the run `running` and 429s the next request.
            try:
                # The scope-then-read ordering is load-bearing and lives in the one
                # testable helper (_finalize_stream_run): re-enter the live scope
                # BEFORE any expired-ORM-attribute read, so the refresh + finish()
                # UPDATE carry the GUC under FORCE RLS instead of fail-closing to a
                # zero-row no-op (ObjectDeletedError → run stranded `running` → 429).
                tracker = runtime_session.tracker if runtime_session is not None else None
                _finalize_stream_run(
                    run_db,
                    tracker,
                    org_id=run_org_id,
                    realm_id=run_realm_id,
                    status="failed" if state.error or not state.full_answer else "succeeded",
                    output_summary=state.full_answer,
                    token_usage=state.token_usage,
                    error=state.error if state.error else None if state.full_answer else "No assistant answer was generated.",
                )
            finally:
                run_db.close()

        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

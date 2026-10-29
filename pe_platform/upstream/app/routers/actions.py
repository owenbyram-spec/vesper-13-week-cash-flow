import copy
import logging
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Body, Request  # noqa: F401
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator
from decimal import Decimal  # noqa: F401

logger = logging.getLogger(__name__)

from ..db import get_db  # noqa: E402
# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C2 (AC-4 RK-3a) covers app/routers/actions.py serializer; active-plan.json state absent, RATIFICATION-2026-07-13.md on disk
from ..models import AuditEntry, ProposedAction, QBOAccount, QBOActionExecution, User  # noqa: E402
from ..qbo_actions.risk_annotation import JE_ACTION_TYPES, annotate_je_lines  # noqa: E402
from ..dependencies import check_entity_tenant_access, check_tenant_access, get_current_user_from_token  # noqa: E402
from ..qbo_actions.executor import execute_proposed_action, is_executable_action_type  # noqa: E402
from ..qbo_actions.reclassifications import (  # noqa: E402
    build_reclassification_payload,
    normalize_reclassification_payload,
)
from ..qbo_actions.refusals import QBOActionRefused  # noqa: E402
from ..qbo_actions.je_lines import resolve_je_lines  # noqa: E402
# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — expiry predicate import (INV-EXPIRE)
from ..qbo_actions.transitions import action_is_expired, claim_action_transition  # noqa: E402
from ..qbo_actions.proposer import (  # noqa: E402
    propose_je_reversal,
    propose_journal_entry,
    propose_reclassification,
    reversal_block_reason,
)
from ..services.errors import safe_error_detail, user_safe_http_exception  # noqa: E402
from ..services.redaction import redact_sensitive, redact_to_string  # noqa: E402
from ..services.diff import compute_diff  # noqa: E402
from ..services.schedule_engine import run_post_execution_schedule_hook  # noqa: E402
from ..services.tenant_companies import company_is_suspended, organization_company_for_realm, organization_for_company  # noqa: E402
from ..services.correction_capture import record_correction  # noqa: E402
from ..services.proposal_provenance import (edit_proposal_payload, validate_approval, validate_autonomous_approval)  # noqa: E402
from ..services.task_autonomy import gate_decision  # noqa: E402
# PLAN-EXEMPT: B5-1 P0 field bug (live-prod-corroborated 2026-06-24) — approved cashflow
# categorization silently no-ops; fix wires internal-DB action execution on approval.
from ..tenant_rls import tenant_scope_for_realm  # noqa: E402
# PLAN-EXEMPT: 2026-07-06 stuck-approved remediation (AC-1) — the execution body now
# lives in the shared core so the remediation script and this endpoint cannot drift.
from ..services.internal_action_execution import (  # noqa: E402
    INTERNAL_DB_ACTION_TYPES,
    InternalActionNotWiredError,
    execute_internal_db_action_core,
    internal_execution_summary, InternalExecutionOutcome, REVIEW_MESSAGE,
)
# PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md) — decision-8 approve-lane plan-status consult.
from ..services.agent_plans import plan_status_blocks_execution  # noqa: E402
# Plan 20260716-five-domain-remediation AC-B3/B4: the four-tier SideEffectTier gate —
# the tier facet on /inbox + /stats, the FINDING exclusion from approval counts, and
# the tier-2 (INTERNAL_MATERIAL) fingerprint-bound token approval channel below.
from ..services.action_tiers import SideEffectTier, tier_for_action_type  # noqa: E402
from ..rate_limit import limiter  # noqa: E402

router = APIRouter(prefix="/actions", tags=["Actions"])

# Action types whose approval must run an INTERNAL-DB handler (not the QBO
# executor). These are forecast/close-side writes the assistant drafts via
# `_propose_tool_action` (tool_executor.py): they are NOT in
# EXECUTABLE_ACTION_TYPES (which is QBO-only). The set is the service module's
# frozenset — ONE source of truth (plan 20260706-widen-internal-db-approval-execution
# AC-1); B5-1 (2026-06-24) wired the first type after an approved cashflow
# categorization silently no-op'd, and the 2026-07-06 widening covers the full
# 13-type class so approval always executes instead of the review-only no-op.
INTERNAL_DB_EXECUTABLE_ACTION_TYPES = INTERNAL_DB_ACTION_TYPES

def _human_proposer_user_id(action: ProposedAction) -> int | None:
    """Retained compatibility helper; authority is the server-owned column."""
    return action.human_preparer_user_id


_OPTIONAL_JE_LINE_KEYS = ("customer_name", "entity_type", "entity_id")


def _resolved_je_lines_or_422(
    db: Session,
    realm_id: str,
    lines: list[dict],
    *,
    require_synced_chart: bool,
    strict_names: bool = True,
    carried_names: tuple[str, ...] = (),
) -> list[dict]:
    """Chart + entity check for simplified JE lines (``je_lines.resolve_je_lines``):
    returns the lines with resolved Customer/Vendor ids, or raises a teaching 422.

    qbo_accounts is org-keyed under live RLS and some JE routes carry no
    company_id to auto-pin the GUCs, so the lookups run inside
    tenant_scope_for_realm. Unset optional Name keys are dropped so a line with
    no entity keeps its historical stored shape."""
    with tenant_scope_for_realm(db, realm_id):
        try:
            resolved = resolve_je_lines(
                db, realm_id, lines, require_synced_chart=require_synced_chart,
                strict_names=strict_names, carried_names=carried_names,
            )
        except QBOActionRefused as exc:
            raise user_safe_http_exception(
                status_code=422, error_code=exc.error_code, user_message=exc.user_message,
                log_context=f"je_lines_refused:{exc.error_code}",
            ) from exc
    return [
        {key: value for key, value in line.items() if not (key in _OPTIONAL_JE_LINE_KEYS and value in (None, ""))}
        for line in resolved
    ]


def _validate_review(db: Session, action: ProposedAction, user_id: int | None, revision: int | None, *, reviewer: str, lane: str) -> None:
    try:
        validate_approval(action, approver_user_id=user_id, expected_revision=revision)
        # A journal entry is approvable only if every line names an account
        # and, once the realm's chart is synced, an active account of this realm,
        # with every A/R / A/P line carrying its Customer / Vendor: refused here,
        # the draft stays PENDING and editable instead of failing at QuickBooks.
        if action.action_type in ("JOURNAL_ENTRY", "POST_JE") and isinstance(action.payload, dict) and "Line" not in action.payload:
            _resolved_je_lines_or_422(
                db, action.company_id, action.payload.get("lines") or [],
                require_synced_chart=False, strict_names=False,
            )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        _audit_action_review(db, action=action, user_email=reviewer,
                             audit_action="APPROVE_ACTION_FAILED",
                             details={"source": lane, "reason": detail.get("error_code")})
        db.commit()
        raise


# Map a ProposedAction.action_type to an AICorrectionEvent task_type. Action
# types with no AICorrectionEvent task_type (e.g. RECONCILE, UPDATE_VENDOR) are
# intentionally not captured this phase.
#
# Phase D (MATERIAL): the close-and-review action types — REVIEW_CHECK (nightly
# analytical-review proposer, app/tasks/proactive.py:347) and CLOSE_STEP_REVIEW
# (close-orchestration driver, app/agent/runtime/close_orchestration.py:498) —
# map to the close_review task_type so a human accept/dismiss/edit of a
# close-review finding feeds the #16 correction loop. Until this phase they
# no-op'd (the cockpit's headline content was outside the allowlist), which made
# the loop-feed silently false. This WIDENS the captured set only; the existing
# JE/reclassify capture behavior is unchanged.
_TASK_TYPE_BY_ACTION = {
    "RECLASSIFY_TXN": "reclassification",
    "JOURNAL_ENTRY": "journal_entry",
    "POST_JE": "journal_entry",
    "REVIEW_CHECK": "close_review",
    "CLOSE_STEP_REVIEW": "close_review",
}


def _capture_action_correction(
    db: Session,
    *,
    action: ProposedAction,
    human_decision: str,
    human_output: Optional[Dict[str, Any]],
    actor_user_id: Optional[int],
    ai_output: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort capture of a human decision on a ProposedAction. No-op for
    action types that have no AICorrectionEvent task_type."""
    task_type = _TASK_TYPE_BY_ACTION.get(action.action_type)
    if task_type is None:
        return
    record_correction(
        task_type=task_type,
        human_decision=human_decision,
        company_id=action.company_id,
        source_object_ref=str(action.id),
        ai_output=ai_output if ai_output is not None else {
            "action_type": action.action_type,
            "payload": action.payload,
            "reasoning": action.reasoning,
        },
        ai_confidence=action.confidence_score,
        human_output=human_output,
        actor_user_id=actor_user_id,
        db=db,
    )


def _audit_action_review(
    db: Session,
    *,
    action: ProposedAction,
    user_email: str,
    audit_action: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    db.add(AuditEntry(
        company_id=action.company_id,
        user_email=user_email,
        action=audit_action,
        entity_type="proposed_action",
        entity_id=str(action.id),
        details={
            "status": action.status,
            "action_type": action.action_type,
            **(details or {}),
        },
    ))


def _execution_error_category(action: ProposedAction) -> Dict[str, Any]:
    """Small, non-sensitive error CATEGORY for failure audits. Pulls only the
    error_code / execution_status / retryable flag from the execution result —
    never raw payloads or provider messages."""
    result = action.execution_result if isinstance(action.execution_result, dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    return {
        "error_code": error.get("error_code") or "qbo_action_execution_failed",
        "execution_status": result.get("execution_status"),
        "retryable": error.get("retryable"),
    }


def _payload_first(payload: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _review_contract(action: ProposedAction) -> Dict[str, Any]:
    payload = action.payload if isinstance(action.payload, dict) else {}
    before_state = (
        payload.get("before_state")
        or payload.get("before")
        or payload.get("original_payload")
        or payload.get("from")
    )
    after_state = (
        payload.get("after_state")
        or payload.get("after")
        or payload.get("updated_payload")
        or payload.get("updates")
        or payload
    )
    affected_entity = (
        payload.get("affected_entity")
        or payload.get("entity")
        or payload.get("account_name")
        or payload.get("vendor_name")
        or payload.get("txn_id")
        or action.action_type
    )
    source_data = (
        payload.get("source_data")
        or payload.get("source_metadata")
        or payload.get("source")
        or {"source": action.source or "unknown"}
    )
    execution_result = action.execution_result if isinstance(action.execution_result, dict) else {}
    rejection_reason = None
    if action.status == "REJECTED":
        rejection_reason = (
            execution_result.get("rejection_reason")
            or execution_result.get("reviewer_comment")
            or execution_result.get("comment")
        )
    return {
        "summary": payload.get("summary") or f"Proposed {action.action_type}",
        "originating_module": _payload_first(
            payload,
            "originating_module",
            "module",
            "module_id",
            "tool_name",
        ) or action.category or action.source or "unknown",
        "originating_agent_run_id": _payload_first(payload, "originating_agent_run_id", "agent_run_id", "run_id"),
        "originating_agent_step_id": _payload_first(payload, "originating_agent_step_id", "agent_step_id", "step_id"),
        "requester": _payload_first(payload, "requester", "requested_by", "requester_email", "user_email"),
        "affected_entity": affected_entity,
        "before_state": before_state,
        "after_state": after_state,
        "reason": action.reasoning,
        "source_data": redact_sensitive(source_data),
        "risk_level": payload.get("risk_level") or ("high" if is_executable_action_type(action.action_type) else "medium"),
        # Side-effect tier facet (plan 20260716-five-domain-remediation AC-B4):
        # BEHAVIOR-derived from the frozen tier map (never the payload — a stored
        # copy could be stale), additive and always present with a safe default
        # (unknown types read as external_financial, the most approval-gated tier).
        "tier": tier_for_action_type(action.action_type).value,
        "required_role": payload.get("required_role") or "EDITOR",
        # Behavior-derived first (AC-4, plan 20260706-widen-internal-db-approval-execution):
        # internal-DB types EXECUTE on approval as of 2026-07-06, so the "Mode" pill must say
        # so even for pre-widening rows whose payload still carries the stale "review_only".
        "execution_mode": (
            "executable" if is_executable_action_type(action.action_type)
            else "internal_db" if action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES
            else payload.get("execution_mode") or "review_only"
        ),
        "approval_effect": payload.get("approval_effect"),
        "expires_at": action.expires_at.isoformat() if action.expires_at else None,
        "approval_status": action.status,
        "status": action.status,
        "rejection_reason": rejection_reason,
        "confirmation_fingerprint": _payload_first(
            payload,
            "confirmation_fingerprint",
            "action_fingerprint",
            "fingerprint",
        ),
        "execution_status": execution_result.get("execution_status") or execution_result.get("status"),
        # Field-level before→after diff (C2). Redacted like source_data; the FE renders
        # it field-by-field and falls back to the raw before/after blocks when empty.
        "diff": redact_sensitive(compute_diff(before_state, after_state)),
    }


def _execution_status(db: Session, action: ProposedAction) -> Dict[str, Any] | None:
    execution = (
        db.query(QBOActionExecution)
        # action_id is an indexed FK, not unique - no ceiling on its own. `action` was
        # already authorized for exactly this realm by the caller, so bind it here too.
        .filter(
            QBOActionExecution.action_id == action.id,
            QBOActionExecution.company_id == action.company_id,
        )
        .order_by(QBOActionExecution.id.desc())
        .first()
    )
    if not execution:
        return None
    return {
        "id": execution.id,
        "status": execution.status,
        "attempts": execution.attempts,
        "provider_request_id": execution.provider_request_id,
        "before_state": execution.before_state_json,
        "after_state": execution.after_state_json,
        "request_summary": execution.request_summary_json,
        "response_summary": execution.response_summary_json,
        "error": execution.error,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        "updated_at": execution.updated_at.isoformat() if execution.updated_at else None,
    }


def _audit_history(db: Session, action: ProposedAction) -> list[Dict[str, Any]]:
    rows = (
        db.query(AuditEntry)
        .filter(
            AuditEntry.company_id == action.company_id,
            AuditEntry.entity_type == "proposed_action",
            AuditEntry.entity_id == str(action.id),
        )
        .order_by(AuditEntry.created_at.asc(), AuditEntry.id.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "action": row.action,
            "user_email": row.user_email,
            "details": redact_sensitive(row.details or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def _company_eligibility(db: Session, action: ProposedAction) -> Dict[str, Any]:
    organization = organization_for_company(db, action.company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=action.company_id)
        if organization
        else None
    )
    tenant_suspended = bool(organization and not organization.is_active)
    company_suspended = company_is_suspended(db, organization_company=organization_company)
    reason = None
    if tenant_suspended:
        reason = "tenant_suspended"
    elif company_suspended:
        reason = "company_suspended"
    return {
        "eligible": not tenant_suspended and not company_suspended,
        "reason": reason,
        "organization_id": organization.id if organization else None,
        "organization_company_id": organization_company.id if organization_company else None,
        "company_status": organization_company.status if organization_company else None,
    }


class _JEAccountInfo(NamedTuple):
    """One ``qbo_accounts`` row's serve-time facets for a PENDING JE line:
    ``account_type`` feeds the RK-3a risk annotation; ``account_name``
    (``fully_qualified_name or name``) + ``active`` feed the batch-5 AC-J1
    render-time name enrichment (an inactive/unknown id is never given a name —
    the FE falls back to the id)."""

    account_type: str
    account_name: str
    active: bool


# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C2 (AC-4 RK-3a) — serve-time risk annotation on PENDING JE proposals; active-plan.json state absent, RATIFICATION-2026-07-13.md on disk
def _prefetch_je_account_types(
    db: Session, actions: list[ProposedAction]
) -> dict[tuple[str, str], _JEAccountInfo]:
    """ONE batched ``qbo_accounts`` lookup (account_type + display name +
    active) for a response's PENDING JE lines (plan decision 3: a single
    IN-query per response, never per-row; batch-5 AC-J1 extended the SELECT to
    carry names for render-time enrichment — same single query).

    List endpoints call this ONCE over the whole page and pass the result down
    to every ``_action_response``; the single-action path self-fetches at the
    top of ``_action_response`` (still exactly one query). Keyed
    ``(realm_id, qbo_id)`` so a page can safely span realms; returns ``{}``
    (no query) when no PENDING JE line carries an account_id.
    """
    keys: set[tuple[str, str]] = set()
    for action in actions:
        if action.status != "PENDING" or action.action_type not in JE_ACTION_TYPES:
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
        for line in lines:
            if not isinstance(line, dict):
                continue
            account_id = str(line.get("account_id") or "").strip()  # SA-OK: in-memory dict read over the payload lines, not a DB query
            if account_id:
                keys.add((action.company_id, account_id))
    if not keys:
        return {}
    # One query per realm with a top-level realm EQUALITY, rather than one query with
    # `realm_id.in_(...)`. An IN is not a ceiling - it returns N caller-chosen realms -
    # so it cannot bound a read of an org-keyed guard table. In practice a page carries a
    # single realm, so this stays one query; the loop only widens if a caller ever mixes.
    accounts_by_realm: dict[str, set[str]] = {}
    for realm_id, account_id in keys:
        accounts_by_realm.setdefault(realm_id, set()).add(account_id)
    rows = []
    for realm_id, account_ids in sorted(accounts_by_realm.items()):
        rows.extend(
            db.query(
                QBOAccount.realm_id,
                QBOAccount.qbo_id,
                QBOAccount.account_type,
                QBOAccount.name,
                QBOAccount.fully_qualified_name,
                QBOAccount.active,
            )
            .filter(
                QBOAccount.realm_id == realm_id,
                QBOAccount.qbo_id.in_(sorted(account_ids)),
            )
            .all()
        )
    return {
        (realm_id, qbo_id): _JEAccountInfo(
            account_type=account_type or "",
            account_name=fully_qualified_name or name or "",
            active=bool(active),
        )
        for realm_id, qbo_id, account_type, name, fully_qualified_name, active in rows
    }


# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Decision 6: the provenance-based
# annotation upgrade. The notice RETAINS the CPA gate — it NEVER lifts (the
# methodology is drafted-for-CPA-review); only the "no computational support"
# claim is replaced, because for a stamped proposal a deterministic engine DID
# compute the numbers.
ENGINE_BACKED_NOTICE = (
    "engine-backed estimate — computation evidence attached; CPA review still required"
)


def _attach_risk_annotation(
    db: Session,
    action: ProposedAction,
    response: dict[str, object],
    je_account_types: dict[tuple[str, str], _JEAccountInfo] | None,
) -> None:
    """Attach the RK-3a ``risk_annotation`` to a PENDING JE-type response.

    Serve-time only (decision 3) — nothing is persisted and no draft path is
    touched. Lines that resolve neither by account_type nor by name keyword are
    logged as ``unmapped_accounts`` — the honest branch, never a guessed topic.

    Provenance upgrade (plan 20260713-wave2-5-cecl-allowance decision 6): a
    PENDING JE proposal whose payload carries the BACKEND-stamped
    ``computation_evidence`` key gets ``engine_backed: true`` + an evidence
    pointer + the RETAINED "CPA review still required" notice. Key-presence IS
    provenance: every JE draft lane allowlist-constructs its payload (the chat
    handler's resolved_params, the router's JELine schema), so a model-supplied
    imitation of the key can never reach a stored payload — only the extended
    ``propose_journal_entry(computation_evidence=...)`` backend kwarg can stamp
    it. Stampless 326 JEs keep the full engine-less annotation unchanged; ASC
    326 STAYS in ENGINELESS_HIGH_RISK_TOPICS (topics never move registers).
    """
    if action.status != "PENDING" or action.action_type not in JE_ACTION_TYPES:
        return
    if je_account_types is None:
        je_account_types = _prefetch_je_account_types(db, [action])
    payload = action.payload if isinstance(action.payload, dict) else {}
    lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
    account_types_by_id = {
        qbo_id: info.account_type
        for (realm_id, qbo_id), info in je_account_types.items()
        if realm_id == action.company_id
    }
    annotation, unmapped = annotate_je_lines(lines, account_types_by_id)
    # PLAN-EXEMPT: plan 20260713-wave2-5-cecl-allowance chunk B decision 6 — the
    # provenance branch (see the docstring). ``annotate_je_lines`` stays PURE and
    # always emits engine_backed: False; the upgrade lives HERE, in the serializer.
    evidence = payload.get("computation_evidence")
    if isinstance(evidence, dict) and evidence:
        response["risk_annotation"] = {
            "engine_backed": True,
            # Topic grounding when the lines resolved one; the CECL proposer lane
            # is the ONLY backend stamper today, so ASC 326 is the honest default
            # for engine-drafted lines (id-only lines carry no name to keyword-map).
            "topics": annotation["topics"] if annotation is not None else ["ASC 326"],
            "notice": ENGINE_BACKED_NOTICE,
            "evidence_key": "computation_evidence",
            "method": str(evidence.get("method") or ""),
        }
    elif annotation is not None:
        response["risk_annotation"] = annotation
    if unmapped:
        logger.info(
            "risk_annotation: action %s has %d unmapped JE account(s): %s",
            action.id,
            len(unmapped),
            unmapped[:20],
        )


def _enrich_je_line_account_names(
    action: ProposedAction,
    response: dict[str, object],
    payload_key: str,
    je_account_types: dict[tuple[str, str], _JEAccountInfo],
) -> None:
    """Fill a falsy ``account_name`` on the SERVED JE lines from the batched
    account lookup — render-time enrichment only (batch-5 AC-J1, crit-eval
    finding 10).

    ``action.payload`` is a JSON column: an in-place fill would either persist
    render enrichment into the stored proposal on a later flush or be silently
    lost, so the fill happens on a DEEP COPY of the served lines and is written
    back ONLY into ``response`` (the ``_attach_risk_annotation`` response-only
    precedent). An unknown or inactive account id gets NO name — the frontend
    falls back to rendering the id; the serializer never fabricates a name.
    """
    if action.status != "PENDING" or action.action_type not in JE_ACTION_TYPES:
        return
    served = response.get(payload_key)
    if not isinstance(served, dict):
        return
    lines = served.get("lines")
    if not isinstance(lines, list):
        return
    enriched = copy.deepcopy(lines)
    changed = False
    for line in enriched:
        if not isinstance(line, dict) or line.get("account_name"):  # SA-OK: in-memory dict read over the deep-copied payload lines, not a DB query
            continue
        account_id = str(line.get("account_id") or "").strip()  # SA-OK: in-memory dict read over the deep-copied payload lines, not a DB query
        if not account_id:
            continue
        info = je_account_types.get((action.company_id, account_id))  # SA-OK: in-memory dict-index into the ONE batched prefetch result, not a DB query
        if info is not None and info.active and info.account_name:
            line["account_name"] = info.account_name
            changed = True
    if changed:
        served["lines"] = enriched


def _action_response(
    db: Session,
    action: ProposedAction,
    *,
    include_payload: bool = True,
    je_account_types: dict[tuple[str, str], _JEAccountInfo] | None = None,
) -> dict[str, object]:
    payload_key = "payload" if include_payload else "payload_preview"
    # Single-action serving path: self-fetch ONCE here so both the risk
    # annotation and the AC-J1 name enrichment share the same batched lookup
    # (no query at all when the action carries no PENDING JE line).
    if je_account_types is None:
        je_account_types = _prefetch_je_account_types(db, [action])
    response = {
        "id": action.id,
        "revision": action.revision,
        "human_preparer_user_id": action.human_preparer_user_id,
        "last_human_editor_user_id": action.last_human_editor_user_id,
        "company_id": action.company_id,
        "action_type": action.action_type,
        "summary": f"Proposed {action.action_type}",
        "reasoning": action.reasoning,
        "confidence_score": float(action.confidence_score) if action.confidence_score else 0.0,
        "status": action.status,
        payload_key: redact_sensitive(action.payload),
        "execution_result": redact_sensitive(action.execution_result),
        "internal_execution": internal_execution_summary(db, action),
        "recovery": _recovery_read_summary(db, action),
        "reviewed_by": action.reviewed_by,
        "reviewed_at": action.reviewed_at.isoformat() if action.reviewed_at else None,
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "review": _review_contract(action),
        "execution": _execution_status(db, action),
        "audit_history": _audit_history(db, action),
        "eligibility": _company_eligibility(db, action),
    }
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C2 (AC-4 RK-3a) — serve-time annotation attach
    _attach_risk_annotation(db, action, response, je_account_types)
    # Batch-5 AC-J1: fill falsy account_name on the served (deep-copied) JE
    # lines; the stored JSON payload stays byte-identical.
    _enrich_je_line_account_names(action, response, payload_key, je_account_types)
    return response


def _mark_review_only_approved(action: ProposedAction) -> ProposedAction:
    payload = action.payload if isinstance(action.payload, dict) else {}
    action.execution_result = {
        "execution_mode": payload.get("execution_mode") or "review_only",
        "approved": True,
        "message": "This proposal was approved for manual follow-up. Vesper Bot did not execute a financial write automatically.",
    }
    return action


# PLAN-EXEMPT: B5-1 P0 field bug (live-prod-corroborated 2026-06-24); execution body
# extracted to services/internal_action_execution.py (2026-07-06) so the stuck-approved
# remediation script shares the SAME core — this wrapper keeps the HTTP envelope.
def _execute_internal_db_action(db: Session, action: ProposedAction, current_user: User):
    """Execute an approved INTERNAL_DB_EXECUTABLE_ACTION_TYPES action.

    Unlike QBO writes (execute_proposed_action), these persist to Vesper Bot's own
    DB via an assistant handler. The shared core (execute_internal_db_action_core)
    pins org+realm scope for action.company_id so the realm-keyed CashForecastActual
    read/write — and the handler's post-commit reads — pass FORCE-RLS, enforces the
    two shared gates unified with the QBO executor (a suspended company OR a
    hard-locked target period REFUSES: success=False + honest message + NO mutation,
    matching qbo_actions/executor.py — the eligibility that _company_eligibility
    computes for DISPLAY at actions.py:270-291 is now ENFORCED here), then runs the
    handler and records the outcome HONESTLY: a handler "Error: ..." result or an
    exception surfaces success=False, never the old silent approved-but-unchanged
    no-op. This wrapper owns only the HTTP envelope + exception audit; both the
    single (approve_action) and bulk (bulk_approve) lanes funnel through it, so both
    inherit the gates.
    """
    try:
        outcome = execute_internal_db_action_core(db, action, actor_email=current_user.email)
        succeeded, message = outcome
        result = outcome.result() if isinstance(outcome, InternalExecutionOutcome) else action.execution_result
        return {"success": succeeded, "status": action.status, "result": result, "execution": None}
    except InternalActionNotWiredError as wiring_error:
        raise user_safe_http_exception(status_code=500, error_code="internal_action_not_wired",
            user_message="This action can't be executed right now.",
            log_context=f"internal_db_action_unwired:{action.action_type}") from wiring_error
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        # An exception does not prove rollback of earlier handler commits. The core
        # resolves owned commit ambiguity; wrappers never overwrite durable history.
        raise user_safe_http_exception(status_code=500, error_code="internal_db_action_failed",
            user_message=REVIEW_MESSAGE, exc=exc,
            log_context="internal_db_action_failed") from exc


# ── Pydantic Request Models ───────────────────────────────────────────────────

class JELine(BaseModel):
    account_id: str = Field(..., min_length=1)
    account_name: str = ""
    # The line's Name. A name is resolved to a synced vendor/customer id when the
    # entry is drafted (an unknown name is refused); an A/R line needs a customer
    # and an A/P line a vendor (QuickBooks rejects the post otherwise).
    vendor_name: str = ""
    customer_name: str = ""
    entity_type: Optional[Literal["Customer", "Vendor"]] = None
    entity_id: Optional[str] = None
    debit: float = Field(0.0, ge=0)
    credit: float = Field(0.0, ge=0)
    description: str = ""

    @field_validator("debit", "credit", mode="before")
    @classmethod
    def coerce_none_to_zero(cls, v):
        return v or 0.0


class JournalEntryRequest(BaseModel):
    company_id: str = Field(..., min_length=1)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    memo: str = ""
    lines: List[JELine] = Field(..., min_length=1)


class ReclassifyRequest(BaseModel):
    company_id: str = Field(..., min_length=1)
    txn_id: str = Field(..., min_length=1)
    entity: str = "Purchase"
    from_account_id: Optional[str] = None
    from_account_name: Optional[str] = None
    to_account_id: str = Field(..., min_length=1)
    to_account_name: Optional[str] = None
    # The transaction line whose category changes; required at execution when
    # the transaction has more than one line.
    line_id: Optional[str] = None
    reason: str = "Reclassification"


# ─── Proposal Endpoints ────────────────────────────────────────────────────────

@router.post("/propose/journal-entry")
def propose_journal_entry_action(
    body: JournalEntryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Creates a PENDING ProposedAction for a Journal Entry.
    """
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    lines = [l.model_dump() for l in body.lines]  # noqa: E741
    total_debit = sum(l["debit"] for l in lines)  # noqa: E741
    total_credit = sum(l["credit"] for l in lines)  # noqa: E741
    if abs(total_debit - total_credit) > 0.005:
        raise HTTPException(
            status_code=400,
            detail=f"Journal entry is out of balance: debits={total_debit:.2f} credits={total_credit:.2f}"
        )
    lines = _resolved_je_lines_or_422(db, body.company_id, lines, require_synced_chart=False)

    action_payload = {"date": body.date, "memo": body.memo, "lines": lines}
    reasoning = f"Manual JE: {body.memo or 'Journal Entry'} ({len(lines)} lines, debit ${total_debit:,.2f})"

    action = propose_journal_entry(
        db=db, realm_id=body.company_id,
        payload=action_payload, reasoning=reasoning, confidence_score=1.0,
        human_preparer_user_id=current_user.id,
    )
    return {"action_id": action.id, "status": action.status, "revision": action.revision}


def _maybe_auto_execute_reclassification(
    db: Session,
    *,
    action: ProposedAction,
    current_user: User,
) -> Optional[Dict[str, Any]]:
    """Gated auto-execution for a freshly-proposed reclassification (Phase 2 PR2).

    Returns a response dict when the action was auto-executed, or ``None`` to fall
    back to the unchanged proposal-first path. Auto-execution happens only when
    ``gate_decision`` says every condition holds (supervised_l3 + calibrated
    confidence >= threshold + reversible + within caps). Every auto-executed
    action is recorded as an AICorrectionEvent and audited via the executor.
    """
    try:
        validate_autonomous_approval(action)
    except HTTPException:
        return None
    organization = organization_for_company(db, action.company_id)
    if organization is None:
        return None  # can't resolve tenant scope → never auto-execute

    decision = gate_decision(
        db,
        organization_id=organization.id,
        task_type="reclassification",
        realm_id=action.company_id,
        raw_confidence=action.confidence_score,
    )
    if not decision.get("auto"):
        return None  # falls back to proposal-first, unchanged

    # Record the auto-execution as a training signal BEFORE executing, so the
    # per-run cap and circuit breaker see it. actor_user_id=None marks it as
    # engine-driven; matched_human=True because the AI output IS what was applied.
    record_correction(
        task_type="reclassification",
        human_decision="accepted",
        company_id=action.company_id,
        source_object_ref=str(action.id),
        ai_output={"action_type": action.action_type, "payload": action.payload},
        ai_confidence=action.confidence_score,
        human_output={"payload": action.payload, "auto_executed": True},
        actor_user_id=None,
        would_autoexecute=True,
        matched_human=True,
        db=db,
    )

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXPIRE on the L3 lane:
    # an expired proposal must not be auto-executed (defense-in-depth; the row was created
    # moments ago in this flow, so this fires only on a clock-skewed/edge row).
    if action_is_expired(action):
        return {"action_id": action.id, "status": action.status, "auto_executed": False}

    # PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md) — decision-8 consult on the L3 lane: a plan-step
    # proposal whose parent plan is paused/cancelled/expired/failed is never
    # auto-executed (a freshly-drafted reclassification carries no plan_step_id,
    # so this fires only on an edge row — same defense-in-depth as INV-EXPIRE above).
    if action.plan_step_id is not None and plan_status_blocks_execution(db, action.plan_step_id, realm_id=action.company_id):
        return {"action_id": action.id, "status": action.status, "auto_executed": False}

    # Atomic PENDING→APPROVED claim (F-07): defense-in-depth for the engine lane.
    # This action was created PENDING moments ago in this same flow, so the claim
    # all but always wins — but if a concurrent human decision already moved it,
    # this auto-execute must NOT fire a QBO write on an already-decided action.
    if not claim_action_transition(
        db, action_id=action.id, from_status="PENDING", to_status="APPROVED",
        reviewed_by="autonomy:supervised_l3", expected_revision=action.revision, autonomous=True,
    ):
        db.rollback()
        return {"action_id": action.id, "status": action.status, "auto_executed": False}
    db.commit()

    try:
        executed = execute_proposed_action(db, action.id)
        return {
            "action_id": action.id,
            "status": executed.status,
            "auto_executed": True,
            "calibrated_confidence": decision.get("calibrated"),
            "result": executed.execution_result,
        }
    except HTTPException as exc:
        # AC-E1 (D-B6-2, 2026-07-16): a 4xx (e.g. 409 period_locked, guarded BEFORE
        # any write) is a clean refusal, NOT a failure — log it honestly (declined,
        # not "failed"), decline the auto-exec, and leave the proposal for review.
        # Never break the propose response by re-raising here.
        logger.info(
            "Auto-execution declined for action %s (status %s)", action.id, exc.status_code
        )
        return {"action_id": action.id, "status": action.status, "auto_executed": False}
    except Exception as e:  # auto-exec failed — surface, but don't break proposal creation
        logger.warning(
            "Auto-execution failed for action %s: %s",
            action.id,
            redact_to_string(str(e), max_length=500),
        )
        return {"action_id": action.id, "status": action.status, "auto_executed": False}


@router.post("/propose/reclassify")
def propose_reclassification_action(
    body: ReclassifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Creates a PENDING ProposedAction for a transaction reclassification.
    """
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    # The category lives on a transaction LINE; the executor changes exactly
    # that line and never the header payment account (F121).
    action_payload = build_reclassification_payload(
        body.entity, body.txn_id, body.to_account_id, line_id=body.line_id,
    )
    action_payload["from_account_id"] = body.from_account_id
    try:
        normalize_reclassification_payload(action_payload)
    except QBOActionRefused as exc:
        raise user_safe_http_exception(
            status_code=422, error_code=exc.error_code, user_message=exc.user_message,
        ) from exc

    from_name = body.from_account_name or body.from_account_id or "?"
    to_name = body.to_account_name or body.to_account_id
    reasoning = f"Reclassify txn {body.txn_id}: {from_name} → {to_name}. Reason: {body.reason}"

    action = propose_reclassification(
        db=db, realm_id=body.company_id,
        payload=action_payload, reasoning=reasoning, confidence_score=0.95,
        human_preparer_user_id=current_user.id,
    )

    return {"action_id": action.id, "status": action.status, "revision": action.revision}


# ─── Unified Inbox (AI-Led Platform) ──────────────────────────────────────────

@router.get("/inbox")
def get_inbox(
    company_id: str,
    source: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Unified AI-led inbox — shows all PENDING proposals from all sources.
    Supports filtering by source (ai_assistant, ai_proactive, system),
    category, and priority (low, medium, high, critical).
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    query = db.query(ProposedAction).filter(
        ProposedAction.company_id == company_id,
        ProposedAction.status == "PENDING",
    )
    if source:
        query = query.filter(ProposedAction.source == source)
    if category:
        query = query.filter(ProposedAction.category == category)
    if priority:
        query = query.filter(ProposedAction.priority == priority)

    actions = query.order_by(
        # Critical first, then high, medium, low
        ProposedAction.created_at.desc()
    ).limit(50).all()

    # Sort by priority weight
    priority_weight = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    actions.sort(key=lambda a: priority_weight.get(getattr(a, 'priority', 'medium') or 'medium', 2))

    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C2 (AC-2) — prefetch ONCE per response, pass the dict down (never per-row)
    je_account_types = _prefetch_je_account_types(db, actions)
    return [
        {
            **_action_response(db, a, include_payload=False, je_account_types=je_account_types),
            "source": getattr(a, "source", "ai_assistant"),
            "category": getattr(a, "category", None),
            "priority": getattr(a, "priority", "medium"),
            # Additive tier facet (plan 20260716-five-domain-remediation AC-B4) —
            # same behavior-derived source as _review_contract's "tier" key.
            "tier": tier_for_action_type(a.action_type).value,
        }
        for a in actions
    ]


@router.get("/stats")
def get_action_stats(
    company_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Returns counts of actions by status and type for the dashboard.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    from sqlalchemy import func

    # Count by status
    status_counts = (
        db.query(ProposedAction.status, func.count(ProposedAction.id))
        .filter(ProposedAction.company_id == company_id)
        .group_by(ProposedAction.status)
        .all()
    )

    # Count PENDING by type
    type_counts = (
        db.query(ProposedAction.action_type, func.count(ProposedAction.id))
        .filter(
            ProposedAction.company_id == company_id,
            ProposedAction.status == "PENDING",
        )
        .group_by(ProposedAction.action_type)
        .all()
    )

    # Count PENDING by source
    source_counts = (
        db.query(ProposedAction.source, func.count(ProposedAction.id))
        .filter(
            ProposedAction.company_id == company_id,
            ProposedAction.status == "PENDING",
        )
        .group_by(ProposedAction.source)
        .all()
    )

    # Side-effect tier facet + FINDING exclusion (plan 20260716-five-domain-remediation
    # AC-B4): FINDING rows are surfaced insight with NO executor — they leave approval
    # semantics, so the approval count (total_pending) excludes them and they are
    # reported separately (pending_findings — nothing is hidden). pending_by_tier is
    # the additive facet the Wave-3 FE consumes, derived from the frozen tier map
    # (the same behavior-derived source as _review_contract's "tier").
    pending_by_tier: dict[str, int] = {}
    total_pending_approvals = 0
    pending_findings = 0
    for atype, count in type_counts:
        tier = tier_for_action_type(atype)
        pending_by_tier[tier.value] = pending_by_tier.get(tier.value, 0) + count  # SA-OK: in-memory dict.get accumulator over already-fetched GROUP BY rows, not a DB query
        if tier is SideEffectTier.FINDING:
            pending_findings += count
        else:
            total_pending_approvals += count

    return {
        "by_status": {status: count for status, count in status_counts},
        "pending_by_type": {atype: count for atype, count in type_counts},
        "pending_by_source": {src: count for src, count in source_counts},
        "pending_by_tier": pending_by_tier,
        "pending_findings": pending_findings,
        "total_pending": total_pending_approvals,
    }

@router.get("/pending", response_model=List[Dict[str, Any]])
def get_pending_actions(
    company_id: str, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user_from_token)
):
    """
    List PENDING proposed actions for a company.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    actions = db.query(ProposedAction).filter(
        ProposedAction.company_id == company_id,
        ProposedAction.status == "PENDING"
    ).all()
    
    # We serialize the response.
    # For a real response model, we would define a Pydantic ProposedActionResponse schema.
    # Returning dicts for simplicity in this endpoint matching the prompt definition.
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C2 (AC-2) — prefetch ONCE per response, pass the dict down (never per-row)
    je_account_types = _prefetch_je_account_types(db, actions)
    return [
        _action_response(db, a, include_payload=False, je_account_types=je_account_types)
        for a in actions
    ]

class ApprovalRequest(BaseModel):
    expected_revision: int | None = Field(None, strict=True, ge=1)


class BulkApprovalItem(ApprovalRequest):
    action_id: int


@router.post("/{action_id}/approve")
def approve_action(
    action_id: int,
    body: ApprovalRequest | None = Body(None),
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user_from_token)
):
    """
    Approves a ProposedAction. Executable QBO actions are submitted to QBO;
    review-only assistant drafts are marked approved for manual follow-up.
    """
    action = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=ProposedAction,
        entity_id=action_id,
        not_found_detail="Action not found",
    )
        
    if action.status != "PENDING":
        # Audit the failed attempt (e.g. double-click / stale tab approving an
        # already-processed action) — category only, no payloads.
        _audit_action_review(
            db,
            action=action,
            user_email=current_user.email,
            audit_action="APPROVE_ACTION_FAILED",
            details={"source": "dashboard", "reason": "invalid_status"},
        )
        db.commit()
        raise HTTPException(status_code=400, detail="Only PENDING actions can be approved.")

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXPIRE: an expired
    # proposal cannot be approved (it was drafted against books that may have changed;
    # re-draft to get a fresh preview). 409 = state conflict per the status taxonomy.
    if action_is_expired(action):
        _audit_action_review(
            db,
            action=action,
            user_email=current_user.email,
            audit_action="APPROVE_ACTION_FAILED",
            details={"source": "dashboard", "reason": "expired"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the sibling APPROVE_ACTION_FAILED branches
        raise user_safe_http_exception(
            status_code=409,
            error_code="action_expired",
            user_message=(
                "This proposal expired and can no longer be approved. Ask Vesper Bot to "
                "draft it again so you review a fresh preview against current records."
            ),
        )

    # PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md) — decision-8 approve-lane consult: a plan-step
    # proposal whose parent AgentPlan is PAUSED/CANCELLED/EXPIRED/FAILED is refused
    # BEFORE any claim or execution (defense in depth — the PENDING→EXPIRED claim
    # at cancel time is the real race defense). The action stays PENDING; the 409
    # mirrors the expired branch above per the api-contract status taxonomy.
    if action.plan_step_id is not None and plan_status_blocks_execution(db, action.plan_step_id, realm_id=action.company_id):
        _audit_action_review(
            db,
            action=action,
            user_email=current_user.email,
            audit_action="APPROVE_ACTION_FAILED",
            details={"source": "dashboard", "reason": "plan_blocked"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the sibling APPROVE_ACTION_FAILED branches
        raise user_safe_http_exception(
            status_code=409,
            error_code="agent_plan_blocked",
            user_message=(
                "This proposal belongs to an agent plan that is paused, cancelled, "
                "expired, or failed — it can no longer be approved from here. Resume "
                "the plan (or ask Vesper Bot to draft the change again) to proceed."
            ),
        )

    expected_revision = body.expected_revision if body else None
    _validate_review(db, action, current_user.id, expected_revision, reviewer=current_user.email, lane="dashboard")

    # Atomic PENDING→APPROVED claim (F-07): the status precheck above is a
    # fast-path; this conditional UPDATE is the authoritative gate against a
    # concurrent approve+reject. On a lost race the action was decided elsewhere
    # between the precheck and here — return a revision/state conflict rather
    # than fall through and execute a QBO write on a rejected action.
    if not claim_action_transition(  # AUTH-OK: ProposedAction workflow-state transition, not an access-control mutation
        db, action_id=action.id, from_status="PENDING", to_status="APPROVED",
        reviewed_by=current_user.email, expected_revision=expected_revision, approver_user_id=current_user.id,
    ):
        db.rollback()
        raise HTTPException(status_code=409, detail="The proposal changed or is no longer PENDING. Refresh and review it again.")
    _audit_action_review(
        db,
        action=action,
        user_email=current_user.email,
        audit_action="APPROVE_ACTION",
        details={"source": "dashboard"},
    )
    # Phase 1 (autonomy): capture the approval as a training signal. The payload
    # is unchanged at approve time (edits go through /edit), so this is an
    # accept-as-is. Best-effort/non-blocking — rides this commit via a savepoint.
    _capture_action_correction(
        db,
        action=action,
        human_decision="accepted",
        human_output={"payload": action.payload},
        actor_user_id=current_user.id,
    )
    db.commit()  # LAYER-OK: approval-state commit in the approval-execution endpoint

    # PLAN-EXEMPT: B5-1 P0 field bug (live-prod-corroborated 2026-06-24).
    # Internal-DB actions (e.g. cashflow categorization) are NOT QBO writes, so
    # they are not in EXECUTABLE_ACTION_TYPES — but they DO mutate Vesper Bot's own
    # DB and must run on approval. Without this branch they fell through to
    # _mark_review_only_approved (a silent no-op): the approval was recorded but
    # the categorization never persisted, so the txns stayed uncategorized.
    if action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES:
        return _execute_internal_db_action(db, action, current_user)

    if not is_executable_action_type(action.action_type):
        _mark_review_only_approved(action)
        db.commit()
        db.refresh(action)
        return {"success": True, "status": action.status, "result": action.execution_result, "execution": None}

    try:
        executed_action = execute_proposed_action(db, action.id)
        # The executor returns (not raises) on QBO failure, with status FAILED.
        # Be honest in the response (success=False) and audit the failed attempt.
        if executed_action.status == "FAILED":
            _audit_action_review(
                db,
                action=executed_action,
                user_email=current_user.email,
                audit_action="APPROVE_ACTION_EXECUTION_FAILED",
                details={
                    "source": "dashboard",
                    "reason": "execution_failed",
                    **_execution_error_category(executed_action),
                },
            )
            db.commit()
        else:
            # Mark linked ScheduleEntry rows POSTED. Hook failures surface
            # loudly (SCHEDULE_POST_HOOK_FAILED audit row + warning stamped
            # into execution_result) — never a silent warning log.
            run_post_execution_schedule_hook(db, executed_action, actor=current_user.email)
            db.refresh(executed_action)
        return {
            "success": executed_action.status != "FAILED",
            "status": executed_action.status,
            "result": executed_action.execution_result,
            "execution": _execution_status(db, executed_action),
        }
    except HTTPException:
        # AC-E1 (D-B6-2, 2026-07-16): a 4xx the executor raises — e.g. a 409
        # period_locked, guarded BEFORE _claim_execution (no ledger row, nothing
        # posted) — must surface AS ITSELF, not be re-wrapped as a 500. A clean
        # period-lock is a refusal, NOT an execution failure, so it is deliberately
        # NOT audited as APPROVE_ACTION_EXECUTION_FAILED (truthful audit). rollback
        # is a safe no-op on the clean session; the bare re-raise keeps the 409.
        db.rollback()
        raise
    except Exception as e:
        # e.g. concurrent "already running" — audit the failed attempt before
        # surfacing the safe error. Roll back first: the session may be dirty.
        db.rollback()
        _audit_action_review(
            db,
            action=action,
            user_email=current_user.email,
            audit_action="APPROVE_ACTION_EXECUTION_FAILED",
            details={
                "source": "dashboard",
                "reason": "execution_exception",
                "error_code": "proposed_action_execution_failed",
            },
        )
        db.commit()
        raise user_safe_http_exception(
            status_code=500,
            error_code="proposed_action_execution_failed",
            user_message="The proposed action could not be executed. No additional action was approved.",
            exc=e,
            retryable=True,
            log_context="proposed_action_execution_failed",
        )


# ── Tier-2 (INTERNAL_MATERIAL) in-chat approval channel ──────────────────────────────
# Plan 20260716-five-domain-remediation AC-B3: the fingerprint-bound one-time
# confirmation-token machinery (Control-3, app/agent/runtime/confirmation_tokens.py)
# becomes an approval CHANNEL for tier-2 ProposedActions — the token flips
# PENDING→APPROVED and the action executes through the SAME shared internal-DB core
# the dashboard approve uses. It is NEVER a token-gated direct write: the draft is a
# ProposedAction first (AWB-06 structural for memory/knowledge), decision stays
# separated from execution (Control-2), and the fingerprint binds the token to THIS
# action's stored payload — an edited or different action rejects the token.


class ConfirmActionRequest(BaseModel):
    confirmation_token: str = Field(..., min_length=1)


def _tier2_confirmation_fingerprint_args(action: ProposedAction) -> dict:
    """The deterministic fingerprint inputs binding a token to ONE tier-2 action.

    The ENTIRE stored payload is folded in (as ``tool_input={"payload": ...}``) plus
    the action id and type, so any payload edit (PUT /{id}/edit) or a different action
    invalidates an already-issued token. Both the issue endpoint and the confirm
    endpoint derive the fingerprint from these same server-side values — the client
    never supplies any part of it.
    """
    payload = action.payload if isinstance(action.payload, dict) else {}
    return {
        "tool_name": str(payload.get("tool_name") or action.action_type),
        "tool_input": {"payload": payload, "proposal_revision": action.revision},
        "action_type": action.action_type,
        "risk_level": SideEffectTier.INTERNAL_MATERIAL.value,
        "proposed_action_id": action.id,
    }


def _tier2_action_or_refuse(db: Session, action: ProposedAction, current_user: User, *, lane: str) -> None:
    """Shared guard set for the tier-2 token channel (mirrors approve_action's order)."""
    if tier_for_action_type(action.action_type) is not SideEffectTier.INTERNAL_MATERIAL:
        raise user_safe_http_exception(
            status_code=400,
            error_code="action_not_token_confirmable",
            user_message=(
                "Only material internal changes can be approved with a confirmation "
                "token. Use the Actions page approval for this proposal."
            ),
        )
    if action.status != "PENDING":
        _audit_action_review(
            db, action=action, user_email=current_user.email,
            audit_action="CONFIRM_ACTION_FAILED",
            details={"source": lane, "reason": "invalid_status"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the approve endpoint's failed-attempt branches
        raise HTTPException(status_code=400, detail="Only PENDING actions can be confirmed.")
    if action_is_expired(action):
        _audit_action_review(
            db, action=action, user_email=current_user.email,
            audit_action="CONFIRM_ACTION_FAILED",
            details={"source": lane, "reason": "expired"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the approve endpoint's failed-attempt branches
        raise user_safe_http_exception(
            status_code=409,
            error_code="action_expired",
            user_message=(
                "This proposal expired and can no longer be approved. Ask Vesper Bot to "
                "draft it again so you review a fresh preview against current records."
            ),
        )
    if action.plan_step_id is not None and plan_status_blocks_execution(db, action.plan_step_id, realm_id=action.company_id):
        _audit_action_review(
            db, action=action, user_email=current_user.email,
            audit_action="CONFIRM_ACTION_FAILED",
            details={"source": lane, "reason": "plan_blocked"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the approve endpoint's failed-attempt branches
        raise user_safe_http_exception(
            status_code=409,
            error_code="agent_plan_blocked",
            user_message=(
                "This proposal belongs to an agent plan that is paused, cancelled, "
                "expired, or failed — it can no longer be approved from here."
            ),
        )
    # The issue route validates the rendered revision; consume binds it through
    # the token fingerprint and rechecks the immutable human identities.
    _validate_review(db, action, current_user.id, action.revision, reviewer=current_user.email, lane=lane)


@router.post("/{action_id}/confirmation-token")
@limiter.limit("10/minute")
def issue_action_confirmation_token(
    action_id: int,
    request: Request,
    body: ApprovalRequest | None = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Issue a one-time, fingerprint-bound confirmation token for a PENDING tier-2 action.

    The token is backend-issued, expires in 5 minutes, is single-use, and is bound to
    the action's CURRENT stored payload — the consume side recomputes the fingerprint
    from the row and rejects a token whose action changed underneath it. Model output
    can never mint or satisfy one (Control-3).
    """
    # Lazy import — pulling app.agent.runtime at module import would execute the whole
    # runtime package init from a router import (the assistant/main.py precedent).
    from ..agent.runtime.confirmation_tokens import issue_agent_confirmation_token

    action = check_entity_tenant_access(
        "EDITOR", db=db, user=current_user, model=ProposedAction,
        entity_id=action_id, not_found_detail="Action not found",
    )
    _tier2_action_or_refuse(db, action, current_user, lane="chat_confirmation_issue")
    _validate_review(db, action, current_user.id, body.expected_revision if body else None, reviewer=current_user.email, lane="chat_confirmation_issue")

    organization = organization_for_company(db, action.company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=action.company_id)
        if organization
        else None
    )
    token, token_row = issue_agent_confirmation_token(
        db,
        user_id=current_user.id,
        company_id=action.company_id,
        tenant_id=str(organization.id) if organization else None,
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        qbo_realm_id=action.company_id,
        **_tier2_confirmation_fingerprint_args(action),
    )
    db.commit()
    db.refresh(token_row)
    _audit_action_review(
        db, action=action, user_email=current_user.email,
        audit_action="ISSUE_ACTION_CONFIRMATION_TOKEN",
        details={
            "source": "chat_confirmation",
            "confirmation_token_id": token_row.id,
            "action_fingerprint": token_row.action_fingerprint,
        },
    )
    db.commit()  # LAYER-OK: token issuance + audit commit for the confirmation-channel endpoint
    return {
        "action_id": action.id,
        "confirmation_token": token,
        "action_fingerprint": token_row.action_fingerprint,
        "expires_at": token_row.expires_at.isoformat() if token_row.expires_at else None,
        "tier": SideEffectTier.INTERNAL_MATERIAL.value,
    }


@router.post("/{action_id}/confirm")
@limiter.limit("10/minute")
def confirm_action_with_token(
    action_id: int,
    body: ConfirmActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Approve + execute a PENDING tier-2 action via its fingerprint-bound token.

    The token is the approval CHANNEL, not the executor: consume (single-use, expiry,
    secret, scope, fingerprint — confirmation_tokens.py enforcement order) → the same
    atomic PENDING→APPROVED claim the dashboard approve uses → the SAME shared
    internal-DB execution core. A missing/invalid/mismatched token leaves the action
    PENDING and unexecuted.
    """
    # Lazy import — see issue_action_confirmation_token.
    from ..agent.runtime.confirmation_tokens import (
        compute_action_fingerprint,
        consume_agent_confirmation_token,
    )

    action = check_entity_tenant_access(
        "EDITOR", db=db, user=current_user, model=ProposedAction,
        entity_id=action_id, not_found_detail="Action not found",
    )
    _tier2_action_or_refuse(db, action, current_user, lane="chat_confirmation")
    # Token consumption commits and expires ORM state. Bind the transition to
    # the revision inspected for this token, even if another editor commits
    # before we claim approval and SQLAlchemy reloads the action.
    reviewed_revision = action.revision

    organization = organization_for_company(db, action.company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=action.company_id)
        if organization
        else None
    )
    expected_fingerprint = compute_action_fingerprint(
        user_id=current_user.id,
        company_id=action.company_id,
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        qbo_realm_id=action.company_id,
        **_tier2_confirmation_fingerprint_args(action),
    )
    try:
        token_row = consume_agent_confirmation_token(
            db,
            body.confirmation_token,
            user_id=current_user.id,
            company_id=action.company_id,
            tenant_id=str(organization.id) if organization else None,
            organization_id=organization.id if organization else None,
            organization_company_id=organization_company.id if organization_company else None,
            expected_fingerprint=expected_fingerprint,
            enforce_fingerprint=True,
        )
    except ValueError as exc:
        _audit_action_review(
            db, action=action, user_email=current_user.email,
            audit_action="CONFIRM_ACTION_FAILED",
            details={"source": "chat_confirmation", "reason": "invalid_token"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the approve endpoint's failed-attempt branches
        envelope = user_safe_http_exception(
            status_code=400,
            error_code="action_confirmation_token_invalid",
            user_message=(
                "The confirmation token is invalid, expired, already used, or does not "
                "match this exact action. Request a fresh token and try again."
            ),
            exc=exc,
            log_context="action_confirmation_token_invalid",
        )
        raise envelope from exc

    # Atomic PENDING→APPROVED claim (F-07) — same authoritative gate as approve_action.
    if not claim_action_transition(  # AUTH-OK: ProposedAction workflow-state transition, not an access-control mutation
        db, action_id=action.id, from_status="PENDING", to_status="APPROVED",
        reviewed_by=current_user.email, expected_revision=reviewed_revision, approver_user_id=current_user.id,
    ):
        db.rollback()
        raise HTTPException(status_code=400, detail="Only PENDING actions can be confirmed.")
    _audit_action_review(
        db, action=action, user_email=current_user.email,
        audit_action="APPROVE_ACTION",
        details={
            "source": "chat_confirmation",
            "confirmation_token_id": token_row.id,
            "tier": SideEffectTier.INTERNAL_MATERIAL.value,
        },
    )
    _capture_action_correction(
        db,
        action=action,
        human_decision="accepted",
        human_output={"payload": action.payload},
        actor_user_id=current_user.id,
    )
    db.commit()  # LAYER-OK: approval-state commit in the approval-execution endpoint (mirrors approve_action)

    if action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES:
        return _execute_internal_db_action(db, action, current_user)

    # Defensive: every INTERNAL_MATERIAL type is internal-DB executable today (the
    # tier-map integrity check enforces it); an unexpected fall-through records the
    # honest review-only approval rather than silently no-op'ing.
    _mark_review_only_approved(action)
    db.commit()
    db.refresh(action)
    return {"success": True, "status": action.status, "result": action.execution_result, "execution": None}


@router.post("/{action_id}/reject")
def reject_action(
    action_id: int, 
    comment: str = "",
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user_from_token)
):
    """
    Rejects a ProposedAction, stopping it from executing.
    Also cleans up linked ScheduleEntry records so schedules can be re-generated.
    """
    action = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=ProposedAction,
        entity_id=action_id,
        not_found_detail="Action not found",
    )
        
    if action.status != "PENDING":
        # Audit the failed attempt — category only, no payloads.
        _audit_action_review(
            db,
            action=action,
            user_email=current_user.email,
            audit_action="REJECT_ACTION_FAILED",
            details={"source": "dashboard", "reason": "invalid_status"},
        )
        db.commit()
        raise HTTPException(status_code=400, detail="Only PENDING actions can be rejected.")

    # Atomic PENDING→REJECTED claim (F-07): the precheck above is a fast-path;
    # this conditional UPDATE is the authoritative gate against a concurrent
    # approve+reject (the case where an approve would otherwise post while this
    # reject wins the status column). On a lost race, return the same 400.
    if not claim_action_transition(
        db, action_id=action.id, from_status="PENDING", to_status="REJECTED",
        reviewed_by=current_user.email,
    ):
        db.rollback()
        raise HTTPException(status_code=400, detail="Only PENDING actions can be rejected.")

    # Optionally store comment in execution_result or a new column
    action.execution_result = {"reviewer_comment": comment}
    _audit_action_review(
        db,
        action=action,
        user_email=current_user.email,
        audit_action="REJECT_ACTION",
        details={"source": "dashboard", "comment": comment},
    )
    # Phase 1 (autonomy): capture the rejection as a training signal.
    # Best-effort/non-blocking — rides this commit via a savepoint.
    _capture_action_correction(
        db,
        action=action,
        human_decision="rejected",
        human_output={"reviewer_comment": comment} if comment else None,
        actor_user_id=current_user.id,
    )

    # Clean up linked ScheduleEntry records so schedules can be re-generated
    from ..models import ScheduleEntry
    db.query(ScheduleEntry).filter(
        ScheduleEntry.proposed_action_id == action_id,
    ).delete(synchronize_session="fetch")
    
    db.commit()
    db.refresh(action)
    
    return {"success": True, "status": action.status}


@router.post("/{action_id}/reverse")
def reverse_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Create a PENDING reversing journal-entry proposal from an EXECUTED JE.

    The reversal is routed as a NEW proposal through the normal proposer →
    executor pipeline (propose_je_reversal → propose_journal_entry), never a
    direct write. Same EDITOR role + tenant check as the propose-JE endpoint.

    Guardrails: only an EXECUTED POST_JE/JOURNAL_ENTRY proposal that is neither
    itself a reversal nor already reversed can be reversed (reversal_block_reason).
    The closed-period guard is NOT bypassed — if the original entry's period is
    locked, the reversal proposal is still created but the executor's C1 guard
    will (correctly) block execution at approve time.
    """
    original = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=ProposedAction,
        entity_id=action_id,
        not_found_detail="Action not found",
    )

    block = reversal_block_reason(original)
    if block is not None:
        status_code, error_code, message = block
        raise user_safe_http_exception(
            status_code=status_code,
            error_code=error_code,
            user_message=message,
        )

    reversal = propose_je_reversal(db, original, human_preparer_user_id=current_user.id)
    _audit_action_review(
        db,
        action=reversal,
        user_email=current_user.email,
        audit_action="PROPOSE_JE_REVERSAL",
        details={"source": "dashboard", "reversal_of_action_id": original.id},
    )
    db.commit()

    return {
        "action_id": reversal.id,
        "status": reversal.status,
        "reversal_of_action_id": original.id,
    }


class EditJERequest(BaseModel):
    expected_revision: int | None = Field(None, strict=True, ge=1)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    memo: str = ""
    lines: List[JELine] = Field(..., min_length=1)


@router.put("/{action_id}/edit")
def edit_approved_action(
    action_id: int,
    body: EditJERequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Edit an unapproved PENDING journal entry for separate human review.
    """
    action = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=ProposedAction,
        entity_id=action_id,
        not_found_detail="Action not found",
    )

    if action.status != "PENDING":
        raise HTTPException(status_code=409, detail="Only PENDING journal entries can be edited. Draft a correction or reversal for a posted entry.")

    if action.action_type not in ("JOURNAL_ENTRY", "POST_JE"):
        raise user_safe_http_exception(
            status_code=400,
            error_code="je_edit_not_a_journal_entry",
            user_message="Only journal entries can be edited.",
            log_context="je_edit_wrong_type",
        )

    # Validate debit/credit balance
    lines = [ln.model_dump() for ln in body.lines]
    total_debit = sum(ln["debit"] for ln in lines)
    total_credit = sum(ln["credit"] for ln in lines)
    if abs(total_debit - total_credit) > 0.005:
        raise user_safe_http_exception(
            status_code=400,
            error_code="je_edit_out_of_balance",
            user_message=(
                f"Journal entry is out of balance: debits={total_debit:.2f} "
                f"credits={total_credit:.2f}"
            ),
            log_context="je_edit_out_of_balance",
        )

    # Every line's account_id must be a real, active account in THIS realm
    # (batch-5 AC-J3): the modal picker sets both id and name, but the edit lane
    # is the AUTHORITY — a typed/stale/unknown/inactive id is rejected with a
    # teaching 422 rather than posting to a non-existent account. A duplicate
    # account across lines is legal (splits), so the DISTINCT id set is checked.
    # The same shared check resolves each line's Name to a synced vendor/customer
    # id and refuses an A/R or A/P line without one (QuickBooks rejects it).
    # The lookups run inside tenant_scope_for_realm: this route carries no
    # company_id to auto-pin the org GUC (the INCIDENT-qbo-tokens-rls class).
    # A Name already on the stored draft (e.g. a schedule's label) is kept as is;
    # only a newly typed one must match a synced vendor/customer.
    stored_lines = action.payload.get("lines") if isinstance(action.payload, dict) else None
    carried_names = tuple(
        str((stored or {}).get(key) or "")
        for stored in (stored_lines if isinstance(stored_lines, list) else [])
        for key in ("vendor_name", "customer_name")
    )
    lines = _resolved_je_lines_or_422(
        db, action.company_id, lines, require_synced_chart=True, carried_names=carried_names,
    )

    # Preserve the original payload for audit trail
    original_payload = action.payload.copy() if isinstance(action.payload, dict) else action.payload
    prev_status = action.status

    # Update the payload with new values, keeping other metadata
    updated_payload = action.payload.copy() if isinstance(action.payload, dict) else {}
    updated_payload["date"] = body.date
    updated_payload["memo"] = body.memo
    updated_payload["lines"] = lines
    changed = edit_proposal_payload(
        db, action, updated_payload, editor_user_id=current_user.id,
        expected_revision=body.expected_revision,
        reasoning=f"Edited JE: {body.memo or 'Journal Entry'} ({len(lines)} lines, debit ${total_debit:,.2f})",
    )
    if not changed:
        return {"action_id": action.id, "status": action.status, "revision": action.revision, "changed": False}
    _audit_action_review(db, action=action, user_email=current_user.email,
                         audit_action="EDIT_ACTION", details={"revision": action.revision, "previous_status": prev_status})

    # Phase 1 (autonomy): capture the edit as a training signal — AI's original
    # payload vs the human's corrected payload. Best-effort/non-blocking.
    _capture_action_correction(
        db,
        action=action,
        human_decision="edited",
        ai_output={"action_type": action.action_type, "payload": original_payload},
        human_output={"payload": updated_payload},
        actor_user_id=current_user.id,
    )

    db.commit()
    db.refresh(action)

    logger.info(f"Action {action_id} edited by {current_user.email}, reset to PENDING")
    return {"action_id": action.id, "status": action.status, "revision": action.revision}


@router.get("/history", response_model=List[Dict[str, Any]])
def get_action_history(
    company_id: str,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Full action history for a company — all statuses.
    Supports pagination and optional status filter.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    query = db.query(ProposedAction).filter(
        ProposedAction.company_id == company_id,
    )
    if status:
        query = query.filter(ProposedAction.status == status.upper())

    actions = (
        query.order_by(ProposedAction.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C2 (AC-2) — prefetch ONCE per response, pass the dict down (never per-row)
    je_account_types = _prefetch_je_account_types(db, actions)
    return [
        _action_response(db, a, include_payload=True, je_account_types=je_account_types)
        for a in actions
    ]


@router.post("/bulk-approve")
def bulk_approve_actions(
    action_ids: List[BulkApprovalItem | int],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Approve multiple actions in one call. Executable QBO actions are submitted
    to QBO; review-only assistant drafts are marked approved for manual follow-up.
    """
    results = []
    for item in action_ids:
        action_id = item.action_id if isinstance(item, BulkApprovalItem) else item
        expected_revision = item.expected_revision if isinstance(item, BulkApprovalItem) else None
        try:
            action = check_entity_tenant_access(
                "EDITOR",
                db=db,
                user=current_user,
                model=ProposedAction,
                entity_id=action_id,
                not_found_detail="Action not found",
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                results.append({"action_id": action_id, "error": "Not found"})
                continue
            raise
        if not action:
            results.append({"action_id": action_id, "error": "Not found"})
            continue
        if action.status != "PENDING":
            _audit_action_review(
                db,
                action=action,
                user_email=current_user.email,
                audit_action="APPROVE_ACTION_FAILED",
                details={"source": "dashboard_bulk", "reason": "invalid_status"},
            )
            db.commit()
            results.append({"action_id": action_id, "error": f"Status is {action.status}, not PENDING"})
            continue

        try:
            _validate_review(db, action, current_user.id, expected_revision, reviewer=current_user.email, lane="dashboard_bulk")
        except HTTPException as exc:
            results.append({"action_id": action_id, "error": exc.detail["user_message"]})
            continue

        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXPIRE per-item on
        # the bulk lane; same refusal shape as the other per-item error branches.
        if action_is_expired(action):
            _audit_action_review(
                db,
                action=action,
                user_email=current_user.email,
                audit_action="APPROVE_ACTION_FAILED",
                details={"source": "dashboard_bulk", "reason": "expired"},
            )
            db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the sibling per-item branches
            results.append({"action_id": action_id, "error": "This proposal expired; re-draft it to approve."})
            continue

        # PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker
        # RATIFICATION-2026-07-14.md) — decision-8 consult per-item on the bulk lane;
        # same refusal shape as the expired branch above. The row stays PENDING.
        if action.plan_step_id is not None and plan_status_blocks_execution(db, action.plan_step_id, realm_id=action.company_id):
            _audit_action_review(
                db,
                action=action,
                user_email=current_user.email,
                audit_action="APPROVE_ACTION_FAILED",
                details={"source": "dashboard_bulk", "reason": "plan_blocked"},
            )
            db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the sibling per-item branches
            results.append({
                "action_id": action_id,
                "error": "This proposal's agent plan is paused, cancelled, expired, or failed; it was not approved.",
            })
            continue

        # Atomic PENDING→APPROVED claim (F-07): per-item authoritative gate. On a
        # lost race (this row was decided concurrently) record a per-item error
        # and continue — the same shape as the invalid_status branch above.
        if not claim_action_transition(  # AUTH-OK: ProposedAction workflow-state transition, not an access-control mutation
            db, action_id=action.id, from_status="PENDING", to_status="APPROVED",
            reviewed_by=current_user.email, expected_revision=expected_revision, approver_user_id=current_user.id,
        ):
            db.rollback()
            results.append({"action_id": action_id, "error": "Status is not PENDING, not approved"})
            continue
        _audit_action_review(
            db,
            action=action,
            user_email=current_user.email,
            audit_action="APPROVE_ACTION",
            details={"source": "dashboard_bulk"},
        )
        db.commit()

        # Internal-DB actions execute on approval via the shared core — bulk-lane
        # parity with the single-approve branch (plan
        # 20260706-widen-internal-db-approval-execution AC-2): without this, a bulk
        # approval of the 13 types silently recorded review-only and kept minting
        # the stuck-APPROVED class the widening exists to end.
        if action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES:
            try:
                outcome = _execute_internal_db_action(db, action, current_user)
                results.append({
                    "action_id": action_id,
                    "status": outcome["status"],
                    "success": outcome["success"],
                    "result": outcome["result"],
                    "execution": None,
                })
            except HTTPException as exc:
                # The wrapper did not rewrite history. Preserve an explicit uncertain
                # envelope rather than returning an older action projection.
                results.append({
                    "action_id": action_id,
                    "status": action.status,
                    "success": False,
                    "result": InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None).result(),
                    "execution": None,
                    "error": exc.detail,
                })
            continue

        if not is_executable_action_type(action.action_type):
            _mark_review_only_approved(action)
            db.commit()
            db.refresh(action)
            results.append({
                "action_id": action_id,
                "status": action.status,
                "result": action.execution_result,
                "execution": None,
            })
            continue

        try:
            executed = execute_proposed_action(db, action.id)
            if executed.status != "FAILED":
                # Same loud schedule hook as the single-approve path.
                run_post_execution_schedule_hook(db, executed, actor=current_user.email)
                db.refresh(executed)
            item: Dict[str, Any] = {
                "action_id": action_id,
                "status": executed.status,
                "result": executed.execution_result,
                "execution": _execution_status(db, executed),
            }
            # The executor returns (not raises) on QBO failure with status FAILED:
            # surface the error envelope at the top level so per-item failure
            # handling is uniform, and audit the failed attempt (category only).
            if executed.status == "FAILED":
                execution_result = executed.execution_result if isinstance(executed.execution_result, dict) else {}
                item["error"] = execution_result.get("error") or safe_error_detail(
                    error_code="qbo_action_execution_failed",
                    user_message="The approved action could not be posted to QuickBooks.",
                    retryable=True,
                    log_context=f"action_bulk_approval_failed action_id={action_id}",
                )
                _audit_action_review(
                    db,
                    action=executed,
                    user_email=current_user.email,
                    audit_action="APPROVE_ACTION_EXECUTION_FAILED",
                    details={
                        "source": "dashboard_bulk",
                        "reason": "execution_failed",
                        **_execution_error_category(executed),
                    },
                )
                db.commit()
            results.append(item)
        except HTTPException as exc:
            # AC-E1 (D-B6-2, 2026-07-16): a 4xx (e.g. 409 period_locked) is a clean
            # per-item refusal, guarded BEFORE any write — surface the envelope at
            # the item top level (like the internal-DB lane above) and do NOT audit
            # it as APPROVE_ACTION_EXECUTION_FAILED. rollback is a safe no-op here.
            db.rollback()
            results.append({
                "action_id": action_id,
                "status": action.status,
                "error": exc.detail,
            })
        except Exception as e:
            db.rollback()
            _audit_action_review(
                db,
                action=action,
                user_email=current_user.email,
                audit_action="APPROVE_ACTION_EXECUTION_FAILED",
                details={
                    "source": "dashboard_bulk",
                    "reason": "execution_exception",
                    "error_code": "action_bulk_approval_failed",
                },
            )
            db.commit()
            results.append(
                {
                    "action_id": action_id,
                    "status": "FAILED",
                    "error": safe_error_detail(
                        error_code="action_bulk_approval_failed",
                        user_message="The action could not be approved or executed.",
                        exc=e,
                        retryable=True,
                        log_context=f"action_bulk_approval_failed action_id={action_id}",
                    ),
                }
            )

    return {"success": True, "results": results}


@router.get("/{action_id}")
def get_action_detail(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Returns full detail for a single ProposedAction."""
    action = check_entity_tenant_access(
        "VIEWER",
        db=db,
        user=current_user,
        model=ProposedAction,
        entity_id=action_id,
        not_found_detail="Action not found.",
    )
    return _action_response(db, action, include_payload=True)


# ─────────────────────────────────────────────────────────────────────────────
# One-Click Email Approval Tokens (no login required)
#
# Security model: the signed, single-use token (bound to action/company/
# recipient/decision, stored as a hash only) IS the authorization. The GET
# endpoints below NEVER mutate state — they only validate the token and render
# an HTML confirmation page. The actual approve/reject decision is applied by
# the POST endpoints, which the confirmation page's form submits the token to.
# ─────────────────────────────────────────────────────────────────────────────

import html as _html  # noqa: E402

from fastapi.responses import HTMLResponse  # noqa: E402
from ..services.email_service import verify_approval_token, is_used_approval_token  # noqa: E402
from sqlalchemy.sql import func as sqlfunc  # noqa: E402


def _confirm_summary(action: ProposedAction) -> Dict[str, Any]:
    """Build a small, non-sensitive action summary for the confirm page."""
    payload = action.payload if isinstance(action.payload, dict) else {}
    amount = (
        payload.get("amount")
        or payload.get("total")
        or payload.get("debit")
    )
    if amount is None:
        lines = payload.get("lines")
        if isinstance(lines, list):
            try:
                amount = sum(float(l.get("debit") or 0) for l in lines if isinstance(l, dict))  # noqa: E741
            except (TypeError, ValueError):
                amount = None
    proposer = (
        _payload_first(payload, "requester", "requested_by", "requester_email", "user_email")
        or action.source
        or "Vesper Bot"
    )
    try:
        amount_str = f"${float(amount):,.2f}" if amount not in (None, "") else "—"
    except (TypeError, ValueError):
        amount_str = "—"
    return {
        "action_type": action.action_type or "Action",
        "company": action.company_id,
        "amount": amount_str,
        "proposer": str(proposer),
        "summary": payload.get("summary") or action.reasoning or f"Proposed {action.action_type}",
    }


def _validate_token_for_confirm(
    db: Session, token: str, expected_decision: str
) -> tuple[Optional[ProposedAction], Optional["ApprovalTokenLike"], Optional[HTMLResponse]]:
    """
    Shared validation for both the GET confirm page and the POST handler.

    Returns (action, token_row, error_response). When error_response is not None
    the caller should return it directly; it deliberately does not leak details
    (invalid / expired / used / wrong company / already-processed all render a
    friendly page). No state is mutated here.
    """
    token_row = verify_approval_token(db, token, expected_decision=expected_decision)
    if not token_row:
        # A recipient re-clicking a one-click link presents a now-used token.
        # If the secret still matches, that is benign — render the friendly
        # "already processed" page (200) rather than a generic error. The secret
        # check inside is_used_approval_token keeps this from leaking token
        # existence to anyone who does not hold the original link.
        if is_used_approval_token(db, token, expected_decision):
            return None, None, HTMLResponse(
                content=_token_response_page(
                    "ℹ️ Already Processed",
                    "This link has already been processed. Review the action and its execution evidence in the dashboard.",
                    "info",
                )
            )
        return None, None, HTMLResponse(
            content=_token_response_page(
                "❌ Invalid or Expired Link",
                "This link is no longer valid, or this action has already been processed. "
                "Please log in to the dashboard to review this action.",
                "error",
            ),
            status_code=400,
        )

    action = db.query(ProposedAction).filter(ProposedAction.id == token_row.action_id).first()
    if not action or action.company_id != token_row.company_id:
        return None, None, HTMLResponse(
            content=_token_response_page(
                "❌ Invalid Link",
                "This link is not valid for this action. "
                "Please log in to the dashboard to review.",
                "error",
            ),
            status_code=400,
        )

    if expected_decision == "APPROVED" and token_row.proposal_revision != action.revision:
        return None, None, HTMLResponse(content=_token_response_page(
            "Review Changed", "This proposal changed after this link was issued. Review it in the dashboard.", "error"), status_code=409)

    if action.status != "PENDING":
        if action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES:
            with tenant_scope_for_realm(db, action.company_id):
                summary = internal_execution_summary(db, action)
            if summary and summary["execution_status"] not in {"succeeded", "not_applied"}:
                return None, None, HTMLResponse(content=_token_response_page("⚠️ Review Required", REVIEW_MESSAGE, "error"))
        return None, None, HTMLResponse(
            content=_token_response_page(
                "ℹ️ Already Processed",
                "This link has already been processed. Review the action and its execution evidence in the dashboard.",
                "info",
            )
        )

    return action, token_row, None


# Type alias kept loose to avoid importing the ORM model at module import time
ApprovalTokenLike = Any


@router.get("/approve-token/{token}", response_class=HTMLResponse)
def approve_via_token(token: str, db: Session = Depends(get_db)):
    """
    Render the approve confirmation page linked from an approval email.

    This GET handler NEVER mutates state — it only validates the signed token and
    renders an HTML page with a POST form that carries the token. The actual
    approval happens in ``approve_via_token_submit`` when the user clicks Approve.
    """
    action, _token_row, error = _validate_token_for_confirm(db, token, "APPROVED")
    if error is not None:
        return error
    return HTMLResponse(content=_token_confirm_page(action, token, "APPROVED"))


@router.post("/approve-token/{token}", response_class=HTMLResponse)
def approve_via_token_submit(token: str, db: Session = Depends(get_db)):
    """
    Apply a one-click email approval. Authorization is the signed, single-use
    token itself (no session). Re-clicking a used token renders a friendly
    "already processed" page rather than erroring.
    """
    action, token_row, error = _validate_token_for_confirm(db, token, "APPROVED")
    if error is not None:
        return error

    recipient_email = (token_row.recipient_email or "").strip().lower()
    approver_user_id = db.query(User.id).filter(User.email == recipient_email).scalar() if recipient_email else None
    try:
        _validate_review(db, action, approver_user_id, token_row.proposal_revision,
                         reviewer=recipient_email or "email_token", lane="email_token")
    except HTTPException as exc:
        return HTMLResponse(content=_token_response_page("Cannot Approve", exc.detail["user_message"], "error"), status_code=exc.status_code)

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXPIRE + INV-ATOMIC
    # (SYNTHESIS GAP-A1/A4): the email-token lane was the ONLY approve lane outside the F-07
    # conditional-UPDATE net — a plain ORM assignment after a read-then-check, racy against a
    # concurrent dashboard decision — and it never checked expiry. Both close here: refuse an
    # expired proposal, then claim PENDING→APPROVED atomically; on a lost race render the
    # same "already processed" page the precheck renders.
    reviewer = token_row.recipient_email or "email_token"
    if action_is_expired(action):
        _audit_action_review(
            db,
            action=action,
            user_email=reviewer,
            audit_action="APPROVE_ACTION_FAILED",
            details={"source": "email_token", "reason": "expired"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the dashboard lane's expired branch
        return HTMLResponse(
            content=_token_response_page(
                "⏰ Proposal Expired",
                "This proposal expired and can no longer be approved from this link. "
                "Ask Vesper Bot to draft it again for a fresh review.",
                "error",
            ),
            status_code=409,
        )
    # PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md) — decision-8 consult on the email-token lane;
    # same 409 refusal shape as the expired branch above. The action stays PENDING.
    if action.plan_step_id is not None and plan_status_blocks_execution(db, action.plan_step_id, realm_id=action.company_id):
        _audit_action_review(
            db,
            action=action,
            user_email=reviewer,
            audit_action="APPROVE_ACTION_FAILED",
            details={"source": "email_token", "reason": "plan_blocked"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the expired branch on this lane
        return HTMLResponse(
            content=_token_response_page(
                "⛔ Plan No Longer Active",
                "This proposal belongs to an agent plan that is paused, cancelled, "
                "expired, or failed — it can no longer be approved from this link. "
                "Review the plan in the dashboard to proceed.",
                "error",
            ),
            status_code=409,
        )
    if not claim_action_transition(  # AUTH-OK: ProposedAction workflow-state transition, not an access-control mutation
        db, action_id=action.id, from_status="PENDING", to_status="APPROVED",
        reviewed_by=f"email_token:{reviewer}", expected_revision=token_row.proposal_revision, approver_user_id=approver_user_id,
    ):
        db.rollback()
        return HTMLResponse(
            content=_token_response_page(
                "ℹ️ Already Processed",
                ("This action was processed by another request. Review the action and its execution evidence in the dashboard."
                 if action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES else
                 "This action has already been processed. No further action is needed."),
                "info",
            )
        )
    token_row.used_at = sqlfunc.now()
    token_row.used_by = reviewer
    is_internal_db = action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES
    # Internal-DB and QBO-executable types run their executor below; only a
    # genuinely review-only type (neither executable class) is stamped
    # review-only here. Previously the internal-DB types fell into this branch
    # (they are not is_executable_action_type), so their execution_result was
    # overwritten to review_only and their approval silently no-op'd — the
    # stuck-APPROVED class this fix closes.
    if not is_internal_db and not is_executable_action_type(action.action_type):
        _mark_review_only_approved(action)
    db.add(AuditEntry(
        company_id=action.company_id,
        user_email=reviewer,
        action="APPROVE_ACTION_EMAIL_TOKEN",
        entity_type="proposed_action",
        entity_id=str(action.id),
        details={"decision": "APPROVED", "approval_token_id": token_row.id},
    ))
    db.commit()

    # Internal-DB actions (the 13-type forecast/close-side write class) execute
    # through the C1-gated shared core — the SAME core the dashboard lane and the
    # remediation script use — so a one-click email approval actually persists the
    # write behind C1's period/suspension gates instead of silently no-op'ing.
    # The core records the honest EXECUTED/FAILED status + audit itself; a gate
    # refusal returns (False, reason) with the row FAILED and no mutation. Render
    # the honest page from that outcome — never a blanket success.
    if is_internal_db:
        try:
            outcome = execute_internal_db_action_core(
                db, action, actor_email=reviewer, failure_audit_source="email_token",
            )
            succeeded, message = outcome
        except InternalActionNotWiredError:
            logger.exception("Post-token internal-DB execution unwired for action_id=%s", action.id)
            return HTMLResponse(content=_token_response_page("⚠️ Could Not Apply Change",
                "The action was approved but could not be applied automatically. "
                "Please log in to the dashboard to review.", "error"))
        except Exception:
            db.rollback()
            logger.exception("Post-token internal execution outcome unavailable")
            return HTMLResponse(content=_token_response_page("⚠️ Review Required", REVIEW_MESSAGE, "error"))
        if not succeeded:
            title = "⚠️ Review Required" if isinstance(outcome, InternalExecutionOutcome) and outcome.execution_status in {"recovery_required", "blocked"} else "⚠️ Could Not Apply Change"
            return HTMLResponse(content=_token_response_page(title, message, "error"))
        return HTMLResponse(content=_token_response_page("✅ Action Approved",
            "The action has been approved and applied. You can close this tab.", "success"))

    if is_executable_action_type(action.action_type):
        # The QBO executor RETURNS (not raises) a FAILED action on a rejected write,
        # and raises only on an operational error (e.g. already-running). Inspect the
        # resulting status and render the honest page — the old code always claimed
        # "submitted to QuickBooks" even when the write FAILED.
        try:
            executed_action = execute_proposed_action(db, action.id)
        except HTTPException as exc:
            # AC-E1 (D-B6-2, 2026-07-16): a 409 period_locked (guarded BEFORE any
            # write — nothing reached QBO) is a clean refusal, not a QBO submission
            # failure. Render an honest closed-period page rather than mislabeling it.
            if exc.status_code == 409:
                return HTMLResponse(content=_token_response_page("⚠️ Period Closed",
                    "This entry is dated in a locked accounting period, so it was not "
                    "posted. Ask an admin to unlock the period, or enter the corrective "
                    "amount in an open month.", "error"))
            logger.warning(
                "Post-token approval refused for action_id=%s (status %s)", action.id, exc.status_code
            )
            return HTMLResponse(content=_token_response_page("⚠️ Could Not Submit to QuickBooks",
                "The action was approved but could not be submitted to QuickBooks. "
                "Please log in to the dashboard to review.", "error"))
        except Exception:
            logger.exception("Post-token approval execution failed for action_id=%s", action.id)
            return HTMLResponse(content=_token_response_page("⚠️ Could Not Submit to QuickBooks",
                "The action was approved but could not be submitted to QuickBooks. "
                "Please log in to the dashboard to review.", "error"))
        if executed_action.status == "FAILED":
            return HTMLResponse(content=_token_response_page("⚠️ Could Not Submit to QuickBooks",
                "The action was approved but QuickBooks rejected the change, so nothing was posted. "
                "Please log in to the dashboard to review.", "error"))
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R1/B1 — the executor now keeps
        # RETRYABLE (never-processed) and UNCERTAIN (outcome-unknown) actions APPROVED instead
        # of terminal FAILED; a binary FAILED-vs-success page would render those as "submitted
        # to QuickBooks" — the exact dishonesty this lane was previously fixed for. Render the
        # honest intermediate states.
        execution_status = str(((executed_action.execution_result or {}).get("execution_status")) or "")
        if execution_status == "uncertain":
            return HTMLResponse(content=_token_response_page("⏳ Outcome Unconfirmed",
                "The approval was recorded, but QuickBooks did not confirm whether the change "
                "was applied (the connection failed mid-request). Vesper Bot checks QuickBooks "
                "for it before any retry and does not post it again if it landed; if that "
                "cannot be confirmed, the dashboard asks you to check QuickBooks. "
                "Check the dashboard later for the outcome.", "info"))
        if execution_status == "retryable_failed":
            return HTMLResponse(content=_token_response_page("⏳ Temporarily Not Applied",
                "The approval was recorded, but QuickBooks temporarily refused the request "
                "(it was not processed). The action remains approved and will be retried "
                "automatically. Check the dashboard later for the outcome.", "info"))
        return HTMLResponse(content=_token_response_page("✅ Action Approved",
            "The action has been approved and submitted to QuickBooks. You can close this tab.", "success"))

    return HTMLResponse(content=_token_response_page("✅ Action Approved",
        "The action has been approved for manual follow-up. No QuickBooks changes were made automatically.", "success"))


@router.get("/reject-token/{token}", response_class=HTMLResponse)
def reject_via_token(token: str, db: Session = Depends(get_db)):
    """
    Render the reject confirmation page linked from an approval email.

    This GET handler NEVER mutates state — it only validates the signed token and
    renders an HTML page with a POST form that carries the token. The actual
    rejection happens in ``reject_via_token_submit`` when the user clicks Reject.
    """
    action, _token_row, error = _validate_token_for_confirm(db, token, "REJECTED")
    if error is not None:
        return error
    return HTMLResponse(content=_token_confirm_page(action, token, "REJECTED"))


@router.post("/reject-token/{token}", response_class=HTMLResponse)
def reject_via_token_submit(token: str, db: Session = Depends(get_db)):
    """
    Apply a one-click email rejection. Authorization is the signed, single-use
    token itself (no session). Re-clicking a used token renders a friendly
    "already processed" page rather than erroring.
    """
    action, token_row, error = _validate_token_for_confirm(db, token, "REJECTED")
    if error is not None:
        return error

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-ATOMIC: the email-token
    # reject lane joins the F-07 conditional-UPDATE net (it was a plain ORM assignment after a
    # read-then-check). Expiry is deliberately NOT checked here — rejecting a stale draft is
    # always safe.
    reviewer = token_row.recipient_email or "email_token"
    if not claim_action_transition(  # AUTH-OK: ProposedAction workflow-state transition, not an access-control mutation
        db, action_id=action.id, from_status="PENDING", to_status="REJECTED",
        reviewed_by=f"email_token:{reviewer}",
    ):
        db.rollback()
        return HTMLResponse(
            content=_token_response_page(
                "ℹ️ Already Processed",
                ("This action was processed by another request. Review the action and its execution evidence in the dashboard."
                 if action.action_type in INTERNAL_DB_EXECUTABLE_ACTION_TYPES else
                 "This action has already been processed. No further action is needed."),
                "info",
            )
        )
    action.execution_result = {"reviewer_comment": "Rejected via email link"}
    token_row.used_at = sqlfunc.now()
    token_row.used_by = reviewer
    db.add(AuditEntry(
        company_id=action.company_id,
        user_email=reviewer,
        action="REJECT_ACTION_EMAIL_TOKEN",
        entity_type="proposed_action",
        entity_id=str(action.id),
        details={"decision": "REJECTED", "approval_token_id": token_row.id},
    ))
    db.commit()

    return HTMLResponse(content=_token_response_page("✅ Action Rejected",
        "The action has been rejected. No changes were made to QuickBooks.", "success"))


def _token_confirm_page(action: ProposedAction, token: str, decision: str) -> str:
    """
    Confirmation page rendered on GET. Contains a POST form that carries the
    (URL-encoded) signed token back to the matching POST endpoint. No state is
    changed until the user submits this form.
    """
    summary = _confirm_summary(action)
    is_approve = decision == "APPROVED"
    verb = "Approve" if is_approve else "Reject"
    accent = "#27ae60" if is_approve else "#e74c3c"
    icon = "✅" if is_approve else "❌"
    # The token is already URL-encoded; it is safe inside an action="" path.
    # html-escape every interpolated value to avoid HTML injection.
    safe_token = _html.escape(token, quote=True)
    action_type = _html.escape(str(summary["action_type"]))
    company = _html.escape(str(summary["company"]))
    amount = _html.escape(str(summary["amount"]))
    proposer = _html.escape(str(summary["proposer"]))
    summary_text = _html.escape(str(summary["summary"]))
    return f"""<!DOCTYPE html>
<html><head><title>{verb} Action — Vesper Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:Arial,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f0f4f8;}}
.card{{background:white;border-radius:12px;padding:40px;max-width:520px;width:90%;box-shadow:0 4px 24px rgba(0,0,0,0.1);}}
.badge{{display:inline-block;background:#1e3a5f;color:white;padding:6px 16px;border-radius:20px;font-size:12px;margin-bottom:16px;}}
h1{{color:#1e3a5f;font-size:22px;margin:0 0 16px;}}
table{{width:100%;border-collapse:collapse;margin:16px 0;}}
th{{text-align:left;color:#888;font-size:12px;padding:6px 8px;width:120px;vertical-align:top;}}
td{{color:#333;font-size:14px;padding:6px 8px;}}
.summary{{background:#f8f9fa;border-left:4px solid #1e3a5f;padding:12px;margin:16px 0;font-size:13px;color:#555;}}
button{{background:{accent};color:white;border:none;padding:14px 32px;border-radius:6px;font-size:15px;font-weight:bold;cursor:pointer;width:100%;}}
.note{{margin-top:16px;font-size:12px;color:#999;}}
</style></head>
<body><div class="card">
  <div class="badge">🦅 Vesper Bot</div>
  <h1>Confirm: {verb} this action?</h1>
  <table>
    <tr><th>Type</th><td>{action_type}</td></tr>
    <tr><th>Company</th><td>{company}</td></tr>
    <tr><th>Amount</th><td>{amount}</td></tr>
    <tr><th>Proposed by</th><td>{proposer}</td></tr>
  </table>
  <div class="summary">{summary_text}</div>
  <form method="POST" action="{safe_token}">
    <button type="submit">{icon} {verb}</button>
  </form>
  <p class="note">Clicking {verb} will {verb.lower()} this action. This link can be used once and expires in 7 days.</p>
</div></body></html>"""


def _token_response_page(title: str, message: str, status: str = "info") -> str:
    color = {"success": "#27ae60", "error": "#e74c3c", "info": "#1e3a5f"}.get(status, "#1e3a5f")
    return f"""<!DOCTYPE html>
<html><head><title>{title} — Vesper Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:Arial,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f0f4f8;}}
.card{{background:white;border-radius:12px;padding:40px;max-width:480px;width:90%;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,0.1);}}
h1{{color:{color};font-size:22px;}} p{{color:#555;font-size:15px;line-height:1.6;}} .badge{{display:inline-block;background:{color};color:white;padding:6px 16px;border-radius:20px;font-size:12px;margin-bottom:16px;}}
</style></head>
<body><div class="card"><div class="badge">🦅 Vesper Bot</div><h1>{title}</h1><p>{message}</p></div></body></html>"""


# Forward-only governed recovery: no caller-supplied actor, finding, or source.
class RecoveryPreparationRequest(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    execution_id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    evidence_locator: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class RecoveryDecisionRequest(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _recovery_read_summary(db, action):
    from ..services.internal_action_reconciliation import inspect_recovery
    return inspect_recovery(db, action) if action.action_type in INTERNAL_DB_ACTION_TYPES else None


def _recovery_action(db, user, action_id, minimum):
    # Existing platform access auditing can commit: do it before recovery locks.
    return check_entity_tenant_access(minimum, db=db, user=user,
        model=ProposedAction, entity_id=action_id, not_found_detail="Action not found")


@router.get("/{action_id}/recovery")
def get_action_recovery(action_id: int, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user_from_token)):
    from ..services.internal_action_reconciliation import authorize, inspect_recovery, refuse
    action = _recovery_action(db, user, action_id, "ACCOUNTING")
    try:
        authorize(db, action.company_id, user.id, "ACCOUNTING")
        return inspect_recovery(db, action)
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        refuse("recovery_durable_state_unavailable", 503)


@router.post("/{action_id}/recovery/proposals")
def prepare_action_recovery(action_id: int, body: RecoveryPreparationRequest,
                            db: Session = Depends(get_db), user: User = Depends(get_current_user_from_token)):
    from ..services.internal_action_reconciliation import prepare_recovery
    action = _recovery_action(db, user, action_id, "TENANT_ADMIN")
    return prepare_recovery(db, company_id=action.company_id, action_id=action.id,
        actor_id=user.id, **body.model_dump())


@router.post("/{action_id}/recovery/proposals/{case_id}/confirm")
def confirm_action_recovery(action_id: int, case_id: int, body: RecoveryDecisionRequest,
                            db: Session = Depends(get_db), user: User = Depends(get_current_user_from_token)):
    from ..services.internal_action_reconciliation import decide_recovery
    action = _recovery_action(db, user, action_id, "TENANT_ADMIN")
    return decide_recovery(db, company_id=action.company_id, action_id=action.id, case_id=case_id,
        actor_id=user.id, candidate_digest=body.candidate_digest)


@router.post("/{action_id}/recovery/proposals/{case_id}/reject")
def reject_action_recovery(action_id: int, case_id: int, body: RecoveryDecisionRequest,
                           db: Session = Depends(get_db), user: User = Depends(get_current_user_from_token)):
    from ..services.internal_action_reconciliation import decide_recovery
    action = _recovery_action(db, user, action_id, "TENANT_ADMIN")
    return decide_recovery(db, company_id=action.company_id, action_id=action.id, case_id=case_id,
        actor_id=user.id, candidate_digest=body.candidate_digest, reject=True)

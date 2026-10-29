import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict  # noqa: F401

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ProposedAction, QBOActionExecution
from ..qbo_client import get_qbo_client_from_db
# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4/B1 — expiry predicate (INV-EXPIRE)
# + typed uncertain-outcome error and the JE reconciliation probe (INV-UNCERTAIN)
from ..qbo_errors import QBOUncertainOutcomeError
from .journal_entries import find_posted_journal_entry, stamp_journal_entry_payload
from .probe_paging import probe_or_uncertain
from .transitions import action_is_expired
from ..services.errors import error_envelope, safe_error_detail, user_safe_http_exception
from ..services.redaction import redact_sensitive, redact_to_string
from ..services.tenant_companies import company_is_suspended, organization_company_for_realm, organization_for_company
from ..tenant_rls import tenant_scope_for_realm

# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C1 (marker RATIFICATION-2026-07-13.md) — AR lane (AC-3)
from .ar_actions import AR_EXECUTABLE_ACTION_TYPES, closed_period_context, execute_ar_action
# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk C (marker RATIFICATION-2026-07-13.md) — BILL_PAY arm (AC-1/AC-3)
from .ap_actions import AP_EXECUTABLE_ACTION_TYPES, execute_bill_pay_action
from .journal_entries import create_journal_entry
from .je_lines import prepare_journal_entry_payload
from .reclassifications import reclassify_transaction
from .reconciliations import reconcile_transaction
from .refusals import QBOActionRefused
from .vendors import update_vendor

logger = logging.getLogger(__name__)

EXECUTABLE_ACTION_TYPES = (
    frozenset({"POST_JE", "JOURNAL_ENTRY", "RECLASSIFY_TXN", "RECONCILE", "UPDATE_VENDOR"})
    | AR_EXECUTABLE_ACTION_TYPES
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk C (marker RATIFICATION-2026-07-13.md) — AC-1 union
    | AP_EXECUTABLE_ACTION_TYPES
)

# Legacy action-type strings still present on older ProposedAction rows (and
# accepted by the legacy /proposals create API). The executor's canonical set
# is EXECUTABLE_ACTION_TYPES; everything entering the executor is normalized
# through canonical_action_type so legacy-typed proposals dispatch correctly
# instead of being misclassified as review-only.
ACTION_TYPE_ALIASES = {
    "RECLASSIFICATION": "RECLASSIFY_TXN",
    "VENDOR_UPDATE": "UPDATE_VENDOR",
}


def canonical_action_type(action_type: str | None) -> str:
    upper = str(action_type or "").upper()
    return ACTION_TYPE_ALIASES.get(upper, upper)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_executable_action_type(action_type: str | None) -> bool:
    return canonical_action_type(action_type) in EXECUTABLE_ACTION_TYPES


def _canonical_json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def _execution_idempotency_key(action: ProposedAction, action_type: str) -> str:
    raw = _canonical_json(
        {
            "action_id": action.id,
            "company_id": action.company_id,
            "action_type": action_type,
            "payload": action.payload or {},
        }
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _provider_request_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in ("request_id", "requestId", "Request-Id", "intuit_tid", "intuit_tid_header"):
        value = result.get(key)
        if value:
            return str(value)
    headers = result.get("headers")
    if isinstance(headers, dict):
        for key in ("intuit_tid", "Request-Id", "request-id"):
            value = headers.get(key)
            if value:
                return str(value)
    return None


def _payload_before_state(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("before_state") or payload.get("before") or payload.get("current_state")
    return None


def _is_retryable_error(exc: Exception) -> bool:
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R1 — tightened classification.
    # The old text markers included bare digits ("429","500","502","503","504"), which
    # misclassified a CONFIRMED validation reject whose fault TEXT merely contained the
    # digits (e.g. "QBO 500: ValidationFault account inactive") as retryable — and since
    # R1 keeps retryable actions APPROVED, that overmatch would retry a permanent failure
    # forever. Structured status codes stay authoritative; text markers are now explicit
    # transient phrases only. (JE-path timeouts/5xx never reach here — they raise the
    # typed QBOUncertainOutcomeError upstream.)
    status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in ("rate limit", "too many requests", "temporarily unavailable", "timed out", "timeout"))


def _action_scope(db: Session, action: ProposedAction):
    organization = organization_for_company(db, action.company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=action.company_id)
        if organization
        else None
    )
    return organization, organization_company


def _claim_execution(db: Session, action: ProposedAction, action_type: str) -> QBOActionExecution:
    idempotency_key = _execution_idempotency_key(action, action_type)
    existing = (
        db.query(QBOActionExecution)
        .filter(
            QBOActionExecution.company_id == action.company_id,
            QBOActionExecution.idempotency_key == idempotency_key,
        )
        .first()
    )
    if existing:
        return existing

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B1 — INV-UNCERTAIN edit guard: if a
    # PRIOR attempt on this action ended `uncertain` under a DIFFERENT key (the payload was
    # edited afterward), executing the new payload could double-book on top of a QBO-side
    # already-applied original. Refuse until the uncertain row is reconciled (retried under
    # its own key, which runs the reconcile probe) or resolved manually.
    prior_uncertain = (
        db.query(QBOActionExecution)
        .filter(
            QBOActionExecution.company_id == action.company_id,
            QBOActionExecution.action_id == action.id,
            QBOActionExecution.status == "uncertain",
        )
        .first()
    )
    if prior_uncertain is not None:
        raise QBOUncertainOutcomeError(
            f"Action {action.id} has a prior UNCERTAIN QuickBooks attempt "
            f"(execution {prior_uncertain.id}). Reconcile that attempt before executing an "
            "edited payload — executing now could double-post."
        )

    organization, organization_company = _action_scope(db, action)
    execution = QBOActionExecution(
        action_id=action.id,
        idempotency_key=idempotency_key,
        company_id=action.company_id,
        qbo_realm_id=action.company_id,
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        action_type=action_type,
        status="pending",
        attempts=0,
        before_state_json=redact_sensitive(_payload_before_state(action.payload)),
        request_summary_json=redact_sensitive(action.payload or {}),
    )
    db.add(execution)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(QBOActionExecution)
            .filter(QBOActionExecution.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing
        raise
    return execution


def _mark_execution_running(db: Session, execution: QBOActionExecution) -> None:
    """Only the winner of the observed attempt's CAS may cross the writer seam."""
    now = _utcnow()
    if execution.status not in {"pending", "failed", "retryable_failed", "uncertain"}:
        raise RuntimeError(f"QBO execution {execution.id} cannot be claimed from {execution.status}.")
    result = db.execute(update(QBOActionExecution).where(
        QBOActionExecution.id == execution.id,
        QBOActionExecution.company_id == execution.company_id,
        QBOActionExecution.status == execution.status,
        QBOActionExecution.attempts == execution.attempts,
    ).values(status="running", attempts=QBOActionExecution.attempts + 1,
             locked_at=now, started_at=now, finished_at=None, error=None)
      .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise RuntimeError(f"QBO execution {execution.id} was claimed by another worker.")
    db.commit()
    db.refresh(execution)


def _mark_execution_success(
    db: Session,
    *,
    action: ProposedAction,
    execution: QBOActionExecution,
    result: Any,
) -> ProposedAction:
    safe_result = redact_sensitive(result)
    execution.status = "succeeded"
    execution.provider_request_id = _provider_request_id(result)
    execution.after_state_json = safe_result
    execution.response_summary_json = safe_result
    execution.error = None
    execution.finished_at = _utcnow()
    action.status = "EXECUTED"
    action.execution_result = {
        "execution_status": "succeeded",
        "qbo_action_execution_id": execution.id,
        "idempotency_key": execution.idempotency_key,
        "provider_request_id": execution.provider_request_id,
        "provider_response": safe_result,
    }
    db.commit()
    db.refresh(action)
    return action


def uncertain_execution_result(execution: QBOActionExecution) -> dict:
    """The action's ``execution_result`` while its QuickBooks outcome is unknown.

    The action stays APPROVED; the recovery lane (``qbo_actions.recovery``)
    re-runs it under the same idempotency key, and the reconcile probe decides
    whether the earlier attempt landed before anything is posted again.
    """
    return {
        "execution_status": "uncertain",
        "qbo_action_execution_id": execution.id,
        "idempotency_key": execution.idempotency_key,
        "error": error_envelope(
            error_code="uncertain_external_state",
            user_message=(
                "QuickBooks may or may not have applied this action — the connection "
                "failed after the request was sent. Vesper Bot checks QuickBooks for it "
                "before any retry; do not re-draft this action."
            ),
            retryable=True,
        ),
    }


def _mark_execution_failure(
    db: Session,
    *,
    action: ProposedAction,
    execution: QBOActionExecution,
    exc: Exception,
) -> ProposedAction:
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B1 — INV-UNCERTAIN (SYNTHESIS
    # GAP-A6): an UNKNOWN external outcome (timeout/5xx after the POST was dispatched) is not
    # a confirmed failure. Before this branch, it landed FAILED with no retry lane — and a
    # human re-drafting the "failed" JE minted a NEW idempotency key, so a QBO-side
    # already-posted entry could be double-booked. Now: the ledger row records `uncertain`,
    # the action STAYS APPROVED (so re-invoking the executor reuses the SAME idempotency key),
    # and the retry path reconciles against QBO's actual ledger before any re-POST.
    error = redact_to_string(str(exc), max_length=1000)
    if isinstance(exc, QBOUncertainOutcomeError):
        execution.status = "uncertain"
        execution.error = error
        execution.finished_at = _utcnow()
        action.execution_result = uncertain_execution_result(execution)
        db.commit()
        db.refresh(action)
        return action

    status = (
        "retryable_failed"
        if not isinstance(exc, QBOActionRefused) and _is_retryable_error(exc)
        else "failed"
    )
    execution.status = status
    execution.error = error
    execution.finished_at = _utcnow()
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R1 (round-2 follow-up, merged PR #381
    # remediation row): a RETRYABLE failure (429/transient — the request was never processed by
    # QBO) previously landed terminal FAILED with `retryable: true` in the envelope but NO retry
    # lane (approve requires PENDING; re-draft mints a new idempotency key). The action now stays
    # APPROVED so the recovery lane (qbo_actions.recovery, a bounded number of attempts) re-invokes
    # the executor under the SAME idempotency key — blind retry is safe for this class precisely
    # because QBO never processed the request. Confirmed (non-retryable) failures keep today's
    # FAILED semantics.
    if status == "retryable_failed":
        action.execution_result = {
            "execution_status": status,
            "qbo_action_execution_id": execution.id,
            "idempotency_key": execution.idempotency_key,
            "error": error_envelope(
                error_code="qbo_action_execution_failed",
                user_message=(
                    "QuickBooks temporarily refused the request (it was not processed). "
                    "The action remains approved and will be retried automatically."
                ),
                retryable=True,
            ),
        }
        db.commit()
        db.refresh(action)
        return action

    action.status = "FAILED"
    # A typed pre-write refusal carries its own Vesper Bot-authored message (never
    # provider text), so the reviewer sees why instead of a generic failure.
    refused = isinstance(exc, QBOActionRefused)
    action.execution_result = {
        "execution_status": status,
        "qbo_action_execution_id": execution.id,
        "idempotency_key": execution.idempotency_key,
        "error": error_envelope(
            error_code=exc.error_code if refused else "qbo_action_execution_failed",
            user_message=(
                exc.user_message if refused else "The approved action could not be posted to QuickBooks."
            ),
            retryable=False,
        ),
    }
    db.commit()
    db.refresh(action)
    return action


def _return_success_from_ledger(db: Session, action: ProposedAction, execution: QBOActionExecution) -> ProposedAction:
    action.status = "EXECUTED"
    reversal_id = (action.execution_result or {}).get("reversed_by_action_id")
    action.execution_result = {
        "execution_status": "succeeded",
        "qbo_action_execution_id": execution.id,
        "idempotency_key": execution.idempotency_key,
        "provider_request_id": execution.provider_request_id,
        "provider_response": execution.response_summary_json,
    }
    if reversal_id is not None:
        action.execution_result["reversed_by_action_id"] = reversal_id
    db.commit()
    db.refresh(action)
    return action


def _ensure_company_can_execute(db: Session, action: ProposedAction) -> None:
    _organization, organization_company = _action_scope(db, action)
    if company_is_suspended(db, organization_company=organization_company):
        raise PermissionError("QBO write-back is blocked because this company is suspended or unpaid.")


def execute_proposed_action(db: Session, action_id: int) -> ProposedAction:
    """
    Executes a ProposedAction that was approved.
    Calls the correct module based on action_type.
    """
    action = db.query(ProposedAction).filter(ProposedAction.id == action_id).first()
    if not action:
        raise ValueError(f"Action {action_id} not found.")
        
    if action.status not in {"APPROVED", "EXECUTED"}:
        raise ValueError(f"Action {action_id} is not in APPROVED state. Current state: {action.status}")

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXPIRE defense-in-depth:
    # every approve lane now refuses expired proposals, so the only way an APPROVED row can be
    # expired-with-no-reviewer is legacy data approved before the gate existed (or a direct DB
    # write). Refuse it here rather than post a stale draft to the GL.
    if action.reviewed_at is None and action_is_expired(action):
        raise ValueError(
            f"Action {action_id} expired before any recorded approval; refusing to execute a "
            "stale proposal. Re-draft it for a fresh review."
        )
        
    action_type = canonical_action_type(action.action_type)
    if not is_executable_action_type(action_type):
        raise ValueError(f"Action type {action.action_type} is review-only and has no executor.")

    if action.status == "EXECUTED":
        idempotency_key = _execution_idempotency_key(action, action_type)
        execution = (
            db.query(QBOActionExecution)
            .filter(QBOActionExecution.idempotency_key == idempotency_key)
            .first()
        )
        if execution and execution.status == "succeeded":
            return _return_success_from_ledger(db, action, execution)
        return action

    # Closed-period protection (C1): never post/modify into a HARD-locked accounting
    # period. Runs BEFORE _claim_execution so a rejected action leaves NO execution
    # ledger row — a later guard would orphan a "running" claim and block every retry.
    # The autonomy gate only covers L3 auto-exec; this funnel also catches approvals.
    # D-B6-1 (2026-07-16): the WRITE floor is the EXPLICIT hard lock (period_is_hard_locked),
    # NOT soft-CLOSED — a soft-CLOSED (auto-rolled) month is books-rolled, not write-blocked,
    # so a June-soft-close no longer blocks a posting. This aligns the QBO executor with the
    # already-hard internal executor (internal_action_execution.py:330).
    # PLAN-EXEMPT: plan 20260713-wave2-1-ar-collections C1 — decision 3: the per-type
    # date-key map + SEND exemption live in ar_actions.closed_period_context (JE 'date'
    # parsing byte-equivalent; AR creates parse 'TxnDate' — else UNGUARDED via the
    # fail-OPEN dateless fallback, task_autonomy.py:266-276).
    from ..services.task_autonomy import period_is_hard_locked
    period_exempt, closed_when = closed_period_context(action_type, action.payload)
    if not period_exempt and period_is_hard_locked(db, realm_id=action.company_id, when=closed_when):
        raise user_safe_http_exception(
            status_code=409,
            error_code="period_locked",
            user_message="The accounting period is locked; this entry can't be posted.",
        )

    execution = _claim_execution(db, action, action_type)
    if execution.status == "succeeded":
        return _return_success_from_ledger(db, action, execution)
    # Historical successes remain readable; unknown preparation cannot authorize
    # any new provider side effect, even through direct retry/executor entry.
    from ..services.proposal_provenance import validate_material_execution
    validate_material_execution(action)
    if execution.status == "running":
        raise RuntimeError(f"QBO action {action_id} is already running.")

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B1 — INV-UNCERTAIN retry path:
    # remember whether the prior attempt on this SAME idempotency key ended uncertain, so a
    # JE retry reconciles against QBO before any re-POST (double-post protection). Non-JE
    # types (reclassify/reconcile/vendor) re-apply the same target values — idempotent.
    prior_attempt_uncertain = execution.status == "uncertain"
    # The probes' time window (JE CreateTime, reminder DeliveryTime) must cover EVERY
    # attempt under this key, not only the last one: an attempt that itself stayed
    # uncertain (it could not reach QuickBooks, or its probe was ambiguous) restarted
    # started_at, and a window from there would hide an earlier attempt's landed post.
    # The ledger row is created just before the first attempt.
    prior_started_at = (execution.created_at or execution.started_at) if prior_attempt_uncertain else None
    _mark_execution_running(db, execution)

    try:
        try:
            _ensure_company_can_execute(db, action)
            client = get_qbo_client_from_db(db, action.company_id)
        except Exception as exc:
            if not prior_attempt_uncertain:
                raise
            # Nothing reached QuickBooks on THIS attempt, so the earlier attempt's
            # outcome is still unknown: it must stay uncertain (never FAILED, which
            # would invite a re-draft and a double post).
            raise QBOUncertainOutcomeError(
                "Retry could not reach QuickBooks; the earlier attempt is still unconfirmed "
                f"({type(exc).__name__})."
            ) from exc
        if action_type in ("POST_JE", "JOURNAL_ENTRY"):
            # Every post carries "Vesper Bot proposal #<id>" in its PrivateNote, so an
            # uncertain post is reconciled by identity inside the attempt's window.
            if prior_attempt_uncertain:
                reconciled = probe_or_uncertain(
                    find_posted_journal_entry, client, action.payload, prior_started_at, action.id
                )
                if reconciled is not None:
                    logger.info(
                        "[executor] action %s reconciled from uncertain prior post — no re-POST",
                        action.id,
                    )
                    return _mark_execution_success(db, action=action, execution=execution, result=reconciled)
            # Every line must name an active account of this realm, and an A/R or
            # A/P line must carry its Customer / Vendor (resolved to an id here for
            # legacy name-only drafts); a refusal is recorded before any POST.
            with tenant_scope_for_realm(db, action.company_id):
                postable = prepare_journal_entry_payload(db, action.company_id, action.payload)
            result = create_journal_entry(client, stamp_journal_entry_payload(postable, action.id))  # IDEMPOTENT-OK: claimed lines above via _claim_execution + status=="running" guard; this edit only adds the uncertain-reconcile branch ahead of the unchanged call
        elif action_type == "RECLASSIFY_TXN":
            result = reclassify_transaction(client, action.payload)
        elif action_type == "RECONCILE":
            result = reconcile_transaction(client, action.payload)
        elif action_type == "UPDATE_VENDOR":
            result = update_vendor(client, action.payload)
        elif action_type in AR_EXECUTABLE_ACTION_TYPES:
            # PLAN-EXEMPT: plan 20260713-wave2-1-ar-collections C1 — AR arms dispatch
            # (probe-first on uncertain; SEND arm takes db for the decision-7 throttle).
            result = execute_ar_action(  # IDEMPOTENT-OK: claimed lines above via _claim_execution + status=="running" guard — the same funnel the sibling JE/reclassify arms ride
                client=client,
                db=db,
                action=action,
                action_type=action_type,
                prior_attempt_uncertain=prior_attempt_uncertain,
                prior_started_at=prior_started_at,
            )
        elif action_type in AP_EXECUTABLE_ACTION_TYPES:
            # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk C (marker
            # RATIFICATION-2026-07-13.md) — BILL_PAY dispatch. No prior_attempt flag needed:
            # the shared core probes every extant per-bill claim on encounter (decision 3).
            result = execute_bill_pay_action(  # IDEMPOTENT-OK: claimed lines above via _claim_execution + status=="running" guard, PLUS the per-bill BillPaymentExecution claim ledger inside the shared core
                client=client,
                db=db,
                action=action,
            )
        else:
            raise ValueError(f"Unknown action_type: {action.action_type}")
            
        return _mark_execution_success(db, action=action, execution=execution, result=result)
        
    except Exception as e:
        logger.exception(f"Failed to execute action {action_id}")
        return _mark_execution_failure(db, action=action, execution=execution, exc=e)


def execute_all_approved(db: Session, company_id: str) -> list:
    """
    Finds all APPROVED proposed actions for a company and executes them
    sequentially. Returns a list of result summaries.

    Individual failures do NOT abort the batch — each action is handled
    independently so one bad payload doesn't block the rest.
    """
    actions = (
        db.query(ProposedAction)
        .filter(
            ProposedAction.company_id == company_id,
            ProposedAction.status == "APPROVED",
        )
        .order_by(ProposedAction.id.asc())
        .all()
    )

    logger.info(f"Batch executing {len(actions)} approved actions for company {company_id}")
    results = []

    for action in actions:
        if not is_executable_action_type(action.action_type):
            results.append({
                "action_id": action.id,
                "status": action.status,
                "result": {
                    "execution_mode": "review_only",
                    "message": "Review-only action skipped by QBO executor.",
                },
            })
            continue
        try:
            executed = execute_proposed_action(db, action.id)
            results.append({
                "action_id": executed.id,
                "status": executed.status,
                "result": executed.execution_result,
            })
        except HTTPException as exc:
            # AC-E1 (D-B6-2, 2026-07-16): a 409 period_locked is a clean refusal (the
            # action is dated in a HARD-locked period), NOT a batch execution failure —
            # record it truthfully as SKIPPED rather than mislabeling it FAILED.
            if exc.status_code == 409:
                results.append({
                    "action_id": action.id,
                    "status": "SKIPPED",
                    "result": {"execution_status": "skipped_period_locked", "error": exc.detail},
                })
            else:
                results.append({
                    "action_id": action.id,
                    "status": "FAILED",
                    "result": {"execution_status": "failed", "error": exc.detail},
                })
        except Exception as e:
            logger.error(
                "Batch: action %s raised: %s",
                action.id,
                redact_to_string(str(e), max_length=500),
            )
            results.append({
                "action_id": action.id,
                "status": "FAILED",
                "result": {
                    "execution_status": "failed",
                    "error": safe_error_detail(
                        error_code="qbo_action_batch_execution_failed",
                        user_message="The approved action could not be executed.",
                        exc=e,
                        retryable=_is_retryable_error(e),
                        log_context="qbo_action_batch_execution_failed",
                    ),
                },
            })

    return results

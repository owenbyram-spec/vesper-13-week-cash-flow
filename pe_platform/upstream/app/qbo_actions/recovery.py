"""Recovery lane for QBO write-backs the executor left APPROVED.

The executor keeps an action APPROVED when its QuickBooks outcome is not final:

* ``retryable_failed`` — QuickBooks refused the request without processing it
  (429 / 5xx), so a blind retry is safe;
* ``uncertain`` — the request may have landed (timeout / 5xx after the POST was
  sent); the arm's reconcile probe must decide before anything is re-posted;
* ``running`` left behind by a worker or web process that died mid-write. No
  later call can claim such a row, so the reaper first moves a row that has been
  running longer than ``RUNNING_STALE_SECONDS`` to ``uncertain``.

``recover_realm`` re-invokes ``execute_proposed_action`` for those actions,
under the SAME idempotency key (the executor recomputes it from the unchanged
approved payload and reuses the ledger row), so the uncertain path runs its
probe and a landed post is adopted rather than posted twice. Attempts are
bounded by ``MAX_ATTEMPTS`` (the ledger's ``attempts`` counter, the first
attempt included). When they run out:

* a ``retryable_failed`` action is marked FAILED (QuickBooks never processed it);
* an ``uncertain`` action stays APPROVED/uncertain and its result says plainly
  that a human must check QuickBooks; it is never marked FAILED, because a
  re-draft could then post it twice.

Every write here runs inside the realm's tenant scope (the Celery wrapper in
``app/tasks/qbo_action_recovery.py`` pins it per realm). A suspended or unpaid
company is skipped entirely: the executor would refuse it anyway.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import _env_int
from ..models import ProposedAction, QBOActionExecution
from ..qbo_client import REQUEST_TIMEOUT_SECONDS
from ..services import audit
from ..services.errors import error_envelope
from ..services.redaction import redact_to_string
from ..services.tenant_companies import (
    company_is_suspended,
    organization_company_for_realm,
    organization_for_company,
)
from .executor import (
    _execution_idempotency_key,
    canonical_action_type,
    execute_proposed_action,
    is_executable_action_type,
    uncertain_execution_result,
)

logger = logging.getLogger(__name__)

RECOVERABLE_STATUSES = ("retryable_failed", "uncertain")

# A single QuickBooks call can take a few minutes in the worst case (a 60 s
# per-realm semaphore wait, then up to 4 attempts of REQUEST_TIMEOUT_SECONDS each
# with capped backoff), and one action may make several calls (a bill-payment
# batch makes one per bill). A row is reaped only well past that, so a slow but
# live attempt is never declared dead while it can still post.
RUNNING_STALE_SECONDS = max(
    _env_int("QBO_ACTION_RUNNING_STALE_SECONDS", 3600),
    10 * REQUEST_TIMEOUT_SECONDS,
)
MAX_ATTEMPTS = max(_env_int("QBO_ACTION_MAX_ATTEMPTS", 5), 2)

_REAPED_ERROR = (
    "Execution did not finish: the process running it stopped before recording an outcome. "
    "Treated as uncertain; the reconcile probe decides on the next attempt."
)


@dataclass
class RealmRecovery:
    realm_id: str
    reaped: int = 0
    rerun: int = 0
    succeeded: int = 0
    failed: int = 0
    still_pending: int = 0
    exhausted: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stale_running_cutoff(now: datetime | None = None) -> datetime:
    return (now or _utcnow()) - timedelta(seconds=RUNNING_STALE_SECONDS)


def realms_needing_recovery(db: Session, now: datetime | None = None) -> list[str]:
    """Realms with a stale ``running`` row or a re-runnable APPROVED action.

    Reads across tenants: the caller pins a system bypass scope for this census
    only; every write happens later under the realm's own scope.
    """
    cutoff = stale_running_cutoff(now)
    stale = {
        row[0]
        for row in db.query(QBOActionExecution.company_id)
        .filter(
            QBOActionExecution.status == "running",
            QBOActionExecution.started_at < cutoff,
        )
        .distinct()
        .all()
    }
    pending = {
        row[0]
        for row in db.query(QBOActionExecution.company_id)
        .join(ProposedAction, ProposedAction.id == QBOActionExecution.action_id)
        .filter(
            ProposedAction.company_id == QBOActionExecution.company_id,
            ProposedAction.status == "APPROVED",
            QBOActionExecution.status.in_(RECOVERABLE_STATUSES),
            QBOActionExecution.attempts < MAX_ATTEMPTS,
        )
        .distinct()
        .all()
    }
    return sorted(stale | pending)


def reap_stale_running(db: Session, realm_id: str, now: datetime | None = None) -> int:
    """Move this realm's ``running`` rows older than the cutoff to ``uncertain``."""
    now = now or _utcnow()
    rows = (
        db.query(QBOActionExecution)
        .filter(
            QBOActionExecution.company_id == realm_id,
            QBOActionExecution.status == "running",
            QBOActionExecution.started_at < stale_running_cutoff(now),
        )
        .order_by(QBOActionExecution.id.asc())
        .all()
    )
    reaped = 0
    for execution in rows:
        # Conditional on the observed attempt, so a row a live worker re-claimed
        # in the meantime is never clobbered.
        flipped = (
            db.query(QBOActionExecution)
            .filter(
                QBOActionExecution.id == execution.id,
                QBOActionExecution.company_id == realm_id,
                QBOActionExecution.status == "running",
                QBOActionExecution.attempts == execution.attempts,
            )
            .update(
                {
                    QBOActionExecution.status: "uncertain",
                    QBOActionExecution.error: _REAPED_ERROR,
                    QBOActionExecution.finished_at: now,
                },
                synchronize_session=False,
            )
        )
        if flipped != 1:
            continue
        db.refresh(execution)
        action = (
            db.query(ProposedAction)
            .filter(ProposedAction.id == execution.action_id, ProposedAction.company_id == realm_id)
            .first()
        )
        if action is not None and action.status == "APPROVED":
            action.execution_result = uncertain_execution_result(execution)
        audit.log_audit_event(
            db,
            company_id=realm_id,
            action="QBO_ACTION_EXECUTION_REAPED",
            entity_type="proposed_action",
            entity_id=str(execution.action_id),
            previous_state={"execution_status": "running", "attempts": execution.attempts},
            new_state={"execution_status": "uncertain", "qbo_action_execution_id": execution.id},
        )
        db.commit()
        reaped += 1
    return reaped


def _mark_exhausted(db: Session, action: ProposedAction, execution: QBOActionExecution) -> None:
    """Stop retrying and say so truthfully (see the module docstring)."""
    attempts = execution.attempts
    if execution.status == "retryable_failed":
        if canonical_action_type(action.action_type) == "BILL_PAY":
            # A bill-payment batch is retryable after a PARTIAL success: some bills
            # may be paid, so "nothing was posted" would be false.
            message = (
                f"The payment batch still had unpaid bills after {attempts} attempts and is no "
                "longer retried automatically. The batch shows which bills were paid; resubmit "
                "it from Accounts Payable if the rest are still due."
            )
        else:
            message = (
                f"QuickBooks refused this action {attempts} times without processing it, so "
                "nothing was posted. It is marked failed; draft it again if it is still needed."
            )
        execution.status = "failed"
        action.status = "FAILED"
        action.execution_result = {
            "execution_status": "failed",
            "qbo_action_execution_id": execution.id,
            "idempotency_key": execution.idempotency_key,
            "recovery_exhausted": True,
            "error": error_envelope(
                error_code="qbo_action_retries_exhausted",
                user_message=message,
                retryable=False,
            ),
        }
    else:
        if (action.execution_result or {}).get("recovery_exhausted"):
            return
        action.execution_result = {
            "execution_status": "uncertain",
            "qbo_action_execution_id": execution.id,
            "idempotency_key": execution.idempotency_key,
            "recovery_exhausted": True,
            "error": error_envelope(
                error_code="uncertain_external_state_unresolved",
                user_message=(
                    f"Peregrine could not confirm whether QuickBooks applied this action after "
                    f"{attempts} attempts and has stopped retrying. Check QuickBooks for it before "
                    "doing anything else; drafting it again could post it twice."
                ),
                retryable=False,
            ),
        }
    audit.log_audit_event(
        db,
        company_id=action.company_id,
        action="QBO_ACTION_RECOVERY_EXHAUSTED",
        entity_type="proposed_action",
        entity_id=str(action.id),
        new_state={"execution_status": execution.status, "attempts": attempts},
    )
    db.commit()


def _recoverable(db: Session, realm_id: str) -> list[tuple[QBOActionExecution, ProposedAction]]:
    rows = (
        db.query(QBOActionExecution, ProposedAction)
        .join(ProposedAction, ProposedAction.id == QBOActionExecution.action_id)
        .filter(
            QBOActionExecution.company_id == realm_id,
            ProposedAction.company_id == realm_id,
            ProposedAction.status == "APPROVED",
            QBOActionExecution.status.in_(RECOVERABLE_STATUSES),
        )
        .order_by(ProposedAction.id.asc(), QBOActionExecution.id.desc())
        .all()
    )
    seen: set[int] = set()
    selected = []
    for execution, action in rows:
        if action.id in seen:
            continue
        seen.add(action.id)
        selected.append((execution, action))
    return selected


def recover_realm(db: Session, realm_id: str, now: datetime | None = None) -> RealmRecovery:
    """Reap stale ``running`` rows, then re-run this realm's recoverable actions once."""
    result = RealmRecovery(realm_id=str(realm_id))
    organization = organization_for_company(db, realm_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=realm_id)
        if organization is not None
        else None
    )
    if company_is_suspended(db, organization_company=organization_company):
        result.skipped = len(_recoverable(db, realm_id))
        return result

    result.reaped = reap_stale_running(db, realm_id, now)

    for execution, action in _recoverable(db, realm_id):
        action_type = canonical_action_type(action.action_type)
        if not is_executable_action_type(action_type):
            result.skipped += 1
            continue
        if execution.idempotency_key != _execution_idempotency_key(action, action_type):
            # Not this payload's ledger row; the executor refuses an edited payload
            # over an uncertain attempt, so a human reconciles it.
            result.skipped += 1
            continue
        if execution.attempts >= MAX_ATTEMPTS:
            _mark_exhausted(db, action, execution)
            result.exhausted += 1
            continue

        action_id = action.id
        result.rerun += 1
        try:
            executed = execute_proposed_action(db, action_id)
        except HTTPException as exc:
            db.rollback()
            # A 409 is the hard period lock: nothing can post into that period.
            result.skipped += 1
            logger.info(
                "[qbo_action_recovery] action %s not re-run (HTTP %s)", action_id, exc.status_code
            )
            continue
        except Exception as exc:  # noqa: BLE001 — one action never aborts the realm pass
            db.rollback()
            result.errors += 1
            logger.warning(
                "[qbo_action_recovery] action %s re-run raised: %s",
                action_id,
                redact_to_string(str(exc), max_length=500),
            )
            continue

        if executed.status == "EXECUTED":
            result.succeeded += 1
        elif executed.status == "FAILED":
            result.failed += 1
        else:
            result.still_pending += 1
            db.refresh(execution)
            if execution.status in RECOVERABLE_STATUSES and execution.attempts >= MAX_ATTEMPTS:
                _mark_exhausted(db, executed, execution)
                result.exhausted += 1
    return result

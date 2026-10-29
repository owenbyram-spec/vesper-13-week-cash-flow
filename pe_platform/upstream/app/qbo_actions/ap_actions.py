# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk C (marker
# RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
# sub-agent session) — AC-3: the BILL_PAY executor arm (decisions 5/8/9/12).
"""AP payment-batch executor arm — BILL_PAY (Wave 2.2 chunk C, decisions 5/8/9/12).

Pure executor arm: invoked ONLY by ``executor.execute_proposed_action`` after the
status gate ({APPROVED, EXECUTED} refusal), the closed-period guard (decision 12 —
``"BILL_PAY": "TxnDate"`` in ``ar_actions.CLOSED_PERIOD_DATE_KEYS``), and the
SHA-256 idempotency claim. Nothing in the chat loop calls this module directly.

Find-or-create materialization (decision 5, E1-blocking): one approved proposal =
ONE ``BillPayment`` batch row, keyed on the UNIQUE-where-NOT-NULL
``bill_payments.proposed_action_id`` linkage column. A resume re-enters the SAME
batch row — same batch_id → same per-bill idempotency keys — so a retry can never
mint fresh keys and double-pay. Money safety itself lives in the FROZEN shared
core (``bill_payment_core.submit_bill_payment_batch``): per-bill claims,
probe-on-encounter, balance re-read, release-only-on-confirmed-reject.

Action-status mapping (decision 8):

- ANY unresolved money outcome (``uncertain`` / ``skipped_uncertain``) →
  ``QBOUncertainOutcomeError`` — the action lands ``uncertain`` (reconcile-first
  retry lane; a payment MAY already exist in QBO).
- all-confirmed-failed with zero paid → plain ``ValueError`` — confirmed FAILED.
- partial success / confirmed-reject remainder / a rival batch's in-flight claim →
  ``BillPayBatchRetryableError`` (``status_code=503`` so
  ``executor._is_retryable_error`` classifies it) — the action lands
  ``retryable_failed``, the ProposedAction stays APPROVED, and the resume
  re-enters the same batch.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditEntry, BillPayment, ProposedAction
from ..qbo_client import QBOClient
from ..qbo_errors import QBOUncertainOutcomeError
from .bill_payment_core import batch_status_for, submit_bill_payment_batch

logger = logging.getLogger(__name__)

AP_EXECUTABLE_ACTION_TYPES = frozenset({"BILL_PAY"})


class BillPayBatchRetryableError(Exception):
    """Typed retryable signal (decision 8): partial success or a confirmed-reject
    remainder. Carries ``status_code=503`` so ``executor._is_retryable_error``
    classifies it — the action lands ``retryable_failed`` and stays APPROVED, and
    the resume re-enters the SAME batch row (find-or-create on
    ``proposed_action_id``) under the same per-bill idempotency keys."""

    status_code = 503


def _bill_amount(bill: dict) -> float:
    """The amount the core will pay: ``balance`` falling back to ``amount``."""
    raw = (bill or {}).get("balance", (bill or {}).get("amount", 0))  # SA-OK: dict.get on a JSON bill dict, not an ORM query
    try:
        return round(float(raw or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _find_batch(db: Session, action: ProposedAction) -> BillPayment | None:
    return (
        db.query(BillPayment)
        .filter(
            BillPayment.company_id == action.company_id,
            BillPayment.proposed_action_id == action.id,
        )
        .first()
    )


def materialize_bill_payment_batch(db: Session, action: ProposedAction) -> BillPayment:
    """FIND-OR-CREATE the one batch row for this proposal (decision 5, E1).

    A resume finds the extant row (same batch_id → same per-bill idempotency
    keys); only the first invocation creates. The UNIQUE-where-NOT-NULL
    ``proposed_action_id`` column is the race gate — a concurrent creation
    surfaces as IntegrityError and the loser adopts the winner's row.
    """
    existing = _find_batch(db, action)
    if existing is not None:
        return existing

    payload = action.payload or {}
    bills = payload.get("bills") or []
    if not bills:
        raise ValueError("BILL_PAY payload carries no bills — refusing an empty payment batch.")
    payment_method = str(payload.get("payment_method") or "check").strip().lower()
    if payment_method != "check":
        raise ValueError(
            f"BILL_PAY supports payment_method 'check' only; '{payment_method}' is refused "
            "(decision 9 — no ach/wire in v1)."
        )
    account_id = str(payload.get("payment_account_id") or "").strip()
    if not account_id:
        raise ValueError("BILL_PAY payload requires 'payment_account_id'.")
    raw_txn_date = str(payload.get("TxnDate") or "").strip()
    if not raw_txn_date:
        raise ValueError("BILL_PAY payload requires 'TxnDate'.")
    try:
        payment_date = date.fromisoformat(raw_txn_date[:10])
    except ValueError as exc:
        raise ValueError(f"BILL_PAY 'TxnDate' is not a valid ISO date: {raw_txn_date!r}.") from exc

    total = round(sum(_bill_amount(bill) for bill in bills), 2)
    batch = BillPayment(
        company_id=action.company_id,
        batch_name=str(payload.get("batch_name") or f"AI payment batch (proposal #{action.id})"),
        bills=bills,
        total_amount=Decimal(str(total)),
        bill_count=len(bills),
        payment_method="check",
        payment_account_id=account_id,
        payment_date=payment_date,  # decision 9: the batch transmits the payload TxnDate
        status="APPROVED",  # AUTH-OK: payment-batch workflow status, not an access-control field; the CREATE_PAYMENT_BATCH AuditEntry below records provenance
        created_by="ai_assistant",
        approved_by=action.reviewed_by,  # decision 5: attribution = the proposal's human reviewer
        approved_at=action.reviewed_at,
        proposed_action_id=action.id,
    )
    try:
        with db.begin_nested():
            db.add(batch)  # WRITEBACK-OK: human-approval-gated — reached only via execute_proposed_action's APPROVED/EXECUTED refusal; the UNIQUE proposed_action_id linkage makes this find-or-create race-safe
    except IntegrityError:
        # A concurrent invocation materialized the row between our check and our
        # insert; the savepoint absorbed the refusal — adopt the winner's row.
        winner = _find_batch(db, action)
        if winner is not None:
            logger.info(
                "[ap_actions] batch for proposal %s already materialized as batch %s — adopting",
                action.id,
                winner.id,
            )
            return winner
        raise
    # The savepoint release flushed the INSERT — batch.id is assigned; write the
    # CREATE parity row (decision 5 / E6 — mirrors ap_workflow.py:283-290).
    db.add(
        AuditEntry(
            company_id=action.company_id,
            user_email=action.reviewed_by,
            action="CREATE_PAYMENT_BATCH",
            entity_type="bill_payment",
            entity_id=str(batch.id),
            details={
                "bill_count": len(bills),
                "total": total,
                "source": "ai_assistant",
                "proposed_action_id": action.id,
            },
        )
    )
    db.commit()
    db.refresh(batch)
    return batch


def execute_bill_pay_action(*, client: QBOClient, db: Session, action: ProposedAction) -> dict:
    """Run one approved BILL_PAY proposal through the frozen shared core.

    Materializes (find-or-create) the batch row, runs
    ``submit_bill_payment_batch``, records the batch bookkeeping + SUBMIT audit
    parity row, then maps the BatchOutcome to the decision-8 action statuses.
    """
    batch = materialize_bill_payment_batch(db, action)
    outcome = submit_bill_payment_batch(db, client, batch=batch)  # WRITEBACK-OK: approval-gated executor arm — reached only via execute_proposed_action's APPROVED/EXECUTED refusal + SHA-256 claim; the core's per-bill BillPaymentExecution ledger is the money gate
    counts = outcome["counts"]
    status = batch_status_for(counts)

    paid = counts["succeeded"] + counts["skipped_paid"]
    unresolved_uncertain = counts["uncertain"] + counts["skipped_uncertain"]
    unpaid_remainder = outcome["bill_count"] - paid

    batch.status = status  # AUTH-OK: payment-batch workflow status, not an access-control field; the SUBMIT_PAYMENT_BATCH AuditEntry below records the outcome
    batch.submitted_at = datetime.now(timezone.utc)
    batch.error_message = (
        None if status == "COMPLETED" else f"{unpaid_remainder} of {outcome['bill_count']} bills not paid"
    )
    # SUBMIT parity row (decision 5 / E6 — mirrors ap_workflow.py:503-512) with the
    # decision-4 DISTINCT skip counts (skipped_uncertain surfaced separately).
    db.add(
        AuditEntry(
            company_id=action.company_id,
            user_email=action.reviewed_by,
            action="SUBMIT_PAYMENT_BATCH",
            entity_type="bill_payment",
            entity_id=str(batch.id),
            details={
                "successes": counts["succeeded"],
                "failures": counts["confirmed_reject"],
                "skipped_already_paid": counts["skipped_paid"],
                "skipped_uncertain": counts["skipped_uncertain"],
                "skipped_in_flight": counts["skipped_in_flight"],
                "uncertain": counts["uncertain"],
                "batch_status": status,
                "source": "ai_assistant",
                "proposed_action_id": action.id,
            },
        )
    )
    db.commit()

    if unresolved_uncertain:
        # Decision 8: ANY unresolved outcome → the action lands `uncertain`
        # (reconcile-first). A payment MAY exist in QBO; the kept per-bill claims
        # make the next invocation probe before any re-create.
        raise QBOUncertainOutcomeError(
            f"Payment batch {batch.id}: {unresolved_uncertain} bill payment(s) have an UNRESOLVED "
            "QuickBooks outcome (a payment may already exist). The per-bill claims are kept; the "
            "reconcile-first retry probes QuickBooks before any re-create — do not re-draft."
        )
    if status == "FAILED":
        # All confirmed-rejected, zero paid → confirmed FAILED (terminal).
        raise ValueError(
            f"Payment batch {batch.id}: all {counts['confirmed_reject']} bill payment(s) were "
            "confirmed-rejected by QuickBooks and nothing was paid."
        )
    if status != "COMPLETED":
        # Partial success or confirmed-reject remainder (incl. a rival batch's
        # live claim) → retryable: the action stays APPROVED and the resume
        # re-enters the SAME batch row, paying only the remainder.
        raise BillPayBatchRetryableError(
            f"Payment batch {batch.id} is PARTIALLY_FAILED: {paid} paid, "
            f"{counts['confirmed_reject']} confirmed-rejected, {counts['skipped_in_flight']} held "
            "by a rival claim. The action stays APPROVED; a retry re-enters the same batch and "
            "pays only the remainder."
        )
    return {
        "batch_id": batch.id,
        "batch_status": status,
        "bill_count": outcome["bill_count"],
        "counts": counts,
        "outcomes": outcome["outcomes"],
    }

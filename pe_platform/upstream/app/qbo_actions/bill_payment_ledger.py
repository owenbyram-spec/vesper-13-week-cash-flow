# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (WF-3c, decisions 2 + E5) —
# marker RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this
# dispatched sub-agent session (the ratifying keyboard turn lives in the orchestrator
# transcript).
"""
app/qbo_actions/bill_payment_ledger.py
──────────────────────────────────────
Race-safe idempotency ledger for the HUMAN AP payment-batch submit lane (WF-3c).

Mirrors the agent lane's executor claim pattern (``executor.py::_claim_execution``)
on its OWN table: ``QBOActionExecution``/``InternalActionExecution`` are
proposal-keyed (``action_id`` FK ``nullable=False`` → ``proposed_actions``,
models.py:1284/:1322) and CANNOT back a router-lane batch submit that has no
``ProposedAction`` row (refuter change E2).

ALL ledger ORM writes live in this module — never in the router body (E5,
``pre-write-no-orm-in-router``). The contract with the submit loop
(``app/routers/ap_workflow.py``):

  1. ``claim_bill_payment`` is called BEFORE ``qbo.create("BillPayment", ...)``
     and COMMITS the claim so it is durable before the external side effect.
     The UNIQUE constraint on ``idempotency_key`` is the race gate; the
     check-then-insert is savepoint-wrapped (``begin_nested``) per the
     FORCE-RLS house pattern so a constraint refusal never poisons the outer
     transaction.
  2. An already-claimed bill is SKIPPED by the caller (zero new provider calls
     — a resubmit or a concurrent submit can never double-pay it).
  3. On provider failure the claim is RELEASED (``release_bill_payment_claim``)
     so the failed bill retries on a later resubmit. A crash between the
     provider failure and the release leaves the claim in place — the bill is
     conservatively skipped (under-pay + surfaced count, never double-pay).
"""
from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import BillPaymentExecution

logger = logging.getLogger(__name__)


def bill_payment_idempotency_key(
    *,
    batch_id: int,
    bill_id: str,
    amount: float,
    payment_account_id: str | None,
) -> str:
    """Deterministic SHA-256 over the payment intent (batch + bill + amount + account)."""
    raw = json.dumps(
        {
            "batch_id": batch_id,
            "bill_id": str(bill_id),
            "amount": f"{float(amount or 0):.2f}",
            "payment_account_id": str(payment_account_id or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_claim(db: Session, company_id: str, idempotency_key: str) -> BillPaymentExecution | None:
    return (
        db.query(BillPaymentExecution)
        .filter(
            BillPaymentExecution.company_id == company_id,
            BillPaymentExecution.idempotency_key == idempotency_key,
        )
        .first()
    )


# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk A (marker
# RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
# sub-agent session) — AC-4 ledger status support: claim/success/uncertain/adoption
# stamping + the cross-batch UNIQUE(company_id, bill_id) claim semantics (decisions 3/6).
def find_extant_execution(db: Session, *, company_id: str, bill_id: str) -> BillPaymentExecution | None:
    """Any extant ledger row for this (company, bill) — ANY batch.

    Plan 20260713-wave2-2-ap-batches decision 6: succeeded/uncertain/claimed rows
    persist and block a second batch from paying the same bill; this is the
    read-side companion of the UNIQUE(company_id, bill_id) constraint (which
    remains the actual race gate).
    """
    return (
        db.query(BillPaymentExecution)
        .filter(
            BillPaymentExecution.company_id == company_id,
            BillPaymentExecution.bill_id == str(bill_id),
        )
        .first()
    )


def execution_status(execution: BillPaymentExecution) -> str:
    """'claimed' | 'succeeded' | 'uncertain' — legacy rows (status NULL) derive.

    Legacy semantics (pre-status migration, documented in the migration):
    qbo_txn_id present = succeeded, else claimed.
    """
    if execution.status:
        return execution.status
    return "succeeded" if execution.qbo_txn_id else "claimed"


def claim_bill_payment(
    db: Session,
    *,
    company_id: str,
    batch_id: int,
    bill_id: str,
    idempotency_key: str,
) -> tuple[BillPaymentExecution, bool]:
    """Claim one bill payment; returns ``(row, claimed)``.

    ``claimed=False`` means a blocking row already exists — the caller must SKIP
    the provider call. The blocker is EITHER the same idempotency key (a prior
    submit / concurrent same-batch race) OR — plan 20260713-wave2-2-ap-batches
    decision 6 — ANOTHER batch's row for the same (company, bill) refused by the
    cross-batch UNIQUE(company_id, bill_id) constraint; compare the returned
    row's ``batch_id`` to distinguish. ``claimed=True`` means the claim row is
    COMMITTED (durable before the QBO side effect, ``status='claimed'``) and the
    caller may create the BillPayment.
    """
    existing = _find_claim(db, company_id, idempotency_key)
    if existing is not None:
        return existing, False
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk A (marker
    # RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this
    # dispatched sub-agent session) — decision 6: deliberately NO cross-batch
    # pre-check here. The UNIQUE(company_id, bill_id) CONSTRAINT — not a
    # check-then-act — is the cross-batch gate; the core's probe-on-encounter
    # handles known extant rows, and a racing rival surfaces below via
    # IntegrityError → winner lookup.
    execution = BillPaymentExecution(
        company_id=company_id,
        batch_id=batch_id,
        bill_id=str(bill_id),
        idempotency_key=idempotency_key,
        status="claimed",
    )
    try:
        with db.begin_nested():
            db.add(execution)
    except IntegrityError:
        # A concurrent submit inserted a blocking row between our check and our
        # insert; the savepoint absorbed the refusal — the outer transaction is
        # intact. Read the winner's committed row back: same key first (same-batch
        # race), then the cross-batch (company, bill) winner (decision 6 — an
        # honest refusal outcome, never a crash).
        winner = _find_claim(db, company_id, idempotency_key)
        if winner is None:
            winner = find_extant_execution(db, company_id=company_id, bill_id=bill_id)
        if winner is not None:
            logger.info(
                "bill_payment_ledger: duplicate claim refused for batch=%s bill=%s (already claimed by batch=%s)",
                batch_id,
                bill_id,
                winner.batch_id,
            )
            return winner, False
        raise
    db.commit()  # durable BEFORE the QBO create — a crash after this point can only under-pay
    return execution, True


def record_bill_payment_success(
    db: Session,
    execution: BillPaymentExecution,
    *,
    qbo_txn_id: str | None,
) -> None:
    """Record the provider transaction id on a successful create (status='succeeded')."""
    execution.qbo_txn_id = qbo_txn_id
    execution.status = "succeeded"
    db.commit()


def mark_bill_payment_uncertain(db: Session, execution: BillPaymentExecution) -> None:
    """Stamp an UNCERTAIN outcome — the claim is KEPT (the payment MAY exist in QBO).

    Plan 20260713-wave2-2-ap-batches decision 3: ``QBOUncertainOutcomeError``
    keeps the claim; the next submit/resume probes QBO before any re-create.
    """
    execution.status = "uncertain"
    db.commit()


def adopt_bill_payment(
    db: Session,
    execution: BillPaymentExecution,
    *,
    qbo_txn_id: str | None,
) -> None:
    """Adopt a probe-found prior create: record its txn id, stamp succeeded (decision 3)."""
    execution.qbo_txn_id = qbo_txn_id
    execution.status = "succeeded"
    db.commit()


def release_bill_payment_claim(db: Session, execution: BillPaymentExecution) -> None:
    """Release a claim so the bill can retry.

    Plan 20260713-wave2-2-ap-batches decision 3: invoked ONLY on a CONFIRMED
    reject (a QBO 4xx / a probe that confirmed the payment is absent) — never on
    an uncertain outcome, which keeps its claim (``mark_bill_payment_uncertain``).
    """
    db.delete(execution)
    db.commit()


def qbo_billpayment_txn_id(result: object) -> str | None:
    """Extract the created BillPayment's QBO id from a create() response, if present."""
    if isinstance(result, dict):
        entity = result.get("BillPayment")
        if isinstance(entity, dict) and entity.get("Id"):
            return str(entity["Id"])
    return None

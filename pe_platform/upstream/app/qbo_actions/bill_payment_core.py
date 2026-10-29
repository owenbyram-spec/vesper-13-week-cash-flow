# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk A (marker
# RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
# sub-agent session) — AC-1/AC-2: the ONE shared per-bill core (decisions 2/3/4/9).
"""
app/qbo_actions/bill_payment_core.py
────────────────────────────────────
The ONE shared per-bill payment core serving BOTH lanes (plan
20260713-wave2-2-ap-batches, decisions 2/3/4/9):

- the HUMAN AP lane (``app/routers/ap_workflow.py`` repoints here — chunk B), and
- the AGENT lane (the ``BILL_PAY`` executor arm — chunk C).

The outcome contract is PINNED and FROZEN (decision 2 / E6): per-bill outcomes are
the closed vocabulary ``{succeeded, confirmed_reject, uncertain, skipped_paid,
skipped_uncertain, skipped_in_flight}``; the CALLER owns its error envelope (the
router wraps HTTP, the executor arm maps action statuses).

Per-bill flow (decision 3 — probe-on-encounter lives HERE, both lanes get it):

1. Any extant claim row for the bill?
   - ANOTHER batch's row → honest refusal: ``skipped_paid`` (it succeeded) or
     ``skipped_in_flight`` (its claim is live) — never a crash, never a payment.
   - THIS batch, succeeded → ``skipped_paid`` (idempotent resubmit).
   - THIS batch, claimed-without-txn OR uncertain → PROBE QBO (BillPayment query,
     ``Line[].LinkedTxn.TxnId == bill_id`` match, the ar_actions trichotomy):
     found → ADOPT (record txn id, ``skipped_paid``); absent → RELEASE + retry the
     bill THIS run; ambiguous → KEEP the claim, ``skipped_uncertain``. This
     subsumes the claimed-but-crashed case — no separate reaper.
2. Fresh attempt: re-read ``Bill.Balance`` at execution and REFUSE over-balance
   (``confirmed_reject`` — decision 9, the human lane gains overpay protection);
   claim (the UNIQUE constraints are the race gates — a violation surfaces as an
   honest skip, never a crash); ``create_bill_payment``; record.
3. Release ONLY on a confirmed 4xx reject; ``QBOUncertainOutcomeError`` (5xx /
   network-after-dispatch / any non-confirmed failure) KEEPS the claim and stamps
   ``status='uncertain'`` — the next submit/resume probes before any re-create.
"""
from __future__ import annotations

import logging
from typing import Literal, TypedDict

import requests
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import BillPayment
from ..qbo_client import QBOClient
from ..qbo_errors import QBOUncertainOutcomeError
from ..services.redaction import redact_to_string
from .bill_payment_ledger import (
    adopt_bill_payment,
    bill_payment_idempotency_key,
    claim_bill_payment,
    execution_status,
    find_extant_execution,
    mark_bill_payment_uncertain,
    record_bill_payment_success,
    release_bill_payment_claim,
)
from .qbo_query import qbo_escape_str
from .refusals import QBOActionRefused

logger = logging.getLogger(__name__)

# ── The FROZEN outcome contract (decision 2 / E6) — chunks B and C build on this ──

BillPayOutcomeKind = Literal[
    "succeeded",  # paid THIS run; txn id recorded
    "confirmed_reject",  # QBO 4xx / over-balance refusal; claim released — retryable next submit
    "uncertain",  # 5xx/network after dispatch THIS run; claim KEPT, status='uncertain'
    "skipped_paid",  # already paid (prior success, or probe ADOPTED the prior create)
    "skipped_uncertain",  # extant claim probed AMBIGUOUS (or unprobeable) — kept for a human
    "skipped_in_flight",  # another batch/submit holds a live claim on this bill
]

OUTCOME_KINDS: tuple[BillPayOutcomeKind, ...] = (
    "succeeded",
    "confirmed_reject",
    "uncertain",
    "skipped_paid",
    "skipped_uncertain",
    "skipped_in_flight",
)


class BillOutcome(TypedDict):
    bill_id: str
    outcome: BillPayOutcomeKind
    qbo_txn_id: str | None
    detail: str


class BatchOutcome(TypedDict):
    outcomes: list[BillOutcome]
    counts: dict[str, int]  # every OUTCOME_KINDS key always present
    bill_count: int


def batch_status_for(counts: dict[str, int]) -> str:
    """Decision 4 — batch status HONESTY, shared so both lanes agree.

    A batch with ANY skipped_uncertain / skipped_in_flight / uncertain /
    confirmed_reject can NEVER read COMPLETED. All-confirmed-failed with zero
    paid → FAILED; anything mixed → PARTIALLY_FAILED.
    """
    paid = counts["succeeded"] + counts["skipped_paid"]
    unresolved = counts["uncertain"] + counts["skipped_uncertain"] + counts["skipped_in_flight"]
    rejected = counts["confirmed_reject"]
    if unresolved == 0 and rejected == 0:
        return "COMPLETED"
    if paid == 0 and unresolved == 0 and rejected > 0:
        return "FAILED"
    return "PARTIALLY_FAILED"


# ── Probe (decision 3 — the ar_actions find_created_ar_entity trichotomy) ──

# QBO-OK: idempotent-upstream — this probe is READ-ONLY (client.query); it never
# posts. It protects the per-bill claim ledger from a double-create.
def find_created_bill_payment(qbo: QBOClient, *, bill_id: str, vendor_id: str | None) -> dict | None:
    """Deterministic reconciliation probe for an uncertain/crashed prior create.

    Queries QBO BillPayment rows for the bill's vendor and matches
    ``Line[].LinkedTxn.TxnId == bill_id`` in Python. Trichotomy (the ar_actions
    template): EXACTLY ONE match → adopt (return it); NONE → confirmed absent
    (return None — safe to pay THIS run); AMBIGUOUS (>1 match, or no vendor to
    key the query on) → raise the typed uncertain error — guessing either way
    risks a double-pay or a false success.
    """
    if not vendor_id:
        raise QBOUncertainOutcomeError(
            f"Cannot deterministically probe QuickBooks for a prior payment of bill {bill_id}: "
            "the batch entry carries no vendor_id to key the BillPayment query on. "
            "Keeping the claim — a human must reconcile."
        )
    response = qbo.query(
        f"SELECT * FROM BillPayment WHERE VendorRef = '{qbo_escape_str(str(vendor_id))}' MAXRESULTS 100"  # SA-OK: QBO SQL-like API string (values escaped via qbo_escape_str), not a DB query
    )
    candidates = ((response or {}).get("QueryResponse") or {}).get("BillPayment") or []
    matches = [payment for payment in candidates if _payment_links_bill(payment, bill_id)]
    if not matches:
        return None
    if len(matches) > 1:
        raise QBOUncertainOutcomeError(
            f"Reconciliation is ambiguous: {len(matches)} BillPayment rows link bill {bill_id}. "
            "Keeping the claim — a human must reconcile before this bill is retried."
        )
    return matches[0]


def _payment_links_bill(payment: dict, bill_id: str) -> bool:
    for line in payment.get("Line") or []:
        for linked in line.get("LinkedTxn") or []:
            if str(linked.get("TxnType") or "") == "Bill" and str(linked.get("TxnId") or "") == str(bill_id):
                return True
    return False


# ── Helpers ──

def _amount_of(bill: dict) -> float:
    raw = bill.get("balance", bill.get("amount", 0))
    try:
        return round(float(raw or 0), 2)
    except (TypeError, ValueError):
        return 0.0


# QuickBooks BillPayment.PayType is required and is Check or CreditCard. A check,
# an ACH debit and a wire all draw on a Bank account (PayType Check, with
# CheckPayment.BankAccountRef); a card payment is PayType CreditCard with
# CreditCardPayment.CCAccountRef.
PAY_TYPE_BY_METHOD = {
    "check": "Check",
    "ach": "Check",
    "wire": "Check",
    "credit_card": "CreditCard",
}
# The QBO AccountType the payment account must have, per PayType.
PAYMENT_ACCOUNT_TYPE_BY_PAY_TYPE = {"Check": "Bank", "CreditCard": "Credit Card"}


def bill_pay_type(payment_method: str | None, payment_account_id: str | None) -> str:
    """The QBO PayType for a batch, or refuse (before any claim or provider call)."""
    method = str(payment_method or "").strip().lower()
    pay_type = PAY_TYPE_BY_METHOD.get(method)
    if pay_type is None:
        raise QBOActionRefused(
            "payment_method_unsupported",
            f"Payment method '{method or '(none)'}' is not supported. Use one of: "
            f"{', '.join(sorted(PAY_TYPE_BY_METHOD))}.",
        )
    if not str(payment_account_id or "").strip():
        raise QBOActionRefused(
            "payment_account_required",
            "A payment batch needs the account the bills are paid from "
            f"(a {PAYMENT_ACCOUNT_TYPE_BY_PAY_TYPE[pay_type]} account for {method}).",
        )
    return pay_type


def _payment_payload(batch: BillPayment, bill: dict, amount: float) -> dict:
    payload: dict = {
        "VendorRef": {"value": bill.get("vendor_id")},
        "TotalAmt": amount,
        "Line": [
            {
                "Amount": amount,
                "LinkedTxn": [{"TxnId": str(bill.get("bill_id")), "TxnType": "Bill"}],
            }
        ],
    }
    account_ref = {"value": str(batch.payment_account_id)}
    if bill_pay_type(batch.payment_method, batch.payment_account_id) == "CreditCard":
        payload["PayType"] = "CreditCard"
        payload["CreditCardPayment"] = {"CCAccountRef": account_ref}
    else:
        payload["PayType"] = "Check"
        payload["CheckPayment"] = {"BankAccountRef": account_ref}
    # Decision 9: transmit TxnDate when the batch carries a payment_date (a
    # documented behavior change — the legacy lane never sent one and QBO
    # defaulted to today).
    if batch.payment_date:
        payload["TxnDate"] = batch.payment_date.isoformat()
    return payload


def _qbo_txn_id(result: object) -> str | None:
    if isinstance(result, dict):
        entity = result.get("BillPayment")
        if isinstance(entity, dict) and entity.get("Id"):
            return str(entity["Id"])
    return None


def _outcome(bill_id: str, outcome: BillPayOutcomeKind, *, qbo_txn_id: str | None = None, detail: str = "") -> BillOutcome:
    return {"bill_id": bill_id, "outcome": outcome, "qbo_txn_id": qbo_txn_id, "detail": detail}


def _reject_status_code(exc: requests.HTTPError) -> str:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return str(code) if code is not None else "4xx"


# ── The shared per-bill core (decision 2) ──

def submit_bill_payment_batch(db: Session, qbo: QBOClient, *, batch: BillPayment) -> BatchOutcome:
    """Pay every payable bill in ``batch``; return the PINNED BatchOutcome.

    The CALLER owns error envelopes and batch-row bookkeeping (status via
    ``batch_status_for``, audit rows, HTTP mapping). This core owns money
    safety: per-bill claims, probe-on-encounter, balance re-read, and the
    release-only-on-confirmed-reject discipline. A QBO 401 (auth) aborts the
    whole run by re-raising: the request was rejected before processing, so the
    claim for the in-flight bill is released and untouched bills stay unpaid.
    """
    # PayType is required by QuickBooks: a batch without a usable payment method
    # and account is refused here, before any claim or provider call.
    bill_pay_type(batch.payment_method, batch.payment_account_id)
    outcomes: list[BillOutcome] = []
    bills = batch.bills or []

    # SA-OK: bounded-loop (human-curated payment batch) — the per-bill
    # claim-commit BEFORE each provider call IS the idempotency contract;
    # batching the claims would reopen the mid-loop-crash double-pay window.
    for bill in bills:
        bill_id = str(bill.get("bill_id") or "").strip()  # SA-OK: dict.get on a JSON bill dict, not an ORM query
        if not bill_id:
            outcomes.append(_outcome("", "confirmed_reject", detail="Batch entry is missing bill_id."))
            continue
        outcomes.append(_submit_one_bill(db, qbo, batch=batch, bill=bill, bill_id=bill_id))

    counts = {kind: 0 for kind in OUTCOME_KINDS}
    for entry in outcomes:
        counts[entry["outcome"]] += 1
    return {"outcomes": outcomes, "counts": counts, "bill_count": len(bills)}


def _submit_one_bill(db: Session, qbo: QBOClient, *, batch: BillPayment, bill: dict, bill_id: str) -> BillOutcome:
    amount = _amount_of(bill)
    if amount <= 0:
        return _outcome(bill_id, "confirmed_reject", detail="Non-positive payment amount refused.")

    # 1) PROBE-ON-ENCOUNTER (decision 3): any extant claim row, ANY batch.
    extant = find_extant_execution(db, company_id=batch.company_id, bill_id=bill_id)  # SA-OK: bounded-loop — per-bill claim lookup is the idempotency contract
    if extant is not None:
        resolved = _resolve_extant_claim(db, qbo, batch=batch, bill=bill, bill_id=bill_id, extant=extant)
        if resolved is not None:
            return resolved
        # Probe confirmed ABSENT → the stale claim was released; retry THIS run.

    # 2) Execution-time Bill.Balance re-read with over-balance refusal (decision 9).
    refusal = _balance_refusal(qbo, bill_id=bill_id, amount=amount)
    if refusal is not None:
        return refusal

    # 3) Claim → create → record. The UNIQUE constraints are the race gates.
    key = bill_payment_idempotency_key(
        batch_id=batch.id,
        bill_id=bill_id,
        amount=amount,
        payment_account_id=batch.payment_account_id,
    )
    execution, claimed = claim_bill_payment(  # SA-OK: bounded-loop — per-bill claim-commit BEFORE each provider call
        db,
        company_id=batch.company_id,
        batch_id=batch.id,
        bill_id=bill_id,
        idempotency_key=key,
    )
    if not claimed:
        # A concurrent writer won the constraint race mid-run — honest skip.
        if execution_status(execution) == "succeeded":
            return _outcome(bill_id, "skipped_paid", qbo_txn_id=execution.qbo_txn_id, detail="Already paid (concurrent claim won the race).")
        return _outcome(bill_id, "skipped_in_flight", detail=f"Bill is claimed by a concurrent submit (batch {execution.batch_id}).")

    try:
        result = qbo.create_bill_payment(_payment_payload(batch, bill, amount))  # WRITEBACK-OK: both lanes reach this ONLY through human approval — the router's EDITOR-gated APPROVED/PARTIALLY_FAILED submit gate, or the executor's APPROVED/EXECUTED refusal; the per-bill claim above is the idempotency gate
    except QBOUncertainOutcomeError as exc:
        # Decision 3: uncertain KEEPS the claim — never blindly retried.
        mark_bill_payment_uncertain(db, execution)
        logger.error("BillPayment outcome uncertain for bill %s: %s", bill_id, redact_to_string(exc, max_length=500))
        return _outcome(bill_id, "uncertain", detail="QuickBooks outcome unknown — claim kept; the next submit reconciles before any retry.")
    except HTTPException:
        # QBO auth reject (401) — confirmed unprocessed; release and abort the run.
        release_bill_payment_claim(db, execution)
        raise
    except requests.HTTPError as exc:
        # Confirmed 4xx reject — the ONLY branch that releases (decision 3).
        release_bill_payment_claim(db, execution)
        logger.error("BillPayment rejected for bill %s: %s", bill_id, redact_to_string(exc, max_length=500))
        return _outcome(bill_id, "confirmed_reject", detail=f"QuickBooks rejected the payment (HTTP {_reject_status_code(exc)}); it can be retried on a later submit.")
    except Exception as exc:  # OBS-OK: surfaced, not swallowed — recorded as an 'uncertain' outcome + logged redacted; an unclassified failure may have dispatched money, so the claim is conservatively KEPT (never double-pay)
        mark_bill_payment_uncertain(db, execution)
        logger.error("BillPayment unclassified failure for bill %s: %s", bill_id, redact_to_string(exc, max_length=500))
        return _outcome(bill_id, "uncertain", detail="Unclassified failure during the payment create — claim kept; reconcile before retry.")

    txn_id = _qbo_txn_id(result)
    record_bill_payment_success(db, execution, qbo_txn_id=txn_id)
    return _outcome(bill_id, "succeeded", qbo_txn_id=txn_id, detail="Paid.")


def _resolve_extant_claim(
    db: Session,
    qbo: QBOClient,
    *,
    batch: BillPayment,
    bill: dict,
    bill_id: str,
    extant,
) -> BillOutcome | None:
    """Resolve an extant ledger row. Returns an outcome, or None = retry THIS run.

    ANOTHER batch's row is never probed/released from here (that would race its
    owner's resume) — it surfaces as the decision-6 honest refusal. THIS batch's
    non-final rows get the decision-3 probe.
    """
    status = execution_status(extant)
    if extant.batch_id != batch.id:
        if status == "succeeded":
            return _outcome(bill_id, "skipped_paid", qbo_txn_id=extant.qbo_txn_id, detail=f"Already paid by batch {extant.batch_id}.")
        return _outcome(bill_id, "skipped_in_flight", detail=f"Bill is already claimed by batch {extant.batch_id} (status: {status}); refusing a second payment path.")

    if status == "succeeded":
        return _outcome(bill_id, "skipped_paid", qbo_txn_id=extant.qbo_txn_id, detail="Already paid in a prior submit of this batch.")

    # claimed-without-txn (incl. a crashed claim) OR uncertain → probe QBO.
    try:
        found = find_created_bill_payment(qbo, bill_id=bill_id, vendor_id=bill.get("vendor_id"))  # SA-OK: dict.get on a JSON bill dict, not an ORM query
    except QBOUncertainOutcomeError as exc:
        mark_bill_payment_uncertain(db, extant)
        logger.warning("BillPayment probe ambiguous for bill %s: %s", bill_id, redact_to_string(exc, max_length=500))
        return _outcome(bill_id, "skipped_uncertain", detail="Prior attempt is unresolved and the QuickBooks probe was ambiguous — a human must reconcile.")
    except HTTPException:
        raise  # auth reject — abort the run; the claim is untouched (conservative keep)
    except Exception as exc:  # OBS-OK: surfaced, not swallowed — probe failure is recorded as a kept-claim 'skipped_uncertain' outcome + logged redacted; releasing on an unreadable probe could double-pay
        mark_bill_payment_uncertain(db, extant)
        logger.error("BillPayment probe failed for bill %s: %s", bill_id, redact_to_string(exc, max_length=500))
        return _outcome(bill_id, "skipped_uncertain", detail="Prior attempt is unresolved and QuickBooks could not be probed — claim kept; a human must reconcile.")

    if found is not None:
        adopt_bill_payment(db, extant, qbo_txn_id=_qbo_txn_id({"BillPayment": found}) or str(found.get("Id") or "") or None)
        return _outcome(bill_id, "skipped_paid", qbo_txn_id=extant.qbo_txn_id, detail="Prior uncertain create FOUND in QuickBooks — adopted, not re-paid.")

    # Confirmed ABSENT in QBO → the stale claim is safe to release; pay THIS run.
    release_bill_payment_claim(db, extant)
    return None


def _balance_refusal(qbo: QBOClient, *, bill_id: str, amount: float) -> BillOutcome | None:
    """Decision 9: re-read Bill.Balance at execution; refuse over-balance.

    An unreadable/absent balance is a REFUSAL (no claim exists yet, so the bill
    stays retryable on a later submit) — paying against an unverified balance is
    how overpays happen.
    """
    try:
        current = qbo.get_transaction("bill", bill_id)  # SA-OK: bounded-loop — per-bill execution-time balance re-read is the decision-9 overpay gate
    except HTTPException:
        raise  # auth reject — abort the run
    except Exception as exc:  # OBS-OK: surfaced, not swallowed — read failure is recorded as a confirmed_reject outcome + logged redacted; the bill stays retryable
        logger.error("Bill balance re-read failed for bill %s: %s", bill_id, redact_to_string(exc, max_length=500))
        return _outcome(bill_id, "confirmed_reject", detail="The bill's current balance could not be verified in QuickBooks — refusing to pay; retry on a later submit.")
    entity = (current or {}).get("Bill") or {}
    raw_balance = entity.get("Balance")
    if raw_balance is None:
        return _outcome(bill_id, "confirmed_reject", detail="QuickBooks returned no balance for the bill — refusing to pay an unverified amount.")
    balance = float(raw_balance)
    if amount > balance + 0.005:
        return _outcome(
            bill_id,
            "confirmed_reject",
            detail=f"Payment amount {amount:.2f} exceeds the bill's current QuickBooks balance {balance:.2f} — over-balance payment refused.",
        )
    return None

"""
app/routers/ap_workflow.py
──────────────────────────
Accounts Payable workflow: bill payment management, prioritization engine,
and payment batch processing.

GET    /ap/prioritize         Generate a prioritized payment list (read-only)
POST   /ap/prioritize         Generate a prioritized payment list (legacy compatibility)
POST   /ap/payment-batch      Create a payment batch from prioritized bills
GET    /ap/payment-batches    List payment batches
GET    /ap/payment-batch/{id} Get batch detail
PATCH  /ap/payment-batch/{id}/approve   Approve a batch
PATCH  /ap/payment-batch/{id}/submit    Submit batch to QBO
DELETE /ap/payment-batch/{id}           Cancel a batch
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta  # noqa: F401
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_current_user_from_token
from ..db import get_db
from ..dependencies import check_entity_tenant_access, check_tenant_access
from ..models import BillPayment, BillPaymentExecution, AuditEntry, QBOAccount, User
# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk B (marker
# RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
# sub-agent session) — AC-1: the human lane repoints onto the ONE shared per-bill core
# (decisions 1/2); the router keeps only gates, status mapping (batch_status_for,
# decision 4), audit rows, and the HTTP envelope.
from ..qbo_actions.bill_payment_core import (
    PAYMENT_ACCOUNT_TYPE_BY_PAY_TYPE,
    batch_status_for,
    bill_pay_type,
    submit_bill_payment_batch,
)
from ..qbo_actions.refusals import QBOActionRefused
from ..qbo_client import get_qbo_client_from_db
from ..services.errors import safe_error_detail, user_safe_http_exception
from ..tenant_rls import tenant_scope_for_realm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ap", tags=["Accounts Payable"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class PrioritizeRequest(BaseModel):
    realm_id: str
    strategy: str = "due_date"          # due_date | discount | cash_constrained
    available_cash: Optional[float] = None  # For cash_constrained strategy
    max_bills: int = 50

class BatchCreateRequest(BaseModel):
    realm_id: str
    bills: List[Dict[str, Any]]         # [{bill_id, vendor, amount, due_date, priority}]
    batch_name: Optional[str] = None
    # check | ach | wire (paid from a Bank account) or credit_card (a Credit Card
    # account). payment_account_id is required; validated against the realm chart.
    payment_method: str = "check"
    payment_account_id: Optional[str] = None
    payment_date: Optional[str] = None  # ISO date
    strategy: str = "due_date"
    available_cash: Optional[float] = None
    created_by: Optional[str] = None

class BatchApproveRequest(BaseModel):
    approved_by: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _validated_payment_or_422(
    db: Session, realm_id: str, payment_method: Optional[str], payment_account_id: Optional[str]
) -> tuple[str, str]:
    """``(method, account_id)`` once the method maps to a QBO PayType and the
    account is an active account of this realm's synced chart of the matching
    type (Bank for check/ach/wire, Credit Card for credit_card); else a 422.

    qbo_accounts is org-keyed under RLS, so the lookup pins the realm's scope.
    """
    try:
        pay_type = bill_pay_type(payment_method, payment_account_id)
    except QBOActionRefused as exc:
        raise user_safe_http_exception(
            status_code=422, error_code=exc.error_code, user_message=exc.user_message,
            log_context="ap_payment_batch_refused",
        ) from exc
    method = str(payment_method).strip().lower()
    account_id = str(payment_account_id).strip()
    required_type = PAYMENT_ACCOUNT_TYPE_BY_PAY_TYPE[pay_type]
    with tenant_scope_for_realm(db, realm_id):
        account = (
            db.query(QBOAccount)
            .filter(
                QBOAccount.realm_id == realm_id,
                QBOAccount.qbo_id == account_id,
                QBOAccount.active.is_(True),
            )
            .first()
        )
    if account is None:
        raise user_safe_http_exception(
            status_code=422,
            error_code="payment_account_unknown",
            user_message="The payment account is not an active account of this company. Pick it from the list.",
            log_context="ap_payment_batch_refused",
        )
    if account.account_type != required_type:
        raise user_safe_http_exception(
            status_code=422,
            error_code="payment_account_wrong_type",
            user_message=(
                f"A {method} payment must come from a {required_type} account; the selected "
                f"account is a {account.account_type or 'different'} account."
            ),
            log_context="ap_payment_batch_refused",
        )
    return method, account_id


# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk B (marker
# RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
# sub-agent session) — AC-1: presentation-only summary of the core's PINNED counts for the
# batch row's error_message; the status DECISION itself is batch_status_for (decision 4).
def _submission_error_message(status: str, counts: dict[str, int]) -> str | None:
    """Honest human-readable summary for a non-COMPLETED submit outcome."""
    if status == "COMPLETED":
        return None
    unresolved = counts["uncertain"] + counts["skipped_uncertain"]  # LAYER-OK: presentation-only summarization of the core's counts, no business decision
    parts: list[str] = []
    if counts["confirmed_reject"]:
        parts.append(f"{counts['confirmed_reject']} payment(s) rejected by QuickBooks (retryable on a later submit)")
    if unresolved:
        parts.append(f"{unresolved} payment(s) with an unresolved outcome — reconcile in QuickBooks before retrying")
    if counts["skipped_in_flight"]:
        parts.append(f"{counts['skipped_in_flight']} bill(s) already claimed by another payment batch")
    return "; ".join(parts)


def _prioritize_bills(bills: List[Dict], strategy: str, available_cash: Optional[float] = None) -> List[Dict]:
    """
    Prioritize bills based on the selected strategy.

    Strategies:
    - due_date: Pay most overdue first
    - discount: Pay bills with early-pay discounts first
    - cash_constrained: Maximize bills paid within available cash
    """
    today = date.today()

    for bill in bills:
        due_str = bill.get("due_date", "")
        try:
            due = date.fromisoformat(due_str) if due_str else today
            bill["days_overdue"] = max(0, (today - due).days)
        except (ValueError, TypeError):
            bill["days_overdue"] = 0

    if strategy == "due_date":
        # Most overdue first, then by amount (larger first)
        bills.sort(key=lambda b: (-b.get("days_overdue", 0), -_safe_float(b.get("balance", b.get("amount", 0)))))

    elif strategy == "discount":
        # Bills with discounts first (sorted by discount date proximity)
        # Then by due date
        bills.sort(key=lambda b: (
            0 if b.get("has_discount") else 1,
            -b.get("days_overdue", 0),
        ))

    elif strategy == "cash_constrained":
        # Pack as many bills as possible within available_cash budget
        # Priority: overdue first, then due date order
        bills.sort(key=lambda b: (-b.get("days_overdue", 0), b.get("due_date", "")))

        if available_cash is not None:
            selected = []
            running_total = 0.0
            for bill in bills:
                amt = _safe_float(bill.get("balance", bill.get("amount", 0)))
                if running_total + amt <= available_cash:
                    bill["included"] = True
                    selected.append(bill)
                    running_total += amt
                else:
                    bill["included"] = False
                    selected.append(bill)
            bills = selected

    # Add priority rank
    for i, bill in enumerate(bills):
        bill["priority_rank"] = i + 1

    return bills


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _prioritized_bills_payload(
    *,
    db: Session,
    realm_id: str,
    strategy: str,
    available_cash: Optional[float],
    max_bills: int,
) -> dict:
    qbo = get_qbo_client_from_db(db, realm_id)
    today = date.today()

    # Fetch open bills from QBO
    data = qbo.query(
        f"SELECT * FROM Bill WHERE Balance > '0' "
        f"ORDERBY DueDate ASC MAXRESULTS {max_bills}"
    )
    raw_bills = data.get("QueryResponse", {}).get("Bill", [])

    bills = []
    for bill in raw_bills:
        due_date_str = bill.get("DueDate", "")
        days_overdue = 0
        if due_date_str:
            try:
                due = date.fromisoformat(due_date_str)
                days_overdue = max(0, (today - due).days)
            except ValueError:
                pass

        bills.append({
            "bill_id": bill.get("Id"),
            "doc_number": bill.get("DocNumber", "—"),
            "vendor": bill.get("VendorRef", {}).get("name", "Unknown"),
            "vendor_id": bill.get("VendorRef", {}).get("value"),
            "amount": _safe_float(bill.get("TotalAmt")),
            "balance": _safe_float(bill.get("Balance")),
            "txn_date": bill.get("TxnDate", ""),
            "due_date": due_date_str,
            "days_overdue": days_overdue,
            "has_discount": False,  # QBO doesn't always expose this easily
        })

    # Apply prioritization
    prioritized = _prioritize_bills(bills, strategy, available_cash)

    # Summary stats
    total_outstanding = sum(b["balance"] for b in prioritized)
    overdue_total = sum(b["balance"] for b in prioritized if b.get("days_overdue", 0) > 0)
    overdue_count = sum(1 for b in prioritized if b.get("days_overdue", 0) > 0)

    payable = prioritized
    if strategy == "cash_constrained" and available_cash:
        payable = [b for b in prioritized if b.get("included", True)]

    return {
        "realm_id": realm_id,
        "strategy": strategy,
        "available_cash": available_cash,
        "total_outstanding": round(total_outstanding, 2),
        "overdue_total": round(overdue_total, 2),
        "overdue_count": overdue_count,
        "bills": prioritized,
        "payable_count": len(payable),
        "payable_total": round(sum(b["balance"] for b in payable), 2),
    }


@router.get("/prioritize")
def get_prioritized_bills(
    realm_id: str = Query(...),
    strategy: str = Query("due_date"),
    available_cash: Optional[float] = Query(None),
    max_bills: int = Query(50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Generate a prioritized payment list based on strategy.
    Pulls open bills from QBO and ranks them without requiring CSRF.
    """
    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=current_user)
    return _prioritized_bills_payload(
        db=db,
        realm_id=realm_id,
        strategy=strategy,
        available_cash=available_cash,
        max_bills=max_bills,
    )


@router.post("/prioritize")
def prioritize_bills(
    body: PrioritizeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Generate a prioritized payment list based on strategy.
    Pulls open bills from QBO and ranks them.
    """
    check_tenant_access("VIEWER", company_id=body.realm_id, db=db, user=current_user)
    return _prioritized_bills_payload(
        db=db,
        realm_id=body.realm_id,
        strategy=body.strategy,
        available_cash=body.available_cash,
        max_bills=body.max_bills,
    )


@router.post("/payment-batch")
def create_payment_batch(
    body: BatchCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Create a payment batch from selected bills."""
    check_tenant_access("EDITOR", company_id=body.realm_id, db=db, user=current_user)
    payment_method, payment_account_id = _validated_payment_or_422(
        db, body.realm_id, body.payment_method, body.payment_account_id
    )
    total = sum(_safe_float(b.get("balance", b.get("amount", 0))) for b in body.bills)

    batch = BillPayment(
        company_id=body.realm_id,
        batch_name=body.batch_name or f"Payment Batch {date.today().isoformat()}",
        bills=body.bills,
        total_amount=Decimal(str(round(total, 2))),
        bill_count=len(body.bills),
        payment_method=payment_method,
        payment_account_id=payment_account_id,
        payment_date=date.fromisoformat(body.payment_date) if body.payment_date else None,
        status="DRAFT",
        created_by=body.created_by or current_user.email,
        strategy=body.strategy,
        available_cash=Decimal(str(body.available_cash)) if body.available_cash else None,
    )
    db.add(batch)

    # Audit entry
    audit = AuditEntry(
        company_id=body.realm_id,
        user_email=body.created_by or current_user.email,
        action="CREATE_PAYMENT_BATCH",
        entity_type="bill_payment",
        details={"bill_count": len(body.bills), "total": round(total, 2), "strategy": body.strategy},
    )
    db.add(audit)
    db.commit()
    db.refresh(batch)

    return _batch_to_dict(batch)


@router.get("/payment-batches")
def list_payment_batches(
    realm_id: str = Query(...),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """List payment batches for a company."""
    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=current_user)
    q = db.query(BillPayment).filter(BillPayment.company_id == realm_id)
    if status:
        q = q.filter(BillPayment.status == status)
    batches = q.order_by(BillPayment.created_at.desc()).all()
    return [_batch_to_dict(b) for b in batches]


@router.get("/payment-batch/{batch_id}")
def get_payment_batch(
    batch_id: int,
    realm_id: str = Query(..., description="Company realm_id for ownership check"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Get payment batch detail."""
    batch = check_entity_tenant_access(
        "VIEWER",
        db=db,
        user=current_user,
        model=BillPayment,
        entity_id=batch_id,
        not_found_detail="Payment batch not found",
    )
    if batch.company_id != realm_id:
        raise HTTPException(status_code=404, detail="Payment batch not found")
    return _batch_to_dict(batch)


@router.patch("/payment-batch/{batch_id}/approve")
def approve_payment_batch(
    batch_id: int,
    body: BatchApproveRequest,
    realm_id: str = Query(..., description="Company realm_id for ownership check"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Approve a payment batch for submission."""
    batch = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BillPayment,
        entity_id=batch_id,
        not_found_detail="Payment batch not found",
    )
    if batch.company_id != realm_id:
        raise HTTPException(status_code=404, detail="Payment batch not found")
    if batch.status != "DRAFT":
        raise HTTPException(status_code=400, detail=f"Cannot approve batch in {batch.status} status")

    batch.status = "APPROVED"
    batch.approved_by = body.approved_by or current_user.email
    batch.approved_at = datetime.utcnow()

    audit = AuditEntry(
        company_id=batch.company_id,
        user_email=body.approved_by or current_user.email,
        action="APPROVE_PAYMENT_BATCH",
        entity_type="bill_payment",
        entity_id=str(batch_id),
        details={"total": float(batch.total_amount or 0), "bill_count": batch.bill_count},
    )
    db.add(audit)
    db.commit()
    db.refresh(batch)
    return _batch_to_dict(batch)


@router.patch("/payment-batch/{batch_id}/submit")
def submit_payment_batch(
    batch_id: int,
    realm_id: str = Query(..., description="Company realm_id for ownership check"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Submit an approved payment batch to QBO as BillPayment transactions.
    Creates one QBO BillPayment per bill in the batch.
    """
    batch = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BillPayment,
        entity_id=batch_id,
        not_found_detail="Payment batch not found",
    )
    if batch.company_id != realm_id:
        raise HTTPException(status_code=404, detail="Payment batch not found")
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (WF-3c) — marker
    # RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
    # sub-agent session. A PARTIALLY_FAILED batch is resubmittable: the ledger below guarantees
    # already-paid bills are skipped, so only the failed remainder retries.
    if batch.status not in ("APPROVED", "PARTIALLY_FAILED"):
        # LAYER-OK: constant status-gate message (closed status vocabulary only, no internals) —
        # matches this file's existing DRAFT/APPROVED gate raises pending their remediation wave.
        raise HTTPException(
            status_code=400,
            detail=f"Batch must be APPROVED (or PARTIALLY_FAILED, to resubmit) to submit, current: {batch.status}",
        )
    # A batch created before payment accounts were required (or whose account
    # has since changed type) is refused before any QuickBooks call.
    _validated_payment_or_422(db, batch.company_id, batch.payment_method, batch.payment_account_id)

    qbo = get_qbo_client_from_db(db, batch.company_id)
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk B (marker
    # RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
    # sub-agent session) — AC-1: the per-bill loop is GONE from this handler. The ONE shared
    # core (decisions 2/3/9) owns claims, probe-on-encounter, the Bill.Balance over-balance
    # refusal, and TxnDate transmission; this handler gates, maps the PINNED BatchOutcome via
    # batch_status_for (decision 4 status honesty), and envelopes. A QBO 401 aborts inside
    # the core (in-flight claim released) and propagates as its HTTPException.
    outcome = submit_bill_payment_batch(db, qbo, batch=batch)
    counts = outcome["counts"]

    batch.status = batch_status_for(counts)  # AUTH-OK: payment-batch workflow status, not an access-control field; the SUBMIT_PAYMENT_BATCH AuditEntry below records the outcome
    batch.error_message = _submission_error_message(batch.status, counts)
    batch.submitted_at = datetime.utcnow()

    audit = AuditEntry(
        company_id=batch.company_id,
        action="SUBMIT_PAYMENT_BATCH",
        entity_type="bill_payment",
        entity_id=str(batch_id),
        # Decision 4: the audit row carries the DISTINCT counts — skipped_uncertain is
        # surfaced separately from skipped_paid, never flattened into one "skipped" total.
        details={"counts": counts, "batch_status": batch.status, "bill_count": outcome["bill_count"]},
    )
    db.add(audit)  # LAYER-OK: audit+status bookkeeping stays in this legacy router lane (matches every sibling handler in this file) pending its service-extraction wave
    db.commit()
    db.refresh(batch)

    return {
        **_batch_to_dict(batch),
        "submission_result": {
            # The PINNED outcome contract (decision 2): counts is always fully keyed over
            # {succeeded, confirmed_reject, uncertain, skipped_paid, skipped_uncertain,
            # skipped_in_flight}; outcomes carries the per-bill honest detail (incl. the
            # decision-9 over-balance refusals).
            "counts": counts,
            "outcomes": outcome["outcomes"],
        },
    }


@router.delete("/payment-batch/{batch_id}")
def cancel_payment_batch(
    batch_id: int,
    realm_id: str = Query(..., description="Company realm_id for ownership check"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Cancel/delete a draft or approved payment batch."""
    batch = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BillPayment,
        entity_id=batch_id,
        not_found_detail="Payment batch not found",
    )
    if batch.company_id != realm_id:
        raise HTTPException(status_code=404, detail="Payment batch not found")
    if batch.status in ("SUBMITTED", "COMPLETED"):
        raise HTTPException(status_code=400, detail="Cannot cancel a submitted/completed batch")

    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk B (marker
    # RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
    # sub-agent session) — AC-2 / decision 7 (E3, blocking): cancel is REFUSED over execution
    # evidence. The batch_id ondelete="CASCADE" would erase the succeeded/uncertain/claimed
    # BillPaymentExecution rows that the double-pay refusals depend on.
    evidence_count = (
        db.query(BillPaymentExecution)  # LAYER-OK: trivial tenant-filtered evidence-count read guarding the delete (decision 7)
        .filter(
            BillPaymentExecution.company_id == batch.company_id,
            BillPaymentExecution.batch_id == batch.id,
        )
        .count()
    )
    if evidence_count:
        raise HTTPException(
            status_code=409,
            detail=safe_error_detail(
                error_code="payment_batch_has_execution_evidence",
                user_message=(
                    f"This batch cannot be cancelled: {evidence_count} bill payment(s) carry "
                    "execution evidence (paid, uncertain, or claimed). Deleting the batch would "
                    "erase the ledger rows that prevent paying those bills twice. Reconcile the "
                    "executed bills in QuickBooks instead."
                ),
                retryable=False,
                log_context="payment_batch_cancel_refused",
            ),
        )

    db.delete(batch)
    db.commit()
    return {"status": "cancelled", "id": batch_id}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _batch_to_dict(b: BillPayment) -> Dict[str, Any]:
    return {
        "id": b.id,
        "company_id": b.company_id,
        "batch_name": b.batch_name,
        "bills": b.bills or [],
        "total_amount": float(b.total_amount or 0),
        "bill_count": b.bill_count,
        "payment_method": b.payment_method,
        "payment_account_id": b.payment_account_id,
        "payment_date": b.payment_date.isoformat() if b.payment_date else None,
        "status": b.status,
        "strategy": b.strategy,
        "available_cash": float(b.available_cash) if b.available_cash else None,
        "created_by": b.created_by,
        "approved_by": b.approved_by,
        "approved_at": b.approved_at.isoformat() if b.approved_at else None,
        "submitted_at": b.submitted_at.isoformat() if b.submitted_at else None,
        "error_message": b.error_message,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }

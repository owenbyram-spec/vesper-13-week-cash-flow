"""
app/routers/weekly_review.py
─────────────────────────────
Weekly transaction snapshot — pulls the last 7 days of Purchases and Deposits
from QBO, compares inflow/outflow to the prior 4-week rolling average, and
flags any individual transactions that deviate significantly from historical norms.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..qbo_client import get_qbo_client_from_db, QBOClient
from ..qbo_actions.qbo_query import qbo_entity, qbo_escape_str, qbo_int
from ..auth import get_current_user_from_token
from ..dependencies import check_tenant_access
from ..models import User
from ..services.errors import user_safe_http_exception

router = APIRouter(prefix="/weekly-review", tags=["Weekly Review"])
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _query_all_txns(
    client: QBOClient,
    entity: str,
    start_str: str,
    end_str: str,
    page_size: int = 500,
) -> list[dict]:
    # PLAN-EXEMPT: batch-4 bug 7 — page through EVERY row via STARTPOSITION; a
    # single MAXRESULTS call silently truncated busy weeks.
    """Page through every <entity> in the date range. QBO caps a page at 1000;
    we loop STARTPOSITION until a short page returns."""
    # Neutralise every interpolated value: entity name against the allowlist,
    # date literals single-quote-escaped, pagination ints bounded. QBO has no
    # bind params, so this is the only injection-safe way to build the string.
    safe_entity = qbo_entity(entity)
    safe_start = qbo_escape_str(start_str)
    safe_end = qbo_escape_str(end_str)
    page = qbo_int(min(max(page_size, 1), 1000), min=1, max=1000)
    rows: list[dict] = []
    start_pos = 1
    while True:
        pos = qbo_int(start_pos, min=1)
        q = (
            f"SELECT * FROM {safe_entity} WHERE TxnDate >= '{safe_start}' "
            f"AND TxnDate <= '{safe_end}' "
            f"STARTPOSITION {pos} MAXRESULTS {page}"
        )
        resp = client.query(q)  # SA-OK: QBO REST pagination over the API, not an ORM/DB query (client is QBOClient)
        batch = resp.get("QueryResponse", {}).get(entity, [])
        rows.extend(batch)
        if len(batch) < page:
            break
        start_pos += page
    return rows


def _fetch_transactions_for_period(
    client: QBOClient,
    start: date,
    end: date,
    max_results: int = 500,
) -> list[dict]:
    """
    Query QBO for every cash-affecting transaction between start and end and
    return a normalized list. A reconciled bank register is dominated by
    customer Payments, vendor BillPayments and SalesReceipts as much as by
    Purchases and Deposits — querying only the latter two silently dropped the
    former, so the weekly view "missed" prior-week cash despite a clean bank rec.

    Transfers are intentionally excluded: a transfer between two of the
    company's own accounts is net-zero to company-level inflow/outflow, so
    counting it would double the totals. Bank-account-scoped journal entries
    need the account-scoped TransactionList report (a separate follow-up), not
    a blanket JE pull whose multi-line debit/credit cash impact is ambiguous here.
    """
    start_str = start.isoformat()
    end_str = end.isoformat()
    results: list[dict] = []

    # ── Purchases (money out) ─────────────────────────────────────────────
    try:
        for p in _query_all_txns(client, "Purchase", start_str, end_str, max_results):
            lines = p.get("Line", [])
            desc = p.get("PrivateNote") or ""
            account_name = ""
            if lines:
                detail = (
                    lines[0].get("AccountBasedExpenseLineDetail")
                    or lines[0].get("ItemBasedExpenseLineDetail")
                    or {}
                )
                account_name = detail.get("AccountRef", {}).get("name", "")
                if not desc:
                    desc = lines[0].get("Description", "")

            entity = p.get("EntityRef", {})
            results.append({
                "id": f"purchase-{p['Id']}",
                "txn_date": p.get("TxnDate", ""),
                "amount": float(p.get("TotalAmt", 0)),
                "direction": "outflow",
                "description": desc or p.get("DocNumber", ""),
                "entity_name": entity.get("name", "") if entity else "",
                "account_name": account_name,
                "txn_type": "Purchase",
            })
    except Exception as e:
        logger.warning(f"[WeeklyReview] Purchase query failed: {e}")

    # ── Deposits (money in) ───────────────────────────────────────────────
    try:
        for d in _query_all_txns(client, "Deposit", start_str, end_str, max_results):
            lines = d.get("Line", [])
            desc = d.get("PrivateNote") or ""
            account_name = ""
            entity_name = ""
            if lines:
                detail = lines[0].get("DepositLineDetail", {})
                account_name = detail.get("AccountRef", {}).get("name", "")
                entity_ref = detail.get("Entity", {}).get("EntityRef", {})
                entity_name = entity_ref.get("name", "") if entity_ref else ""
                if not desc:
                    desc = lines[0].get("Description", "")

            results.append({
                "id": f"deposit-{d['Id']}",
                "txn_date": d.get("TxnDate", ""),
                "amount": abs(float(d.get("TotalAmt", 0))),
                "direction": "inflow",
                "description": desc or "",
                "entity_name": entity_name,
                "account_name": account_name,
                "txn_type": "Deposit",
            })
    except Exception as e:
        logger.warning(f"[WeeklyReview] Deposit query failed: {e}")

    # ── Customer Payments (money in) ──────────────────────────────────────
    try:
        for pmt in _query_all_txns(client, "Payment", start_str, end_str, max_results):
            customer = pmt.get("CustomerRef", {}) or {}
            results.append({
                "id": f"payment-{pmt['Id']}",
                "txn_date": pmt.get("TxnDate", ""),
                "amount": abs(float(pmt.get("TotalAmt", 0))),
                "direction": "inflow",
                "description": pmt.get("PrivateNote") or f"Payment {pmt.get('DocNumber', '')}".strip(),
                "entity_name": customer.get("name", ""),
                "account_name": (pmt.get("DepositToAccountRef", {}) or {}).get("name", ""),
                "txn_type": "Payment",
            })
    except Exception as e:
        logger.warning(f"[WeeklyReview] Payment query failed: {e}")

    # ── Sales Receipts (cash sales, money in) ─────────────────────────────
    try:
        for sr in _query_all_txns(client, "SalesReceipt", start_str, end_str, max_results):
            customer = sr.get("CustomerRef", {}) or {}
            results.append({
                "id": f"salesreceipt-{sr['Id']}",
                "txn_date": sr.get("TxnDate", ""),
                "amount": abs(float(sr.get("TotalAmt", 0))),
                "direction": "inflow",
                "description": sr.get("PrivateNote") or f"Sales receipt {sr.get('DocNumber', '')}".strip(),
                "entity_name": customer.get("name", ""),
                "account_name": (sr.get("DepositToAccountRef", {}) or {}).get("name", ""),
                "txn_type": "SalesReceipt",
            })
    except Exception as e:
        logger.warning(f"[WeeklyReview] SalesReceipt query failed: {e}")

    # ── Bill Payments (vendor outflow) ────────────────────────────────────
    try:
        for bp in _query_all_txns(client, "BillPayment", start_str, end_str, max_results):
            vendor = bp.get("VendorRef", {}) or {}
            results.append({
                "id": f"billpayment-{bp['Id']}",
                "txn_date": bp.get("TxnDate", ""),
                "amount": abs(float(bp.get("TotalAmt", 0))),
                "direction": "outflow",
                "description": bp.get("PrivateNote") or f"Bill payment {bp.get('DocNumber', '')}".strip(),
                "entity_name": vendor.get("name", ""),
                "account_name": "",
                "txn_type": "BillPayment",
            })
    except Exception as e:
        logger.warning(f"[WeeklyReview] BillPayment query failed: {e}")

    return results


def _week_totals(txns: List[Dict[str, Any]]) -> Dict[str, float]:
    """Compute total inflow, outflow, and net for a list of transactions."""
    inflow = sum(t["amount"] for t in txns if t["direction"] == "inflow")
    outflow = sum(t["amount"] for t in txns if t["direction"] == "outflow")
    return {"inflow": inflow, "outflow": outflow, "net": inflow - outflow, "count": len(txns)}


def _detect_anomalies(
    current_txns: List[Dict[str, Any]],
    history_txns: List[Dict[str, Any]],
    z_threshold: float = 1.8,
) -> List[Dict[str, Any]]:
    """
    Flag individual transactions in current_txns whose amount deviates from
    the rolling historical distribution (by direction) by more than z_threshold
    standard deviations.
    """
    hist_inflow = [t["amount"] for t in history_txns if t["direction"] == "inflow"]
    hist_outflow = [t["amount"] for t in history_txns if t["direction"] == "outflow"]

    def stats(vals):
        if len(vals) < 2:
            return None, None
        return statistics.mean(vals), statistics.stdev(vals)

    in_mean, in_std = stats(hist_inflow)
    out_mean, out_std = stats(hist_outflow)

    anomalies = []
    for t in current_txns:
        mean = in_mean if t["direction"] == "inflow" else out_mean
        std = in_std if t["direction"] == "inflow" else out_std
        if mean is None or std is None or std == 0:
            continue
        z = (t["amount"] - mean) / std
        if abs(z) >= z_threshold:
            anomaly = dict(t)
            anomaly["z_score"] = round(z, 2)
            anomaly["expected_avg"] = round(mean, 2)
            anomaly["severity"] = "critical" if abs(z) >= 3.0 else "warning"
            anomalies.append(anomaly)

    return sorted(anomalies, key=lambda x: abs(x["z_score"]), reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/summary")
def weekly_review_summary(
    realm_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Returns:
     - current week inflow/outflow/net totals
     - prior 4-week average inflow/outflow/net
     - week-over-week deltas (as percentages)
     - anomalous transactions
    """
    if not realm_id:
        raise HTTPException(status_code=400, detail="realm_id is required")
    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=current_user)
    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="qbo_connection_failed",
            user_message="Could not connect to QuickBooks for this company.",
            exc=e,
            retryable=True,
            log_context="weekly_review_qbo_connection_failed",
        )

    today = date.today()
    # Current week: last 7 days
    current_start = today - timedelta(days=7)
    current_end = today

    # Prior 4 weeks: 5-35 days ago (used for rolling avg)
    prior_start = today - timedelta(days=35)
    prior_end = today - timedelta(days=8)

    try:
        current_txns = _fetch_transactions_for_period(client, current_start, current_end)
        prior_txns = _fetch_transactions_for_period(client, prior_start, prior_end)
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="weekly_review_qbo_query_failed",
            user_message="Weekly QuickBooks transactions could not be loaded.",
            exc=e,
            retryable=True,
            log_context="weekly_review_qbo_query_failed",
        )

    current_totals = _week_totals(current_txns)

    # Prior 4-week weekly average
    prior_inflow = sum(t["amount"] for t in prior_txns if t["direction"] == "inflow")
    prior_outflow = sum(t["amount"] for t in prior_txns if t["direction"] == "outflow")
    prior_net = prior_inflow - prior_outflow
    prior_count = len(prior_txns)

    avg_weekly_inflow = prior_inflow / 4
    avg_weekly_outflow = prior_outflow / 4

    def pct_change(current, prior_avg):
        if prior_avg == 0:
            return None
        return round((current - prior_avg) / prior_avg, 4)

    anomalies = _detect_anomalies(current_txns, prior_txns)

    return {
        "period": {
            "current_start": current_start.isoformat(),
            "current_end": current_end.isoformat(),
            "prior_start": prior_start.isoformat(),
            "prior_end": prior_end.isoformat(),
        },
        "current_week": current_totals,
        "prior_4week_avg": {
            "inflow": round(avg_weekly_inflow, 2),
            "outflow": round(avg_weekly_outflow, 2),
            "net": round((prior_net) / 4, 2),
            "count": round(prior_count / 4, 1),
        },
        "changes": {
            "inflow_pct": pct_change(current_totals["inflow"], avg_weekly_inflow),
            "outflow_pct": pct_change(current_totals["outflow"], avg_weekly_outflow),
        },
        "anomalies": anomalies,
        "transactions": sorted(current_txns, key=lambda x: x["txn_date"], reverse=True),
    }


@router.get("/transactions")
def weekly_transactions(
    realm_id: Optional[str] = None,
    days_back: int = 7,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> List[Dict[str, Any]]:
    """
    Returns the raw transaction list for the past `days_back` days.
    """
    if not realm_id:
        raise HTTPException(status_code=400, detail="realm_id is required")
    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=current_user)
    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="qbo_connection_failed",
            user_message="Could not connect to QuickBooks for this company.",
            exc=e,
            retryable=True,
            log_context="weekly_review_transactions_qbo_connection_failed",
        )

    end = date.today()
    start = end - timedelta(days=days_back)

    txns = _fetch_transactions_for_period(client, start, end)
    return sorted(txns, key=lambda x: x["txn_date"], reverse=True)

from __future__ import annotations

import logging
from datetime import date, timedelta
from collections import defaultdict
from ..qbo_client import QBOClient

logger = logging.getLogger(__name__)


def vendor_spend_summary(qbo_client: QBOClient, limit: int = 1000):
    """
    Returns P&L-based expense volume per VENDOR over the trailing 12 months.

    Sources:
      - Bills (always accrual P&L expense — vendor payables)
      - Purchases / Expenses that have a named vendor/payee reference

    Excludes raw checks with no vendor reference (owner draws posted as checks
    typically have no EntityRef or use the owner's name with a draw account).

    This gives a vendor-level view of P&L operating costs, keeping distributions
    and balance-sheet items out of the picture.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=365)

    results: dict = defaultdict(float)

    # ── 1. Bills (AP invoices from vendors — always P&L) ────────────────────
    bills = qbo_client.query(
        f"SELECT * FROM Bill WHERE TxnDate >= '{start_date}' "
        f"AND TxnDate <= '{end_date}' STARTPOSITION 1 MAXRESULTS {limit}"
    )
    for bill in bills.get("QueryResponse", {}).get("Bill", []):
        vendor = bill.get("VendorRef", {}).get("name", "").strip()
        if not vendor:
            continue
        amount = float(bill.get("TotalAmt", 0) or 0)
        results[vendor] += amount

    # ── 2. Purchases / Expenses with a vendor/payee reference ───────────────
    #    These are credit-card charges, debit-card purchases, or ACH expenses.
    #    We only include rows where EntityRef.name is set (meaning a vendor was
    #    explicitly assigned), which filters out un-categorised bank withdrawals.
    purchases = qbo_client.query(
        f"SELECT * FROM Purchase WHERE TxnDate >= '{start_date}' "
        f"AND TxnDate <= '{end_date}' STARTPOSITION 1 MAXRESULTS {limit}"
    )
    for exp in purchases.get("QueryResponse", {}).get("Purchase", []):
        vendor = exp.get("EntityRef", {}).get("name", "").strip()
        if not vendor:
            # No vendor assigned — skip (likely owner draw or bank fee with no payee)
            continue
        # Skip cheque-type transactions without a vendor (owner draws)
        if exp.get("PaymentType") in ("Check", "Cash") and not vendor:
            continue
        amount = float(exp.get("TotalAmt", 0) or 0)
        results[vendor] += amount

    sorted_vendors = sorted(results.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_vendors": len(sorted_vendors),
        "top_5_vendors": sorted_vendors[:5],
        "vendor_breakdown": sorted_vendors,
        "source": "PnL_vendors",  # accrual Bills + tagged Purchases
    }


# PLAN-EXEMPT: Steven work order — agent reads AP vendor-spend actuals (not budget)
# for "are we missing any vendors in {month}?"; adds a missing-vendor detector.
# Thresholds named (not inlined) per .claude/rules/financial-modeling.md AP-FIN-08.
RECURRING_MIN_MONTHS = 3        # present in >= this many prior months => "recurring"
ABSENT_AMOUNT_EPSILON = 0.005   # target-month spend <= this is treated as $0 / absent


def _resolve_target_month(sorted_months: list, target_month, current_month: str):
    """Pick the month to evaluate for missing/new vendors.

    Explicit ``target_month`` wins; otherwise the latest COMPLETE month (the
    latest observed month strictly before the current calendar month, so a
    partial in-progress month is never mistaken for a real drop-off); otherwise
    the latest observed month.
    """
    if target_month:
        return target_month
    complete = [m for m in sorted_months if m < current_month]
    if complete:
        return complete[-1]
    return sorted_months[-1] if sorted_months else None


def vendor_spend_monthly(
    qbo_client: QBOClient,
    months: int = 6,
    limit: int = 1000,
    target_month: str | None = None,
):
    """
    Returns per-vendor ACTUAL spend broken out by month for the last N months
    (Bills + vendor-tagged Purchases only — no budget, no balance-sheet items).
    Includes MoM growth rate for each vendor.

    ``target_month`` (``YYYY-MM``) is the month evaluated for the derived
    ``missing_vendors`` / ``new_vendors`` lists; it defaults to the latest
    complete month. ``missing_vendors`` are vendors that recur in the prior
    window (spend in >= RECURRING_MIN_MONTHS prior months) but are absent / ~$0
    in the target month — i.e. a likely forgotten or un-entered bill.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=months * 31)

    # month_key → vendor → amount
    monthly: dict = defaultdict(lambda: defaultdict(float))

    # Bills
    bills = qbo_client.query(
        f"SELECT * FROM Bill WHERE TxnDate >= '{start_date}' "
        f"AND TxnDate <= '{end_date}' STARTPOSITION 1 MAXRESULTS {limit}"
    )
    for bill in bills.get("QueryResponse", {}).get("Bill", []):
        vendor = bill.get("VendorRef", {}).get("name", "").strip()
        if not vendor:
            continue
        amount = float(bill.get("TotalAmt", 0) or 0)
        txn_date = bill.get("TxnDate", "")
        if txn_date:
            monthly[txn_date[:7]][vendor] += amount

    # Purchases
    purchases = qbo_client.query(
        f"SELECT * FROM Purchase WHERE TxnDate >= '{start_date}' "
        f"AND TxnDate <= '{end_date}' STARTPOSITION 1 MAXRESULTS {limit}"
    )
    for exp in purchases.get("QueryResponse", {}).get("Purchase", []):
        vendor = exp.get("EntityRef", {}).get("name", "").strip()
        if not vendor:
            continue
        amount = float(exp.get("TotalAmt", 0) or 0)
        txn_date = exp.get("TxnDate", "")
        if txn_date:
            monthly[txn_date[:7]][vendor] += amount

    sorted_months = sorted(monthly.keys())

    # Compute per-vendor totals
    vendor_totals: dict = defaultdict(float)
    for month_data in monthly.values():
        for v, amt in month_data.items():
            vendor_totals[v] += amt

    top_vendors = sorted(vendor_totals.items(), key=lambda x: x[1], reverse=True)[:15]

    growth_analysis = []
    for vendor_name, total in top_vendors:
        month_values = [monthly[m].get(vendor_name, 0) for m in sorted_months]
        if len(month_values) >= 2 and month_values[-2] > 0:
            mom_growth = (month_values[-1] - month_values[-2]) / month_values[-2]
        else:
            mom_growth = None

        growth_analysis.append({
            "vendor": vendor_name,
            "total": round(total, 2),
            "monthly": {m: round(monthly[m].get(vendor_name, 0), 2) for m in sorted_months},
            "mom_growth_pct": round(mom_growth * 100, 1) if mom_growth is not None else None,
        })

    # PLAN-EXEMPT: Steven work order — missing-vendor detector (actuals, not budget).
    # ── Derived missing / new vendors for "are we missing any vendors in {month}?" ──
    # Considers ALL vendors (not just the top-15 growth set), actuals-only. Every
    # `.get(` below is in-memory dict access on the already-fetched payload, NOT a DB
    # query — annotated for the no-query-in-loop hook (false positive on dict.get).
    current_month = end_date.strftime("%Y-%m")
    target_key = _resolve_target_month(sorted_months, target_month, current_month)
    prior_months = [m for m in sorted_months if target_key and m < target_key]

    missing_vendors: list = []
    new_vendors: list = []
    if target_key and prior_months:
        recurring_min = max(2, min(RECURRING_MIN_MONTHS, len(prior_months)))
        target_spend = monthly.get(target_key, {})  # SA-OK: dict access on the in-memory payload, not a DB query
        for vendor in vendor_totals:
            present_amounts = [
                monthly[m][vendor] for m in prior_months
                if monthly[m].get(vendor, 0) > ABSENT_AMOUNT_EPSILON  # SA-OK: dict access on the in-memory payload, not a DB query
            ]
            target_amt = target_spend.get(vendor, 0)  # SA-OK: dict access on the in-memory payload, not a DB query
            if len(present_amounts) >= recurring_min and target_amt <= ABSENT_AMOUNT_EPSILON:
                # Recurring in the prior window but absent / ~$0 in the target month.
                typical = sum(present_amounts) / len(present_amounts)
                missing_vendors.append({
                    "vendor": vendor,
                    "typical_monthly_amount": round(typical, 2),
                    "months_present_of_prior": len(present_amounts),
                    "prior_months_considered": len(prior_months),
                    "target_month_amount": round(target_amt, 2),
                })
            elif target_amt > ABSENT_AMOUNT_EPSILON and not present_amounts:
                # Present in the target month, absent across the whole prior window.
                new_vendors.append({
                    "vendor": vendor,
                    "target_month_amount": round(target_amt, 2),
                })
        missing_vendors.sort(key=lambda x: x["typical_monthly_amount"], reverse=True)
        new_vendors.sort(key=lambda x: x["target_month_amount"], reverse=True)

    if missing_vendors:
        names = ", ".join(
            f"{v['vendor']} (~${v['typical_monthly_amount']:,.0f}/mo)" for v in missing_vendors
        )
        missing_summary = (
            f"{len(missing_vendors)} recurring vendor(s) absent / ~$0 in {target_key} "
            f"(actuals — Bills + tagged Purchases): {names}."
        )
    elif target_key:
        missing_summary = (
            f"No recurring vendors are missing in {target_key} "
            f"(checked actual Bills + tagged Purchases)."
        )
    else:
        missing_summary = "Not enough monthly history to evaluate missing vendors."

    return {
        "months_analyzed": sorted_months,
        "total_vendors": len(vendor_totals),
        "vendors": growth_analysis,
        "source": "PnL_vendors_actuals",  # accrual Bills + tagged Purchases (no budget)
        "target_month": target_key,
        "missing_vendors": missing_vendors,
        "new_vendors": new_vendors,
        "missing_vendors_summary": missing_summary,
    }

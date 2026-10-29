from __future__ import annotations

from datetime import date, timedelta
from collections import defaultdict
from ..qbo_client import QBOClient


def customer_revenue_summary(qbo_client: QBOClient, limit: int = 1000):
    """
    Returns TTM revenue per customer based on Invoices in the trailing 12 months.
    Filters by TxnDate to avoid inflating concentration with historical data.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=365)

    revenue: dict = defaultdict(float)

    invoices = qbo_client.query(
        f"SELECT * FROM Invoice WHERE TxnDate >= '{start_date}' "
        f"AND TxnDate <= '{end_date}' STARTPOSITION 1 MAXRESULTS {limit}"
    )
    for inv in invoices.get("QueryResponse", {}).get("Invoice", []):
        customer = inv.get("CustomerRef", {}).get("name", "Unknown Customer")
        amount = float(inv.get("TotalAmt", 0) or 0)
        revenue[customer] += amount

    sorted_customers = sorted(revenue.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_customers": len(sorted_customers),
        "top_5_customers": sorted_customers[:5],
        "customer_breakdown": sorted_customers,
    }


def customer_revenue_monthly(qbo_client: QBOClient, months: int = 6, limit: int = 1000):
    """
    Returns per-customer revenue broken out by month for the last N months.
    Includes MoM growth rate for each customer.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=months * 31)

    # month_key → customer → amount
    monthly: dict = defaultdict(lambda: defaultdict(float))

    invoices = qbo_client.query(
        f"SELECT * FROM Invoice WHERE TxnDate >= '{start_date}' "
        f"AND TxnDate <= '{end_date}' STARTPOSITION 1 MAXRESULTS {limit}"
    )
    for inv in invoices.get("QueryResponse", {}).get("Invoice", []):
        customer = inv.get("CustomerRef", {}).get("name", "Unknown Customer")
        amount = float(inv.get("TotalAmt", 0) or 0)
        txn_date = inv.get("TxnDate", "")
        if txn_date:
            month_key = txn_date[:7]  # YYYY-MM
            monthly[month_key][customer] += amount

    # Build sorted month list
    sorted_months = sorted(monthly.keys())

    # Compute per-customer totals and growth
    customer_totals: dict = defaultdict(float)
    for month_data in monthly.values():
        for cust, amt in month_data.items():
            customer_totals[cust] += amt

    top_customers = sorted(customer_totals.items(), key=lambda x: x[1], reverse=True)[:15]

    # Build growth analysis for top customers
    growth_analysis = []
    for cust_name, total in top_customers:
        month_values = [monthly[m].get(cust_name, 0) for m in sorted_months]
        # Calculate MoM growth if we have at least 2 months
        if len(month_values) >= 2 and month_values[-2] > 0:
            mom_growth = (month_values[-1] - month_values[-2]) / month_values[-2]
        else:
            mom_growth = None

        # 3-month trend
        recent_3 = month_values[-3:] if len(month_values) >= 3 else month_values
        earliest_3 = month_values[:3] if len(month_values) >= 3 else month_values
        if sum(earliest_3) > 0:
            trend_pct = (sum(recent_3) - sum(earliest_3)) / sum(earliest_3)
        else:
            trend_pct = None

        growth_analysis.append({
            "customer": cust_name,
            "total": round(total, 2),
            "monthly": {m: round(monthly[m].get(cust_name, 0), 2) for m in sorted_months},
            "mom_growth_pct": round(mom_growth * 100, 1) if mom_growth is not None else None,
            "trend_pct": round(trend_pct * 100, 1) if trend_pct is not None else None,
        })

    # Sort by trend to highlight fastest growers
    growth_analysis.sort(key=lambda x: x.get("trend_pct") or -999, reverse=True)

    return {
        "months_analyzed": sorted_months,
        "total_customers": len(customer_totals),
        "customers": growth_analysis,
    }

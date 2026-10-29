"""
app/reporting/trend_table.py
──────────────────────────────
Builds a 12-month trailing P&L trend table from QBO data.
Returns month-by-month revenue, COGS, gross profit, GM%, EBITDA, EBITDA%.
Used by the PDF report and the Reports page UI.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..qbo_client import QBOClient
from ..services.redaction import redact_to_string

import logging

logger = logging.getLogger(__name__)


def build_trend_table(client: QBOClient, months: int = 12) -> List[Dict[str, Any]]:
    """
    Fetches a summarize_column_by=Month P&L from QBO for the last `months` months.
    Returns a list of dicts, one per month (ascending date order), with fields:
        month, revenue, cogs, gross_profit, gm_pct, opex, ebitda, ebitda_pct
    """
    from datetime import date, timedelta

    today = date.today()
    end_date = date(today.year, today.month, 1) - timedelta(days=1)  # last day of prior month
    start_date = date(end_date.year - 1, end_date.month + 1, 1) if end_date.month < 12 else date(end_date.year, 1, 1)

    try:
        report = client.get_report("ProfitAndLoss", {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        })
    except Exception as exc:
        logger.warning(
            "Trend table ProfitAndLoss fetch failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
        raise RuntimeError("Trend table data could not be loaded from QuickBooks.") from exc

    # ── Extract column headers (month labels) ────────────────────────────────
    columns = report.get("Columns", {}).get("Column", [])
    month_cols = [c for c in columns if c.get("ColType") == "Money" and "Total" not in (c.get("ColTitle") or "")]
    month_labels = [c.get("ColTitle", "") for c in month_cols]
    n = len(month_labels)
    if n == 0:
        raise RuntimeError("Trend table data could not be parsed from QuickBooks report columns.")

    ALIASES = {
        "income": ["income", "revenue", "total income", "total revenue", "sales"],
        "cogs": ["cost of goods sold", "cost of sales", "total cost of goods sold"],
        "expenses": ["expenses", "total expenses", "operating expenses"],
    }

    def find_section(rows, target):
        aliases = ALIASES.get(target, [target])
        for row in rows:
            if (row.get("type") or "").lower() != "section":
                continue
            header = row.get("Header", {}).get("ColData", [])
            label = (header[0].get("value", "") if header else "").lower().strip()
            group = (row.get("group", "") or "").lower()
            if label in aliases or group in aliases:
                return row
            sub = row.get("Rows", {}).get("Row", [])
            found = find_section(sub, target)
            if found:
                return found
        return None

    def extract_monthly_values(section_row, n_cols):
        if not section_row:
            return [0.0] * n_cols
        summary = section_row.get("Summary", {}).get("ColData", [])
        if summary and len(summary) > 1:
            vals = []
            for c in summary[1:]:
                v = str(c.get("value", "0")).replace(",", "").strip()
                if v.startswith("(") and v.endswith(")"):
                    v = "-" + v[1:-1]
                try:
                    vals.append(float(v))
                except ValueError:
                    vals.append(0.0)
            if len(vals) < n_cols:
                vals += [0.0] * (n_cols - len(vals))
            return vals[:n_cols]
        return [0.0] * n_cols

    root_rows = report.get("Rows", {}).get("Row", [])
    income_vals = extract_monthly_values(find_section(root_rows, "income"), n)
    cogs_vals = extract_monthly_values(find_section(root_rows, "cogs"), n)
    opex_vals = extract_monthly_values(find_section(root_rows, "expenses"), n)

    result = []
    for i, label in enumerate(month_labels):
        rev = income_vals[i]
        cogs = cogs_vals[i]
        opex = opex_vals[i]
        gp = rev - cogs
        ebitda = gp - opex
        result.append({
            "month": label,
            "revenue": round(rev, 2),
            "cogs": round(cogs, 2),
            "gross_profit": round(gp, 2),
            "gm_pct": round(gp / rev, 4) if rev else 0.0,
            "opex": round(opex, 2),
            "ebitda": round(ebitda, 2),
            "ebitda_pct": round(ebitda / rev, 4) if rev else 0.0,
        })

    return result

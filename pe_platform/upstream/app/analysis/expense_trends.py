from __future__ import annotations

import logging
from datetime import date, timedelta
from collections import defaultdict  # noqa: F401
from ..qbo_client import QBOClient

logger = logging.getLogger(__name__)

# Month abbreviation → number (for parsing QBO column titles)
_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# P&L section titles that represent operating expenses
_EXPENSE_TITLES = {
    "expense", "expenses", "operating", "payroll", "salary", "salaries",
    "overhead", "rent", "utilities", "insurance", "professional fees",
    "marketing", "advertising", "travel", "meals", "depreciation",
    "amortization", "other expense", "other expenses", "total expenses",
    "general", "g&a", "cost of revenue", "cost of services",
}


def _parse_qbo_month(label: str) -> str:
    """Convert 'Feb 2025' → '2025-02'.  Returns '' for non-month labels."""
    parts = label.strip().split()
    if len(parts) == 2:
        abbr = parts[0][:3].lower()
        year = parts[1]
        mo = _MONTH_ABBR.get(abbr)
        if mo and year.isdigit() and len(year) == 4:
            return f"{year}-{mo:02d}"
    return ""


def _is_total_col(label: str) -> bool:
    """Return True for QBO's aggregate Total column (not a real month)."""
    l = label.strip().lower()  # noqa: E741
    if l in ("total", ""):
        return True
    # QBO sometimes labels it as a date range: "Feb 26, 2025 Feb 26, 2026"
    # These contain a year digit that is > 4 chars when normalized, or contain commas
    if "," in l:
        return True
    return False


def _extract_amount(cell: dict) -> float:
    try:
        return abs(float(cell.get("value", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def expense_trend_mom(qbo_client: QBOClient) -> dict:
    """
    Returns month-over-month OPERATING EXPENSE totals from the QBO P&L report.

    Uses ProfitAndLoss summarised by Month (same parameters as profit_margin.py)
    so the numbers are accrual-basis and exclude balance-sheet items like owner
    distributions, loan drawdowns, and asset purchases.
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=365)

    report = qbo_client.get_report(
        "ProfitAndLoss",
        {
            "summarize_column_by": "Month",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "accounting_method": "Accrual",
        },
    )

    cols = report.get("Columns", {}).get("Column", [])
    col_keys = [c.get("ColTitle", "") for c in cols]
    n = len(col_keys)

    if n == 0:
        logger.warning("[expense_trend] P&L returned 0 columns")
        raise RuntimeError("Expense trend data could not be loaded from QuickBooks.")

    expense_by_col = [0.0] * n

    def _accumulate(rows: list, target: list, in_expense: bool) -> None:
        for row in rows:
            if row.get("type") == "Section":
                header = (
                    row.get("Header", {})
                    .get("ColData", [{}])[0]
                    .get("value", "")
                    .lower()
                )
                is_exp = in_expense or any(t in header for t in _EXPENSE_TITLES)
                _accumulate(row.get("Rows", {}).get("Row", []), target, is_exp)
            elif row.get("type") in ("Data", "data") and in_expense:
                cells = row.get("ColData", [])
                for i, cell in enumerate(cells[1:], start=0):
                    if i < len(target):
                        target[i] += _extract_amount(cell)

    for section in report.get("Rows", {}).get("Row", []):
        if section.get("type") != "Section":
            continue
        header = (
            section.get("Header", {})
            .get("ColData", [{}])[0]
            .get("value", "")
            .lower()
        )
        is_exp = any(t in header for t in _EXPENSE_TITLES)
        _accumulate(section.get("Rows", {}).get("Row", []), expense_by_col, is_exp)

    trend: list = []
    for i, label in enumerate(col_keys):
        if _is_total_col(label):
            continue
        key = _parse_qbo_month(label)
        if not key:
            continue
        trend.append((key, expense_by_col[i]))

    trend.sort(key=lambda x: x[0])

    return {
        "months": [m for m, _ in trend],
        "expense_totals": [v for _, v in trend],
        "month_over_month": trend,
    }

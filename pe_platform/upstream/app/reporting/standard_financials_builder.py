# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4 (fix #7 — financials Excel monthly columns)
"""
app/reporting/standard_financials_builder.py
──────────────────────────────────────────────
Payload builder for the standard_financials report, moved out of
named_report_builders.py (which sits near the 500-line cap — the same split
covenant_monthly_builder.py / bank_reconciliation_builder.py use).

W-4 fix #7: for the YTD / TTM statement windows every statement is fetched
``summarize_column_by=Month`` (the SAME monthly shape the financials page
renders) so the Excel export carries per-month columns. The P&L and Cash Flow —
FLOW statements — additionally carry QBO's own Total column (Σ month columns ==
Total per row, golden-tested in tests/reporting). The Balance Sheet is
POINT-IN-TIME: each column is that month-end balance and NO aggregate column is
emitted (summing balances across months is meaningless).

DISPLAY-ONLY (AP-DR-06): every figure comes verbatim from the QBO report walk
(``parse_qbo_multicolumn_report``). The single-column detail rows the PDF
sections and the KPI extraction consume are a pure SELECTION over that same
parse — the flow statements take QBO's Total column, the balance sheet takes
the LAST month column (the as-of-end balance) — never a re-summation here.

TENANT-SAFETY PRECONDITION (AP-DR-07): the caller (routers/reporting.py) gates
tenant access via ``check_tenant_access`` BEFORE invoking this builder, and the
QBO client is realm-scoped via ``get_qbo_client_from_db(db, company_id)``.
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any  # CRAFT-OK: Any = dynamic provider-derived QBO report payload boundary

from sqlalchemy.orm import Session

from ..qbo_client import get_qbo_client_from_db
from .data_gatherer import parse_qbo_multicolumn_report, parse_qbo_report_rows
from .named_report_builders import _commentary, _company_name, _metadata

# A report payload is dynamic provider-derived QBO JSON of varying shape.
Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary

# The supported statement windows — mirrors the financials page period selector
# (Current Month / Year to Date / Trailing 12 Months). Unknown values fall back
# to ytd, the page's default view.
_VIEWS = ("ytd", "ttm", "month")


def _normalize_view(view: "str | None") -> str:
    v = (view or "ytd").strip().lower()
    return v if v in _VIEWS else "ytd"


def _period_year_month(period: str) -> "tuple[int, int]":
    """Parse a ``YYYY-MM`` period; fall back to the current month when the period
    is unparsable (the same tolerance the month_end gatherer applies)."""
    try:
        year_s, month_s = period.split("-")[:2]
        year, month = int(year_s), int(month_s)
        if not 1 <= month <= 12:
            raise ValueError(period)
        return year, month
    except (ValueError, AttributeError):
        today = date.today()
        return today.year, today.month


def _statement_window(period: str, view: str) -> "tuple[str, str]":
    """Explicit start/end ISO dates for the statement window, anchored at
    ``period`` (YYYY-MM). These are the SAME windows the financials page's
    period selector computes (calendar YTD / trailing-12-months / single
    month), so the export ties to the on-screen statements."""
    year, month = _period_year_month(period)
    end = date(year, month, calendar.monthrange(year, month)[1])
    if view == "ttm":
        start_year, start_month = (year - 1, month + 1) if month < 12 else (year, 1)
        start = date(start_year, start_month, 1)
    elif view == "month":
        start = date(year, month, 1)
    else:  # ytd — calendar year-to-date, matching the page's getDateRange
        start = date(year, 1, 1)
    return start.isoformat(), end.isoformat()


def _flatten_monthly_rows(rows: "list[Payload]", *, point_in_time: bool = False) -> "list[Payload]":
    """Derive the single-column detail rows (the FIX-6 row shape the PDF sections
    + KPI extraction consume) from the Month-summarized parse — a SELECTION, not a
    re-summation: flow rows take QBO's Total column; point-in-time (balance sheet)
    rows take the LAST month column, i.e. the as-of-end balance."""
    flat: "list[Payload]" = []
    for row in rows:
        if row.get("is_header"):
            value = None
        elif point_in_time:
            # Last month column = the as-of-end balance. When the report carries
            # NO month columns (a Total-only pull walked through the multicolumn
            # seam), the Total column IS the point-in-time balance — fall back.
            values = row.get("values") or []
            value = values[-1] if values else row.get("total")
        else:
            value = row.get("total")
        flat.append({
            "name": row.get("name", ""),
            "value": value,
            "depth": row.get("depth", 0),
            "is_total": bool(row.get("is_total")),
            "is_header": bool(row.get("is_header")),
        })
    return flat


def build_standard_financials(
    db: Session, company_id: str, period: str, view: str = "ytd"
) -> Payload:
    """Assemble the standard-financials payload (Income Statement + Balance Sheet
    + Cash Flow) from the SAME QBO reports the /api/reports/* endpoints fetch.

    ``view`` selects the statement window (ytd default | ttm | month). YTD/TTM
    fetch ``summarize_column_by=Month`` and carry the monthly grids the Excel
    sections render (P&L/CF: months + Total; BS: months only); the month view
    keeps the single-Total-column layout."""
    view = _normalize_view(view)
    client = get_qbo_client_from_db(db, company_id)
    start_date, end_date = _statement_window(period, view)

    monthly = view in ("ytd", "ttm")
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "summarize_column_by": "Month" if monthly else "Total",
        "accounting_method": "Accrual",
    }
    raw_pnl = client.get_report("ProfitAndLoss", params)
    raw_bs = client.get_report("BalanceSheet", params)
    raw_cf = client.get_report("CashFlow", params)

    pnl_monthly_columns: "list[str]" = []
    pnl_monthly_rows: "list[Payload]" = []
    bs_monthly_columns: "list[str]" = []
    bs_monthly_rows: "list[Payload]" = []
    cf_monthly_columns: "list[str]" = []
    cf_monthly_rows: "list[Payload]" = []

    if monthly:
        if raw_pnl:
            pnl_monthly_columns, pnl_monthly_rows = parse_qbo_multicolumn_report(raw_pnl)
        if raw_bs:
            bs_monthly_columns, bs_monthly_rows = parse_qbo_multicolumn_report(raw_bs)
        if raw_cf:
            cf_monthly_columns, cf_monthly_rows = parse_qbo_multicolumn_report(raw_cf)
        pnl_detail = _flatten_monthly_rows(pnl_monthly_rows)
        bs_detail = _flatten_monthly_rows(bs_monthly_rows, point_in_time=True)
        cf_detail = _flatten_monthly_rows(cf_monthly_rows)
    else:
        pnl_detail = parse_qbo_report_rows(raw_pnl) if raw_pnl else []
        bs_detail = parse_qbo_report_rows(raw_bs) if raw_bs else []
        cf_detail = parse_qbo_report_rows(raw_cf) if raw_cf else []

    # KPI totals from the parsed P&L rows (display-only; the same detail-total
    # extraction the KPI binder uses). Imported lazily to avoid a render-engine
    # import cycle at module load.
    from .section_registry import _extract_pnl_totals_from_detail

    kpis = _extract_pnl_totals_from_detail(pnl_detail)
    notes = _commentary(period, {"kpis": kpis})

    return {
        "metadata": _metadata(
            company_id, period, _company_name(client),
            f"Standard Financial Statements — {period}",
        ),
        # The statement window + view the sections annotate their subtitles with.
        "financials_view": view,
        "statement_window": {"start_date": start_date, "end_date": end_date},
        # Single-column detail (the FIX-6 row shape) — PDF sections + KPI binder.
        "pnl_detail": pnl_detail,
        "bs_detail": bs_detail,
        "cf_detail": cf_detail,
        # Monthly grids (W-4 fix #7) — the Excel sections render these when present.
        "pnl_monthly_columns": pnl_monthly_columns,
        "pnl_monthly_rows": pnl_monthly_rows,
        "bs_monthly_columns": bs_monthly_columns,
        "bs_monthly_rows": bs_monthly_rows,
        "cf_monthly_columns": cf_monthly_columns,
        "cf_monthly_rows": cf_monthly_rows,
        "analysis_notes": notes,  # optional section self-skips when empty
        # balance_sheet summary fallback is unused when bs_detail is present, but
        # the section reads it defensively, so provide an empty dict.
        "balance_sheet": {},
        "current_pnl": {},
        "budget_map": {},
        "errors": [],
        "data_quality": {"is_partial": False, "unavailable_sections": []},
    }

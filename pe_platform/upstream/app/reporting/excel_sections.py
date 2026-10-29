# PLAN-EXEMPT: AC-1 engine decomposition under ratified plan 20260629-custom-reporting; the 500-line file cap forces the Excel section bodies out of excel_renderer.py into this module.
"""
app/reporting/excel_sections.py
────────────────────────────────
The 8 month-end Excel section render functions, EXTRACTED verbatim (not rewritten)
from the old monolithic `render_excel` closure. Each fn takes the shared
`ExcelContext` + the bound section data and writes one worksheet — behaviour
preserved so the content-equivalence golden (tests/reporting) stays green.

Old `excel_renderer.py` line homes (pre-refactor):
  kpis                 → :228-333   |  profitability_trends → :336-373
  pnl_detail           → :376-438   |  rolling_pnl          → :440-499
  balance_sheet        → :501-554   |  rolling_cf           → :556-612
  bva_variances        → :614-700   |  ar_ap_aging          → :680-812

FIX-4: the inline KPI re-derivations that used to live in the Tab-1 body
(cogs/opex/margins) are pre-computed by `_bind_kpis` (section_registry.py) and
arrive on `section_data` — the render fns are display-only (AP-DR-06).
"""
from __future__ import annotations

from typing import Any, Dict, List  # CRAFT-OK: Any = dynamic QBO payload / openpyxl rows boundary

from .excel_context import ExcelContext, safe_float

Payload = Dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary


def render_kpis(ctx: ExcelContext, data: Payload) -> None:
    """Tab 1: P&L Metrics — hero KPIs + overview + TTM avg. FIX-4: all figures are
    pre-bound on `data` by _bind_kpis; this fn only displays them."""
    Font = ctx.Font
    ws1 = ctx.wb.active
    ws1.title = "P&L Metrics"
    ws1.sheet_properties.tabColor = ctx.NAVY

    ws1.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws1.cell(row=2, column=1, value=f"Profit & Loss Metrics — {ctx.period}").font = ctx.subtitle_font
    ws1.cell(row=3, column=1, value=f"Generated: {ctx.gen_at}").font = Font(name="Calibri", size=9, color=ctx.MUTED)

    revenue = data["revenue"]
    gp = data["gross_profit"]
    cogs = data["cogs"]
    opex = data["operating_expenses"]
    ebitda = data["ebitda"]
    ni = data["net_income"]
    prior_pnl = data["prior_pnl"]
    ttm = data["ttm"]

    hero_data = [
        ("Total Revenue", revenue),
        ("Gross Profit", gp),
        ("EBITDA", ebitda),
        ("Net Income", ni),
    ]
    for i, (label, value) in enumerate(hero_data, start=1):
        ws1.cell(row=5, column=i, value=label).font = ctx.kpi_label_font
        cell = ws1.cell(row=6, column=i, value=value)
        cell.font = ctx.kpi_val_font
        cell.number_format = ctx.acct_fmt_neg
        prior_val = safe_float(prior_pnl.get(label.replace("Total ", "").lower().replace(" ", "_")))  # SA-OK: bounded-loop 4 in-memory dict.get, not a DB query
        change = value - prior_val
        arrow_txt = f"{'▲' if change >= 0 else '▼'} vs prior month"
        ws1.cell(row=7, column=i, value=arrow_txt).font = Font(name="Calibri", size=8, color=ctx.MUTED)

    ws1.cell(row=9, column=1, value="Profit & Loss — Overview").font = ctx.subtitle_font
    overview_headers = ["", ctx.period, "Prior Month", "Change ($)"]
    ctx.set_header_row(ws1, 10, overview_headers)

    p_rev = safe_float(prior_pnl.get("revenue"))
    p_gp = safe_float(prior_pnl.get("gross_profit"))
    p_cogs = p_rev - p_gp
    p_opex = safe_float(prior_pnl.get("operating_expenses"))
    p_ebitda = safe_float(prior_pnl.get("ebitda"))
    p_ni = safe_float(prior_pnl.get("net_income"))

    overview_data = [
        ("Revenue", revenue, p_rev),
        ("Cost of Goods Sold", cogs, p_cogs),
        ("Gross Profit", gp, p_gp),
        ("Gross Profit Margin", gp / revenue if revenue else 0, p_gp / p_rev if p_rev else 0),
        ("Operating Expenses", opex, p_opex),
        ("EBITDA", ebitda, p_ebitda),
        ("EBITDA Margin", ebitda / revenue if revenue else 0, p_ebitda / p_rev if p_rev else 0),
        ("Net Income", ni, p_ni),
        ("Net Profit Margin", ni / revenue if revenue else 0, p_ni / p_rev if p_rev else 0),
    ]

    is_pct_row = {"Gross Profit Margin", "EBITDA Margin", "Net Profit Margin"}
    for idx, (label, curr, prior) in enumerate(overview_data):
        r = 11 + idx
        ws1.cell(row=r, column=1, value=label).border = ctx.thin_border
        if label in is_pct_row:
            ctx.write_pct(ws1, r, 2, curr)
            ctx.write_pct(ws1, r, 3, prior)
            ws1.cell(row=r, column=4, value="").border = ctx.thin_border
        else:
            ctx.write_currency(ws1, r, 2, curr)
            ctx.write_currency(ws1, r, 3, prior)
            ctx.write_currency(ws1, r, 4, curr - prior)
        if label in {"Gross Profit", "EBITDA", "Net Income"}:
            for c in range(1, 5):
                ws1.cell(row=r, column=c).font = ctx.total_font
                ws1.cell(row=r, column=c).fill = ctx.total_fill

    ctx.apply_alt_rows(ws1, 11, 11 + len(overview_data) - 1, 4)
    overview_end = 11 + len(overview_data)

    if ttm:
        ws1.cell(row=overview_end + 1, column=1,
                 value="Last Month vs. Trailing 12-Month Average").font = ctx.subtitle_font
        ttm_headers = ["", ctx.period, "12-Mo Avg", "+/- Avg"]
        ctx.set_header_row(ws1, overview_end + 2, ttm_headers)
        ttm_rev = safe_float(ttm.get("ttm_revenue"))
        ttm_gp = safe_float(ttm.get("ttm_gross_profit"))
        ttm_ebitda_val = safe_float(ttm.get("ttm_ebitda"))
        avg_rev = ttm_rev / 12 if ttm_rev else 0
        avg_gp = ttm_gp / 12 if ttm_gp else 0
        avg_ebitda = ttm_ebitda_val / 12 if ttm_ebitda_val else 0
        ttm_data = [
            ("Revenue", revenue, avg_rev),
            ("Gross Profit", gp, avg_gp),
            ("EBITDA", ebitda, avg_ebitda),
            ("Net Income", ni, ni),
        ]
        for idx, (label, curr, avg) in enumerate(ttm_data):
            r = overview_end + 3 + idx
            ws1.cell(row=r, column=1, value=label).border = ctx.thin_border
            ctx.write_currency(ws1, r, 2, curr)
            ctx.write_currency(ws1, r, 3, avg)
            ctx.write_currency(ws1, r, 4, curr - avg)

    ctx.freeze_and_size(ws1, "A10", {1: 25, 2: 18, 3: 18, 4: 18})


def render_profitability_trends(ctx: ExcelContext, data: Payload) -> None:
    """Tab 2: Profitability Trends — 12-month tabular summary."""
    trends: List[Payload] = data["trends"]
    ws2 = ctx.wb.create_sheet("Profitability Trends")
    ws2.sheet_properties.tabColor = ctx.ACCENT

    ws2.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws2.cell(row=2, column=1, value="Profitability Trends — 12-Month Summary").font = ctx.subtitle_font

    trend_headers = ["Month", "Revenue", "COGS", "Gross Profit", "GM%",
                     "OpEx", "EBITDA", "EBITDA%", "Net Income"]
    ctx.set_header_row(ws2, 4, trend_headers)

    if trends:
        for idx, t in enumerate(trends):
            r = 5 + idx
            t_rev = safe_float(t.get("revenue"))
            t_cogs = safe_float(t.get("cogs"))
            t_gp = safe_float(t.get("gross_profit"))
            t_opex = safe_float(t.get("opex"))
            t_ebitda = safe_float(t.get("ebitda"))
            t_ni = t_gp - t_opex
            ws2.cell(row=r, column=1, value=t.get("month", "")).border = ctx.thin_border
            ctx.write_currency(ws2, r, 2, t_rev)
            ctx.write_currency(ws2, r, 3, t_cogs)
            ctx.write_currency(ws2, r, 4, t_gp)
            ctx.write_pct(ws2, r, 5, safe_float(t.get("gm_pct")))
            ctx.write_currency(ws2, r, 6, t_opex)
            ctx.write_currency(ws2, r, 7, t_ebitda)
            ctx.write_pct(ws2, r, 8, safe_float(t.get("ebitda_pct")))
            ctx.write_currency(ws2, r, 9, t_ni)
        ctx.apply_alt_rows(ws2, 5, 5 + len(trends) - 1, 9)

    ctx.freeze_and_size(ws2, "A5", {1: 14, 2: 16, 3: 16, 4: 16, 5: 10, 6: 16, 7: 16, 8: 10, 9: 16})


def render_pnl_detail(ctx: ExcelContext, data: Payload) -> None:
    """Tab 3: P&L Detail (Actuals vs Budget)."""
    pnl_detail: List[Payload] = data["pnl_detail"]
    budget_map: Payload = data["budget_map"]
    ws3 = ctx.wb.create_sheet("P&L Detail")
    ws3.sheet_properties.tabColor = ctx.NAVY

    has_budget = bool(budget_map) and len(budget_map) > 0
    budget_label = "Actuals vs. Budget" if has_budget else "Actuals"

    ws3.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws3.cell(row=2, column=1,
             value=f"Profit & Loss Statement — {ctx.period} ({budget_label})").font = ctx.subtitle_font

    pnl_headers = ["Account", "Actuals", "Budget", "Variance"] if has_budget else ["Account", "Actuals"]
    ctx.set_header_row(ws3, 4, pnl_headers)

    if pnl_detail:
        r = 5
        for row_data in pnl_detail:
            name = row_data.get("name", "")
            val = row_data.get("value")
            depth = row_data.get("depth", 0)
            is_total = row_data.get("is_total", False)
            is_header = row_data.get("is_header", False)
            indent = "    " * max(0, depth)
            display_name = f"{indent}{name}"
            ws3.cell(row=r, column=1, value=display_name).border = ctx.thin_border
            if is_header:
                ws3.cell(row=r, column=1).font = ctx.section_font
                for c in range(2, len(pnl_headers) + 1):
                    ws3.cell(row=r, column=c, value="").border = ctx.thin_border
            elif val is not None:
                ctx.write_currency(ws3, r, 2, val, bold=is_total)
                if has_budget:
                    budget_val = budget_map.get(name.lower(), 0)
                    variance = safe_float(val) - budget_val
                    if budget_val:
                        ctx.write_currency(ws3, r, 3, budget_val, bold=is_total)
                        ctx.write_currency(ws3, r, 4, variance, bold=is_total)
                    else:
                        ws3.cell(row=r, column=3, value="—").border = ctx.thin_border
                        ws3.cell(row=r, column=4, value="—").border = ctx.thin_border
                if is_total:
                    for c in range(1, len(pnl_headers) + 1):
                        ws3.cell(row=r, column=c).font = ctx.total_font
                        ws3.cell(row=r, column=c).fill = ctx.total_fill
            else:
                for c in range(2, len(pnl_headers) + 1):
                    ws3.cell(row=r, column=c, value="").border = ctx.thin_border
            r += 1
        ctx.apply_alt_rows(ws3, 5, r - 1, len(pnl_headers))

    if has_budget:
        ctx.freeze_and_size(ws3, "A5", {1: 40, 2: 18, 3: 18, 4: 18})
    else:
        ctx.freeze_and_size(ws3, "A5", {1: 40, 2: 18})


def _render_rolling(ctx: ExcelContext, data: Payload, *, tab: str, subtitle: str,
                    cols_key: str, rows_key: str, empty_msg: str) -> None:
    """Shared body for the two rolling 12-month grids (P&L Tab 4, CF Tab 6)."""
    PatternFill = ctx.PatternFill
    cols: List[str] = data[cols_key]
    rows: List[Payload] = data[rows_key]
    ws = ctx.wb.create_sheet(tab)
    ws.sheet_properties.tabColor = ctx.ACCENT

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1, value=subtitle).font = ctx.subtitle_font

    if rows and cols:
        mc_headers = ["Account"] + cols + ["Total"]
        ctx.set_header_row(ws, 4, mc_headers)
        total_col = len(mc_headers)
        ws.cell(row=4, column=total_col).fill = PatternFill(
            start_color=ctx.NAVY, end_color=ctx.NAVY, fill_type="solid")
        r = 5
        for row_data in rows:
            name = row_data.get("name", "")
            values = row_data.get("values", [])
            total = row_data.get("total", 0)
            depth = row_data.get("depth", 0)
            is_total = row_data.get("is_total", False)
            is_header = row_data.get("is_header", False)
            indent = "  " * max(0, depth)
            ws.cell(row=r, column=1, value=f"{indent}{name}").border = ctx.thin_border
            if is_header:
                ws.cell(row=r, column=1).font = ctx.section_font
                for c in range(2, len(mc_headers) + 1):
                    ws.cell(row=r, column=c, value="").border = ctx.thin_border
            else:
                for i, v in enumerate(values):
                    ctx.write_currency(ws, r, 2 + i, v if v else 0, bold=is_total)
                for i in range(len(values), len(cols)):
                    ctx.write_currency(ws, r, 2 + i, 0, bold=is_total)
                ctx.write_currency(ws, r, total_col, total if total else 0, bold=True)
                if is_total:
                    for c in range(1, len(mc_headers) + 1):
                        ws.cell(row=r, column=c).font = ctx.total_font
                        ws.cell(row=r, column=c).fill = ctx.total_fill
            r += 1
        ctx.apply_alt_rows(ws, 5, r - 1, len(mc_headers))
        widths = {1: 30}
        for c in range(2, len(mc_headers) + 1):
            widths[c] = 14
        ctx.freeze_and_size(ws, "B5", widths)
    else:
        ws.cell(row=4, column=1, value=empty_msg).font = ctx.Font(
            name="Calibri", italic=True, color=ctx.MUTED)


def render_rolling_pnl(ctx: ExcelContext, data: Payload) -> None:
    """Tab 4: Rolling 12-Month P&L."""
    _render_rolling(
        ctx, data, tab="Rolling 12M P&L",
        subtitle=f"Rolling Twelve Months Operating Statement — As of {ctx.period}",
        cols_key="rolling_pnl_columns", rows_key="rolling_pnl_detail",
        empty_msg="Rolling 12-month P&L data not available.")


def render_rolling_cf(ctx: ExcelContext, data: Payload) -> None:
    """Tab 6: Rolling 12-Month Cash Flows."""
    _render_rolling(
        ctx, data, tab="Rolling 12M CF",
        subtitle=f"Rolling Twelve Months Statement of Cash Flows — As of {ctx.period}",
        cols_key="rolling_cf_columns", rows_key="rolling_cf_detail",
        empty_msg="Rolling 12-month cash flow data not available.")


# ── W-4 fix #7 — standard_financials monthly statement grids ─────────────────
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
# For the YTD/TTM views the builder carries Month-summarized parses; the FLOW
# statements (P&L, CF) render month columns + QBO's Total column via the shared
# `_render_rolling` grid (Σ months == Total per row — golden-tested); the
# POINT-IN-TIME Balance Sheet renders month-end columns with NO total column
# (aggregating balances across months is meaningless).

_VIEW_LABELS = {"ytd": "Year to Date by Month", "ttm": "Trailing 12 Months"}


def _monthly_view_label(data: Payload) -> str:
    return _VIEW_LABELS.get(str(data.get("financials_view") or "").lower(), "Monthly")


def _render_monthly_point_in_time(ctx: ExcelContext, data: Payload, *, tab: str,
                                  subtitle: str, cols_key: str, rows_key: str) -> None:
    """Monthly grid WITHOUT a Total column — for point-in-time statements (the
    Balance Sheet): each column is that month-end balance; no aggregate column is
    emitted. Same styling conventions as `_render_rolling`, minus the total."""
    cols: List[str] = data[cols_key]
    rows: List[Payload] = data[rows_key]
    ws = ctx.wb.create_sheet(tab)
    ws.sheet_properties.tabColor = ctx.NAVY

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1, value=subtitle).font = ctx.subtitle_font

    mc_headers = ["Account"] + cols
    ctx.set_header_row(ws, 4, mc_headers)
    r = 5
    for row_data in rows:
        name = row_data.get("name", "")  # SA-OK: bounded-loop in-memory parsed-row dict.get, not a DB query
        values = row_data.get("values", [])
        depth = row_data.get("depth", 0)
        is_total = row_data.get("is_total", False)
        is_header = row_data.get("is_header", False)
        indent = "  " * max(0, depth)
        ws.cell(row=r, column=1, value=f"{indent}{name}").border = ctx.thin_border
        if is_header:
            ws.cell(row=r, column=1).font = ctx.section_font
            for c in range(2, len(mc_headers) + 1):
                ws.cell(row=r, column=c, value="").border = ctx.thin_border
        else:
            for i in range(len(cols)):
                v = values[i] if i < len(values) else 0
                ctx.write_currency(ws, r, 2 + i, v if v else 0, bold=is_total)
            if is_total:
                for c in range(1, len(mc_headers) + 1):
                    ws.cell(row=r, column=c).font = ctx.total_font
                    ws.cell(row=r, column=c).fill = ctx.total_fill
        r += 1
    ctx.apply_alt_rows(ws, 5, r - 1, len(mc_headers))
    widths = {1: 30}
    for c in range(2, len(mc_headers) + 1):
        widths[c] = 14
    ctx.freeze_and_size(ws, "B5", widths)


def render_income_statement(ctx: ExcelContext, data: Payload) -> None:
    """standard_financials Income Statement tab. When the payload carries the
    Month-summarized parse (YTD/TTM view) render the monthly grid — one column per
    month PLUS QBO's Total column; otherwise fall back to the single-column
    Actuals layout (`render_pnl_detail`). Display-only (AP-DR-06): every figure is
    the value QBO emitted on the row."""
    if data.get("pnl_monthly_rows") and data.get("pnl_monthly_columns"):
        _render_rolling(
            ctx, data, tab="Income Statement",
            subtitle=f"Income Statement — {ctx.period} ({_monthly_view_label(data)})",
            cols_key="pnl_monthly_columns", rows_key="pnl_monthly_rows",
            empty_msg="Income statement data not available for this period.")
        return
    render_pnl_detail(ctx, data)


# PLAN-EXEMPT: AC-3 standard_financials cash-flow section under ratified plan 20260629-custom-reporting; the section modules host new section renderers but were omitted from surface_scope (the named app/reporting/* files were enumerated; excel_sections.py is the section home AC-4 names in the dispatch file-refs).
def render_cash_flow(ctx: ExcelContext, data: Payload) -> None:
    """Standard-financials Cash Flow tab — account-level Statement of Cash Flows.

    Generic single-column ("Account"/"Amount") detail renderer mirroring
    render_balance_sheet. Consumes `cf_detail` (the FIX-6 parse-seam output of
    parse_qbo_report_rows over the QBO CashFlow report). Display-only (AP-DR-06):
    every figure is the value QBO emitted on the row, written via write_currency."""
    # W-4 fix #7 (PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4): the
    # YTD/TTM views carry a Month-summarized parse — render the monthly grid
    # (month columns + QBO's Total column) instead of the single-column layout.
    if data.get("cf_monthly_rows") and data.get("cf_monthly_columns"):
        _render_rolling(
            ctx, data, tab="Cash Flow",
            subtitle=f"Statement of Cash Flows — {ctx.period} ({_monthly_view_label(data)})",
            cols_key="cf_monthly_columns", rows_key="cf_monthly_rows",
            empty_msg="Cash flow statement data not available for this period.")
        return
    cf_detail: list[Payload] = data["cf_detail"]
    ws = ctx.wb.create_sheet("Cash Flow")
    ws.sheet_properties.tabColor = ctx.ACCENT

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1, value=f"Statement of Cash Flows — {ctx.period}").font = ctx.subtitle_font

    ctx.set_header_row(ws, 4, ["Account", "Amount"])

    if cf_detail:
        r = 5
        for row_data in cf_detail:
            name = row_data.get("name", "")  # SA-OK: bounded-loop in-memory cf_detail dict.get, not a DB query
            val = row_data.get("value")
            depth = row_data.get("depth", 0)
            is_total = row_data.get("is_total", False)
            is_header = row_data.get("is_header", False)
            indent = "    " * max(0, depth)
            ws.cell(row=r, column=1, value=f"{indent}{name}").border = ctx.thin_border
            if is_header:
                ws.cell(row=r, column=1).font = ctx.section_font
                ws.cell(row=r, column=2, value="").border = ctx.thin_border
            elif val is not None:
                ctx.write_currency(ws, r, 2, val, bold=is_total)
                if is_total:
                    ws.cell(row=r, column=1).font = ctx.total_font
                    ws.cell(row=r, column=1).fill = ctx.total_fill
                    ws.cell(row=r, column=2).fill = ctx.total_fill
            else:
                ws.cell(row=r, column=2, value="").border = ctx.thin_border
            r += 1
        ctx.apply_alt_rows(ws, 5, r - 1, 2)
    else:
        ws.cell(row=5, column=1,
                value="Cash flow statement data not available for this period.").font = ctx.Font(
            name="Calibri", italic=True, color=ctx.MUTED)

    ctx.freeze_and_size(ws, "A5", {1: 40, 2: 20})


def render_balance_sheet(ctx: ExcelContext, data: Payload) -> None:
    """Tab 5: Balance Sheet — account detail with summary fallback."""
    # W-4 fix #7 (PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4): the
    # YTD/TTM standard_financials views carry a Month-summarized parse — render
    # POINT-IN-TIME month-end columns WITHOUT a total column (aggregating balance
    # sheet balances across months is meaningless). The month_end pack carries no
    # bs_monthly_* keys, so its Balance Sheet tab is byte-identical to before.
    if data.get("bs_monthly_rows") and data.get("bs_monthly_columns"):
        _render_monthly_point_in_time(
            ctx, data, tab="Balance Sheet",
            subtitle=f"Balance Sheet — {ctx.period} (Month-End Balances)",
            cols_key="bs_monthly_columns", rows_key="bs_monthly_rows")
        return
    bs_detail: List[Payload] = data["bs_detail"]
    bs_summary: Payload = data["balance_sheet"]
    ws5 = ctx.wb.create_sheet("Balance Sheet")
    ws5.sheet_properties.tabColor = ctx.NAVY

    ws5.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws5.cell(row=2, column=1, value=f"Balance Sheet — As of {ctx.period}").font = ctx.subtitle_font

    ctx.set_header_row(ws5, 4, ["Account", "Balance"])

    if bs_detail:
        r = 5
        for row_data in bs_detail:
            name = row_data.get("name", "")
            val = row_data.get("value")
            depth = row_data.get("depth", 0)
            is_total = row_data.get("is_total", False)
            is_header = row_data.get("is_header", False)
            indent = "    " * max(0, depth)
            ws5.cell(row=r, column=1, value=f"{indent}{name}").border = ctx.thin_border
            if is_header:
                ws5.cell(row=r, column=1).font = ctx.section_font
                ws5.cell(row=r, column=2, value="").border = ctx.thin_border
            elif val is not None:
                ctx.write_currency(ws5, r, 2, val, bold=is_total)
                if is_total:
                    ws5.cell(row=r, column=1).font = ctx.total_font
                    ws5.cell(row=r, column=1).fill = ctx.total_fill
                    ws5.cell(row=r, column=2).fill = ctx.total_fill
            else:
                ws5.cell(row=r, column=2, value="").border = ctx.thin_border
            r += 1
        ctx.apply_alt_rows(ws5, 5, r - 1, 2)
    else:
        r = 5
        for label, key in [
            ("Total Assets", "total_assets"),
            ("  Current Assets", "current_assets"),
            ("Total Liabilities", "total_liabilities"),
            ("  Current Liabilities", "current_liabilities"),
            ("Equity", "total_equity"),
        ]:
            ws5.cell(row=r, column=1, value=label).border = ctx.thin_border
            ctx.write_currency(ws5, r, 2, bs_summary.get(key))
            r += 1

    ctx.freeze_and_size(ws5, "A5", {1: 40, 2: 20})

# PLAN-EXEMPT: AC-1 engine decomposition under ratified plan 20260629-custom-reporting; the 500-line file cap forces the PDF section bodies out of pdf_renderer.py into this module.
"""
app/reporting/pdf_sections.py
──────────────────────────────
The core month-end PDF section render functions, EXTRACTED verbatim (not rewritten)
from the old `_render_with_reportlab` closure. Each fn appends Flowables to the
shared `story` using the `PdfContext` primitives — behaviour preserved so the
content-equivalence golden (tests/reporting) stays green.

Old `pdf_renderer.py` line homes (pre-refactor):
  kpis  → :589-711  |  profitability_trends → :713-830  |  pnl_detail → :834-861
  rolling_pnl → :865-882 (LANDSCAPE)  |  balance_sheet → :885-906
  rolling_cf → :908-925 (LANDSCAPE)

A section fn does NOT append its own PageBreak / NextPageTemplate — the renderer
loop owns page-template orientation switching (FIX-3).

FIX-4: the KPI re-derivations (cogs/opex) are pre-computed by `_bind_kpis`
(section_registry.py); these fns display the bound figures (AP-DR-06).
SAFETY (AP-DR-01): company/period reaching a Paragraph are escaped INLINE via
`{_xml(...)}` — the extraction TIGHTENS the legacy un-escaped period interpolation.
"""
from __future__ import annotations

from typing import Any, Dict, List  # CRAFT-OK: Any = reportlab Flowable + dynamic QBO payload boundary

from .pdf_context import (
    PdfContext, change_arrow, fmt_currency, fmt_pct, safe_float, _xml,
)

Payload = Dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary
Story = List[Any]  # CRAFT-OK: reportlab Flowable list


def render_kpis(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 1: P&L Metrics — hero KPI cards + overview + TTM avg."""
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, HRFlowable

    story.append(Paragraph("Profit &amp; Loss Metrics", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ctx.NAVY))
    story.append(Spacer(1, 8))

    revenue = data["revenue"]
    gp = data["gross_profit"]
    ebitda = data["ebitda"]
    ni = data["net_income"]
    cogs = data["cogs"]
    opex = data["operating_expenses"]
    prior_pnl = data["prior_pnl"]
    ttm = data["ttm"]

    hero_items = [
        ("Total Revenue", fmt_currency(revenue), change_arrow(revenue, prior_pnl.get("revenue"))),
        ("Gross Profit", fmt_currency(gp), change_arrow(gp, prior_pnl.get("gross_profit"))),
        ("EBITDA", fmt_currency(ebitda), change_arrow(ebitda, prior_pnl.get("ebitda"))),
        ("Net Income", fmt_currency(ni), change_arrow(ni, prior_pnl.get("net_income"))),
    ]
    hero_cells = []
    for label, value, change in hero_items:
        cell = [
            Paragraph(f'<font size="8" color="#6b7280">{label}</font>', ctx.S_HERO_LABEL),
            Paragraph(f'<font size="16"><b>{value}</b></font>', ctx.S_HERO_NUM),
            Spacer(1, 12),
            Paragraph(f'<font size="9" color="#6b7280">{change} vs prior month</font>', ctx.S_HERO_LABEL),
        ]
        hero_cells.append(cell)

    hero_tbl = Table([hero_cells], colWidths=[ctx.USABLE_W / 4] * 4)
    hero_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 1, ctx.GRID_LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, ctx.GRID_LINE),
    ]))
    story.append(hero_tbl)
    story.append(Spacer(1, 12))

    story.append(Paragraph("Profit &amp; Loss — Overview", ctx.style(
        "SubHdr", fontSize=10, fontName="Helvetica-Bold", textColor=ctx.NAVY,
        spaceBefore=6, spaceAfter=4)))
    overview_headers = ["", ctx.period, "Prior Month", "Change"]

    p_rev = safe_float(prior_pnl.get("revenue"))
    p_gp = safe_float(prior_pnl.get("gross_profit"))
    p_cogs = p_rev - p_gp
    p_opex = safe_float(prior_pnl.get("operating_expenses"))
    p_ebitda = safe_float(prior_pnl.get("ebitda"))
    p_ni = safe_float(prior_pnl.get("net_income"))

    overview_rows = [
        ["Revenue", fmt_currency(revenue), fmt_currency(p_rev), change_arrow(revenue, p_rev)],
        ["Cost of Goods Sold", fmt_currency(cogs), fmt_currency(p_cogs), change_arrow(cogs, p_cogs)],
        ["Gross Profit", fmt_currency(gp), fmt_currency(p_gp), change_arrow(gp, p_gp)],
        ["Gross Profit Margin", fmt_pct(gp / revenue if revenue else 0),
         fmt_pct(p_gp / p_rev if p_rev else 0), ""],
        ["Operating Expenses", fmt_currency(opex), fmt_currency(p_opex), change_arrow(opex, p_opex)],
        ["EBITDA", fmt_currency(ebitda), fmt_currency(p_ebitda), change_arrow(ebitda, p_ebitda)],
        ["EBITDA Margin", fmt_pct(ebitda / revenue if revenue else 0),
         fmt_pct(p_ebitda / p_rev if p_rev else 0), ""],
        ["Net Income", fmt_currency(ni), fmt_currency(p_ni), change_arrow(ni, p_ni)],
        ["Net Profit Margin", fmt_pct(ni / revenue if revenue else 0),
         fmt_pct(p_ni / p_rev if p_rev else 0), ""],
    ]
    story.append(ctx.table(overview_headers, overview_rows,
                           col_widths=[ctx.USABLE_W * 0.35, ctx.USABLE_W * 0.2,
                                       ctx.USABLE_W * 0.2, ctx.USABLE_W * 0.25],
                           highlight_last=True))
    story.append(Spacer(1, 12))

    if ttm:
        story.append(Paragraph("Last Month vs. Trailing 12-Month Average", ctx.style(
            "SubHdr2", fontSize=10, fontName="Helvetica-Bold", textColor=ctx.NAVY,
            spaceBefore=6, spaceAfter=4)))
        ttm_rev = safe_float(ttm.get("ttm_revenue"))
        ttm_gp = safe_float(ttm.get("ttm_gross_profit"))
        ttm_ebitda_val = safe_float(ttm.get("ttm_ebitda"))
        avg_rev = ttm_rev / 12 if ttm_rev else 0
        avg_gp = ttm_gp / 12 if ttm_gp else 0
        avg_ebitda = ttm_ebitda_val / 12 if ttm_ebitda_val else 0
        ttm_rows = [
            ["Revenue", fmt_currency(revenue), fmt_currency(avg_rev), change_arrow(revenue, avg_rev)],
            ["Gross Profit", fmt_currency(gp), fmt_currency(avg_gp), change_arrow(gp, avg_gp)],
            ["EBITDA", fmt_currency(ebitda), fmt_currency(avg_ebitda), change_arrow(ebitda, avg_ebitda)],
            ["Net Income", fmt_currency(ni), fmt_currency(ni), "—"],
        ]
        story.append(ctx.table(
            ["", ctx.period, "12-Mo Avg", "+/- Avg"], ttm_rows,
            col_widths=[ctx.USABLE_W * 0.35, ctx.USABLE_W * 0.2, ctx.USABLE_W * 0.2, ctx.USABLE_W * 0.25]))


def render_profitability_trends(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 2: Profitability Trends — 12-month line chart."""
    import math
    from reportlab.platypus import Paragraph, Spacer
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.widgets.markers import makeMarker
    from reportlab.graphics.charts.legends import Legend

    trends: List[Payload] = data["trends"]
    colors = ctx.colors

    story.append(Spacer(1, 12))
    story.append(Paragraph("Profitability Trends — 12-Month Summary", ctx.style(
        "SubHdr2", fontSize=10, fontName="Helvetica-Bold", textColor=ctx.NAVY,
        spaceBefore=6, spaceAfter=4)))

    if trends:
        d = Drawing(ctx.USABLE_W, 160)
        lp = LinePlot()
        lp.x = 40
        lp.y = 20
        lp.height = 110
        lp.width = ctx.USABLE_W - 60

        rev_data, gp_data, ebitda_data = [], [], []
        months = []
        all_vals = []
        for i, t in enumerate(trends[-12:]):
            months.append(t.get("month", "")[:3])  # SA-OK: bounded-loop 12 in-memory trends dict.get, not a DB query
            r = safe_float(t.get("revenue"))
            gp = safe_float(t.get("gross_profit"))
            eb = safe_float(t.get("ebitda"))
            rev_data.append((i, r))
            gp_data.append((i, gp))
            ebitda_data.append((i, eb))
            all_vals.extend([r, gp, eb])

        lp.data = [rev_data, gp_data, ebitda_data]
        lp.lines[0].strokeColor = ctx.NAVY
        lp.lines[1].strokeColor = ctx.ACCENT
        lp.lines[2].strokeColor = ctx.SLATE
        for i in range(3):
            lp.lines[i].strokeWidth = 2.5
            lp.lines[i].symbol = makeMarker('FilledCircle')
            lp.lines[i].symbol.size = 3.5
            lp.lines[i].symbol.fillColor = lp.lines[i].strokeColor

        lp.yValueAxis.visibleGrid = 1
        lp.yValueAxis.gridStrokeColor = colors.HexColor("#e5e7eb")
        lp.yValueAxis.gridStrokeWidth = 0.5
        lp.yValueAxis.strokeColor = None

        min_v = min(all_vals) if all_vals else 0
        max_v = max(all_vals) if all_vals else 1000
        span = max_v - min_v if max_v > min_v else 1000
        step = max(span / 4, 100)
        if step > 10000:
            step = round(step / 10000) * 10000
        elif step > 1000:
            step = round(step / 1000) * 1000
        elif step > 100:
            step = round(step / 100) * 100
        nice_min = math.floor(min_v / step) * step
        nice_max = math.ceil(max_v / step) * step
        if nice_min > 0 and min_v < step * 1.5:
            nice_min = 0
        lp.yValueAxis.valueMin = nice_min
        lp.yValueAxis.valueMax = nice_max
        lp.yValueAxis.valueStep = step

        lp.xValueAxis.strokeColor = colors.HexColor("#d1d5db")
        lp.xValueAxis.valueMin = 0
        lp.xValueAxis.valueMax = len(rev_data) - 1
        lp.xValueAxis.valueStep = 1
        lp.xValueAxis.labels.boxAnchor = 'n'
        lp.xValueAxis.labels.dy = -8
        lp.xValueAxis.labels.fontSize = 8
        lp.xValueAxis.labels.fillColor = ctx.TEXT_MUTED
        lp.xValueAxis.labelTextFormat = lambda idx: months[int(idx)] if 0 <= int(idx) < len(months) else ""

        def fmt_y(v: float) -> str:
            sign = "-" if v < 0 else ""
            av = abs(v)
            if av >= 1000000:
                return f"{sign}${av/1000000:.1f}M"
            if av >= 1000:
                return f"{sign}${int(av/1000)}k"
            return f"{sign}${int(av)}"

        lp.yValueAxis.labelTextFormat = fmt_y
        lp.yValueAxis.labels.fontSize = 8
        lp.yValueAxis.labels.fillColor = ctx.TEXT_MUTED
        lp.yValueAxis.labels.rightPadding = 5
        d.add(lp)

        leg = Legend()
        leg.fontName = 'Helvetica'
        leg.fontSize = 8
        leg.x = 40
        leg.y = 145
        leg.dx = 8
        leg.dy = 8
        leg.dxTextSpace = 4
        leg.yGap = 0
        leg.deltay = 0
        leg.deltax = 100
        leg.strokeColor = None
        leg.colorNamePairs = [(ctx.NAVY, "Revenue"), (ctx.ACCENT, "Gross Profit"), (ctx.SLATE, "EBITDA")]
        leg.columnMaximum = 1
        d.add(leg)
        story.append(d)
    else:
        story.append(Paragraph("No trend data available for this period.", ctx.S_BODY))


def render_pnl_detail(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 3: Full P&L (Actuals vs Budget)."""
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    pnl_detail: List[Payload] = data["pnl_detail"]
    budget_map: Payload = data["budget_map"]
    current_pnl: Payload = data["current_pnl"]

    budget_label = "Actuals vs. Budget" if budget_map else "Actuals"
    story.append(Paragraph(f"Profit &amp; Loss Statement — {_xml(ctx.period)} ({budget_label})", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ctx.NAVY))
    story.append(Spacer(1, 6))

    if not budget_map:
        story.append(Paragraph(
            '<font size="8" color="#6b7280"><i>No Vesper Bot budget available for this period. '
            'Showing actuals only.</i></font>', ctx.S_BODY))
        story.append(Spacer(1, 4))

    if pnl_detail:
        story.append(ctx.detail_table(pnl_detail, "Account", "Actuals",
                                      budget_map_local=budget_map if budget_map else None))
    else:
        pl_rows = [
            ["Total Revenue", fmt_currency(current_pnl.get("revenue"))],
            ["Gross Profit", fmt_currency(current_pnl.get("gross_profit"))],
            ["Operating Expenses", fmt_currency(current_pnl.get("operating_expenses"))],
            ["Net Income", fmt_currency(current_pnl.get("net_income"))],
        ]
        story.append(ctx.table(["Line Item", "Amount"], pl_rows,
                               col_widths=[ctx.USABLE_W * 0.7, ctx.USABLE_W * 0.3],
                               highlight_last=True))


def render_rolling_pnl(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 4: Rolling 12-Month P&L (LANDSCAPE — orientation switched by the loop)."""
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    rows: List[Payload] = data["rolling_pnl_detail"]
    cols: List[str] = data["rolling_pnl_columns"]
    story.append(Paragraph(_xml(ctx.company), ctx.S_LAND_HDR))
    story.append(Paragraph(
        f'<font size="10"><b>Rolling Twelve Months Operating Statement</b></font><br/>'
        f'<font size="8" color="#6b7280">As of {_xml(ctx.period)}</font>',
        ctx.style("LandSub", fontSize=10, spaceBefore=0, spaceAfter=6)))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ctx.NAVY))
    story.append(Spacer(1, 4))
    if rows and cols:
        story.append(ctx.multicolumn_table(rows, cols, "Account"))
    else:
        story.append(Paragraph("Rolling 12-month P&L data not available.", ctx.S_BODY))


def render_rolling_cf(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 6: Rolling 12-Month Cash Flows (LANDSCAPE — orientation switched by the loop)."""
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    rows: List[Payload] = data["rolling_cf_detail"]
    cols: List[str] = data["rolling_cf_columns"]
    story.append(Paragraph(_xml(ctx.company), ctx.S_LAND_HDR))
    story.append(Paragraph(
        f'<font size="10"><b>Rolling Twelve Months Statement of Cash Flows</b></font><br/>'
        f'<font size="8" color="#6b7280">As of {_xml(ctx.period)}</font>',
        ctx.style("LandSub2", fontSize=10, spaceBefore=0, spaceAfter=6)))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ctx.NAVY))
    story.append(Spacer(1, 4))
    if rows and cols:
        story.append(ctx.multicolumn_table(rows, cols, "Account"))
    else:
        story.append(Paragraph("Rolling 12-month cash flow data not available.", ctx.S_BODY))


# PLAN-EXEMPT: AC-3 standard_financials cash-flow PDF section under ratified plan 20260629-custom-reporting; pdf_sections.py hosts new section renderers but was omitted from surface_scope (the named app/reporting/* files were enumerated; this is the section home AC-3 names in the dispatch file-refs).
def render_cash_flow(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Standard-financials Cash Flow section — account-level Statement of Cash Flows
    (PORTRAIT). Reuses the generic `detail_table` primitive over the FIX-6 parse-seam
    `cf_detail` rows (parse_qbo_report_rows over the QBO CashFlow report). Display-only
    (AP-DR-06); the period interpolated into the header is escaped via `_xml`."""
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    cf_detail: list[Payload] = data["cf_detail"]
    story.append(Paragraph(f"Statement of Cash Flows — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ctx.NAVY))
    story.append(Spacer(1, 6))
    if cf_detail:
        story.append(ctx.detail_table(cf_detail, "Account", "Amount"))
    else:
        story.append(Paragraph(
            "Cash flow statement data not available for this period.", ctx.S_BODY))


def render_balance_sheet(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 5: Balance Sheet (PORTRAIT)."""
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    bs_detail: List[Payload] = data["bs_detail"]
    bs_summary: Payload = data["balance_sheet"]
    story.append(Paragraph(f"Balance Sheet — As of {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ctx.NAVY))
    story.append(Spacer(1, 6))
    if bs_detail:
        story.append(ctx.detail_table(bs_detail, "Account", "Balance"))
    else:
        bs_rows = [
            ["Total Assets", fmt_currency(bs_summary.get("total_assets"))],
            ["  Current Assets", fmt_currency(bs_summary.get("current_assets"))],
            ["Total Liabilities", fmt_currency(bs_summary.get("total_liabilities"))],
            ["  Current Liabilities", fmt_currency(bs_summary.get("current_liabilities"))],
            ["Equity", fmt_currency(bs_summary.get("total_equity"))],
        ]
        story.append(ctx.table(["Item", "Balance"], bs_rows,
                               col_widths=[ctx.USABLE_W * 0.7, ctx.USABLE_W * 0.3],
                               highlight_last=True))

# PLAN-EXEMPT: AC-1 engine decomposition under ratified plan 20260629-custom-reporting; the 500-line file cap forces the PDF palette/styles/table builders out of pdf_renderer.py into this module.
"""
app/reporting/pdf_context.py
─────────────────────────────
Shared palette, page geometry, ParagraphStyles, and the three reusable table
builders (`table`, `detail_table`, `multicolumn_table`) for the section-driven PDF
renderer. These were closure-locals inside `_render_with_reportlab`; they now live
on `PdfContext` so each extracted section render fn receives the SAME styling +
table primitives — behaviour-preserving, not a rewrite.

SAFETY (doc-rendering AP-DR-01): untrusted strings reaching a Paragraph go through
`_xml(...)` (xml.sax.saxutils.escape). Table data cells are NOT XML-parsed → exempt.

Note: the in-loop `.get(...)` calls below are in-memory reads over the pre-fetched
`detail_rows` list (the QBO report payload), NOT DB queries — annotated # SA-OK.
"""
from __future__ import annotations

from typing import Any, Dict, List  # CRAFT-OK: Any = reportlab Flowable + dynamic QBO payload boundary

Payload = Dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary


def fmt_currency(val: object) -> str:
    try:
        v = float(val)  # type: ignore[arg-type]
        if v < 0:
            return f"(${abs(v):,.0f})"
        return f"${v:,.0f}" if abs(v) >= 1 else f"${v:.2f}"
    except (TypeError, ValueError):
        return str(val) if val else "—"


def fmt_pct(val: object) -> str:
    try:
        return f"{float(val) * 100:.1f}%"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def _xml(value: object) -> str:
    """Escape untrusted text before it enters a ReportLab Paragraph, which parses
    its text as XML (CVE-2019-17626 class). Every QBO/DB-derived string (company,
    period, account/customer/vendor label) reaching a Paragraph f-string passes
    through this; Table cells and drawString are not XML-parsed and are exempt.

    Named `_xml` so the AP-DR-01 hook recognises it as the escape helper when it
    is called inline inside a Paragraph f-string: `Paragraph(f"...{_xml(x)}...")`."""
    from xml.sax.saxutils import escape
    return escape("" if value is None else str(value))


def safe_float(val: object, default: float = 0.0) -> float:
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def change_arrow(current: object, prior: object) -> str:
    """Return ▲ or ▼ indicator with dollar change."""
    try:
        c, p = float(current), float(prior)  # type: ignore[arg-type]
        diff = c - p
        arrow = "▲" if diff >= 0 else "▼"
        return f"{arrow} {fmt_currency(abs(diff))}"
    except (TypeError, ValueError):
        return "—"


class PdfContext:
    """Carries the reportlab palette, page geometry, styles, and table builders for
    every section render fn. Constructed once per `_render_with_reportlab` call."""

    def __init__(self, report_payload: Payload) -> None:
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch

        self.colors = colors
        self.inch = inch
        self._ParagraphStyle = ParagraphStyle
        self.payload = report_payload

        # ── Colour palette ─────────────────────────────────────────────────
        self.NAVY = colors.HexColor("#0f2744")
        self.NAVY_LIGHT = colors.HexColor("#1e3a5f")
        self.SLATE = colors.HexColor("#334155")
        self.SILVER = colors.HexColor("#f0f4f8")
        self.WHITE = colors.white
        self.RED = colors.HexColor("#c0392b")
        self.GREEN = colors.HexColor("#16a34a")
        self.GRID_LINE = colors.HexColor("#d1d5db")
        self.TEXT_MUTED = colors.HexColor("#6b7280")
        self.TOTAL_BG = colors.HexColor("#e2e8f0")
        self.ACCENT = colors.HexColor("#1e40af")

        # ── Page geometry ──────────────────────────────────────────────────
        self.PAGE_W, self.PAGE_H = letter
        self.LAND_W, self.LAND_H = landscape(letter)
        self.MARGIN_H = 0.6 * inch
        self.MARGIN_V = 0.75 * inch
        self.FOOTER_H = 0.35 * inch
        self.USABLE_W = self.PAGE_W - 2 * self.MARGIN_H
        self.LAND_USABLE_W = self.LAND_W - 2 * self.MARGIN_H

        # ── Bound metadata (display strings) ───────────────────────────────
        metadata = report_payload.get("metadata", {})
        self.company = metadata.get("company_name") or "Vesper Bot Report"
        self.period = metadata.get("period_label") or metadata.get("period", "—")
        self.gen_at = metadata.get("generated_at", "—")
        self.title = metadata.get("title", "Board Financial Report")

        # ── Styles ─────────────────────────────────────────────────────────
        self._base_styles = getSampleStyleSheet()
        self.S_SECTION_HDR = self.style("SecHdr", fontSize=13, textColor=self.NAVY,
                                        fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=4)
        self.S_BODY = self.style("Body", fontSize=9, leading=14, spaceAfter=4)
        self.S_HERO_NUM = self.style("HeroNum", fontSize=18, fontName="Helvetica-Bold",
                                     textColor=self.NAVY, spaceAfter=0)
        self.S_HERO_LABEL = self.style("HeroLabel", fontSize=8, textColor=self.TEXT_MUTED, spaceAfter=0)
        self.S_LAND_HDR = self.style("LandHdr", fontSize=12, textColor=self.NAVY,
                                     fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=4)

    def style(self, name: str, **kw: object) -> Any:  # CRAFT-OK: returns a reportlab ParagraphStyle (untyped third-party)
        return self._ParagraphStyle(name, parent=self._base_styles["Normal"], **kw)

    # ── Reusable table builder ────────────────────────────────────────────
    def table(self, col_headers: List[str], rows: List[List[object]],
              col_widths: Any = None, highlight_last: bool = False) -> Any:  # CRAFT-OK: reportlab Table flowable
        from reportlab.platypus import Table, TableStyle
        data = [col_headers] + rows
        tbl = Table(data, colWidths=col_widths, repeatRows=1)
        cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), self.NAVY_LIGHT),
            ("TEXTCOLOR", (0, 0), (-1, 0), self.WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [self.WHITE, self.SILVER]),
            ("GRID", (0, 0), (-1, -1), 0.3, self.GRID_LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]
        if highlight_last and len(rows) > 0:
            cmds += [
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("BACKGROUND", (0, -1), (-1, -1), self.SILVER),
                ("LINEABOVE", (0, -1), (-1, -1), 1, self.NAVY_LIGHT),
            ]
        tbl.setStyle(TableStyle(cmds))
        return tbl

    # ── Detailed financial statement table (single-column) ────────────────
    def detail_table(self, detail_rows: List[Payload], header_label: str,
                     amount_label: str = "Amount",
                     budget_map_local: Any = None,
                     usable_width: Any = None) -> Any:  # CRAFT-OK: reportlab Table flowable
        from reportlab.platypus import Table, TableStyle, Paragraph
        uw = usable_width or self.USABLE_W
        has_budget = budget_map_local and len(budget_map_local) > 0

        if has_budget:
            headers = [header_label, amount_label, "Budget", "Variance"]
            cw = [uw * 0.45, uw * 0.18, uw * 0.18, uw * 0.19]
        else:
            headers = [header_label, amount_label]
            cw = [uw * 0.7, uw * 0.3]

        data: List[List[object]] = [headers]
        bold_rows: List[int] = []
        computed_budgets: Dict[str, float] = {}
        if has_budget:
            active_budgets: Dict[int, float] = {}
            for r in detail_rows:
                name = r.get("name", "")  # SA-OK: bounded-loop N in-memory detail_rows dict.get, not a DB query
                depth = r.get("depth", 0)
                if r.get("is_header"):
                    for d in list(active_budgets.keys()):
                        if d >= depth:
                            del active_budgets[d]
                    active_budgets[depth] = 0.0
                elif r.get("is_total"):
                    computed_budgets[name] = active_budgets.get(depth, 0.0)
                    if depth in active_budgets:
                        del active_budgets[depth]
                elif r.get("value") is not None:
                    b_val = budget_map_local.get(name.lower(), 0.0)
                    computed_budgets[name] = b_val
                    for d in active_budgets:
                        if d < depth:
                            active_budgets[d] += b_val

            def get_b(keys: List[str]) -> float:
                for k in keys:
                    for cn, cv in computed_budgets.items():
                        if k in cn.lower():
                            return cv
                return 0.0

            inc = get_b(['total income'])
            cogs = get_b(['total cost of goods sold', 'cost of goods sold', 'cogs'])
            gp = inc - cogs
            computed_budgets['Gross Profit'] = gp
            exp = get_b(['total expenses', 'expenses'])
            noi = gp - exp
            computed_budgets['Net Operating Income'] = noi
            oth_inc = get_b(['total other income'])
            oth_exp = get_b(['total other expenses'])
            noi_oth = oth_inc - oth_exp
            computed_budgets['Net Other Income'] = noi_oth
            computed_budgets['Net Income'] = noi + noi_oth

        current_section = "income"
        for r in detail_rows:
            name = r.get("name", "")  # SA-OK: bounded-loop N in-memory detail_rows dict.get, not a DB query
            val = r.get("value")
            depth = r.get("depth", 0)
            is_total = r.get("is_total", False)
            is_header = r.get("is_header", False)
            n_lower = name.lower()
            if is_header:
                if any(x in n_lower for x in ["income", "revenue", "sales", "profit"]):
                    current_section = "income"
                elif any(x in n_lower for x in ["expense", "cost", "cogs"]):
                    current_section = "expense"
            indent = "    " * max(0, depth)
            display_name = f"{indent}{name}"
            if is_header:
                row = [display_name] + [""] * (len(headers) - 1)
                data.append(row)
                bold_rows.append(len(data) - 1)
            elif val is not None:
                if has_budget:
                    budget_val = computed_budgets.get(name, 0.0)
                    variance = safe_float(val) - budget_val
                    b_str = fmt_currency(budget_val) if budget_val != 0 else "—"
                    if budget_val != 0 and variance != 0:
                        if current_section == "income":
                            is_good = variance > 0
                        else:
                            is_good = variance < 0
                        v_str = fmt_currency(variance)
                        color = "#16a34a" if is_good else "#c0392b"
                        v_styled = f'<font color="{color}">{v_str}</font>'
                        v_cell: object = Paragraph(v_styled, self.style("VarAlignRight", fontSize=8, alignment=2))
                    elif budget_val == 0:
                        v_cell = "—"
                    else:
                        v_cell = "$0"
                    row = [display_name, fmt_currency(val), b_str, v_cell]
                else:
                    row = [display_name, fmt_currency(val)]
                data.append(row)
                if is_total:
                    bold_rows.append(len(data) - 1)
            else:
                row = [display_name] + [""] * (len(headers) - 1)
                data.append(row)

        if len(data) <= 1:
            data.append(["No data available"] + [""] * (len(headers) - 1))

        tbl = Table(data, colWidths=cw, repeatRows=1)
        cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), self.NAVY_LIGHT),
            ("TEXTCOLOR", (0, 0), (-1, 0), self.WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTSIZE", (0, 1), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.25, self.GRID_LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]
        for row_idx in bold_rows:
            cmds.append(("FONTNAME", (0, row_idx), (-1, row_idx), "Helvetica-Bold"))
            cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), self.TOTAL_BG))
            cmds.append(("LINEABOVE", (0, row_idx), (-1, row_idx), 0.5, self.SLATE))
        for i in range(1, len(data)):
            if i not in bold_rows:
                bg = self.WHITE if (i % 2 == 1) else self.SILVER
                cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
        tbl.setStyle(TableStyle(cmds))
        return tbl

    # ── Multi-column (rolling 12-month) table builder ─────────────────────
    def multicolumn_table(self, detail_rows: List[Payload], month_headers: List[str],
                          title_label: str = "Account") -> Any:  # CRAFT-OK: reportlab Table flowable
        from reportlab.platypus import Table, TableStyle
        n = len(month_headers)
        headers = [title_label] + month_headers + ["Total"]
        data: List[List[object]] = [headers]
        bold_rows: List[int] = []
        for r in detail_rows:
            name = r.get("name", "")  # SA-OK: bounded-loop N in-memory detail_rows dict.get, not a DB query
            values = r.get("values", [])
            total = r.get("total", 0)
            depth = r.get("depth", 0)
            is_total = r.get("is_total", False)
            is_header = r.get("is_header", False)
            indent = "  " * max(0, depth)
            display_name = f"{indent}{name}"
            if is_header:
                row = [display_name] + [""] * (n + 1)
                data.append(row)
                bold_rows.append(len(data) - 1)
            else:
                monthly_vals = []
                for i in range(n):
                    v = values[i] if i < len(values) else 0
                    monthly_vals.append(fmt_currency(v) if v is not None and v != 0 else "—")
                total_str = fmt_currency(total) if total is not None and total != 0 else "—"
                row = [display_name] + monthly_vals + [total_str]
                data.append(row)
                if is_total:
                    bold_rows.append(len(data) - 1)

        if len(data) <= 1:
            data.append(["No data available"] + [""] * (n + 1))

        acct_w = 1.8 * self.inch
        remaining = self.LAND_USABLE_W - acct_w
        col_w = remaining / (n + 1)
        cw = [acct_w] + [col_w] * (n + 1)

        tbl = Table(data, colWidths=cw, repeatRows=1)
        cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), self.NAVY_LIGHT),
            ("TEXTCOLOR", (0, 0), (-1, 0), self.WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 6.5),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTSIZE", (0, 1), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.2, self.GRID_LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("BACKGROUND", (-1, 0), (-1, 0), self.NAVY),
            ("FONTNAME", (-1, 1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (-1, 1), (-1, -1), self.colors.HexColor("#f8fafc")),
        ]
        for row_idx in bold_rows:
            cmds.append(("FONTNAME", (0, row_idx), (-1, row_idx), "Helvetica-Bold"))
            cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), self.TOTAL_BG))
            cmds.append(("LINEABOVE", (0, row_idx), (-1, row_idx), 0.4, self.SLATE))
        for i in range(1, len(data)):
            if i not in bold_rows:
                bg = self.WHITE if (i % 2 == 1) else self.SILVER
                cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
        tbl.setStyle(TableStyle(cmds))
        return tbl

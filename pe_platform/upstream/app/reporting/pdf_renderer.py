"""
app/reporting/pdf_renderer.py
──────────────────────────────
Section-driven board-pack PDF renderer for Vesper Bot reports.

`render_pdf(payload, definition=None)` builds the BaseDocTemplate cover + portrait
+ landscape page-template scaffold, then LOOPS the definition's sections: for each
it looks up the `SectionType`, checks its `condition`, runs its `binder`, switches
the page template to the section's `pdf_orientation` (FIX-3), emits the page break,
and calls the section's `pdf_render` fn to append Flowables. The default definition
is the month-end pack, which renders content-identically to the old hardcoded
renderer (proven by the golden in tests/reporting).

Sections (month-end order):
  0. Cover  ·  1. P&L Metrics  ·  2. Profitability Trends  ·  3. Full P&L (A vs B)
  4. Rolling 12M P&L (LANDSCAPE)  ·  5. Balance Sheet  ·  6. Rolling 12M CF (LANDSCAPE)
  7. BVA Variances (conditional)  ·  8. AR/AP Aging

TENANT-SAFETY PRECONDITION (doc-rendering AP-DR-07): the caller MUST gate tenant
access and build `report_payload` from tenant-scoped queries BEFORE calling this
renderer. routers/reporting.py + tasks/reporting.py satisfy this.

Falls back to a minimal plain-text PDF if ReportLab is unavailable.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional  # CRAFT-OK: Any = reportlab Flowable + dynamic QBO payload

from .pdf_context import PdfContext, fmt_currency
from .report_definitions import MONTH_END_REPORT_DEFINITION, definition_sections
from .section_registry import SECTION_REGISTRY

logger = logging.getLogger(__name__)

Payload = Dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary


def render_pdf(report_payload: Payload, definition: Optional[Payload] = None) -> bytes:
    """Render the report payload into a board-pack PDF (ReportLab; falls back to a
    minimal plain-text PDF if ReportLab is unavailable)."""
    try:
        import reportlab  # noqa: F401
        return _render_with_reportlab(report_payload, definition)
    except ImportError:
        logger.warning("reportlab not installed — falling back to minimal PDF")
        return _minimal_pdf_fallback(report_payload)


def _render_with_reportlab(report_payload: Payload, definition: Optional[Payload]) -> bytes:
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib import colors
    from reportlab.platypus import (
        Spacer, PageBreak, NextPageTemplate, PageTemplate, Frame, BaseDocTemplate,
    )

    if definition is None:
        definition = MONTH_END_REPORT_DEFINITION

    ctx = PdfContext(report_payload)
    company = ctx.company
    period = ctx.period
    gen_at = ctx.gen_at
    title = ctx.title

    # ── Cover + footer page callbacks (palette via ctx) ────────────────────────
    def _draw_footer(canvas: Any, doc: Any) -> None:  # CRAFT-OK: reportlab canvas/doc
        canvas.saveState()
        pw, _ = doc.pagesize
        canvas.setFillColor(ctx.NAVY)
        canvas.rect(0, 0, pw, ctx.FOOTER_H, fill=1, stroke=0)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(ctx.WHITE)
        canvas.drawString(ctx.MARGIN_H, 10, company)
        canvas.drawCentredString(pw / 2, 10, "No Assurance Provided")
        canvas.drawRightString(pw - ctx.MARGIN_H, 10, f"Page {doc.page}")
        canvas.restoreState()

    def _draw_cover(canvas: Any, doc: Any) -> None:  # CRAFT-OK: reportlab canvas/doc
        canvas.saveState()
        pw, ph = doc.pagesize
        canvas.setFillColor(ctx.NAVY)
        canvas.rect(0, 0, pw, ph, fill=1, stroke=0)
        bar_y = ph * 0.52
        canvas.setFillColor(ctx.ACCENT)
        canvas.rect(0, bar_y, pw, 4, fill=1, stroke=0)
        canvas.setFont("Helvetica", 16)
        canvas.setFillColor(colors.HexColor("#93c5fd"))
        canvas.drawString(ctx.MARGIN_H, bar_y + 62, title)
        canvas.setFont("Helvetica-Bold", 28)
        canvas.setFillColor(ctx.WHITE)
        canvas.drawString(ctx.MARGIN_H, bar_y + 30, company)
        canvas.setFont("Helvetica", 11)
        canvas.setFillColor(colors.HexColor("#cbd5e1"))
        canvas.drawString(ctx.MARGIN_H, bar_y - 30, f"Period: {period}")
        canvas.drawString(ctx.MARGIN_H, bar_y - 50, f"Generated: {gen_at}")
        canvas.setFillColor(ctx.NAVY_LIGHT)
        canvas.rect(0, 0, pw, ctx.FOOTER_H + 4, fill=1, stroke=0)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawString(ctx.MARGIN_H, 10, "Vesper Bot · Cash Flow")
        canvas.drawRightString(pw - ctx.MARGIN_H, 10, "Confidential — For Internal Use Only")
        canvas.restoreState()

    # ── BaseDocTemplate with portrait + landscape templates (scaffold preserved) ─
    buf = io.BytesIO()
    portrait_frame = Frame(
        ctx.MARGIN_H, ctx.FOOTER_H + 0.3 * ctx.inch,
        ctx.USABLE_W, ctx.PAGE_H - ctx.MARGIN_V - ctx.FOOTER_H - 0.3 * ctx.inch,
        id="portrait_frame")
    landscape_frame = Frame(
        ctx.MARGIN_H, ctx.FOOTER_H + 0.3 * ctx.inch,
        ctx.LAND_USABLE_W, ctx.LAND_H - ctx.MARGIN_V - ctx.FOOTER_H - 0.3 * ctx.inch,
        id="landscape_frame")

    doc = BaseDocTemplate(buf, pagesize=letter)
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[portrait_frame], onPage=_draw_cover, pagesize=letter),
        PageTemplate(id="portrait", frames=[portrait_frame], onPage=_draw_footer, pagesize=letter),
        PageTemplate(id="landscape", frames=[landscape_frame], onPage=_draw_footer,
                     pagesize=landscape(letter)),
    ])

    story: List[Any] = []  # CRAFT-OK: reportlab Flowable list

    # ── Determine the FIRST renderable section's orientation so the body opens on
    # the correct page template post-cover. The month-end pack's first section (kpis)
    # is portrait — so this resolves to "portrait" and month-end output is unchanged;
    # a definition whose first section is landscape (the cashflow forecast grid, FIX-3)
    # opens the body directly on landscape rather than rendering a wide grid on a
    # portrait page.
    first_orientation = "portrait"
    for spec in definition_sections(definition):
        sid = spec.get("section_id")  # SA-OK: bounded-loop over the definition's fixed section list, not a DB query
        sect = SECTION_REGISTRY.get(sid) if sid else None
        if sect is None or not sect.condition(report_payload):
            continue
        first_orientation = sect.pdf_orientation
        break

    # ── Page 0 — Cover (then switch to the first section's orientation for the body) ─
    story.append(NextPageTemplate(first_orientation))
    story.append(Spacer(1, 1))
    story.append(PageBreak())

    # ── Section loop — orientation switch + page break per FIX-3 ───────────────
    # `current_template` tracks the live page template; `pending_break` carries a
    # trailing break a section requested (`page_break_after`) so the NEXT section can
    # combine it with an orientation switch (the legacy double-break that produced the
    # blank page between pnl_detail and the landscape rolling P&L).
    current_template = first_orientation  # the body opens on the first section's template (post-cover)
    first = True
    pending_break = False
    for spec in definition_sections(definition):
        section_id = spec.get("section_id")  # SA-OK: bounded-loop over the definition's fixed section list, not a DB query
        section = SECTION_REGISTRY.get(section_id) if section_id else None
        if section is None:
            continue
        if not section.condition(report_payload):
            continue

        orientation = section.pdf_orientation
        page_break_before = spec.get("page_break_before", True)

        if not first:
            if pending_break:
                story.append(PageBreak())  # the prior section's page_break_after
            if orientation != current_template:
                # Orientation change: switch the template, then break onto it.
                story.append(NextPageTemplate(orientation))
                story.append(PageBreak())
                current_template = orientation
            elif page_break_before and not pending_break:
                story.append(PageBreak())
        pending_break = bool(spec.get("page_break_after", False))

        section_data = section.binder(report_payload)
        section.pdf_render(ctx, story, section_data)
        first = False

    doc.build(story)  # DOCGEN-OK: builds into the in-memory io.BytesIO buffer (buf), never a disk path — no artifact at rest
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────────
# Fallback — minimal valid visible PDF (no ReportLab)
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_pdf_fallback(report_payload: Payload) -> bytes:
    """Generate a minimal but valid PDF when ReportLab is unavailable."""
    title = report_payload.get("metadata", {}).get("title", "Financial Report")
    company = report_payload.get("metadata", {}).get("company_name", "")
    period = report_payload.get("metadata", {}).get("period", "")
    kpis = report_payload.get("kpis", {})

    lines = [
        title,
        f"Company: {company}",
        f"Period: {period}",
        "",
        f"Revenue: {fmt_currency(kpis.get('revenue'))}",
        f"Gross Profit: {fmt_currency(kpis.get('gross_profit'))}",
        f"EBITDA: {fmt_currency(kpis.get('ebitda'))}",
        f"Net Income: {fmt_currency(kpis.get('net_income'))}",
        f"Cash: {fmt_currency(kpis.get('cash'))}",
    ]

    stream_parts = ["BT", "/F1 12 Tf"]
    y = 750
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_parts.append(f"1 0 0 1 72 {y} Tm")
        stream_parts.append(f"({safe}) Tj")
        y -= 18
    stream_parts.append("ET")
    content = "\n".join(stream_parts).encode("latin-1")

    buf = b"%PDF-1.4\n"
    offsets = []
    obj1 = b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n"
    obj2 = b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n"
    obj3 = (
        b"3 0 obj\n<</Type /Page /Parent 2 0 R "
        b"/MediaBox [0 0 612 792] "
        b"/Resources <</Font <</F1 4 0 R>>>> "
        b"/Contents 5 0 R>>\nendobj\n"
    )
    obj4 = b"4 0 obj\n<</Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding>>\nendobj\n"
    obj5 = (
        b"5 0 obj\n<</Length " + str(len(content)).encode() + b">>\n"
        b"stream\n" + content + b"\nendstream\nendobj\n"
    )
    for obj in [obj1, obj2, obj3, obj4, obj5]:
        offsets.append(len(buf))
        buf += obj

    xref_start = len(buf)
    num_objs = len(offsets) + 1
    xref = f"xref\n0 {num_objs}\n"
    xref += "0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n"
    trailer = (
        f"trailer\n<</Size {num_objs} /Root 1 0 R>>\n"
        f"startxref\n{xref_start}\n%%EOF\n"
    )
    return buf + xref.encode("latin-1") + trailer.encode("latin-1")

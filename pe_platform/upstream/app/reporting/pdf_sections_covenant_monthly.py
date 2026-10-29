# PLAN-EXEMPT: covenant monthly-grid section under ratified plan 20260630-covenant-redesign; a NEW per-report section module, kept separate from pdf_sections_covenant.py so neither file nears the 500-line cap — the bank-send monthly report's PDF section home named in this plan's dispatch.
"""
app/reporting/pdf_sections_covenant_monthly.py
────────────────────────────────────────────────
The bank-send MONTHLY covenant PDF section renderer (LANDSCAPE — the 12-month + summary
grid is WIDE): one table per active covenant, in the EXACT row order of the bank workbook
(``work/covenant-redesign/design/Covenant-Examples-TARGET.xlsx``).

DISPLAY-ONLY (AP-DR-06): every figure comes from the pre-computed ``CovenantSeries`` via the
shared row-plan (``covenant_monthly_rows``); this module DISPLAYS it and re-figures nothing.
SAFETY (AP-DR-01): the covenant/loan/period strings reaching a Paragraph are escaped inline
via ``{_xml(...)}``; the grid figures land in Table data cells (NOT XML-parsed → exempt).
Honest degradation (AC-3): an unavailable covenant / a ``no_data`` month renders a visible
reason, never a fabricated 0.
"""
from __future__ import annotations

from typing import Any  # CRAFT-OK: Any = reportlab Flowable + dynamic covenant-series boundary

from .covenant_monthly_rows import (
    KIND_MONEY,
    GridRow,
    month_labels,
    rows_for,
    summary_header,
)
from .pdf_context import PdfContext, _xml, fmt_currency

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived covenant-series payload boundary
Story = list[Any]  # CRAFT-OK: reportlab Flowable list


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.4f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_cell(row: GridRow, value: float | None) -> str:
    if value is None:
        return "—"
    if row.kind == KIND_MONEY:
        return fmt_currency(value)
    return _fmt_ratio(value)


def _row_cells(row: GridRow, n_months: int) -> list[object]:
    """The display strings for one grid row: label · 12 month cells · summary cell.
    A single-value row (Covenant Amount / TTM) leaves the other month cells blank,
    mirroring the workbook."""
    out: list[object] = [row.label]
    for j in range(n_months):
        val = row.months[j] if j < len(row.months) else None
        # A single-value row has only element 0 populated → blank the rest.
        if j < len(row.months):
            out.append(_fmt_cell(row, val))
        else:
            out.append("")
    out.append(_fmt_cell(row, row.summary))
    return out


def _render_one(ctx: PdfContext, story: Story, entry: Payload) -> None:
    from reportlab.platypus import Paragraph, Spacer

    series = entry.get("series") or {}
    labels = month_labels(series)
    n_months = len(labels)
    metric = str(entry.get("metric_type", ""))

    story.append(Paragraph(
        f'<b>{_xml(entry.get("name", ""))}</b> — {_xml(entry.get("loan_name", ""))} '
        f'· {_xml(metric)}', ctx.S_LAND_HDR))

    if not entry.get("available") or n_months == 0:
        reason = str(entry.get("reason") or "This covenant could not be computed from QuickBooks.")
        story.append(Paragraph(
            f'<font color="#6b7280"><i>{_xml(reason)}</i></font>', ctx.S_BODY))
        story.append(Spacer(1, 12))
        return

    headers = ["Line", *labels, summary_header(entry)]
    table_rows: list[list[object]] = []
    for grid_row in rows_for(entry):
        table_rows.append(_row_cells(grid_row, n_months))

    # Landscape: a wide label column, the rest split across the 12 months + summary.
    usable = ctx.LAND_USABLE_W
    label_w = usable * 0.20
    other_w = (usable - label_w) / (n_months + 1)
    col_widths = [label_w, *([other_w] * (n_months + 1))]
    story.append(ctx.table(headers, table_rows, col_widths=col_widths))

    flagged = [str(m.get("period", "")) for m in series.get("months", []) if m.get("no_data")]  # SA-OK: in-memory series-dict read, no DB Session
    if flagged:
        story.append(Paragraph(
            f'<font size="7" color="#6b7280">No data (not computed): '
            f'{_xml(", ".join(flagged))}</font>', ctx.S_BODY))
    story.append(Spacer(1, 12))


def render_covenant_monthly(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section (LANDSCAPE): one 12-month two-lens grid per active covenant in the bank
    workbook's exact row order. DISPLAY-ONLY — every figure from ``CovenantSeries``."""
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    covenants: list[Payload] = data.get("monthly_covenants", []) or []

    story.append(Paragraph(
        f"Monthly Covenant Compliance — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 6))

    if not covenants:
        story.append(Paragraph(
            '<font color="#6b7280"><i>No active covenants are configured for this '
            'company.</i></font>', ctx.S_BODY))
        return

    for entry in covenants:
        _render_one(ctx, story, entry)

# PLAN-EXEMPT: covenant monthly-grid section under ratified plan 20260630-covenant-redesign; a NEW per-report section module, kept separate from excel_sections_covenant.py so neither file nears the 500-line cap — the bank-send monthly report's Excel section home named in this plan's dispatch.
"""
app/reporting/excel_sections_covenant_monthly.py
─────────────────────────────────────────────────
The bank-send MONTHLY covenant Excel section renderer: one worksheet per active
covenant, laid out in the EXACT row order of the bank workbook
(``work/covenant-redesign/design/Covenant-Examples-TARGET.xlsx``) — 12 month columns
+ a summary ("Total" / "Amount as Tested") column.

DISPLAY-ONLY (AP-DR-06): every cell comes from the pre-computed ``CovenantSeries`` via
the shared row-plan (``covenant_monthly_rows``); this module DISPLAYS it and re-figures
nothing. SAFETY: every free-text label routes through ``ctx.safe_text`` (formula-injection
neutralized, AP-DR-02); every figure via ``ctx.write_currency`` (money) / ``ctx.write_number``
(ratio). Honest degradation (AC-3): an unavailable covenant / a ``no_data`` month renders a
visible reason cell, never a fabricated 0.
"""
from __future__ import annotations

from typing import Any  # CRAFT-OK: Any = dynamic covenant-series payload boundary

from .covenant_monthly_rows import (
    KIND_MONEY,
    GridRow,
    month_labels,
    rows_for,
    summary_header,
)
from .excel_context import ExcelContext

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived covenant-series payload boundary

_MAX_SHEET_NAME = 31  # openpyxl worksheet-title ceiling


def _sheet_name(existing: set[str], loan: str, name: str) -> str:
    """A unique, ≤31-char worksheet title from the loan+covenant names (Excel caps titles)."""
    base = f"{loan} {name}".strip() or "Covenant"
    for bad in r'[]:*?/\\':
        base = base.replace(bad, " ")
    base = base[:_MAX_SHEET_NAME].strip() or "Covenant"
    title = base
    n = 2
    while title in existing:  # SA-OK: bounded in-memory de-dup over sheet titles, not a DB query
        suffix = f" {n}"
        title = base[: _MAX_SHEET_NAME - len(suffix)] + suffix
        n += 1
    existing.add(title)
    return title


def _write_row(ctx: ExcelContext, ws: Any, r: int, row: GridRow, n_months: int) -> None:  # CRAFT-OK: ws = opaque openpyxl Worksheet
    """Write one grid row: label · 12 month cells · summary cell (display-only)."""
    label_cell = ctx.safe_text(ws, r, 1, row.label)
    label_cell.border = ctx.thin_border
    if row.emphasis:
        label_cell.font = ctx.Font(name="Calibri", bold=True, size=10)

    # Month columns C..N. A single-value row (Covenant Amount / TTM rows) fills only the
    # first month column, mirroring the workbook (C13 / C18 / C19).
    for j in range(n_months):
        col = 2 + j
        val = row.months[j] if j < len(row.months) else None
        if row.kind == KIND_MONEY:
            ctx.write_currency(ws, r, col, val, bold=row.emphasis)
        else:
            ctx.write_number(ws, r, col, val, decimals=4, bold=row.emphasis)

    # Summary column (O) — the "Total" / "Amount as Tested" value.
    sum_col = 2 + n_months
    if row.kind == KIND_MONEY:
        ctx.write_currency(ws, r, sum_col, row.summary, bold=True)
    else:
        ctx.write_number(ws, r, sum_col, row.summary, decimals=4, bold=True)


def _render_one(ctx: ExcelContext, ws: Any, entry: Payload) -> None:  # CRAFT-OK: ws = opaque openpyxl Worksheet
    """Render one covenant's grid onto its own worksheet."""
    Font = ctx.Font
    series = entry.get("series") or {}
    labels = month_labels(series)
    n_months = len(labels)

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    metric = str(entry.get("metric_type", ""))
    title = f"{entry.get('name', '')} — {entry.get('loan_name', '')} ({metric})"
    ctx.safe_text(ws, 2, 1, title).font = ctx.subtitle_font

    if not entry.get("available") or n_months == 0:
        reason = str(entry.get("reason") or "This covenant could not be computed from QuickBooks.")
        ctx.safe_text(ws, 4, 1, reason).font = Font(name="Calibri", italic=True, color=ctx.MUTED)
        ctx.freeze_and_size(ws, "A5", {1: 40})
        return

    # Header row: "Month" + the 12 month titles + the summary header.
    header_row = 4
    ctx.safe_text(ws, header_row, 1, "Month")
    hc = ws.cell(row=header_row, column=1)
    hc.font = ctx.header_font
    hc.fill = ctx.header_fill
    hc.border = ctx.thin_border
    for j, m in enumerate(labels):
        cell = ctx.safe_text(ws, header_row, 2 + j, m)
        cell.font = ctx.header_font
        cell.fill = ctx.header_fill
        cell.border = ctx.thin_border
    sh = ctx.safe_text(ws, header_row, 2 + n_months, summary_header(entry))
    sh.font = ctx.header_font
    sh.fill = ctx.header_fill
    sh.border = ctx.thin_border

    r = header_row + 1
    for grid_row in rows_for(entry):
        _write_row(ctx, ws, r, grid_row, n_months)
        r += 1

    # An honest per-month no-data footnote (AC-3): list any month the engine flagged.
    flagged = [str(m.get("period", "")) for m in series.get("months", []) if m.get("no_data")]  # SA-OK: in-memory series-dict read, no DB Session
    if flagged:
        note = "No data (not computed): " + ", ".join(flagged)
        ctx.safe_text(ws, r + 1, 1, note).font = Font(name="Calibri", italic=True, color=ctx.MUTED)

    widths = {1: 32}
    for j in range(n_months + 1):
        widths[2 + j] = 13
    ctx.freeze_and_size(ws, "B5", widths)


def render_covenant_monthly(ctx: ExcelContext, data: Payload) -> None:
    """Section: one worksheet per active covenant, each the 12-month two-lens grid in
    the bank workbook's exact row order. DISPLAY-ONLY — every cell from ``CovenantSeries``."""
    covenants: list[Payload] = data.get("monthly_covenants", []) or []

    if not covenants:
        ws = ctx.wb.create_sheet("Covenant Monthly")
        ws.sheet_properties.tabColor = ctx.NAVY
        ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
        ws.cell(row=2, column=1, value="Monthly Covenant Compliance").font = ctx.subtitle_font
        ws.cell(row=4, column=1,
                value="No active covenants are configured for this company.").font = ctx.Font(
            name="Calibri", italic=True, color=ctx.MUTED)
        ctx.freeze_and_size(ws, "A5", {1: 40})
        return

    used: set[str] = set()
    for entry in covenants:
        ws = ctx.wb.create_sheet(
            _sheet_name(used, str(entry.get("loan_name", "")), str(entry.get("name", ""))))
        ws.sheet_properties.tabColor = ctx.NAVY
        _render_one(ctx, ws, entry)

# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — the board_pack Excel section
# renderers (AC-1/AC-2/AC-3); a NEW per-report section module, kept separate so no
# existing section file nears the 500-line cap (the W-7 dispatch owns app/reporting/**).
"""
app/reporting/excel_sections_board.py
───────────────────────────────────────
The two board_pack-specific Excel sections (mirrors pdf_sections_board):

- **Board Commentary** tab: per-section 2-3 sentence commentary from the ONE
  bounded LLM pass — or an HONEST per-section placeholder when the pass was
  unavailable / cap-refused / had no data (AC-2: rendering never depends on it).
- **Budget vs. Actuals (board)**: delegates to the EXISTING month-end BVA tab
  when variances exist; otherwise writes an honest empty-state tab (the
  month-end section's `condition` silently skips — a board pack must say WHY).

SAFETY (AP-DR-02): commentary text is MODEL-GENERATED (untrusted) → every cell
routes through ``ctx.safe_text`` (formula-injection neutralized, forced string
type). DISPLAY-ONLY (AP-DR-06): this module re-figures nothing.
"""
from __future__ import annotations

from typing import Any  # CRAFT-OK: Any = openpyxl Worksheet + dynamic payload boundary

from .board_commentary import BOARD_COMMENTARY_SECTIONS
from .excel_context import ExcelContext
from .pdf_sections_board import PLACEHOLDER_TEXT, _STATUS_NOTES

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived report payload boundary


def render_board_commentary(ctx: ExcelContext, data: Payload) -> None:
    """Tab: Board Commentary — one labeled block per board-pack section, honest
    placeholder per missing block; all model text via ctx.safe_text (AP-DR-02)."""
    commentary: dict[str, str] = data.get("board_commentary") or {}
    status = str(data.get("board_commentary_status") or "")

    ws = ctx.wb.create_sheet("Board Commentary")
    ws.sheet_properties.tabColor = ctx.NAVY
    ws.column_dimensions["A"].width = 110

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1, value=f"Board Commentary — {ctx.period}").font = ctx.subtitle_font

    row = 4
    note = _STATUS_NOTES.get(status)
    if note and not commentary:
        cell = ctx.safe_text(ws, row, 1, note)
        cell.font = ctx.Font(name="Calibri", italic=True, color=ctx.MUTED)
        row += 2

    for key, label in BOARD_COMMENTARY_SECTIONS:
        ws.cell(row=row, column=1, value=label).font = ctx.section_font
        row += 1
        text = commentary.get(key)  # SA-OK: bounded-loop 4 in-memory dict.get over the fixed section tuple, not a DB query
        if text:
            cell = ctx.safe_text(ws, row, 1, text)
            cell.alignment = ctx.Alignment(wrap_text=True, vertical="top")
        else:
            cell = ctx.safe_text(ws, row, 1, PLACEHOLDER_TEXT)
            cell.font = ctx.Font(name="Calibri", italic=True, color=ctx.MUTED)
        row += 2


def render_board_bva(ctx: ExcelContext, data: Payload) -> None:
    """Tab: BVA Variances — the existing month-end BVA tab when variances exist;
    an HONEST empty-state tab otherwise (AC-3)."""
    variances = data.get("bva_top_variances") or []
    if variances:
        from .excel_sections_aging import render_bva_variances
        render_bva_variances(ctx, {"bva_top_variances": variances})
        return

    ws = ctx.wb.create_sheet("BVA Variances")
    ws.sheet_properties.tabColor = ctx.NAVY
    ws.column_dimensions["A"].width = 90
    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1,
            value=f"Budget vs. Actual — Top Variances — {ctx.period}").font = ctx.subtitle_font
    if not data.get("budget_available"):
        message = ("No budget is configured for this period — the budget-vs-actuals "
                   "comparison is unavailable.")
    else:
        message = ("A budget exists for this period, but no P&L account matched a "
                   "budget line — there are no variances to report.")
    ws.cell(row=4, column=1, value=message).font = ctx.Font(
        name="Calibri", italic=True, color=ctx.MUTED)

# PLAN-EXEMPT: bank-reconciliation Excel section under ratified plan 20260701-bank-reconciliation (AC-1.4); a NEW per-report section module, kept separate so it stays well under the 500-line cap — the report's Excel section home named in this plan's dispatch (the covenant_monthly family pattern).
"""
app/reporting/excel_sections_bank_reconciliation.py
─────────────────────────────────────────────────────
The bank-reconciliation Excel section renderer: one worksheet with the tie-out
summary (opening → cleared → statement ending → difference) and the outstanding /
uncleared item list.

DISPLAY-ONLY (AP-DR-06): every cell comes from the pre-computed tie-out payload
(``build_bank_reconciliation`` → ``compute_tieout``); this module DISPLAYS it and
re-figures nothing.
SAFETY: every free-text label/description routes through ``ctx.safe_text``
(formula-injection neutralized, AP-DR-02); every figure via ``ctx.write_currency`` (a
real number + accounting number_format, AP-DR-05) — never a pre-formatted currency
string.
Honest empty-state (AC-1.4): when no session matched (``available=False``) a visible
reason cell renders, never a fabricated all-zero tie-out.
"""
from __future__ import annotations

from typing import Any  # CRAFT-OK: Any = dynamic tie-out payload boundary

from .excel_context import ExcelContext

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived reconciliation payload boundary


def _summary_lines(rec: Payload) -> list[tuple[str, object]]:
    """The tie-out summary (label, numeric value) pairs — values carried VERBATIM from
    the tie-out payload; written as real numbers by the caller (display-only)."""
    return [
        ("Opening balance", rec.get("opening_balance")),
        ("Cleared deposits", rec.get("cleared_deposits")),
        ("Cleared withdrawals", rec.get("cleared_withdrawals")),
        ("Cleared balance", rec.get("cleared_balance")),
        ("Statement ending balance", rec.get("statement_ending_balance")),
        ("Difference", rec.get("difference")),
    ]


def _render_summary(ctx: ExcelContext, ws: Any, rec: Payload) -> int:  # CRAFT-OK: ws = opaque openpyxl Worksheet
    """Write the account header + the tie-out summary block; returns the next free row."""
    account = rec.get("account_name", "")
    period_start = rec.get("period_start", "")
    period_end = rec.get("period_end", "")
    ctx.safe_text(ws, 2, 1, f"{account}  ({period_start} → {period_end})").font = ctx.subtitle_font

    if not rec.get("anchor_configured", True):
        ctx.safe_text(
            ws, 3, 1,
            "No clean-start anchor configured — opening balance is a placeholder.",
        ).font = ctx.Font(name="Calibri", italic=True, color=ctx.MUTED)

    header_row = 5
    ctx.safe_text(ws, header_row, 1, "Line")
    ctx.safe_text(ws, header_row, 2, "Amount")
    for col in (1, 2):
        hc = ws.cell(row=header_row, column=col)
        hc.font = ctx.header_font
        hc.fill = ctx.header_fill
        hc.border = ctx.thin_border

    r = header_row + 1
    for label, value in _summary_lines(rec):  # SA-OK: bounded-loop over the 6 in-memory summary pairs, no DB Session
        emphasis = label in ("Difference", "Cleared balance")
        lc = ctx.safe_text(ws, r, 1, label)
        lc.border = ctx.thin_border
        if emphasis:
            lc.font = ctx.Font(name="Calibri", bold=True, size=10)
        ctx.write_currency(ws, r, 2, value, bold=emphasis)
        r += 1

    verdict = ("Reconciled — cleared balance ties to the statement ending balance."
               if rec.get("is_balanced")
               else "Out of balance — difference not yet explained by outstanding items.")
    ctx.safe_text(ws, r + 1, 1, verdict).font = ctx.Font(
        name="Calibri", bold=True,
        color=ctx.GREEN if rec.get("is_balanced") else ctx.RED)
    return r + 3


def _render_outstanding(ctx: ExcelContext, ws: Any, rec: Payload, start_row: int) -> None:  # CRAFT-OK: ws = opaque openpyxl Worksheet
    """Write the outstanding (uncleared) item list below the summary."""
    ctx.safe_text(ws, start_row, 1, "Outstanding (uncleared) items").font = ctx.section_font
    items: list[Payload] = rec.get("outstanding_items", []) or []

    if not items:
        ctx.safe_text(
            ws, start_row + 1, 1,
            "No outstanding items — every book transaction in this account is cleared.",
        ).font = ctx.Font(name="Calibri", italic=True, color=ctx.MUTED)
        return

    header_row = start_row + 1
    for col, label in ((1, "Date"), (2, "Description"), (3, "Amount")):
        cell = ctx.safe_text(ws, header_row, col, label)
        cell.font = ctx.header_font
        cell.fill = ctx.header_fill
        cell.border = ctx.thin_border

    r = header_row + 1
    for item in items:  # SA-OK: bounded-loop over the pre-fetched outstanding-items list, no DB Session
        ctx.safe_text(ws, r, 1, item.get("date", "")).border = ctx.thin_border
        ctx.safe_text(ws, r, 2, item.get("description", "")).border = ctx.thin_border
        ctx.write_currency(ws, r, 3, item.get("amount"))
        r += 1


def render_bank_reconciliation(ctx: ExcelContext, data: Payload) -> None:
    """Section: one worksheet with the tie-out summary + the outstanding item list.
    DISPLAY-ONLY — every cell from the pre-computed tie-out payload."""
    rec: Payload = data.get("reconciliation", {}) or {}

    ws = ctx.wb.create_sheet("Bank Reconciliation")
    ws.sheet_properties.tabColor = ctx.NAVY
    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font

    if not rec.get("available"):
        reason = rec.get("reason") or "No bank reconciliation session for this period."
        ctx.safe_text(ws, 3, 1, reason).font = ctx.Font(
            name="Calibri", italic=True, color=ctx.MUTED)
        ctx.freeze_and_size(ws, "A4", {1: 60})
        return

    next_row = _render_summary(ctx, ws, rec)
    _render_outstanding(ctx, ws, rec, next_row)
    ctx.freeze_and_size(ws, "A6", {1: 32, 2: 40, 3: 16})

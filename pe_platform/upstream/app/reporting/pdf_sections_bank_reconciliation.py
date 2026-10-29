# PLAN-EXEMPT: bank-reconciliation PDF section under ratified plan 20260701-bank-reconciliation (AC-1.4); a NEW per-report section module, kept separate so it stays well under the 500-line cap — the report's PDF section home named in this plan's dispatch (the covenant_monthly family pattern).
"""
app/reporting/pdf_sections_bank_reconciliation.py
───────────────────────────────────────────────────
The bank-reconciliation PDF section renderer (PORTRAIT): a tie-out summary table
(opening → cleared → statement ending → difference) followed by the outstanding /
uncleared item list.

DISPLAY-ONLY (AP-DR-06): every figure comes from the pre-computed tie-out payload
(``build_bank_reconciliation`` → ``compute_tieout``); this module DISPLAYS it and
re-figures nothing.
SAFETY (AP-DR-01): the account name / period / item description strings reaching a
Paragraph are escaped inline via ``{_xml(...)}``; the figures land in Table data cells
(NOT XML-parsed → exempt).
Honest empty-state (AC-1.4): when no session matched (``available=False``) a visible
reason renders, never a fabricated all-zero tie-out.
"""
from __future__ import annotations

from typing import Any  # CRAFT-OK: Any = reportlab Flowable + dynamic tie-out payload boundary

from .pdf_context import PdfContext, _xml, fmt_currency

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived reconciliation payload boundary
Story = list[Any]  # CRAFT-OK: reportlab Flowable list


def _summary_rows(rec: Payload) -> list[list[object]]:
    """The tie-out summary rows (label · amount) — display strings from the payload's
    tie-out figures. Money via fmt_currency (display-only); no re-derivation."""
    return [
        ["Opening balance", fmt_currency(rec.get("opening_balance"))],
        ["Cleared deposits", fmt_currency(rec.get("cleared_deposits"))],
        ["Cleared withdrawals", fmt_currency(rec.get("cleared_withdrawals"))],
        ["Cleared balance", fmt_currency(rec.get("cleared_balance"))],
        ["Statement ending balance", fmt_currency(rec.get("statement_ending_balance"))],
        ["Difference", fmt_currency(rec.get("difference"))],
    ]


# Bank-statement ACH descriptors run ~200 chars ("ORIG CO NAME:… TRACE#:… EED:…");
# a raw string in a ReportLab Table cell does NOT wrap, so an untruncated
# descriptor overflows the Description column and collides with Date (B5-5, the
# garbled live report). Truncate at the DISPLAY edge only — the Excel section and
# the payload keep the full text. Truncation (not a Paragraph wrap) deliberately
# avoids opening the Paragraph XML-markup surface for untrusted bank text.
_DESC_MAX_CHARS = 80


def _item_rows(items: list[Payload]) -> list[list[object]]:
    """Display rows for the outstanding item list — plain in-memory dict reads over the
    pre-shaped list (NOT a DB query); money via fmt_currency (display-only); the
    description truncated to the column (see _DESC_MAX_CHARS note above)."""
    rows: list[list[object]] = []
    for item in items:  # SA-OK: bounded-loop over the in-memory outstanding-items list, no DB Session
        description = str(item.get("description", "") or "")
        if len(description) > _DESC_MAX_CHARS:
            description = description[: _DESC_MAX_CHARS - 1] + "…"
        rows.append([
            item.get("date", ""),
            description,
            fmt_currency(item.get("amount")),
        ])
    return rows


def render_bank_reconciliation(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section (PORTRAIT): the tie-out summary + the outstanding item list. DISPLAY-ONLY
    — every figure from the pre-computed tie-out payload."""
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    rec: Payload = data.get("reconciliation", {}) or {}

    story.append(Paragraph(
        f"Bank Reconciliation — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 6))

    if not rec.get("available"):
        reason = rec.get("reason") or (
            "No bank reconciliation session is available for this period.")
        story.append(Paragraph(
            f'<font color="#6b7280"><i>{_xml(reason)}</i></font>', ctx.S_BODY))
        return

    # Account + period header line (escaped — reaches Paragraph markup).
    story.append(Paragraph(
        f'<b>{_xml(rec.get("account_name", ""))}</b> · '
        f'{_xml(rec.get("period_start", ""))} → {_xml(rec.get("period_end", ""))} '
        f'· {_xml(rec.get("status", ""))}', ctx.S_LAND_HDR))

    if not rec.get("anchor_configured", True):
        story.append(Paragraph(
            '<font size="8" color="#6b7280"><i>No clean-start anchor is configured for '
            'this account — the opening balance is a placeholder, not a confirmed '
            'baseline.</i></font>', ctx.S_BODY))

    # Tie-out summary table (highlight the last row = Difference).
    uw = ctx.USABLE_W
    story.append(ctx.table(
        ["Line", "Amount"], _summary_rows(rec),
        col_widths=[uw * 0.6, uw * 0.4], highlight_last=True))
    story.append(Spacer(1, 4))

    if rec.get("is_balanced"):
        story.append(Paragraph(
            '<font color="#16a34a"><b>Reconciled — the cleared balance ties to the '
            'statement ending balance.</b></font>', ctx.S_BODY))
    else:
        story.append(Paragraph(
            '<font color="#c0392b"><b>Out of balance — the difference above is not yet '
            'explained by outstanding items.</b></font>', ctx.S_BODY))
    story.append(Spacer(1, 10))

    # Outstanding / uncleared item list.
    items: list[Payload] = rec.get("outstanding_items", []) or []
    story.append(Paragraph("Outstanding (uncleared) items", ctx.S_LAND_HDR))
    if not items:
        story.append(Paragraph(
            '<font color="#6b7280"><i>No outstanding items — every book transaction in '
            'this account is cleared.</i></font>', ctx.S_BODY))
        return

    story.append(ctx.table(
        ["Date", "Description", "Amount"], _item_rows(items),
        col_widths=[ctx.USABLE_W * 0.2, ctx.USABLE_W * 0.55, ctx.USABLE_W * 0.25]))

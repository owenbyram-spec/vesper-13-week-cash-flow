# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — the board_pack PDF section
# renderers (AC-1/AC-2/AC-3); a NEW per-report section module, kept separate so no
# existing section file nears the 500-line cap (the W-7 dispatch owns app/reporting/**).
"""
app/reporting/pdf_sections_board.py
─────────────────────────────────────
The two board_pack-specific PDF sections:

- **Board Commentary** (PORTRAIT): the per-section 2-3 sentence commentary the
  ONE bounded LLM pass produced — or an HONEST placeholder per section when the
  pass was cache-missed-and-failed / cap-refused / had no data (AC-2: the
  report always renders; commentary is never a hard dependency).
- **Budget vs. Actuals (board)** (PORTRAIT): delegates to the EXISTING month-end
  BVA renderer when variances exist; otherwise renders an honest empty state
  (the month-end section's `condition` silently skips — a board pack must say
  WHY the section is empty instead).

SAFETY (AP-DR-01): commentary text is MODEL-GENERATED (untrusted) → escaped
INLINE via ``{_xml(...)}`` before it enters a ReportLab Paragraph (which parses
its text as XML). Section labels are developer literals.
DISPLAY-ONLY (AP-DR-06): this module re-figures nothing.
"""
from __future__ import annotations

from typing import Any  # CRAFT-OK: Any = reportlab Flowable + dynamic payload boundary

from .board_commentary import BOARD_COMMENTARY_SECTIONS
from .pdf_context import PdfContext, _xml

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived report payload boundary
Story = list[Any]  # CRAFT-OK: reportlab Flowable list

# The honest per-section placeholder (AC-2) — figures are unaffected, only prose.
PLACEHOLDER_TEXT = (
    "Commentary is unavailable for this section in this build. "
    "The figures in the report are complete and unaffected."
)

_STATUS_NOTES: dict[str, str] = {
    "no_data": "No section data was available to comment on for this period.",
    "cap_exceeded": (
        "AI commentary was skipped for this report: the section data exceeded the "
        "configured commentary token budget. The figures below are complete and unaffected."
    ),
    "unavailable": (
        "AI commentary could not be generated for this report. "
        "The figures below are complete and unaffected."
    ),
}


def render_board_commentary(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section: Board Commentary — one titled 2-3 sentence block per board-pack
    section, honest placeholder per missing block (AP-DR-01: model text escaped)."""
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    commentary: dict[str, str] = data.get("board_commentary") or {}
    status = str(data.get("board_commentary_status") or "")

    story.append(Paragraph(f"Board Commentary — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 6))

    note = _STATUS_NOTES.get(status)
    if note and not commentary:
        story.append(Paragraph(f"<i>{_xml(note)}</i>", ctx.S_BODY))
        story.append(Spacer(1, 8))

    for key, label in BOARD_COMMENTARY_SECTIONS:
        text = commentary.get(key)  # SA-OK: bounded-loop 4 in-memory dict.get over the fixed section tuple, not a DB query
        story.append(Paragraph(f"<b>{_xml(label)}</b>", ctx.S_BODY))
        if text:
            story.append(Paragraph(_xml(text), ctx.S_BODY))
        else:
            story.append(Paragraph(
                f'<font color="#6b7280"><i>{_xml(PLACEHOLDER_TEXT)}</i></font>',
                ctx.S_BODY))
        story.append(Spacer(1, 8))


def render_board_bva(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section: Budget vs. Actuals — the existing month-end BVA renderer when
    variances exist; an HONEST empty state otherwise (AC-3)."""
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    variances = data.get("bva_top_variances") or []
    if variances:
        from .pdf_sections_aging import render_bva_variances
        render_bva_variances(ctx, story, {"bva_top_variances": variances})
        return

    story.append(Paragraph(
        f"Budget vs. Actual — Top Variances — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 8))
    if not data.get("budget_available"):
        message = ("No budget is configured for this period — the budget-vs-actuals "
                   "comparison is unavailable.")
    else:
        message = ("A budget exists for this period, but no P&L account matched a "
                   "budget line — there are no variances to report.")
    story.append(Paragraph(f'<font color="#6b7280"><i>{_xml(message)}</i></font>', ctx.S_BODY))

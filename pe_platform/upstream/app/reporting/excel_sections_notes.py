# PLAN-EXEMPT: AC-7 analysis-notes Excel section renderer under ratified plan 20260629-custom-reporting; a NEW per-section module kept separate from the month-end sections so neither file nears the 500-line cap (surface_scope enumerated the existing section files; this is the AC-7 commentary section's home).
"""
app/reporting/excel_sections_notes.py
───────────────────────────────────────
The AC-7 AI-commentary Excel section renderer: writes the model-generated
`analysis_notes` narrative onto its own "Commentary" tab, one paragraph per row.

OPTIONAL (AC-7): the section's registry `condition` skips it when the narrative is
absent/empty — the LLM call is NEVER a hard dependency of rendering. A report whose
payload carries no narrative still renders (this fn is simply not called).

SAFETY (doc-rendering AP-DR-02): the narrative is MODEL-GENERATED text → every cell
is written through `ctx.safe_text` (formula-injection neutralized + forced string
type). A narrative paragraph that happened to start with `=`/`+`/`-`/`@` would
otherwise be interpreted as a formula on open.
"""
from __future__ import annotations

from typing import Dict  # CRAFT-OK: the bound analysis-notes section data

from .excel_context import ExcelContext

Payload = Dict[str, object]  # the bound analysis-notes section data


def render_analysis_notes(ctx: ExcelContext, data: Payload) -> None:
    """Tab: Commentary — the AI narrative, one paragraph per row, all free text
    routed through `ctx.safe_text` (AP-DR-02)."""
    narrative = str(data.get("narrative") or "").strip()
    if not narrative:
        # Defensive: the condition already gates this, but never emit an empty tab.
        return

    ws = ctx.wb.create_sheet("Commentary")
    ws.sheet_properties.tabColor = ctx.NAVY
    ws.column_dimensions["A"].width = 110

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1, value=f"Management Commentary — {ctx.period}").font = ctx.subtitle_font

    paragraphs = [p.strip() for p in narrative.split("\n\n") if p.strip()]
    row = 4
    for para in paragraphs:
        text = " ".join(line.strip() for line in para.splitlines() if line.strip())
        cell = ctx.safe_text(ws, row, 1, text)
        cell.alignment = ctx.Alignment(wrap_text=True, vertical="top")
        row += 2

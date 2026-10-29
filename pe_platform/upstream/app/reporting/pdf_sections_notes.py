# PLAN-EXEMPT: AC-7 analysis-notes PDF section renderer under ratified plan 20260629-custom-reporting; a NEW per-section module kept separate from the month-end sections so neither file nears the 500-line cap (surface_scope enumerated the existing section files; this is the AC-7 commentary section's home).
"""
app/reporting/pdf_sections_notes.py
─────────────────────────────────────
The AC-7 AI-commentary PDF section renderer (PORTRAIT): renders the model-generated
`analysis_notes` narrative as flowing prose paragraphs.

OPTIONAL (AC-7): the section's registry `condition` skips it when the narrative is
absent/empty — the LLM call is NEVER a hard dependency of rendering. A report whose
payload carries no narrative still renders (this fn is simply not called).

SAFETY (doc-rendering AP-DR-01): the narrative is MODEL-GENERATED text → it is
escaped INLINE via `{_xml(...)}` before it enters a ReportLab Paragraph (which parses
its text as XML). This is the categorically-required escape — there is no doorway.
"""
from __future__ import annotations

from typing import Dict, List  # CRAFT-OK: List used in the Story alias

from .pdf_context import PdfContext, _xml

Payload = Dict[str, object]  # the bound analysis-notes section data
Story = List[object]  # reportlab Flowable list


def render_analysis_notes(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Render the AI commentary as a titled block of prose paragraphs. The narrative
    is split on blank lines into paragraphs; each is XML-escaped before it reaches a
    Paragraph (AP-DR-01)."""
    from reportlab.platypus import Paragraph, Spacer

    narrative = str(data.get("narrative") or "").strip()
    if not narrative:
        # Defensive: the condition already gates this, but never emit an empty section.
        return

    story.append(Paragraph("Management Commentary", ctx.S_SECTION_HDR))
    story.append(Spacer(1, 4))

    # Split the narrative into paragraphs on blank lines (the fallback + the LLM both
    # separate paragraphs with "\n\n"). A single-paragraph narrative renders as one.
    paragraphs = [p.strip() for p in narrative.split("\n\n") if p.strip()]
    for para in paragraphs:
        # Collapse any single newlines inside a paragraph to spaces (flowing prose).
        text = " ".join(line.strip() for line in para.splitlines() if line.strip())
        story.append(Paragraph(_xml(text), ctx.S_BODY))
        story.append(Spacer(1, 6))

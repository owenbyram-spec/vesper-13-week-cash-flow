# PLAN-EXEMPT: AC-5 covenant PDF section renderer under ratified plan 20260629-custom-reporting; a NEW per-report section module, kept separate from the month-end sections so neither file nears the 500-line cap (the surface_scope enumerated the existing section files; these are the new report's section home named in the AC-5 dispatch).
"""
app/reporting/pdf_sections_covenant.py
─────────────────────────────────────────
The AC-5 lender-covenant PDF section renderers (PORTRAIT): a compliance SUMMARY
(overall status + a per-covenant PASS/WARN/BREACH table) and per-covenant DETAIL
(metric / threshold / current / breach-direction + breakdown sub-components).

DISPLAY-ONLY (AP-DR-06): every figure is the value `build_covenant` carried from the
`/covenants/status` computation; this module DISPLAYS it and re-figures nothing.
SAFETY (AP-DR-01): the period reaching a Paragraph is escaped INLINE via `{_xml(...)}`;
loan/covenant/breakdown free-text strings land in Table data cells (NOT XML-parsed → exempt).
"""
from __future__ import annotations

from typing import Dict, List  # CRAFT-OK: List used in helper signatures

from .pdf_context import PdfContext, fmt_currency, _xml

Payload = Dict[str, object]  # a covenant row / breakdown dict of dynamic shape
Story = List[object]  # reportlab Flowable list

_STATUS_HEX = {
    "PASS": "#166534",
    "WARN": "#b45309",
    "BREACH": "#991b1b",
    "UNKNOWN": "#6b7280",
    "NO_COVENANTS": "#6b7280",
}


def _num(value: object, decimals: int = 2) -> str:
    """Display a ratio/multiple (DSCR 1.25, NOT currency); em-dash for None."""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)


# Machine-facing breakdown keys; the human ``reason`` beside them is what the lender reads.
_HIDDEN_BREAKDOWN_KEYS = {"error_code", "not_meaningful", "enqueue_required", "persisted_error"}


def _flatten_breakdown(breakdown: object) -> List[tuple]:
    out: List[tuple] = []
    if not isinstance(breakdown, dict):
        return out
    for key, val in breakdown.items():  # SA-OK: bounded-loop over in-memory breakdown dict, not a DB query
        if key in _HIDDEN_BREAKDOWN_KEYS:
            continue
        if isinstance(val, dict):
            out.append((str(val.get("label", key)), val.get("value")))
        else:
            out.append((str(key).replace("_", " ").title(), val))
    return out


def _error_notes(cov: Payload) -> str:
    errors = cov.get("errors")
    if not isinstance(errors, list):
        return ""
    return "; ".join(
        str(e["user_message"]) for e in errors if isinstance(e, dict) and e.get("user_message")
    )


def render_covenant(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section: Loan Covenant Compliance — overall status, summary table, per-covenant
    detail with breakdown."""
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    covenants: List[Payload] = data.get("covenants", []) or []  # type: ignore[assignment]
    overall = str(data.get("overall_status", "UNKNOWN"))
    cov_period = str(data.get("covenant_period", "—"))

    story.append(Paragraph(f"Loan Covenant Compliance — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 6))

    o_hex = _STATUS_HEX.get(overall, "#6b7280")
    story.append(Paragraph(
        f'Overall Status: <font color="{o_hex}"><b>{_xml(overall)}</b></font>', ctx.S_BODY))
    story.append(Paragraph(
        f'<font size="8" color="#6b7280">Evaluation window: {_xml(cov_period)}</font>', ctx.S_BODY))
    story.append(Spacer(1, 8))

    if not covenants:
        story.append(Paragraph(
            '<font color="#6b7280"><i>No active covenants are configured for this '
            'company.</i></font>', ctx.S_BODY))
        return

    # ── Summary table ──────────────────────────────────────────────────────────
    headers = ["Loan", "Covenant", "Metric", "Threshold", "Current", "Status"]
    rows: List[List[object]] = []
    for cov in covenants:
        rows.append([
            cov.get("loan_name", ""),
            cov.get("name", ""),
            cov.get("metric_type", ""),
            _num(cov.get("threshold")),
            _num(cov.get("current_value")),
            str(cov.get("status", "UNKNOWN")),
        ])
    col_widths = [ctx.USABLE_W * c for c in (0.22, 0.24, 0.16, 0.13, 0.13, 0.12)]
    story.append(ctx.table(headers, rows, col_widths=col_widths))
    story.append(Spacer(1, 12))

    # ── Per-covenant detail blocks ─────────────────────────────────────────────
    for cov in covenants:
        s_hex = _STATUS_HEX.get(str(cov.get("status", "UNKNOWN")), "#6b7280")
        direction = str(cov.get("breach_direction", "BELOW"))
        breach_txt = "below threshold" if direction == "BELOW" else "above threshold"
        story.append(Paragraph(
            f'<b>{_xml(cov.get("name", ""))}</b> — {_xml(cov.get("loan_name", ""))} · '
            f'<font color="{s_hex}"><b>{_xml(str(cov.get("status", "UNKNOWN")))}</b></font>',
            ctx.S_BODY))
        detail_rows: List[List[object]] = [
            ["Metric type", str(cov.get("metric_type", ""))],
            ["Threshold", _num(cov.get("threshold"))],
            ["Current value", _num(cov.get("current_value"))],
            ["Breach if", breach_txt],
        ]
        # Why a value is missing (no data / provider error) — the /status row's error messages.
        notes = _error_notes(cov)
        if notes:
            detail_rows.append(["Note", notes])
        for label, val in _flatten_breakdown(cov.get("breakdown")):
            display = fmt_currency(val) if isinstance(val, (int, float)) else str(val)
            detail_rows.append([label, display])
        story.append(ctx.table(["Field", "Value"], detail_rows,
                               col_widths=[ctx.USABLE_W * 0.4, ctx.USABLE_W * 0.6]))
        story.append(Spacer(1, 12))


def _cell_status(cell_data: object) -> str:
    """The stored verdict string for a grid cell, or '' (in-memory dict read, not a query)."""
    if isinstance(cell_data, dict):
        status = cell_data["status"] if "status" in cell_data else None
        return str(status) if status else ""
    return ""


def render_covenant_grid(ctx: PdfContext, story: Story, data: Payload) -> None:
    """AC-2 Section (LANDSCAPE): Covenant Compliance History — a per-covenant row ×
    per-month column PASS/WARN/BREACH grid built from the STORED CovenantRecord
    snapshots.

    DISPLAY-ONLY (AP-DR-06): each cell is the stored status verbatim; re-figures nothing.
    SAFETY (AP-DR-01): the period reaching a Paragraph is escaped inline via `{_xml(...)}`;
    covenant/loan/month strings land in Table data cells (NOT XML-parsed → exempt).
    """
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    months: list[str] = data["grid_months"] if "grid_months" in data else []  # type: ignore[assignment]
    grid_rows: list[Payload] = data["grid_rows"] if "grid_rows" in data else []  # type: ignore[assignment]

    story.append(Paragraph(f"Covenant Compliance History — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 6))

    if not grid_rows or not months:
        story.append(Paragraph(
            '<font color="#6b7280"><i>No covenant compliance snapshots are available '
            'yet.</i></font>', ctx.S_BODY))
        return

    headers = ["Covenant", *months]
    table_rows: list[list[object]] = []
    for row in grid_rows:
        name = row["name"] if "name" in row else ""
        loan = row["loan_name"] if "loan_name" in row else ""
        cells = row["cells"] if "cells" in row else []
        line: list[object] = [f"{name} ({loan})"]
        for cell_data in cells:
            verdict = _cell_status(cell_data)
            line.append(verdict if verdict else "—")
        table_rows.append(line)

    # Landscape: one wide column for the covenant label, the rest split across the months.
    usable = ctx.LAND_USABLE_W
    label_w = usable * 0.28
    month_w = (usable - label_w) / max(len(months), 1)
    col_widths = [label_w, *([month_w] * len(months))]
    story.append(ctx.table(headers, table_rows, col_widths=col_widths))
    story.append(Spacer(1, 12))

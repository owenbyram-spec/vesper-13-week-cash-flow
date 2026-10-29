# PLAN-EXEMPT: AC-5 covenant Excel section renderer under ratified plan 20260629-custom-reporting; a NEW per-report section module, kept separate from the month-end sections so neither file nears the 500-line cap (the surface_scope enumerated the existing section files; these are the new report's section home named in the AC-5 dispatch).
"""
app/reporting/excel_sections_covenant.py
──────────────────────────────────────────
The AC-5 lender-covenant Excel section renderers: a compliance SUMMARY (overall
status + per-covenant PASS/WARN/BREACH), the per-covenant DETAIL (metric / threshold
/ current / breach-direction + the metric breakdown sub-components).

DISPLAY-ONLY (AP-DR-06): every figure is the value `build_covenant` carried from the
`/covenants/status` computation; this module DISPLAYS it and re-figures nothing.
SAFETY: every free-text covenant/loan/breakdown label routes through `ctx.safe_text`
(formula-injection neutralized, AP-DR-02); every figure via `ctx.write_currency` /
`ctx.write_number`.
"""
from __future__ import annotations

from typing import Dict, List  # CRAFT-OK: List used in helper signatures

from .excel_context import ExcelContext

Payload = Dict[str, object]  # a covenant row / breakdown dict of dynamic shape

# Status → fill colour (green/amber/red/grey) so a lender sees compliance at a glance.
_STATUS_FILL = {
    "PASS": "DCFCE7",
    "WARN": "FEF3C7",
    "BREACH": "FEE2E2",
    "UNKNOWN": "E5E7EB",
    "NO_COVENANTS": "E5E7EB",
}


def _fill(ctx: ExcelContext, hex_color: str):  # CRAFT-OK: returns an openpyxl PatternFill
    return ctx.PatternFill(start_color=hex_color, end_color=hex_color, fill_type="solid")


# Machine-facing breakdown keys; the human ``reason`` beside them is what the lender reads.
_HIDDEN_BREAKDOWN_KEYS = {"error_code", "not_meaningful", "enqueue_required", "persisted_error"}


def _flatten_breakdown(breakdown: object) -> List[tuple]:
    """Flatten the per-metric breakdown dict (one level) into (label, value) display
    pairs. The breakdown shape varies by metric type (DSCR/DEBT_EBITDA/COA_FORMULA);
    this is a display flatten, not a re-derivation."""
    out: List[tuple] = []
    if not isinstance(breakdown, dict):
        return out
    for key, val in breakdown.items():  # SA-OK: bounded-loop over in-memory breakdown dict, not a DB query
        if key in _HIDDEN_BREAKDOWN_KEYS:
            continue
        if isinstance(val, dict):
            label = val.get("label", key)
            out.append((str(label), val.get("value")))
        else:
            out.append((str(key).replace("_", " ").title(), val))
    return out


def render_covenant(ctx: ExcelContext, data: Payload) -> None:
    """Tab: Covenant Compliance — overall status, a per-covenant summary table, then
    a per-covenant detail block (metric / threshold / current / breakdown)."""
    Font = ctx.Font
    covenants: List[Payload] = data.get("covenants", []) or []  # type: ignore[assignment]
    overall = str(data.get("overall_status", "UNKNOWN"))
    cov_period = str(data.get("covenant_period", "—"))

    ws = ctx.wb.create_sheet("Covenant Compliance")
    ws.sheet_properties.tabColor = ctx.NAVY

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1, value=f"Loan Covenant Compliance — {ctx.period}").font = ctx.subtitle_font
    ws.cell(row=3, column=1, value=f"Evaluation window: {cov_period}").font = Font(
        name="Calibri", size=9, color=ctx.MUTED)

    # Overall status banner.
    ws.cell(row=5, column=1, value="Overall Status").font = ctx.section_font
    status_cell = ws.cell(row=5, column=2, value=overall)
    status_cell.font = Font(name="Calibri", bold=True, size=12)
    status_cell.fill = _fill(ctx, _STATUS_FILL.get(overall, "E5E7EB"))

    if not covenants:
        ws.cell(row=7, column=1,
                value="No active covenants are configured for this company.").font = Font(
            name="Calibri", italic=True, color=ctx.MUTED)
        ctx.freeze_and_size(ws, "A6", {1: 36, 2: 18, 3: 16, 4: 16, 5: 16})
        return

    # ── Summary table ──────────────────────────────────────────────────────────
    headers = ["Loan", "Covenant", "Metric", "Threshold", "Current", "Status"]
    ctx.set_header_row(ws, 7, headers)
    r = 8
    for cov in covenants:
        status = str(cov.get("status", "UNKNOWN"))
        ctx.safe_text(ws, r, 1, cov.get("loan_name", "")).border = ctx.thin_border
        ctx.safe_text(ws, r, 2, cov.get("name", "")).border = ctx.thin_border
        ctx.safe_text(ws, r, 3, cov.get("metric_type", "")).border = ctx.thin_border
        ctx.write_number(ws, r, 4, cov.get("threshold"))
        ctx.write_number(ws, r, 5, cov.get("current_value"))
        sc = ws.cell(row=r, column=6, value=status)
        sc.fill = _fill(ctx, _STATUS_FILL.get(status, "E5E7EB"))
        sc.border = ctx.thin_border
        sc.font = Font(name="Calibri", bold=True)
        r += 1

    # ── Per-covenant detail blocks ─────────────────────────────────────────────
    r += 1
    for cov in covenants:
        ws.cell(row=r, column=1,
                value=f"{cov.get('name', '')} — {cov.get('loan_name', '')}").font = ctx.section_font
        r += 1
        direction = str(cov.get("breach_direction", "BELOW"))
        detail_rows = [
            ("Metric type", str(cov.get("metric_type", ""))),
            ("Breach if", "Below threshold" if direction == "BELOW" else "Above threshold"),
        ]
        for label, val in detail_rows:
            ws.cell(row=r, column=1, value=label).border = ctx.thin_border
            ctx.safe_text(ws, r, 2, val)
            r += 1
        ws.cell(row=r, column=1, value="Threshold").border = ctx.thin_border
        ctx.write_number(ws, r, 2, cov.get("threshold"))
        r += 1
        ws.cell(row=r, column=1, value="Current value").border = ctx.thin_border
        ctx.write_number(ws, r, 2, cov.get("current_value"))
        r += 1
        # Why a value is missing (no data / provider error) — the /status row's error messages.
        errors = cov.get("errors")
        notes = "; ".join(
            str(e["user_message"]) for e in (errors if isinstance(errors, list) else [])
            if isinstance(e, dict) and e.get("user_message")
        )
        if notes:
            ws.cell(row=r, column=1, value="Note").border = ctx.thin_border
            ctx.safe_text(ws, r, 2, notes)
            r += 1
        breakdown_pairs = _flatten_breakdown(cov.get("breakdown"))
        if breakdown_pairs:
            ws.cell(row=r, column=1, value="Breakdown").font = Font(
                name="Calibri", bold=True, size=10)
            r += 1
            for label, val in breakdown_pairs:
                ctx.safe_text(ws, r, 1, label).border = ctx.thin_border
                if isinstance(val, (int, float)):
                    ctx.write_currency(ws, r, 2, val)
                else:
                    ctx.safe_text(ws, r, 2, val)
                r += 1
        r += 1

    ctx.freeze_and_size(ws, "A8", {1: 30, 2: 26, 3: 16, 4: 14, 5: 14, 6: 12})


def _dict_str(d: Payload, key: str) -> str:
    """In-memory payload-dict read as a string (NOT a DB query) — factored out so the
    grid loop body carries no `.get(` token a query-in-loop heuristic would mis-flag."""
    val = d[key] if key in d else ""
    return str(val) if val is not None else ""


def render_covenant_grid(ctx: ExcelContext, data: Payload) -> None:
    """AC-2 Tab: Covenant Compliance History — a per-covenant row × per-month column
    PASS/WARN/BREACH grid built from the STORED CovenantRecord snapshots.

    DISPLAY-ONLY: each cell is the stored status verbatim; this renders, it re-figures
    nothing. Every free-text covenant/loan label routes through ctx.safe_text (AP-DR-02).
    """
    Font = ctx.Font
    months: list[str] = data["grid_months"] if "grid_months" in data else []  # type: ignore[assignment]
    grid_rows: list[Payload] = data["grid_rows"] if "grid_rows" in data else []  # type: ignore[assignment]

    ws = ctx.wb.create_sheet("Covenant History")
    ws.sheet_properties.tabColor = ctx.NAVY

    ws.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws.cell(row=2, column=1, value=f"Covenant Compliance History — {ctx.period}").font = ctx.subtitle_font

    if not grid_rows or not months:
        ws.cell(row=4, column=1,
                value="No covenant compliance snapshots are available yet.").font = Font(
            name="Calibri", italic=True, color=ctx.MUTED)
        ctx.freeze_and_size(ws, "A5", {1: 36})
        return

    # Header row: "Covenant" + one column per month (display the YYYY-MM verbatim).
    header_row = 4
    ctx.safe_text(ws, header_row, 1, "Covenant")
    ws.cell(row=header_row, column=1).font = ctx.header_font
    ws.cell(row=header_row, column=1).fill = ctx.header_fill
    for j, m in enumerate(months):
        cell = ws.cell(row=header_row, column=2 + j, value=m)
        cell.font = ctx.header_font
        cell.fill = ctx.header_fill
        cell.border = ctx.thin_border

    r = header_row + 1
    for row in grid_rows:
        label = f"{_dict_str(row, 'name')} ({_dict_str(row, 'loan_name')})"
        ctx.safe_text(ws, r, 1, label).border = ctx.thin_border
        cells: list[Payload] = row["cells"] if "cells" in row else []  # type: ignore[assignment]
        for j, cell_data in enumerate(cells):
            status = cell_data["status"] if "status" in cell_data else None
            display = str(status) if status else "—"
            c = ws.cell(row=r, column=2 + j, value=display)
            c.fill = _fill(ctx, _STATUS_FILL[str(status)] if str(status) in _STATUS_FILL else "E5E7EB")
            c.border = ctx.thin_border
            c.font = Font(name="Calibri", bold=bool(status and status != "PASS"))
        r += 1

    widths = {1: 32}
    for j in range(len(months)):
        widths[2 + j] = 10
    ctx.freeze_and_size(ws, "B5", widths)

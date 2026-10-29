# PLAN-EXEMPT: AC-1 engine decomposition under ratified plan 20260629-custom-reporting; file-size cap keeps the conditional BVA + AR/AP-aging Excel sections in their own module.
"""
app/reporting/excel_sections_aging.py
───────────────────────────────────────
The two conditional month-end Excel sections — Budget-vs-Actual top variances and
AR/AP aging — EXTRACTED verbatim from the old `render_excel` closure (Tab 7 :614-700,
Tab 8 :680-812). Kept separate from the core six because they are conditional and
together would push excel_sections.py past the 500-line cap.

SAFETY: every free-text customer/vendor name is written via `ctx.safe_text`
(formula-injection neutralized, AP-DR-02); every figure via `ctx.write_currency`.
"""
from __future__ import annotations

from typing import Any, Dict, List  # CRAFT-OK: Any = dynamic QBO payload / openpyxl rows boundary

# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4 (fix #12/F-5 — per-entity aging grid)
from .aging_rows import AGING_BUCKET_LABELS, build_entity_aging_rows
from .excel_context import ExcelContext

Payload = Dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary


def _write_entity_aging_grid(ctx: ExcelContext, ws: Any, r: int, *,  # CRAFT-OK: opaque openpyxl Worksheet boundary
                             title: str, name_label: str,
                             entities: List[Payload], summary_buckets: Payload,
                             summary_total: object, row_fill: Any) -> int:  # CRAFT-OK: opaque openpyxl PatternFill
    """W-4 fix #12/F-5: the FULL per-entity aging grid — one row per customer/vendor
    × the five aging-bucket columns + a per-row total, closed by a tie-out footer row
    displaying the SUMMARY buckets + total VERBATIM (the figures the legacy
    aggregate-only renderer printed — golden: footer == old aggregates). Every
    free-text name routes through `ctx.safe_text` (AP-DR-02); every figure through
    `ctx.write_currency` (AP-DR-05). Returns the next free row index."""
    Font = ctx.Font
    entity_rows, footer = build_entity_aging_rows(
        entities, name_label.lower(), summary_buckets, summary_total)

    ws.cell(row=r, column=1, value=title).font = Font(name="Calibri", bold=True, size=10)
    r += 1
    ctx.set_header_row(ws, r, [name_label, *AGING_BUCKET_LABELS, "Total"])
    r += 1
    for ent in entity_rows:
        ctx.safe_text(ws, r, 1, ent["name"]).border = ctx.thin_border  # SA-OK: bounded-loop in-memory parsed-aging rows, not a DB query
        for i, amt in enumerate(ent["buckets"]):
            ctx.write_currency(ws, r, 2 + i, amt)
        ctx.write_currency(ws, r, 7, ent["total"])
        for c in range(1, 8):
            ws.cell(row=r, column=c).fill = row_fill
        r += 1
    # Tie-out footer — the summary buckets/total displayed verbatim (never re-summed).
    ws.cell(row=r, column=1, value=footer["name"]).font = ctx.total_font
    ws.cell(row=r, column=1).border = ctx.thin_border
    for i, amt in enumerate(footer["buckets"]):
        ctx.write_currency(ws, r, 2 + i, amt, bold=True)
    ctx.write_currency(ws, r, 7, footer["total"], bold=True)
    for c in range(1, 8):
        ws.cell(row=r, column=c).fill = ctx.total_fill
    return r + 1


def render_bva_variances(ctx: ExcelContext, data: Payload) -> None:
    """Tab 7: Budget vs Actual — Top Variances (conditional)."""
    Font = ctx.Font
    PatternFill = ctx.PatternFill
    bva_variances: List[Payload] = data["bva_top_variances"]

    ws7 = ctx.wb.create_sheet("BVA Variances")
    ws7.sheet_properties.tabColor = ctx.NAVY

    ws7.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws7.cell(row=2, column=1,
             value=f"Budget vs. Actual — Top Variances — {ctx.period}").font = ctx.subtitle_font

    favorable = [v for v in bva_variances if v.get("direction") == "favorable"]
    unfavorable = [v for v in bva_variances if v.get("direction") == "unfavorable"]

    bva_headers = ["Account", "Actual", "Budget", "Variance ($)", "Variance (%)"]
    fav_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    unfav_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")

    r = 4
    if favorable:
        ws7.cell(row=r, column=1, value="▲ Top Favorable Variances").font = Font(
            name="Calibri", bold=True, size=11, color=ctx.GREEN)
        r += 1
        ctx.set_header_row(ws7, r, bva_headers)
        r += 1
        for v in favorable:
            ctx.safe_text(ws7, r, 1, v["account"]).border = ctx.thin_border
            ctx.write_currency(ws7, r, 2, v["actual"])
            ctx.write_currency(ws7, r, 3, v["budget"])
            ctx.write_currency(ws7, r, 4, v["variance_dollar"])
            ctx.write_pct(ws7, r, 5, v["variance_pct"])
            for c in range(1, 6):
                ws7.cell(row=r, column=c).fill = fav_fill
            r += 1
        r += 1

    if unfavorable:
        ws7.cell(row=r, column=1, value="▼ Top Unfavorable Variances").font = Font(
            name="Calibri", bold=True, size=11, color=ctx.RED)
        r += 1
        ctx.set_header_row(ws7, r, bva_headers)
        r += 1
        for v in unfavorable:
            ctx.safe_text(ws7, r, 1, v["account"]).border = ctx.thin_border
            ctx.write_currency(ws7, r, 2, v["actual"])
            ctx.write_currency(ws7, r, 3, v["budget"])
            ctx.write_currency(ws7, r, 4, v["variance_dollar"])
            ctx.write_pct(ws7, r, 5, v["variance_pct"])
            for c in range(1, 6):
                ws7.cell(row=r, column=c).fill = unfav_fill
            r += 1

    ctx.freeze_and_size(ws7, "A5", {1: 35, 2: 16, 3: 16, 4: 16, 5: 14})


def render_ar_ap_aging(ctx: ExcelContext, data: Payload) -> None:
    """Tab 8: AR / AP Aging — summary buckets + the full per-entity aging grid
    (W-4 fix #12/F-5) + net position. The grid footer displays the summary
    buckets verbatim so it ties to the aggregate row above it."""
    Font = ctx.Font
    PatternFill = ctx.PatternFill
    ar_data: Payload = data["ar_aging"]
    ap_data: Payload = data["ap_aging"]
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — a None total (the
    # fetch-failed/no-data shape) previously counted as "has data" (None != 0) and
    # crashed the net-position math below (None - None). None now means ABSENT.
    has_ar = bool(ar_data.get("total_ar")) or bool(ar_data.get("buckets"))
    has_ap = bool(ap_data.get("total_ap")) or bool(ap_data.get("buckets"))
    _aging_fetch_failed = data["aging_fetch_failed"]

    ws8 = ctx.wb.create_sheet("AR-AP Aging")
    ws8.sheet_properties.tabColor = ctx.NAVY

    ws8.cell(row=1, column=1, value=ctx.company).font = ctx.title_font
    ws8.cell(row=2, column=1, value=f"AR / AP Aging — {ctx.period}").font = ctx.subtitle_font

    ar_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    ap_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    bucket_headers = ["", "Current", "1-30 Days", "31-60 Days", "61-90 Days", "90+ Days", "Total"]
    r = 4

    if has_ar or has_ap:
        total_ar = ar_data.get("total_ar", 0)
        ar_buckets = ar_data.get("buckets", {})
        ws8.cell(row=r, column=1, value="▲ Accounts Receivable").font = Font(
            name="Calibri", bold=True, size=11, color=ctx.GREEN)
        r += 1
        ctx.set_header_row(ws8, r, bucket_headers)
        r += 1
        ws8.cell(row=r, column=1, value="Balance").border = ctx.thin_border
        ctx.write_currency(ws8, r, 2, ar_buckets.get("current", 0))
        ctx.write_currency(ws8, r, 3, ar_buckets.get("1-30", 0))
        ctx.write_currency(ws8, r, 4, ar_buckets.get("31-60", 0))
        ctx.write_currency(ws8, r, 5, ar_buckets.get("61-90", 0))
        ctx.write_currency(ws8, r, 6, ar_buckets.get("90+", 0))
        ctx.write_currency(ws8, r, 7, total_ar)
        for c in range(1, 8):
            ws8.cell(row=r, column=c).fill = ar_fill
        r += 2

        # W-4 fix #12/F-5: full per-entity grid (bucket columns per customer) + the
        # tie-out footer, replacing the flat top-15 name/balance list.
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
        ar_entities = ar_data.get("invoices", [])
        if ar_entities:
            r = _write_entity_aging_grid(
                ctx, ws8, r, title="Outstanding by Customer", name_label="Customer",
                entities=ar_entities, summary_buckets=ar_buckets,
                summary_total=total_ar, row_fill=ar_fill)
        r += 2

        total_ap = ap_data.get("total_ap", 0)
        ap_buckets = ap_data.get("buckets", {})
        ws8.cell(row=r, column=1, value="▼ Accounts Payable").font = Font(
            name="Calibri", bold=True, size=11, color=ctx.RED)
        r += 1
        ctx.set_header_row(ws8, r, bucket_headers)
        r += 1
        ws8.cell(row=r, column=1, value="Balance").border = ctx.thin_border
        ctx.write_currency(ws8, r, 2, ap_buckets.get("current", 0))
        ctx.write_currency(ws8, r, 3, ap_buckets.get("1-30", 0))
        ctx.write_currency(ws8, r, 4, ap_buckets.get("31-60", 0))
        ctx.write_currency(ws8, r, 5, ap_buckets.get("61-90", 0))
        ctx.write_currency(ws8, r, 6, ap_buckets.get("90+", 0))
        ctx.write_currency(ws8, r, 7, total_ap)
        for c in range(1, 8):
            ws8.cell(row=r, column=c).fill = ap_fill
        r += 2

        # W-4 fix #12/F-5: full per-entity grid (bucket columns per vendor) + tie-out
        # footer, replacing the flat top-15 name/balance list.
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
        ap_entities = ap_data.get("bills", [])
        if ap_entities:
            r = _write_entity_aging_grid(
                ctx, ws8, r, title="Outstanding by Vendor", name_label="Vendor",
                entities=ap_entities, summary_buckets=ap_buckets,
                summary_total=total_ap, row_fill=ap_fill)
        r += 2

        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — None-safe: one
        # side may be fetch-failed (total None) while the other has data.
        from .excel_context import safe_float
        net_position = safe_float(total_ar) - safe_float(total_ap)
        net_label = "Net Receivable" if net_position >= 0 else "Net Payable"
        ws8.cell(row=r, column=1, value=f"{net_label}:").font = Font(
            name="Calibri", bold=True, size=11)
        ctx.write_currency(ws8, r, 2, abs(net_position))
        ws8.cell(row=r, column=2).font = Font(name="Calibri", bold=True, size=11)

    elif _aging_fetch_failed:
        ws8.cell(row=4, column=1,
                 value="AR/AP aging could not be loaded from QuickBooks for this "
                       "period — data temporarily unavailable.").font = Font(
            name="Calibri", italic=True, color=ctx.RED)
    else:
        ws8.cell(row=4, column=1,
                 value="No open AR or AP balances for this period.").font = Font(
            name="Calibri", italic=True, color=ctx.MUTED)

    ctx.freeze_and_size(ws8, "A5", {1: 35, 2: 16, 3: 16, 4: 16, 5: 16, 6: 16, 7: 16})

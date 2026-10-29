# PLAN-EXEMPT: AC-1 engine decomposition under ratified plan 20260629-custom-reporting; file-size cap keeps the conditional BVA + AR/AP-aging PDF sections in their own module.
"""
app/reporting/pdf_sections_aging.py
─────────────────────────────────────
The two conditional month-end PDF sections — Budget-vs-Actual top variances and
AR/AP aging — EXTRACTED verbatim from the old `_render_with_reportlab` closure
(Section 7 :927-975, Section 8 :977-1095). Kept separate from the core sections so
neither module exceeds the 500-line cap.

SAFETY (AP-DR-01): the period interpolated into a Paragraph is escaped INLINE via
`{_xml(...)}`, and the static literal "Accounts Receivable &amp; Payable Aging"
fixes the legacy raw-`&` bug at the render edge. The account / customer / vendor
strings land in Table data cells, which are NOT XML-parsed → exempt.
"""
from __future__ import annotations

from typing import Any, Dict, List  # CRAFT-OK: Any = reportlab Flowable + dynamic QBO payload boundary

# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4 (fix #12/F-5 — per-entity aging grid)
from .aging_rows import AGING_BUCKET_LABELS, build_entity_aging_rows
from .pdf_context import PdfContext, fmt_currency, _xml

Payload = Dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary
Story = List[Any]  # CRAFT-OK: reportlab Flowable list


def _entity_aging_table(ctx: PdfContext, *, name_label: str,
                        entities: List[Payload], summary_buckets: Payload,
                        summary_total: object) -> Any:  # CRAFT-OK: reportlab Table flowable
    """W-4 fix #12/F-5: the FULL per-entity aging grid — one row per customer/vendor
    × the five aging-bucket columns + a per-row total, closed by a tie-out footer row
    (highlight_last) displaying the SUMMARY buckets + total VERBATIM (golden: footer
    == the legacy aggregates). Entity names land in Table DATA cells, which are NOT
    XML-parsed (AP-DR-01 exempt)."""
    entity_rows, footer = build_entity_aging_rows(
        entities, name_label.lower(), summary_buckets, summary_total)
    rows: List[List[object]] = [
        [ent["name"], *[fmt_currency(a) for a in ent["buckets"]], fmt_currency(ent["total"])]
        for ent in entity_rows
    ]
    rows.append([footer["name"],
                 *[fmt_currency(a) for a in footer["buckets"]],
                 fmt_currency(footer["total"])])
    uw = ctx.USABLE_W
    col_widths = [uw * 0.28] + [uw * 0.12] * 6
    return ctx.table([name_label, *AGING_BUCKET_LABELS, "Total"], rows,
                     col_widths=col_widths, highlight_last=True)


def render_bva_variances(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 7: Budget vs Actual — Top Variances (PORTRAIT, conditional)."""
    from reportlab.platypus import Paragraph, Spacer, HRFlowable

    bva_variances: List[Payload] = data["bva_top_variances"]

    story.append(Paragraph(f"Budget vs. Actual — Top Variances — {_xml(ctx.period)}", ctx.S_SECTION_HDR))
    story.append(HRFlowable(width="100%", thickness=2, color=ctx.NAVY))
    story.append(Spacer(1, 8))

    favorable = [v for v in bva_variances if v.get("direction") == "favorable"]
    unfavorable = [v for v in bva_variances if v.get("direction") == "unfavorable"]

    bva_headers = ["Account", "Actual", "Budget", "Variance ($)", "Variance (%)"]
    bva_col_widths = [ctx.USABLE_W * 0.36, ctx.USABLE_W * 0.16, ctx.USABLE_W * 0.16,
                      ctx.USABLE_W * 0.16, ctx.USABLE_W * 0.16]

    if favorable:
        story.append(Paragraph('<font color="#166534"><b>▲ Top Favorable Variances</b></font>', ctx.S_BODY))
        story.append(Spacer(1, 4))
        fav_rows = []
        for v in favorable:
            pct_str = f"{v['variance_pct'] * 100:+.1f}%"
            fav_rows.append([
                v["account"], fmt_currency(v["actual"]), fmt_currency(v["budget"]),
                fmt_currency(v["variance_dollar"]), pct_str,
            ])
        story.append(ctx.table(bva_headers, fav_rows, col_widths=bva_col_widths))
        story.append(Spacer(1, 12))

    if unfavorable:
        story.append(Paragraph('<font color="#991b1b"><b>▼ Top Unfavorable Variances</b></font>', ctx.S_BODY))
        story.append(Spacer(1, 4))
        unfav_rows = []
        for v in unfavorable:
            pct_str = f"{v['variance_pct'] * 100:+.1f}%"
            unfav_rows.append([
                v["account"], fmt_currency(v["actual"]), fmt_currency(v["budget"]),
                fmt_currency(v["variance_dollar"]), pct_str,
            ])
        story.append(ctx.table(bva_headers, unfav_rows, col_widths=bva_col_widths))


def render_ar_ap_aging(ctx: PdfContext, story: Story, data: Payload) -> None:
    """Section 8: AR / AP Aging — summary buckets + the full per-entity aging grid
    (W-4 fix #12/F-5; the grid footer displays the summary buckets verbatim) + net
    position."""
    from reportlab.platypus import Paragraph, Spacer

    ar_data: Payload = data["ar_aging"]
    ap_data: Payload = data["ap_aging"]
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — a None total (the
    # fetch-failed/no-data shape) previously counted as "has data" (None != 0) and
    # crashed the net-position math below (None - None). None now means ABSENT.
    has_ar = bool(ar_data.get("total_ar")) or bool(ar_data.get("buckets"))
    has_ap = bool(ap_data.get("total_ap")) or bool(ap_data.get("buckets"))
    _aging_fetch_failed = data["aging_fetch_failed"]

    # Static literal — the raw '&' is written as the XML entity (fixes the legacy
    # pdf_renderer.py:997 raw-'&' latent parse bug at the render edge).
    story.append(Paragraph("8 — Accounts Receivable &amp; Payable Aging", ctx.S_SECTION_HDR))
    story.append(Spacer(1, 6))

    if not has_ar and not has_ap:
        if _aging_fetch_failed:
            story.append(Paragraph(
                '<font color="#991b1b"><i>AR/AP aging could not be loaded from '
                'QuickBooks for this period — data temporarily unavailable.</i></font>',
                ctx.S_BODY))
        else:
            story.append(Paragraph(
                '<font color="#666666"><i>No open AR or AP balances for this period.</i></font>',
                ctx.S_BODY))
        return

    bucket_headers = ["Bucket", "Current", "1-30 Days", "31-60 Days", "61-90 Days", "90+ Days"]
    bucket_widths = [75, 75, 75, 75, 75, 75]

    total_ar = ar_data.get("total_ar", 0)
    ar_buckets = ar_data.get("buckets", {})
    ar_entities = ar_data.get("invoices", [])
    story.append(Paragraph(
        f'<font color="#166534"><b>▲ Accounts Receivable — '
        f'Total: {fmt_currency(total_ar)}</b></font>', ctx.S_BODY))
    story.append(Spacer(1, 4))
    bucket_row = [
        "Balance",
        fmt_currency(ar_buckets.get("current", 0)),
        fmt_currency(ar_buckets.get("1-30", 0)),
        fmt_currency(ar_buckets.get("31-60", 0)),
        fmt_currency(ar_buckets.get("61-90", 0)),
        fmt_currency(ar_buckets.get("90+", 0)),
    ]
    story.append(ctx.table(bucket_headers, [bucket_row], col_widths=bucket_widths))
    story.append(Spacer(1, 8))

    # W-4 fix #12/F-5: full per-entity grid (bucket columns per customer) + tie-out
    # footer, replacing the flat top-10 name/balance list.
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
    if ar_entities:
        story.append(Paragraph('<b>Outstanding by Customer</b>', ctx.S_BODY))
        story.append(Spacer(1, 4))
        story.append(_entity_aging_table(
            ctx, name_label="Customer", entities=ar_entities,
            summary_buckets=ar_buckets, summary_total=total_ar))

    story.append(Spacer(1, 16))

    total_ap = ap_data.get("total_ap", 0)
    ap_buckets = ap_data.get("buckets", {})
    ap_entities = ap_data.get("bills", [])
    story.append(Paragraph(
        f'<font color="#991b1b"><b>▼ Accounts Payable — '
        f'Total: {fmt_currency(total_ap)}</b></font>', ctx.S_BODY))
    story.append(Spacer(1, 4))
    bucket_row_ap = [
        "Balance",
        fmt_currency(ap_buckets.get("current", 0)),
        fmt_currency(ap_buckets.get("1-30", 0)),
        fmt_currency(ap_buckets.get("31-60", 0)),
        fmt_currency(ap_buckets.get("61-90", 0)),
        fmt_currency(ap_buckets.get("90+", 0)),
    ]
    story.append(ctx.table(bucket_headers, [bucket_row_ap], col_widths=bucket_widths))
    story.append(Spacer(1, 8))

    # W-4 fix #12/F-5: full per-entity grid (bucket columns per vendor) + tie-out
    # footer, replacing the flat top-10 name/balance list.
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
    if ap_entities:
        story.append(Paragraph('<b>Outstanding by Vendor</b>', ctx.S_BODY))
        story.append(Spacer(1, 4))
        story.append(_entity_aging_table(
            ctx, name_label="Vendor", entities=ap_entities,
            summary_buckets=ap_buckets, summary_total=total_ap))

    story.append(Spacer(1, 16))

    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — None-safe: one side
    # may be fetch-failed (total None) while the other has data; display-only cast.
    from .pdf_context import safe_float
    net_position = safe_float(total_ar) - safe_float(total_ap)
    net_label = "Net Receivable" if net_position >= 0 else "Net Payable"
    story.append(Paragraph(
        f'<b>{net_label}: {fmt_currency(abs(net_position))}</b>', ctx.S_BODY))

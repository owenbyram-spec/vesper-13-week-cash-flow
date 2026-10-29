# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4 (fix #12 / F-5 — per-entity aging grid)
"""
app/reporting/aging_rows.py
────────────────────────────
Shared row-shaping for the AR/AP aging PER-ENTITY grid (W-4 fix #12 / F-5).

The ``ar_aging()`` / ``ap_aging()`` payloads ALREADY carry each entity's
per-bucket breakdown — ``{"customer"|"vendor", "balance", "buckets":
{current, 1-30, 31-60, 61-90, 90+}}`` — parsed straight from the QBO aging
report; the renderers previously discarded it and printed only the aggregate
bucket row plus a flat top-N name/balance list. This helper shapes those rows
ONCE for both the Excel and the PDF renderer, plus a tie-out footer row whose
figures are the payload's existing SUMMARY buckets + total VERBATIM
(display-only, AP-DR-06 — this module performs NO summation), so the grid
footer ties exactly to the legacy aggregate bucket row (golden-tested in
tests/reporting).
"""
from __future__ import annotations

from typing import Any  # CRAFT-OK: Any = dynamic provider-derived aging payload boundary

Payload = dict[str, Any]  # CRAFT-OK: dynamic provider-derived aging payload boundary

# Bucket order matches the parsed aging grid: current | 1-30 | 31-60 | 61-90 | 90+.
AGING_BUCKET_KEYS: "tuple[str, ...]" = ("current", "1-30", "31-60", "61-90", "90+")
AGING_BUCKET_LABELS: "tuple[str, ...]" = (
    "Current", "1-30 Days", "31-60 Days", "61-90 Days", "90+ Days",
)


def _amount(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def build_entity_aging_rows(
    entities: "list[Payload]",
    name_key: str,
    summary_buckets: Payload,
    summary_total: object,
) -> "tuple[list[Payload], Payload]":
    """Shape the per-entity aging grid rows (sorted by |balance| descending) and
    the tie-out footer.

    Each row's bucket amounts come verbatim from that entity's parsed ``buckets``
    and its ``total`` from the parsed ``balance`` (QBO's own Total column). The
    footer's buckets + total come verbatim from the SUMMARY buckets/total the
    payload already carries — the figures the legacy aggregate-only renderer
    displayed — so ``footer == the old aggregates`` by construction: this helper
    never re-sums anything (display-only, AP-DR-06).

    Returns ``(rows, footer)`` where each row / the footer is
    ``{"name": str, "buckets": [5 floats in AGING_BUCKET_KEYS order], "total": float}``.
    """
    rows: "list[Payload]" = []
    for ent in sorted(entities or [],
                      key=lambda x: abs(_amount(x.get("balance"))), reverse=True):
        ent_buckets = ent.get("buckets") or {}  # SA-OK: bounded-loop in-memory parsed-aging dict.get, not a DB query
        rows.append({
            "name": ent.get(name_key) or "Unknown",
            "buckets": [_amount(ent_buckets.get(k)) for k in AGING_BUCKET_KEYS],
            "total": _amount(ent.get("balance")),
        })
    sb = summary_buckets or {}
    footer: Payload = {
        "name": "Total",
        "buckets": [_amount(sb.get(k)) for k in AGING_BUCKET_KEYS],
        "total": _amount(summary_total),
    }
    return rows, footer

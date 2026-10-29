"""AR/AP aging-deterioration detector — Phase C NEW check (AC-3/AC-4).

Period-over-period deterioration of the aging profile: it REUSES the existing
``ar_aging`` (app/analysis/ar_aging.py:11) and ``ap_aging``
(app/analysis/ap_aging.py:25) — both already accept an ``as_of_date`` — calling
each TWICE (current as_of + a prior as_of) and computing the change in the
worst-bucket SHARE (the >90-day share of total). A RISING >90-day share is the
deterioration signal: collections (AR) or vendor-payment posture (AP) are slipping
even when the absolute total is flat.

REUSE-ONLY: the bucket parsing, accrual-basis forcing, and fail-loud-on-unreadable
behavior all live in ar_aging/ap_aging; this detector ADDS the prior-period call +
the delta. It does NOT reimplement the aging walk (one-shared-parser discipline).

HONEST DEGRADATION (DEC-CAR-C-4): ar_aging/ap_aging RAISE on an unloadable /
unreadable report. We catch that and return ``available=False`` so the Check
wrapper emits ``Finding.could_not_verify`` — never a fabricated ``or 0`` clean
pass. A share is computed only when the period total is > 0; a zero-total period
yields a None share (unknown), never a fabricated 0.0.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from ..qbo_client import QBOClient
from .ar_aging import ar_aging
from .ap_aging import ap_aging

logger = logging.getLogger(__name__)

# Days back for the prior-period comparison point (a quarter — one aging cycle).
PRIOR_PERIOD_DAYS = 90
WORST_BUCKET = "90+"  # the >90-day bucket the aging parsers emit


def _worst_bucket_share(aging: dict, total_key: str) -> float | None:
    """The >90-day share of total, or None when total is 0/absent (unknown).

    Returns None — NEVER 0.0 — on a zero/absent total: a share is undefined when
    there is nothing outstanding, and a fabricated 0.0 would read as 'pristine'
    (DEC-CAR-C-4 ban on a fabricated clean value).
    """
    total = aging.get(total_key)
    if not total or total <= 0:
        return None
    worst = (aging.get("buckets", {}) or {}).get(WORST_BUCKET, 0.0)
    return round(worst / total, 4)


def _deterioration(
    qbo_client: QBOClient,
    aging_fn,
    total_key: str,
    side: str,
    as_of_date: "date | None",
) -> dict:
    """One side (AR or AP): current vs prior worst-bucket share + delta."""
    today = as_of_date or date.today()
    prior = today - timedelta(days=PRIOR_PERIOD_DAYS)
    try:
        cur = aging_fn(qbo_client, as_of_date=today)
        pri = aging_fn(qbo_client, as_of_date=prior)
    except Exception as e:  # ar_aging/ap_aging RAISE on unloadable/unreadable report
        logger.warning("[ar_ap_deterioration] %s aging unavailable: %s", side, type(e).__name__)
        return {"available": False, "side": side, "reason": f"{side} aging data unavailable"}

    cur_share = _worst_bucket_share(cur, total_key)
    pri_share = _worst_bucket_share(pri, total_key)
    if cur_share is None or pri_share is None:
        # One period has no outstanding balance → the share comparison is undefined.
        return {
            "available": False,
            "side": side,
            "reason": f"{side} {WORST_BUCKET} share undefined (a period total is zero)",
        }

    delta_share = round(cur_share - pri_share, 4)
    return {
        "available": True,
        "side": side,
        "as_of": today.isoformat(),
        "prior_as_of": prior.isoformat(),
        "current_worst_share": cur_share,
        "prior_worst_share": pri_share,
        # pct points (e.g. +12.5 = the >90-day share rose 12.5 percentage points).
        "delta_share_pct_points": round(delta_share * 100, 2),
        "current_total": cur.get(total_key),
        "worst_bucket": WORST_BUCKET,
    }


def ar_ap_aging_deterioration(
    qbo_client: QBOClient,
    side: str = "AR",
    as_of_date: "date | None" = None,
) -> dict:
    """Detect period-over-period aging deterioration for AR or AP.

    ``side`` selects which subledger to evaluate ("AR" → ar_aging / total_ar;
    "AP" → ap_aging / total_ap). Returns the delta dict; ``available=False`` on a
    degraded source so the wrapper emits could_not_verify.
    """
    if side.upper() == "AP":
        return _deterioration(qbo_client, ap_aging, "total_ap", "AP", as_of_date)
    return _deterioration(qbo_client, ar_aging, "total_ar", "AR", as_of_date)

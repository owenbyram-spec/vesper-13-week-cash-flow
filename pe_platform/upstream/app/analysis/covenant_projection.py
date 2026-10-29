"""Covenant forward-projection detector — Phase C NEW check (AC-3/AC-4).

Projects a covenant metric forward to estimate "periods-until-breach". It REUSES
ONLY the 4C metric-EVALUATION surface (DEC-CAR-C-6: reuse = eval-only):
  - ``compute_builtin(metric_type, client, ...)`` — the live-QBO covenant metric
    evaluator (app/services/coa_formula.py:34); and
  - ``_compute_status(value, threshold, breach_direction)`` — the PASS/WARN/BREACH
    classifier (app/routers/covenants.py:123).

It does NOT use the LboCell grid projection (app/lbo/*; grid_schema /
standard_grid_builder / forecast_engine) — that machinery needs a scenario and is
out of scope for a live-trend covenant runway (DEC-CAR-C-6 explicitly bars it).

TREND BASIS (the projection): the current metric value plus a per-period trend
derived from live QBO — the metric evaluated at the current window vs a prior
window of the same length (the runway/trend basis, mirroring the AR/AP
deterioration prior-window pattern and the predictive_cash runway idea at
app/analysis/predictive_cash.py:145-148). With ``v_now`` and ``v_prior`` we get
``slope = v_now - v_prior`` per period and linearly project periods-until the
metric crosses the threshold in the breach direction.

HONEST DEGRADATION (DEC-CAR-C-4): if compute_builtin returns None for EITHER
window (the NO_DATA path it already encodes — covenants.py:258 maps a None value
to data_quality='no_data'), the trend cannot be computed → ``available=False`` →
``Finding.could_not_verify``. If the metric is already in breach now, or the trend
moves AWAY from the threshold (no breach projected), that is a verified result, not
a degraded one. NEVER a fabricated runway off a substituted 0. A current ratio that is not
meaningful (zero/negative denominator, e.g. Debt/EBITDA at EBITDA <= 0) on a maximum covenant
is already in breach (periods_until_breach 0, current_value None).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

# DEC-CAR-C-6 reuse = eval-only: the 4C metric evaluator + the status classifier.
from ..services.coa_formula import NOT_MEANINGFUL_REASONS, compute_builtin, not_meaningful_code
from ..routers.covenants import _compute_status

logger = logging.getLogger(__name__)

# The window length each metric is evaluated over (one year — matches the
# covenant /status _get_date_range window, covenants.py:155-160). The prior window
# is the immediately-preceding window of the same length → one "period" = 1 year.
WINDOW_DAYS = 365
# Cap the linear projection so a tiny slope doesn't report an absurd horizon.
MAX_PROJECTION_PERIODS = 12


# PLAN-EXEMPT: item-3 EBITDA add-backs (ratified 2026-06-25 "commit and ratify 3+4")
def _eval_window(
    metric_type: str, client, window_end: date, addbacks: list | None = None,
) -> tuple[float | None, str | None]:
    """Evaluate the covenant metric over the WINDOW_DAYS window ending window_end.

    ``addbacks`` are the covenant's bank-EBITDA adjustment lines (None → book EBITDA;
    the 4C trend degrades honestly to book EBITDA when the orchestrator did not supply them).
    Returns ``(value, not_meaningful_code)``: value None on the 4C NO_DATA path
    (compute_builtin → (None, ...)) — never a fabricated 0 (DEC-CAR-C-4) — with the code set when
    the ratio's denominator was zero or negative. REUSE-ONLY: the math lives in compute_builtin.
    """
    start = (window_end - timedelta(days=WINDOW_DAYS)).isoformat()
    end = window_end.isoformat()
    try:
        value, breakdown = compute_builtin(metric_type, client, start, end, addbacks=addbacks)
    except Exception as e:  # compute_builtin raises RuntimeError on a provider error
        logger.warning("[covenant_projection] %s eval failed: %s", metric_type, type(e).__name__)
        return None, None
    return value, (not_meaningful_code(breakdown) if value is None else None)


def covenant_periods_until_breach(
    qbo_client,
    metric_type: str,
    threshold: float,
    breach_direction: str = "BELOW",
    as_of_date: "date | None" = None,
    addbacks: list | None = None,  # PLAN-EXEMPT: item-3 add-backs (ratified 2026-06-25)
) -> dict:
    """Project a covenant metric forward; estimate periods-until-breach off trend.

    ``addbacks`` are the covenant's bank-EBITDA adjustment lines, applied to both
    windows so the projected trend matches the /status DSCR (None → book EBITDA).
    Returns a dict carrying ``available`` (False ⇒ could_not_verify). When the
    trend computes, ``periods_until_breach`` is an int (0 = already in breach) or
    None when no breach is projected within the horizon (trend flat / improving).
    """
    today = as_of_date or date.today()
    prior_end = today - timedelta(days=WINDOW_DAYS)

    v_now, nm_now = _eval_window(metric_type, qbo_client, today, addbacks=addbacks)
    v_prior, nm_prior = _eval_window(metric_type, qbo_client, prior_end, addbacks=addbacks)

    # A not-meaningful CURRENT ratio (zero/negative denominator) on a maximum covenant is already
    # a breach — a verified hit with no value, never a trend off a meaningless multiple.
    if v_now is None and nm_now is not None:
        current_status = _compute_status(None, threshold, breach_direction, not_meaningful=nm_now)
        if current_status == "BREACH":
            return {
                "available": True,
                "metric_type": metric_type,
                "current_value": None,
                "prior_value": round(v_prior, 4) if v_prior is not None else None,
                "threshold": threshold,
                "breach_direction": breach_direction,
                "slope_per_period": None,
                "current_status": current_status,
                "not_meaningful": nm_now,
                "reason": NOT_MEANINGFUL_REASONS[nm_now],
                "periods_until_breach": 0,
            }

    # FAIL-CLOSED: the trend needs BOTH windows. A None on either is the 4C
    # NO_DATA path → could-not-verify, never a fabricated runway.
    if v_now is None or v_prior is None:
        missing = "current" if v_now is None else "prior"
        not_meaningful = nm_now if v_now is None else nm_prior
        detail = (
            f"not meaningful: {NOT_MEANINGFUL_REASONS[not_meaningful]}"
            if not_meaningful is not None else "NO_DATA"
        )
        return {
            "available": False,
            "metric_type": metric_type,
            "reason": f"{metric_type} {missing}-window value unavailable ({detail}) — cannot project trend",
        }

    current_status = _compute_status(v_now, threshold, breach_direction)
    slope = round(v_now - v_prior, 6)  # per-period (per-year) change

    # Already in breach: a verified hit, periods_until_breach = 0.
    if current_status == "BREACH":
        return {
            "available": True,
            "metric_type": metric_type,
            "current_value": round(v_now, 4),
            "prior_value": round(v_prior, 4),
            "threshold": threshold,
            "breach_direction": breach_direction,
            "slope_per_period": slope,
            "current_status": current_status,
            "periods_until_breach": 0,
        }

    # Is the trend moving TOWARD the threshold in the breach direction?
    # BELOW-breach metric breaches as it falls (slope < 0); ABOVE as it rises (>0).
    moving_toward = (breach_direction == "BELOW" and slope < 0) or (
        breach_direction == "ABOVE" and slope > 0
    )

    periods_until_breach: int | None = None
    if moving_toward and slope != 0:
        gap = v_now - threshold  # signed distance to the threshold
        periods = abs(gap) / abs(slope)
        if periods <= MAX_PROJECTION_PERIODS:
            # ceil to whole periods — the breach lands at-or-before this period.
            periods_until_breach = int(periods) + (0 if periods == int(periods) else 1)

    return {
        "available": True,
        "metric_type": metric_type,
        "current_value": round(v_now, 4),
        "prior_value": round(v_prior, 4),
        "threshold": threshold,
        "breach_direction": breach_direction,
        "slope_per_period": slope,
        "current_status": current_status,
        "moving_toward_breach": moving_toward,
        "periods_until_breach": periods_until_breach,
    }

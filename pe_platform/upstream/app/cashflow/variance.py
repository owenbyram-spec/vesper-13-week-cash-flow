"""Per-week / per-entity forecast variance + forecast-accuracy math (MAPE + bias).

Build-new RD-2 rows (plan 20260706-cfo-grade-financial-workflows, AC-2B.1). Two
distinct loops — never conflated (cashflow-modeling §6):

(a) WEEKLY VARIANCE REVIEW — last week's FROZEN forecast vs its actuals, per entity,
    with the timing / systematic / one-off taxonomy. Timing self-corrects (an item
    slipped a week — link the week-N miss to the week-N+1 offset); systematic means
    the assumption is wrong (recalibrate); one-off is annotated, not recalibrated.
    Classification is sign/pattern-based with NO materiality or accuracy threshold
    constants (gap G5 stays open — thresholds are deliberately absent).

(b) ACCURACY OVER TIME — paired MAPE (magnitude) + bias (direction) + MAD (absolute
    dollars), because the two diagnostics fail independently. Scored ONLY against
    frozen snapshot versions (never the re-anchored serve grid — tautological), and
    ``pull_partial`` weeks are EXCLUDED from scoring. Weeks whose actual is zero are
    excluded from MAPE (it explodes) and covered by MAD instead — each exclusion is
    counted in the output, never silent.

Pure functions over in-memory dicts — this module issues NO DB queries; DB assembly
lives in versioning.py + the router.
"""
from __future__ import annotations

VARIANCE_TIMING = "timing"
VARIANCE_SYSTEMATIC = "systematic"
VARIANCE_ONE_OFF = "one_off"

# A systematic classification requires the SAME-SIGNED variance to persist across at
# least this many consecutive scored weeks (a cadence count for the pattern rule,
# not an accuracy threshold — G5 excludes accuracy thresholds, not week counts).
SYSTEMATIC_MIN_CONSECUTIVE_WEEKS = 3


def classify_entity_variance(
    variance: float,
    *,
    next_week_offset: float = 0.0,
    prior_variances: list[float] | None = None,
) -> dict:
    """Classify one entity-week variance as timing / systematic / one-off.

    - TIMING: the shortfall/excess is (at least partially) offset by an opposite-
      signed amount for the SAME entity in the ADJACENT week of the SAME frozen grid
      (``next_week_offset`` = that week's actual − forecast). The linked amount is
      reported so a reviewer sees how much self-corrects.
    - SYSTEMATIC: the variance carries the same sign across the last
      ``SYSTEMATIC_MIN_CONSECUTIVE_WEEKS`` scored weeks (``prior_variances`` are the
      entity's variances for the preceding weeks, most recent last).
    - ONE_OFF: everything else. Annotate, don't recalibrate.
    """
    if variance == 0.0:
        return {"classification": VARIANCE_ONE_OFF, "timing_offset_amount": 0.0}

    # Timing: opposite-signed adjacent-week offset overlapping this variance.
    if variance * next_week_offset < 0:
        offset_amount = round(min(abs(variance), abs(next_week_offset)), 2)
        if offset_amount > 0:
            return {"classification": VARIANCE_TIMING, "timing_offset_amount": offset_amount}

    history = list(prior_variances or [])
    recent = history[-(SYSTEMATIC_MIN_CONSECUTIVE_WEEKS - 1):]
    if len(recent) >= SYSTEMATIC_MIN_CONSECUTIVE_WEEKS - 1 and all(
        v * variance > 0 for v in recent
    ):
        return {"classification": VARIANCE_SYSTEMATIC, "timing_offset_amount": 0.0}

    return {"classification": VARIANCE_ONE_OFF, "timing_offset_amount": 0.0}


def build_week_variance_report(
    frozen_grid: dict,
    target_week: str,
    actuals_by_entity: dict[tuple[str, str], float],
    *,
    next_week_actuals_by_entity: dict[tuple[str, str], float] | None = None,
    prior_variances_by_entity: dict[tuple[str, str], list[float]] | None = None,
) -> dict:
    """Per-entity variance for ``target_week`` against a FROZEN snapshot version grid.

    ``frozen_grid`` is the compact stored form ({week_headers, customers, vendors, ...});
    ``actuals_by_entity`` maps (entity_name, entity_type) → weekly actual total
    (customers = cash in, vendors = cash out; amounts positive per lane).
    """
    week_headers = list(frozen_grid.get("week_headers") or [])
    if target_week not in week_headers:
        return {
            "target_week_ending": target_week,
            "status": "week_not_in_frozen_grid",
            "rows": [],
        }
    col = week_headers.index(target_week)
    next_col = col + 1 if col + 1 < len(week_headers) else None
    next_actuals = next_week_actuals_by_entity or {}
    prior_map = prior_variances_by_entity or {}

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    lanes = (
        (list(frozen_grid.get("customers") or []), "customer"),
        (list(frozen_grid.get("vendors") or []), "vendor"),
    )
    for lane_entities, entity_type in lanes:
        for entity in lane_entities:
            name = entity.get("name") or ""  # SA-OK: dict access on the in-memory frozen grid — no DB query in this loop
            amounts = entity.get("amounts") or []  # SA-OK: dict access on the in-memory frozen grid — no DB query in this loop
            forecast = float(amounts[col]) if col < len(amounts) else 0.0
            key = (name, entity_type)
            seen.add(key)
            actual = float(actuals_by_entity.get(key, 0.0))  # SA-OK: dict access on an in-memory actuals map — no DB query in this loop
            variance = round(actual - forecast, 2)
            if forecast == 0.0 and actual == 0.0:
                continue
            next_forecast = (
                float(amounts[next_col])
                if next_col is not None and next_col < len(amounts)
                else 0.0
            )
            next_offset = float(next_actuals.get(key, 0.0)) - next_forecast  # SA-OK: dict access on an in-memory actuals map — no DB query in this loop
            classified = classify_entity_variance(
                variance,
                next_week_offset=next_offset,
                prior_variances=prior_map.get(key),  # SA-OK: dict access on an in-memory history map — no DB query in this loop
            )
            rows.append({
                "entity_name": name,
                "entity_type": entity_type,
                "forecast": round(forecast, 2),
                "actual": round(actual, 2),
                "variance": variance,
                **classified,
            })

    # Actuals with no frozen forecast row at all — unforecast activity, surfaced.
    for key, actual in actuals_by_entity.items():
        if key in seen or actual == 0.0:
            continue
        name, entity_type = key
        rows.append({
            "entity_name": name,
            "entity_type": entity_type,
            "forecast": 0.0,
            "actual": round(float(actual), 2),
            "variance": round(float(actual), 2),
            "classification": VARIANCE_ONE_OFF,
            "timing_offset_amount": 0.0,
            "unforecast_entity": True,
        })

    rows.sort(key=lambda r: -abs(r["variance"]))
    in_rows = [r for r in rows if r["entity_type"] == "customer"]
    out_rows = [r for r in rows if r["entity_type"] == "vendor"]
    return {
        "target_week_ending": target_week,
        "status": "scored",
        "rows": rows,
        "totals": {
            "forecast_in": round(sum(r["forecast"] for r in in_rows), 2),
            "actual_in": round(sum(r["actual"] for r in in_rows), 2),
            "forecast_out": round(sum(r["forecast"] for r in out_rows), 2),
            "actual_out": round(sum(r["actual"] for r in out_rows), 2),
        },
        "taxonomy_counts": {
            VARIANCE_TIMING: sum(1 for r in rows if r["classification"] == VARIANCE_TIMING),
            VARIANCE_SYSTEMATIC: sum(1 for r in rows if r["classification"] == VARIANCE_SYSTEMATIC),
            VARIANCE_ONE_OFF: sum(1 for r in rows if r["classification"] == VARIANCE_ONE_OFF),
        },
    }


def _direction_accuracy(pairs: list[tuple[float, float]]) -> dict:
    """MAPE + bias + MAD over (forecast, actual) weekly pairs for one direction.

    MAPE = mean(|F−A| / |A|) over weeks with a NON-ZERO actual (zero-actual weeks
    are excluded from MAPE — the ratio explodes — and counted; MAD covers them).
    bias = (ΣF − ΣA) / ΣA (direction: positive = persistent over-forecast).
    MAD = mean(|F−A|) in dollars over ALL scored weeks.
    NO judgment thresholds are computed here (gap G5 — method only, no target %).
    """
    if not pairs:
        return {"mape": None, "bias": None, "mad": None, "weeks_scored": 0, "weeks_excluded_zero_actual": 0}
    abs_errors = [abs(f - a) for f, a in pairs]
    mad = round(sum(abs_errors) / len(pairs), 2)
    apes = [abs(f - a) / abs(a) for f, a in pairs if a != 0.0]
    zero_actual = len(pairs) - len(apes)
    mape = round(sum(apes) / len(apes), 4) if apes else None
    sum_f = sum(f for f, _ in pairs)
    sum_a = sum(a for _, a in pairs)
    bias = round((sum_f - sum_a) / sum_a, 4) if sum_a != 0.0 else None
    return {
        "mape": mape,
        "bias": bias,
        "mad": mad,
        "weeks_scored": len(pairs),
        "weeks_excluded_zero_actual": zero_actual,
    }


def compute_forecast_accuracy(scored_weeks: list[dict]) -> dict:
    """Paired MAPE + bias (+MAD) over frozen-version scored weeks, per direction.

    Each entry: {week_ending, forecast_in, actual_in, forecast_out, actual_out,
    pull_partial}. ``pull_partial`` weeks are EXCLUDED from scoring (they were built
    on incomplete QBO pulls — scoring them corrupts the metric) and counted.
    """
    excluded_partial = [w for w in scored_weeks if w.get("pull_partial")]
    scorable = [w for w in scored_weeks if not w.get("pull_partial")]
    in_pairs = [(float(w["forecast_in"]), float(w["actual_in"])) for w in scorable]
    out_pairs = [(float(w["forecast_out"]), float(w["actual_out"])) for w in scorable]
    return {
        "method": (
            "MAPE (magnitude) + bias (direction) + MAD ($) over FROZEN snapshot "
            "versions; pull_partial weeks excluded; no numeric accuracy thresholds "
            "encoded (G5 open — refuse over fabricate)"
        ),
        "cash_in": _direction_accuracy(in_pairs),
        "cash_out": _direction_accuracy(out_pairs),
        "weeks_considered": len(scored_weeks),
        "weeks_excluded_pull_partial": len(excluded_partial),
        "excluded_week_endings": [w.get("week_ending") for w in excluded_partial],
    }

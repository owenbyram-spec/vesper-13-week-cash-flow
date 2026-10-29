"""TWCF scenario / stress transforms — READ-ONLY over the served snapshot payload.

Build-new RD-2 row (plan 20260706-cfo-grade-financial-workflows, AC-2B.1): the
13-week grid has no scenario dimension at HEAD — only per-cell overrides. These
transforms produce named stress views WITHOUT any persistence:

- every case is a parameterized transform over ONE base payload, re-anchored to the
  SAME real bank balance (a case never forks the anchor — cashflow-modeling §5,
  anti-pattern 10);
- historical (lookback) columns are actuals and are NEVER transformed;
- override-vs-scenario precedence: user overrides are already baked into the base
  payload the transform consumes, so overrides apply FIRST and the scenario stresses
  the overridden grid — the scenario never rewrites an override;
- nothing here writes: a persisted forecast change still goes through the approval
  queue (agentic-writeback-safety); this module has no DB access at all;
- every output is labeled with its case name + parameters — a stress number quoted
  as THE forecast is a defect.

The named default cases below are labeled hypothetical STRESS ASSUMPTIONS (drivers
moved coherently), not forecast constants — callers can override the parameters.
"""
from __future__ import annotations

from .liquidity import compute_liquidity_summary

# ── Named default stress cases (labeled hypotheticals; parameters, not truth) ──
# downside_collections: collections slow AND slip — receipts haircut 15% and arrive
#   2 weeks late (the coherent "customers pay less, later" move).
# revenue_shock: a demand shock — receipts haircut 25%, 1-week slip.
# cost_inflation: disbursements inflate 10%; receipts unchanged.
DEFAULT_STRESS_CASES: list[dict] = [
    {
        "name": "downside_collections",
        "label": "Downside collections (receipts −15%, AR slips 2 weeks)",
        "params": {"collections_haircut_pct": 0.15, "ar_slip_weeks": 2, "outflow_inflation_pct": 0.0},
    },
    {
        "name": "revenue_shock",
        "label": "Revenue shock (receipts −25%, AR slips 1 week)",
        "params": {"collections_haircut_pct": 0.25, "ar_slip_weeks": 1, "outflow_inflation_pct": 0.0},
    },
    {
        "name": "cost_inflation",
        "label": "Cost inflation (disbursements +10%)",
        "params": {"collections_haircut_pct": 0.0, "ar_slip_weeks": 0, "outflow_inflation_pct": 0.10},
    },
]


def _roll_balances_with_anchors(
    total_in: list[float],
    total_out: list[float],
    anchors: dict[int, float],
) -> tuple[list[float], list[float]]:
    """Roll ``ending = beginning + in − out`` with explicit anchor re-seeds.

    ``anchors`` maps column index → beginning balance to pin at that column. This
    generalizes the base grid's opening-anchor + control-re-anchor pair so a scenario
    reproduces the base's anchoring exactly (same anchor, transformed flows).
    """
    n = len(total_in)
    beginning = [0.0] * n
    ending = [0.0] * n
    for i in range(n):
        if i in anchors:
            beginning[i] = round(float(anchors[i]), 2)
        elif i > 0:
            beginning[i] = ending[i - 1]
        ending[i] = round(beginning[i] + float(total_in[i]) - float(total_out[i]), 2)
    return beginning, ending


def apply_scenario_transform(
    payload: dict,
    *,
    name: str,
    label: str,
    collections_haircut_pct: float = 0.0,
    ar_slip_weeks: int = 0,
    outflow_inflation_pct: float = 0.0,
    min_cash_floor: float = 0.0,
) -> dict:
    """Apply one named read-only stress case to a served /weekly payload.

    Transforms FORECAST columns only (index >= lookback): receipts slip right by
    ``ar_slip_weeks`` (amounts pushed past the horizon are dropped and honestly
    reported as ``slipped_beyond_horizon``), then receipts are haircut and
    disbursements inflated. Balances re-roll from the base payload's own anchors.
    """
    week_headers = list(payload.get("week_headers") or [])
    lookback = int(payload.get("lookback") or 0)
    total_in = [float(v) for v in (payload.get("total_cash_in") or [])]
    total_out = [float(v) for v in (payload.get("total_cash_out") or [])]
    base_beginnings = [float(v) for v in (payload.get("beginning_balances") or [])]
    n = len(week_headers)

    slipped_beyond_horizon = 0.0
    if ar_slip_weeks > 0 and n > lookback:
        forecast_in = total_in[lookback:]
        shifted = [0.0] * len(forecast_in)
        for i, amount in enumerate(forecast_in):
            j = i + int(ar_slip_weeks)
            if j < len(shifted):
                shifted[j] = round(shifted[j] + amount, 2)
            else:
                slipped_beyond_horizon = round(slipped_beyond_horizon + amount, 2)
        total_in = total_in[:lookback] + shifted

    haircut_factor = 1.0 - float(collections_haircut_pct)
    inflation_factor = 1.0 + float(outflow_inflation_pct)
    for i in range(lookback, n):
        total_in[i] = round(total_in[i] * haircut_factor, 2)
        total_out[i] = round(total_out[i] * inflation_factor, 2)

    # SAME anchors as the base grid: the opening balance, plus the control re-anchor
    # at the first forecast column when the base was re-anchored (its beginning
    # balance at ``lookback`` IS the reconciled control cash).
    anchors: dict[int, float] = {}
    if base_beginnings:
        anchors[0] = base_beginnings[0]
    if 0 < lookback < len(base_beginnings):
        anchors[lookback] = base_beginnings[lookback]
    beginning, ending = _roll_balances_with_anchors(total_in, total_out, anchors)

    return {
        "case": name,
        "label": label,
        "params": {
            "collections_haircut_pct": collections_haircut_pct,
            "ar_slip_weeks": ar_slip_weeks,
            "outflow_inflation_pct": outflow_inflation_pct,
        },
        "writeback": "read_only",  # a persisted change goes through the approval queue
        "week_headers": week_headers,
        "lookback": lookback,
        "total_cash_in": total_in,
        "total_cash_out": total_out,
        "beginning_balances": beginning,
        "ending_balances": ending,
        "slipped_beyond_horizon": slipped_beyond_horizon,
        "anchor_note": "re-anchored to the base grid's own bank anchor(s) — never a forked balance",
        "liquidity": compute_liquidity_summary(
            week_headers, ending, lookback, min_cash_floor=min_cash_floor,
        ),
    }


def build_scenario_set(
    payload: dict,
    *,
    cases: list[dict] | None = None,
    min_cash_floor: float = 0.0,
) -> list[dict]:
    """Apply every named case (default set unless overridden) to one base payload."""
    results = []
    for case in (cases if cases is not None else DEFAULT_STRESS_CASES):
        params = dict(case.get("params") or {})  # SA-OK: bounded-loop 3 named stress cases — dict access on an in-memory case list, no DB query
        results.append(
            apply_scenario_transform(
                payload,
                name=str(case.get("name") or "unnamed_case"),
                label=str(case.get("label") or case.get("name") or "unnamed case"),
                min_cash_floor=min_cash_floor,
                **params,
            )
        )
    return results

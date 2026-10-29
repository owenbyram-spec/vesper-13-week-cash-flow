"""Liquidity runway + minimum-cash-week summary off the PRIMARY snapshot grid.

RD-2 extend row (plan 20260706-cfo-grade-financial-workflows, AC-2B.1): runway and
the minimum-cash week were previously surfaced only by the legacy average-burn
engine (app/analysis/predictive_cash.py). This module computes them from the served
13-week grid's ``ending_balances`` — the grid-based method the cashflow-modeling
skill prefers (§4) — and is wired into every served /weekly payload.

Conventions (cashflow-modeling §4):
- runway is paired with the minimum-balance week (an average hides a mid-quarter
  payroll + debt-service trough);
- runway counts FULL weeks before the first breach and truncates DOWN (conservative,
  matching the legacy engine's direction);
- the floor defaults to zero; a levered tenant passes its covenant minimum-liquidity
  level instead — the floor is a parameter, never a buried constant;
- no breach within the horizon is reported as exactly that (">= horizon"), never
  extrapolated into a fabricated longer runway.
"""
from __future__ import annotations


def compute_liquidity_summary(
    week_headers: list[str],
    ending_balances: list[float],
    lookback: int,
    *,
    min_cash_floor: float = 0.0,
) -> dict:
    """Grid-based liquidity summary over the FORECAST columns (index >= lookback).

    Returns runway (full weeks before the first floor breach, or None with
    ``runway_status="no_breach_within_horizon"``), the breach week, and the
    minimum-cash week + balance. Empty forecast windows return an honest
    zero-horizon summary rather than fabricated numbers.
    """
    total_cols = min(len(week_headers), len(ending_balances))
    first_forecast_idx = max(lookback, 0)
    forecast_idxs = list(range(first_forecast_idx, total_cols))
    horizon_weeks = len(forecast_idxs)
    if horizon_weeks == 0:
        return {
            "method": "grid_ending_balances",
            "horizon_weeks": 0,
            "min_cash_floor": round(float(min_cash_floor), 2),
            "runway_weeks": None,
            "runway_status": "no_forecast_columns",
            "first_breach_week_ending": None,
            "min_cash_week_ending": None,
            "min_cash_balance": None,
        }

    breach_idx = None
    for i in forecast_idxs:
        if float(ending_balances[i]) < float(min_cash_floor):
            breach_idx = i
            break

    min_idx = min(forecast_idxs, key=lambda i: float(ending_balances[i]))

    if breach_idx is None:
        runway_weeks = None
        runway_status = "no_breach_within_horizon"
        first_breach_week = None
    else:
        # FULL weeks of cash before the breach week — truncates DOWN (conservative):
        # a breach in the very first forecast week is a runway of 0 weeks.
        runway_weeks = breach_idx - first_forecast_idx
        runway_status = "breaches_within_horizon"
        first_breach_week = week_headers[breach_idx]

    return {
        "method": "grid_ending_balances",
        "horizon_weeks": horizon_weeks,
        "min_cash_floor": round(float(min_cash_floor), 2),
        "runway_weeks": runway_weeks,
        "runway_status": runway_status,
        "first_breach_week_ending": first_breach_week,
        "min_cash_week_ending": week_headers[min_idx],
        "min_cash_balance": round(float(ending_balances[min_idx]), 2),
    }


def liquidity_from_payload(payload: dict, *, min_cash_floor: float = 0.0) -> dict:
    """Convenience wrapper over a served /weekly payload dict."""
    return compute_liquidity_summary(
        list(payload.get("week_headers") or []),
        [float(b) for b in (payload.get("ending_balances") or [])],
        int(payload.get("lookback") or 0),
        min_cash_floor=min_cash_floor,
    )

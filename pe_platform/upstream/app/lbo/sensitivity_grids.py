"""
app/lbo/sensitivity_grids.py
────────────────────────────
2-D sensitivity grids + the returns-attribution bridge (Phase 2A, RD-1 rows 18/§11;
plan 20260706-cfo-grade-financial-workflows AC-2A.1).

Two canonical highest-torque grids:

- **entry × exit multiple** — LBO returns are convex in entry price and exit
  multiple; a single-point IRR hides it.
- **leverage × EBITDA growth** — tranche sizes scale proportionally to the target
  Debt/LTM-EBITDA turn; growth overrides the overall revenue ramp.

Every cell is a FULL waterfall solve (compute_full_forecast → compute_exit) — never
a shortcut off a stale schedule — and the solver's fail-loud convergence behavior is
preserved in every cell: a WaterfallConvergenceError propagates and fails the whole
grid (a grid of non-converged numbers is a grid of fabrications).

Axis defaults mirror the pre-existing 1-D convention (base ± 2 steps); explicit axes
win. All payloads are the opaque engine dicts (dict[str, object], the
waterfall_solve precedent).

Period-aware re-runs (G7, plan 20260717-three-statement-spine AC-5): the grids
thread the OPTIONAL ``historical_periods`` feed into every cell re-run, so each
cell carries the SAME validated historical columns and period-derived seeds
(base_revenue / ltm_ebitda / historical_bs from the LTM period) as the base run
— a re-run must never silently drop the spine's historical feed. ``num_years``
stays the FORECAST-year count (the Y1..Yn axis after PF-open); historical
columns never join the projection horizon. The leverage grid derives its
scaling LTM EBITDA from the feed when no explicit one is passed, so its guard
and cells agree with the engine's own period seeding.
"""
from __future__ import annotations

from .engine_support import normalize_historical_periods, seed_from_periods
from .exit_waterfall import compute_exit
from .forecast_engine import compute_full_forecast

EnginePayload = dict[str, object]


def _period_aware_ltm(
    ltm_ebitda: float, historical_periods: list[EnginePayload] | None
) -> float:
    """The LTM EBITDA a period-fed engine run would seed itself with.

    An explicit non-zero ``ltm_ebitda`` always wins (the engine's own
    explicit-seed-wins rule); otherwise the LAST fed period (the LTM) provides
    it — keeping the grid's leverage scaling consistent with the cells it runs.
    """
    if ltm_ebitda > 0 or not historical_periods:
        return float(ltm_ebitda)
    seed = seed_from_periods(normalize_historical_periods(historical_periods))
    return float(seed["ltm_ebitda"])  # type: ignore[arg-type]


def _axis_around(
    base: float, step: float, span: int = 2, positive_only: bool = True
) -> list[float]:
    """base ± span·step (mirrors the 1-D exit sweep). ``positive_only`` filters
    non-positive values — required for multiples/leverage (a ≤0 multiple is
    meaningless) but NOT for growth rates (0% and negative growth are legitimate
    stress cells)."""
    values = [round(base + k * step, 4) for k in range(-span, span + 1)]
    return [v for v in values if v > 0] if positive_only else values


def _run_cell(
    drivers: EnginePayload,
    payroll_drivers: list[EnginePayload],
    capex_drivers: list[EnginePayload],
    debt_tranches: list[EnginePayload],
    num_years: int,
    ltm_ebitda: float,
    historical_bs: EnginePayload | None,
    base_revenue: float,
    historical_periods: list[EnginePayload] | None = None,
    forecast_opening_bs: EnginePayload | None = None,
    stub_fraction: float | None = None,
    transaction_close_date: object | None = None,
    period_end_dates: list[object] | None = None,
) -> dict[str, object]:
    """One grid cell = one full forecast + exit solve. Fail-loud end to end.

    W3e: ``forecast_opening_bs`` + ``stub_fraction`` re-seed the cell's PROJECTION to match /returns (the
    re-seed off the last complete FY + the partial-stub proration); ``transaction_close_date`` +
    ``period_end_dates`` give the cell exit the SAME dated-XIRR anchor as /returns — else the grid IRRs
    diverge from the headline on both the projection basis AND uniform-vs-dated axes. All default None →
    byte-parity with the pre-W3e stored-driver grid.
    """
    forecast = compute_full_forecast(
        drivers=drivers,
        payroll_drivers=payroll_drivers,
        capex_drivers=capex_drivers,
        debt_tranches=debt_tranches,
        num_years=num_years,
        ltm_ebitda=ltm_ebitda,
        historical_bs=historical_bs,
        base_revenue=base_revenue,
        historical_periods=historical_periods,
        forecast_opening_bs=forecast_opening_bs,
        stub_fraction=stub_fraction,
    )
    result = compute_exit(
        forecast_years=forecast["pnl"],
        debt_schedule_years=forecast["debt_schedule"]["schedule"],
        drivers=drivers,
        entry_equity=forecast["entry_equity"],
        distributions=forecast["distributions"],
        cash_by_year=[float(b["cash"]) for b in forecast["bs"]],
        transaction_close_date=transaction_close_date,
        period_end_dates=period_end_dates,
    )
    return {
        "irr": result["gross_irr"],
        "moic": result["gross_moic"],
        "equity_value": result["equity_value"],
        "entry_equity": result["entry_equity"],
    }


def compute_entry_exit_grid(
    drivers: EnginePayload,
    payroll_drivers: list[EnginePayload],
    capex_drivers: list[EnginePayload],
    debt_tranches: list[EnginePayload],
    num_years: int,
    ltm_ebitda: float,
    historical_bs: EnginePayload | None = None,
    base_revenue: float = 0,
    entry_multiples: list[float] | None = None,
    exit_multiples: list[float] | None = None,
    historical_periods: list[EnginePayload] | None = None,
    forecast_opening_bs: EnginePayload | None = None,
    stub_fraction: float | None = None,
    transaction_close_date: object | None = None,
    period_end_dates: list[object] | None = None,
) -> dict[str, object]:
    """Gross IRR/MOIC over an entry-multiple × exit-multiple grid.

    The entry axis moves the purchase price, so any explicit
    ``entry_enterprise_value`` override is REMOVED per cell (otherwise a signed-APA
    EV would pin the price and the axis would silently do nothing — a fabricated
    flat grid). Rows = entry multiples; cols = exit multiples.
    """
    base_entry = float(drivers.get("entry_ebitda_multiple", 8.0))
    base_exit = float(drivers.get("exit_ebitda_multiple", 8.0))
    rows = entry_multiples or _axis_around(base_entry, 1.0)
    cols = exit_multiples or _axis_around(base_exit, 1.0)

    cells: list[list[dict[str, object]]] = []
    for entry_m in rows:
        row_cells: list[dict[str, object]] = []
        for exit_m in cols:
            cell_drivers = dict(drivers)
            cell_drivers["entry_ebitda_multiple"] = entry_m
            cell_drivers["exit_ebitda_multiple"] = exit_m
            cell_drivers.pop("entry_enterprise_value", None)
            row_cells.append(
                _run_cell(
                    cell_drivers, payroll_drivers, capex_drivers, debt_tranches,
                    num_years, ltm_ebitda, historical_bs, base_revenue,
                    historical_periods=historical_periods,
                    forecast_opening_bs=forecast_opening_bs,
                    stub_fraction=stub_fraction,
                    transaction_close_date=transaction_close_date,
                    period_end_dates=period_end_dates,
                )
            )
        cells.append(row_cells)

    return {
        "grid_type": "entry_exit",
        "row_axis": {"name": "entry_ebitda_multiple", "values": rows},
        "col_axis": {"name": "exit_ebitda_multiple", "values": cols},
        "cells": cells,
    }


def compute_leverage_growth_grid(
    drivers: EnginePayload,
    payroll_drivers: list[EnginePayload],
    capex_drivers: list[EnginePayload],
    debt_tranches: list[EnginePayload],
    num_years: int,
    ltm_ebitda: float,
    historical_bs: EnginePayload | None = None,
    base_revenue: float = 0,
    leverage_multiples: list[float] | None = None,
    growth_rates: list[float] | None = None,
    historical_periods: list[EnginePayload] | None = None,
    forecast_opening_bs: EnginePayload | None = None,
    stub_fraction: float | None = None,
    transaction_close_date: object | None = None,
    period_end_dates: list[object] | None = None,
) -> dict[str, object]:
    """Gross IRR/MOIC over a leverage (Debt/LTM-EBITDA) × revenue-growth grid.

    Leverage cells scale EVERY tranche's funded amount proportionally to hit the
    target turn (the tranche mix — pricing, seniority, PIK — is preserved; only the
    quantum moves). Growth cells override the overall revenue ramp with a flat rate.
    Raises ``ValueError`` when leverage cannot be varied (no funded debt, or zero
    LTM EBITDA) — an axis that silently does nothing would be a fabricated grid.
    The scaling LTM EBITDA is period-aware (G7): an explicit value wins, else the
    fed LTM period provides it — the SAME seed the engine derives per cell.
    """
    total_debt = sum(float(t.get("funded_amount", 0) or 0) for t in debt_tranches)  # SA-OK: bounded-loop dict.get on in-memory tranche dicts, not a DB query
    eff_ltm_ebitda = _period_aware_ltm(float(ltm_ebitda), historical_periods)
    if total_debt <= 0 or eff_ltm_ebitda <= 0:
        raise ValueError(
            "cannot vary leverage: the scenario needs funded (non-revolver) debt "
            "tranches and a positive LTM EBITDA to scale against"
        )
    base_leverage = round(total_debt / eff_ltm_ebitda, 4)
    growth_cfg = drivers.get("revenue_growth_by_stream") or {}
    overall = growth_cfg.get("overall") if isinstance(growth_cfg, dict) else None  # SA-OK: dict.get on the in-memory driver payload, not a DB query
    base_growth = float(overall[0]) if overall else 0.05

    rows = leverage_multiples or _axis_around(base_leverage, 1.0)
    cols = growth_rates or _axis_around(base_growth, 0.02, positive_only=False)

    cells: list[list[dict[str, object]]] = []
    for lev in rows:
        scale = (lev * eff_ltm_ebitda) / total_debt
        scaled_tranches: list[EnginePayload] = []
        for t in debt_tranches:
            scaled = dict(t)
            if not scaled.get("revolver_flag"):
                scaled["funded_amount"] = round(float(t.get("funded_amount", 0) or 0) * scale, 2)
            scaled_tranches.append(scaled)
        row_cells: list[dict[str, object]] = []
        for growth in cols:
            cell_drivers = dict(drivers)
            cell_drivers["revenue_growth_by_stream"] = {"overall": [growth] * num_years}
            row_cells.append(
                _run_cell(
                    cell_drivers, payroll_drivers, capex_drivers, scaled_tranches,
                    num_years, eff_ltm_ebitda, historical_bs, base_revenue,
                    historical_periods=historical_periods,
                    forecast_opening_bs=forecast_opening_bs,
                    stub_fraction=stub_fraction,
                    transaction_close_date=transaction_close_date,
                    period_end_dates=period_end_dates,
                )
            )
        cells.append(row_cells)

    return {
        "grid_type": "leverage_growth",
        "row_axis": {"name": "total_leverage_x_ltm_ebitda", "values": rows},
        "col_axis": {"name": "revenue_growth_overall", "values": cols},
        "cells": cells,
    }


def returns_attribution(
    sources_uses: EnginePayload, exit_result: EnginePayload
) -> dict[str, object]:
    """Decompose the equity gain into its drivers (the §11 attribution bridge).

    Components (each a hand-checkable arithmetic term):
      - ebitda_growth  = (exit EBITDA − entry EBITDA) × entry multiple
      - multiple_change = (exit multiple − entry multiple) × exit EBITDA
      - net_debt_paydown = entry net debt − exit net debt
        (entry net debt = funded debt − PF opening cash; the PF cash is the
        deal-basis cash the sponsor paid for as a Use, so entry equity + entry
        net debt = EV + fees)
      - fees_and_other = the exact residual (entry + exit transaction/financing
        fees) — NAMED so the bridge ties to the gain to the cent, never silently
        plugged.
    """
    entry_ebitda = float(sources_uses["ltm_ebitda"])
    entry_multiple = float(sources_uses["credit_stats"]["entry_multiple"])
    entry_equity = float(exit_result["entry_equity"])
    opening_cash = float(sources_uses["pf_opening_bs"]["cash"])
    entry_debt = float(sources_uses["sources"]["total_debt"])
    entry_net_debt = round(entry_debt - opening_cash, 2)

    exit_ebitda = float(exit_result["exit_ebitda"])
    exit_multiple = float(exit_result["exit_multiple"])
    exit_net_debt = float(exit_result["net_debt"])
    equity_value = float(exit_result["equity_value"])

    gain = round(equity_value - entry_equity, 2)
    ebitda_growth = round((exit_ebitda - entry_ebitda) * entry_multiple, 2)
    multiple_change = round((exit_multiple - entry_multiple) * exit_ebitda, 2)
    net_debt_paydown = round(entry_net_debt - exit_net_debt, 2)
    fees_and_other = round(gain - ebitda_growth - multiple_change - net_debt_paydown, 2)

    return {
        "equity_gain": gain,
        "components": {
            "ebitda_growth": ebitda_growth,
            "multiple_change": multiple_change,
            "net_debt_paydown": net_debt_paydown,
            "fees_and_other": fees_and_other,
        },
        "ties": round(
            ebitda_growth + multiple_change + net_debt_paydown + fees_and_other - gain, 2
        ) == 0.0,
    }

"""
app/agent/fpa/modes.py
──────────────────────
Model archetype definitions and operational modes for the Universal 3-Statement Platform.
Supports the 3 distinct buyer personas (ICPs):
  1. OPERATING: Fractional CFOs & operating companies (rolling forecast, budgeting, runway).
  2. CONSOLIDATED_ROLLUP: reserved for multi-entity roll-ups; refused until a multi-entity
     input exists (CONSOLIDATED_ROLLUP_UNSUPPORTED).
  3. TRANSACTION_LBO: PE deal teams & M&A advisory (capital structure, debt waterfall, sponsor returns).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import List


class ForecastMode(str, enum.Enum):
    OPERATING = "operating"
    CONSOLIDATED_ROLLUP = "consolidated_rollup"
    TRANSACTION_LBO = "transaction_lbo"


# The workbench builds every session from ONE trial balance for the authorized
# company; it has no member-entity input, mapping or elimination step, so a
# consolidated roll-up is refused rather than presented as a single-entity forecast.
CONSOLIDATED_ROLLUP_UNSUPPORTED = (
    "Consolidated roll-up is not available: a workbench session holds one trial balance "
    "for one company, with no member-entity inputs, account mapping or intercompany "
    "eliminations. Use the operating or transaction mode for a single entity."
)


@dataclass(frozen=True)
class ModeConfig:
    mode: ForecastMode
    display_name: str
    target_icp: str
    description: str
    requires_sources_uses: bool = False
    requires_ebitda_addbacks: bool = False
    allows_intercompany_eliminations: bool = False
    requires_headcount_budgeting: bool = True
    default_forecast_years: int = 5
    primary_metrics: List[str] = field(default_factory=list)


MODE_CONFIGS = {
    ForecastMode.OPERATING: ModeConfig(
        mode=ForecastMode.OPERATING,
        display_name="Operating Budget & Rolling 3-Statement Forecast",
        target_icp="Fractional CFOs & Operating Companies (Buyer B)",
        description=(
            "Operational 3-statement modeling for ongoing business management. "
            "Bypasses transaction purchase accounting, projecting dynamic revenue streams, "
            "departmental opex, working capital cycles, and cash runway from live general ledger actuals."
        ),
        requires_sources_uses=False,
        requires_ebitda_addbacks=False,
        allows_intercompany_eliminations=False,
        requires_headcount_budgeting=True,
        default_forecast_years=5,
        primary_metrics=["revenue", "gross_margin", "operating_cash_flow", "ending_cash", "runway_months"],
    ),
    ForecastMode.CONSOLIDATED_ROLLUP: ModeConfig(
        mode=ForecastMode.CONSOLIDATED_ROLLUP,
        display_name="Multi-Entity Consolidated 3-Statement Roll-Up",
        target_icp="CAS Firms & PE Accounting Roll-Ups (Buyer C)",
        description=CONSOLIDATED_ROLLUP_UNSUPPORTED,
        requires_sources_uses=False,
        requires_ebitda_addbacks=True,
        allows_intercompany_eliminations=False,
        requires_headcount_budgeting=False,
        default_forecast_years=3,
        primary_metrics=["consolidated_revenue", "topside_eliminations", "group_ebitda", "net_income"],
    ),
    ForecastMode.TRANSACTION_LBO: ModeConfig(
        mode=ForecastMode.TRANSACTION_LBO,
        display_name="Private Equity Transaction & LBO Deal Model",
        target_icp="PE Deal Teams & M&A Underwriters (Buyer A)",
        description=(
            "Full buyout transaction modeling. Optimizes capital structures with senior term debt, "
            "mezzanine tranches, PIK interest, cash sweep waterfalls, purchase accounting goodwill, "
            "and sponsor IRR / MOIC equity returns."
        ),
        requires_sources_uses=True,
        requires_ebitda_addbacks=True,
        allows_intercompany_eliminations=False,
        requires_headcount_budgeting=False,
        default_forecast_years=5,
        primary_metrics=["sponsor_irr", "sponsor_moic", "total_leverage", "covenant_headroom", "debt_paydown"],
    ),
}


def get_mode_config(mode: ForecastMode | str) -> ModeConfig:
    """Retrieve the ModeConfig for a given mode string or enum."""
    if isinstance(mode, str):
        try:
            mode = ForecastMode(mode.lower())
        except ValueError:
            mode = ForecastMode.OPERATING
    return MODE_CONFIGS.get(mode, MODE_CONFIGS[ForecastMode.OPERATING])

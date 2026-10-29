"""Pure, finite budget arithmetic. No database, provider calls, or mutation."""
from __future__ import annotations

from decimal import Decimal, DecimalException, ROUND_HALF_UP
from typing import Any

from .constants import MONTHS


class BudgetPlanError(ValueError):
    """The entire financial operation must refuse before changing a budget."""


def money(value: Any, label: str = "amount") -> Decimal:
    if value is None or isinstance(value, bool):
        raise BudgetPlanError(f"{label} is missing or invalid.")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number.copy_abs() > Decimal("1e15"):
            raise BudgetPlanError(f"{label} must be finite and within the supported amount range.")
        return number
    except (DecimalException, TypeError, ValueError) as exc:
        raise BudgetPlanError(f"{label} must be a finite number.") from exc


def cents(value: Any) -> float:
    try:
        return float(money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except DecimalException as exc:
        raise BudgetPlanError("Amount cannot be represented at currency precision.") from exc


def month_values(values: Any, *, complete: bool = False, label: str = "monthly amounts") -> dict[str, float]:
    if not isinstance(values, dict):
        raise BudgetPlanError(f"{label} must be a month map.")
    if complete and any(m not in values or values[m] is None for m in MONTHS):
        raise BudgetPlanError(f"{label} must contain all twelve months; missing data is not zero.")
    if any(m not in MONTHS for m in values):
        raise BudgetPlanError(f"{label} contains an unknown month.")
    return {m: cents(values[m]) if m in values else 0.0 for m in MONTHS}


def calculate_driver_values(
    driver: dict,
    *,
    historical: dict[str, float] | None = None,
    revenue: dict[str, float] | None = None,
) -> tuple[dict[str, float], str]:
    """Calculate all twelve values without modifying the input dictionaries.

    Historical inputs are explicitly resolved source values, never an implicit zero
    on a failed fetch. Volume can consume a reviewed, complete stored computation;
    it cannot manufacture quantities or invoke a model.
    """
    params = driver.get("driver_params") or {}
    if not isinstance(params, dict):
        raise BudgetPlanError("Driver parameters must be an object.")
    method = driver.get("driver_method")
    if method == "fixed":
        amount = cents(money(params.get("amount"), "Fixed amount"))
        computed, basis = {m: amount for m in MONTHS}, "explicit_fixed_amount"
    elif method == "pct_revenue":
        pct = money(params.get("pct"), "Revenue percentage") / Decimal(100)
        if revenue is None:
            raise BudgetPlanError("Projected revenue is unavailable.")
        values = month_values(revenue, complete=True, label="Projected revenue")
        computed = {m: cents(money(values[m]) * pct) for m in MONTHS}
        basis = "planned_same_scenario_revenue"
    elif method in {"ttm_avg", "yoy_growth"}:
        if historical is None:
            raise BudgetPlanError("Historical budget source is unavailable; no amounts were applied.")
        values = month_values(historical, complete=True, label="Historical source")
        if method == "ttm_avg":
            amount = cents(sum((money(v) for v in values.values()), Decimal(0)) / Decimal(12))
            computed = {m: amount for m in MONTHS}
        else:
            growth = money(params.get("growth_pct"), "Growth percentage") / Decimal(100)
            computed = {m: cents(money(values[m]) * (1 + growth)) for m in MONTHS}
        basis = "resolved_historical_budget_values"
    elif method == "volume":
        quantities = params.get("quantities")
        if quantities is None and "quantity" in params:
            quantities = {m: params["quantity"] for m in MONTHS}
        if quantities is not None:
            if not isinstance(quantities, dict) or set(quantities) != set(MONTHS):
                raise BudgetPlanError("Explicit quantities must contain all twelve months.")
            units = {m: money(quantities[m], "Explicit quantity") for m in MONTHS}
            price = money(params.get("price_per_unit"), "Unit price")
            computed = {m: cents(money(units[m]) * price) for m in MONTHS}
            basis = "explicit_quantity_times_price"
        else:
            computed = month_values(driver.get("computed_values"), complete=True, label="Stored volume computation")
            basis = "existing_computed_values_not_reestimated"
    else:
        raise BudgetPlanError(f"Unsupported budget driver method: {method!r}.")
    return computed, basis


def effective_values(computed: dict, overrides: Any) -> dict[str, float]:
    values = month_values(computed, complete=True, label="Computed values")
    if overrides is None:
        return values
    if not isinstance(overrides, dict) or any(m not in MONTHS for m in overrides):
        raise BudgetPlanError("Driver overrides must use known month keys.")
    for month, value in overrides.items():
        values[month] = cents(money(value, f"{month} override"))
    return values

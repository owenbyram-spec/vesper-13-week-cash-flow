from datetime import date
from typing import Dict, List, Optional

from ..models import BudgetLine
from ..semantics import Basis, MetricRef, Period, Scenario, Unit, check_comparable

# ─────────────────────────────────────────────────────────────────────────────
# Budget-vs-actual semantic compatibility (FOS-1 Wave B, AC-3)
# ─────────────────────────────────────────────────────────────────────────────
# The budget-vs-actual comparison crosses scenarios BY DESIGN — it is the product
# feature — so the compat check below always passes ``allow_cross_scenario=True``
# for the SAME metric (``budget.line_amount``) read in the BUDGET and ACTUAL
# worlds. What the check catches is the ACCIDENTAL incompatibility riding along:
# a cash-basis actuals report against an accrual budget, a month-to-date actual
# against a full-month budget line, an entity/currency/unit drift. Observability
# only: reason codes land in the caller's data_quality envelope; no number changes.

_BUDGET_METRIC_ID = "budget.line_amount"

# Budget month keys ("jan".."dec", app/budget/constants.MONTHS) → month number.
_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def period_for_budget_month(budget_year: int, month_key: str) -> Period:
    """The full-month semantic Period for a budget MONTHS key ("jan".."dec") in
    ``budget_year``. Raises ValueError on an unknown key (refuse over fabricate)."""
    month = _MONTH_NUM.get(str(month_key).strip().lower()[:3])
    if not month:
        raise ValueError(f"unknown budget month key {month_key!r}")
    return Period.month(int(budget_year), month)


def new_match_counts() -> Dict[str, int]:
    """A fresh matched-by counter for ``_aggregate_actual_amount``: how many rollup
    lines resolved by QBO account id, by account-name fallback, or not at all."""
    return {"id": 0, "name": 0, "unmatched": 0}


def budget_vs_actual_compat(
    *,
    entity_id: str,
    budget_period: Period,
    actual_period: Optional[Period] = None,
    budget_basis: Basis = Basis.ACCRUAL,
    actual_basis: Basis = Basis.ACCRUAL,
    as_of: Optional[date] = None,
) -> List[str]:
    """Semantic-comparability reason codes for one budget-vs-actual pairing (empty list
    = comparable). Runs ``check_comparable`` (NON-raising) with
    ``allow_cross_scenario=True`` — the BUDGET↔ACTUAL crossing itself is the feature —
    over the period/basis (+ entity/currency/unit) dimensions. ``actual_period``
    defaults to the budget month; pass the actuals report's real window (and ``as_of``
    to sharpen in-flight-month detection) when the caller knows better."""
    budget_ref = MetricRef(
        metric_id=_BUDGET_METRIC_ID,
        entity_id=entity_id,
        period=budget_period,
        basis=budget_basis,
        scenario=Scenario.budget(),
        unit=Unit.CURRENCY,
    )
    actual_ref = MetricRef(
        metric_id=_BUDGET_METRIC_ID,
        entity_id=entity_id,
        period=actual_period if actual_period is not None else budget_period,
        basis=actual_basis,
        scenario=Scenario.actual(),
        unit=Unit.CURRENCY,
    )
    return check_comparable(budget_ref, actual_ref, allow_cross_scenario=True, as_of=as_of)


def budget_vs_actual_data_quality(
    match_counts: Optional[Dict[str, int]] = None,
    compat_codes: Optional[List[str]] = None,
) -> dict:
    """The canonical data_quality summary for one budget-vs-actual aggregation pass —
    the match-basis counts from ``_aggregate_actual_amount`` plus the compat reason
    codes from ``budget_vs_actual_compat`` — in the shape the budget read endpoints
    surface inside their data_quality envelope."""
    return {
        "matched_by": dict(match_counts or {}),
        "compat": list(compat_codes or []),
    }


def _rollup_budget_lines(
    line: BudgetLine,
    children_by_parent: Dict[int, List[BudgetLine]],
) -> List[BudgetLine]:
    lines = [line]
    for child in children_by_parent.get(line.id, []):
        lines.extend(_rollup_budget_lines(child, children_by_parent))
    return lines


def _aggregate_budget_line_amount(
    line: BudgetLine,
    month_key: str,
    children_by_parent: Dict[int, List[BudgetLine]],
) -> float:
    children = children_by_parent.get(line.id, [])
    if children:
        return round(
            sum(_aggregate_budget_line_amount(child, month_key, children_by_parent) for child in children),
            2,
        )
    amounts = line.monthly_amounts or {}
    return round(float(amounts.get(month_key, 0) or 0), 2)


def _aggregate_actual_amount(
    line: BudgetLine,
    actuals: Dict[str, float],
    children_by_parent: Dict[int, List[BudgetLine]],
    actuals_by_id: Optional[Dict[str, float]] = None,
    actual_names_without_id: Optional[set[str]] = None,
    match_counts: Optional[Dict[str, int]] = None,
) -> float:
    """Aggregate the ACTUAL amount for a budget line (id-first, name-fallback matching).

    ``match_counts`` (FOS-1 Wave B, AC-3) is an optional counter dict (see
    ``new_match_counts``): each distinct rollup identity is tallied as matched by
    ``id`` (qbo_account_id hit), by ``name`` (account-name fallback hit), or
    ``unmatched`` (contributed nothing). Counting is observability-only — the
    returned total is byte-identical with or without it."""
    actuals_by_id = actuals_by_id or {}
    actual_names_without_id = actual_names_without_id or set()
    total = 0.0
    seen_identities: set[tuple[str, str]] = set()

    def _count(bucket: str) -> None:
        if match_counts is not None:
            match_counts[bucket] = match_counts.get(bucket, 0) + 1

    for rollup_line in _rollup_budget_lines(line, children_by_parent):
        account_id = getattr(rollup_line, "qbo_account_id", None)
        if account_id and account_id in actuals_by_id:
            identity = ("id", str(account_id))
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            total += float(actuals_by_id.get(account_id, 0) or 0)
            _count("id")
            continue
        if account_id and actuals_by_id and rollup_line.account_name not in actual_names_without_id:
            # Id-bearing line whose id has no actual and whose name is owned by an
            # id-bearing actual elsewhere — it contributes nothing (unchanged skip).
            identity = ("id", str(account_id))
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            _count("unmatched")
            continue
        identity = ("name", rollup_line.account_name)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        total += float(actuals.get(rollup_line.account_name, 0) or 0)
        _count("name" if rollup_line.account_name in actuals else "unmatched")
    return round(total, 2)

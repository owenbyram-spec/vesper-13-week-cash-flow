from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional


DEFAULT_AR_LATE_BUFFER = 7
DEFAULT_AP_LATE_BUFFER = 0

AVG_SENTINEL = date(9999, 12, 31)
DAY_SENTINEL = date(9998, 12, 31)
HIDDEN_SENTINEL = date(9997, 12, 31)
ORDER_SENTINEL = date(9996, 12, 31)
BALANCE_SENTINEL = date(9995, 12, 31)


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _median_int(vals: List[int]) -> int:
    """Median of a list of ints, returned as an int."""
    if not vals:
        return 1
    return int(statistics.median(vals))


# A monthly run-rate (avg_amount) divided by this yields a per-occurrence amount for a
# sub-monthly cadence, so total monthly spend is preserved while the cadence is corrected
# (52 weeks / 12 months). AP-FIN-08: named, not buried in the math.
WEEKS_PER_MONTH = 52 / 12  # ≈ 4.333

# Canonical payment cadences in weeks. A monthly payer keeps the legacy calendar-crossing
# placement (one per calendar month); a sub-monthly payer is placed on an interval cadence;
# a quarterly payer is placed every third month from its last observed payment. 4 = monthly
# (the safe default for sparse data).
WEEKLY = 1
BIWEEKLY = 2
MONTHLY = 4
QUARTERLY = 13

# Median gaps of 10–17 weeks read as quarterly. A slower median gap inside the history
# window is not a cadence the window can evidence (an annual or semi-annual payment is
# seen at most once or twice), so such an entity is not projected. A quarterly payee also
# needs every gap to be at least _QUARTERLY_MIN_GAP_DAYS: two payments a week apart plus
# a third months later are a cluster, not a quarter.
_QUARTERLY_MIN_WEEKS = 10
_QUARTERLY_MAX_WEEKS = 17
_QUARTERLY_MIN_GAP_DAYS = 56
MONTHS_PER_QUARTER = 3

# A monthly-or-faster run-rate needs activity in at least this many distinct calendar
# months; fewer is a one-off (or a one-off split into installments), not a recurrence.
MIN_RECURRING_MONTHS = 3

# History rows QBO cannot attribute to a payee are lumped under these names. The lump mixes
# one-off receipts and payments (a loan draw, an asset sale), so it is never projected as a
# run-rate; the user models it explicitly with the manual "Other Cash In" / "Non Routine
# Expenses" rows instead.
UNATTRIBUTED_HISTORY_NAMES = frozenset({"Other Cash In", "Non Routine Expenses"})


def _median_gap_weeks(txn_dates: list[date]) -> int | None:
    """Median gap between consecutive distinct dates, in whole weeks; None when <2 dates."""
    ordered = sorted(set(txn_dates))
    gaps = [(ordered[i + 1] - ordered[i]).days for i in range(len(ordered) - 1)]
    if not gaps:
        return None
    return round(statistics.median(gaps) / 7)


def _infer_interval_weeks(txn_dates: list[date]) -> int:
    """Infer payment cadence in weeks from the median gap between consecutive transactions.

    Returns one of {WEEKLY, BIWEEKLY, MONTHLY, QUARTERLY}. Defaults to MONTHLY when there are
    <2 observations — we never fabricate a frequent cadence from sparse data (that
    over-forecasts); we only shorten the cadence when the history actually shows
    closely-spaced payments. Whether sparse history is projected at all is decided by
    ``_aggregate_entity_patterns``.
    """
    weeks = _median_gap_weeks(txn_dates)
    if weeks is None:
        return MONTHLY
    if weeks <= 1:
        return WEEKLY
    if weeks <= 3:          # 2–3 week median gap reads as bi-weekly
        return BIWEEKLY
    if weeks >= _QUARTERLY_MIN_WEEKS:
        return QUARTERLY
    return MONTHLY


def _add_months(anchor: date, months: int, day: int) -> date:
    """``anchor`` moved ``months`` calendar months, on ``day`` clamped to the month length."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _crosses_day(fridays: list[date], i: int, entity_day: int) -> bool:
    """Legacy calendar-crossing predicate: True when week i's Friday is the one that crosses
    the entity's day-of-month (i.e. a monthly payment lands in this week)."""
    weeks = len(fridays)
    this_day = fridays[i].day
    next_friday = fridays[i + 1] if i + 1 < weeks else fridays[i] + timedelta(days=7)
    next_day = next_friday.day
    return this_day <= entity_day and (next_day > entity_day or next_day < this_day)


def project_interval_amounts(
    fridays: list[date],
    *,
    interval_weeks: int,
    entity_day: int,
    monthly_avg: float,
    start_idx: int = 0,
    end_idx: int | None = None,
    anchor_date: Optional[date] = None,
) -> dict[int, float]:
    """Return {week_index: amount} for an entity, over the window [start_idx, end_idx).

    QUARTERLY: every third calendar month after ``anchor_date`` (the last observed payment),
    on ``entity_day``, placed in the week whose Friday closes it; the per-occurrence amount is
    monthly_avg × 3. No anchor → nothing is placed (the due dates would be invented).
    MONTHLY: retain each legacy calendar-crossing week, placing an earlier
    day-of-month on that month's first Friday when no earlier Friday exists. Sub-monthly
    (WEEKLY/BIWEEKLY): anchor at the first crossing
    week, then step by `interval_weeks`, placing a per-occurrence amount
    (monthly_avg × interval_weeks / WEEKS_PER_MONTH) so the monthly total is preserved while
    a bi-weekly entity correctly hits ~2–3 times in a month (3 in a 5-Friday month).
    """
    weeks = len(fridays)
    if end_idx is None:
        end_idx = weeks
    if interval_weeks >= QUARTERLY:
        if anchor_date is None or not fridays:
            return {}
        per_occurrence = round(monthly_avg * MONTHS_PER_QUARTER, 2)
        placed: dict[int, float] = {}
        step = 1
        due = _add_months(anchor_date, MONTHS_PER_QUARTER, entity_day)
        while due <= fridays[-1]:
            for i in range(start_idx, end_idx):
                if fridays[i] - timedelta(days=7) < due <= fridays[i]:
                    placed[i] = per_occurrence
                    break
            step += 1
            due = _add_months(anchor_date, MONTHS_PER_QUARTER * step, entity_day)
        return placed
    if interval_weeks >= MONTHLY:
        return {
            i: round(monthly_avg, 2)
            for i in range(start_idx, end_idx)
            # Complete the calendar month's missing early days without inventing catch-up
            # at an arbitrary window start or changing the sub-monthly anchor predicate.
            if _crosses_day(fridays, i, entity_day)
            or entity_day < fridays[i].day <= 7
        }
    anchor = next(
        (i for i in range(start_idx, end_idx) if _crosses_day(fridays, i, entity_day)),
        start_idx,
    )
    per_occurrence = round(monthly_avg * interval_weeks / WEEKS_PER_MONTH, 2)
    return {i: per_occurrence for i in range(anchor, end_idx, interval_weeks)}


def _project_invoices_into_weeks(
    invoices: List[Dict],
    fridays: List[date],
    dso_map: Dict[str, int],
    override_map: Dict[tuple, float],
    entity_type: str,
    forecast_start_idx: int = 0,
) -> List[Dict[str, Any]]:
    """
    Place open invoices/bills into weekly columns based on realistic expected
    payment timing, not just due date.
    """
    weeks = len(fridays)
    first_forecast_idx = min(max(forecast_start_idx, 0), max(weeks - 1, 0))
    first_forecast_monday = fridays[first_forecast_idx] - timedelta(days=4) if fridays else date.today()
    entity_key = "customer" if entity_type == "customer" else "vendor"
    name_field = entity_key
    default_late_buffer = DEFAULT_AR_LATE_BUFFER if entity_type == "customer" else DEFAULT_AP_LATE_BUFFER

    by_entity: Dict[str, List[Dict]] = defaultdict(list)
    for item in invoices:
        by_entity[item[name_field]].append(item)

    rows = []
    for name, items in sorted(by_entity.items(), key=lambda x: -sum(i["balance"] for i in x[1])):
        amounts = [0.0] * weeks
        total_open = sum(i["balance"] for i in items)
        past_due = 0.0
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C3) — cash expected past the last
        # grid Friday is counted per row instead of vanishing through a bare `pass`.
        beyond_horizon_balance = 0.0
        beyond_horizon_items = 0
        known_covered_months: set[str] = set()

        has_historical_data = name in dso_map
        entity_dso = dso_map.get(name, default_late_buffer)
        expected_days_of_month: List[int] = []

        for item in items:
            due = item.get("due_date")
            txn = item.get("txn_date")

            if not due:
                if txn:
                    due = txn + timedelta(days=30)
                else:
                    due = date.today() + timedelta(days=14)

            expected_date = due + timedelta(days=entity_dso)
            expected_days_of_month.append(expected_date.day)
            known_covered_months.add(_month_key(expected_date))

            effective_amount = item["balance"]
            if entity_type == "customer":
                effective_amount *= item.get("collection_pct", 1.0)

            if expected_date < first_forecast_monday:
                past_due += effective_amount
                if weeks:
                    amounts[first_forecast_idx] += effective_amount
                continue

            # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C2/C3). Place into the first
            # forecast week whose Friday is on/after the expected date. For a weekday this is
            # the same Mon..Fri window as before; a WEEKEND expected date (due + DSO lands
            # Sat/Sun) now rolls into the FOLLOWING week — previously it matched no window and
            # the legacy fallback booked it into column 0, a historical column that the
            # actuals-reconciliation phase zeroes (a silent drop). Beyond-horizon cash is
            # excluded-and-counted, never a bare `pass`.
            placed = False
            for i, fri in enumerate(fridays):
                if i < first_forecast_idx:
                    continue
                if expected_date <= fri:
                    amounts[i] += effective_amount
                    placed = True
                    break
            if not placed:
                beyond_horizon_balance += effective_amount
                beyond_horizon_items += 1

        for i in range(weeks):
            cell_key = (name, entity_type, fridays[i])
            if cell_key in override_map:
                amounts[i] = override_map[cell_key]

        avg_key = (name, entity_type, AVG_SENTINEL)
        effective_avg = override_map.get(avg_key, total_open)

        order_key = (name, entity_type, ORDER_SENTINEL)
        sort_order = override_map.get(order_key, 999.0)

        raw_display_day = _median_int(expected_days_of_month) if expected_days_of_month else 15
        display_day = max(1, min(28, raw_display_day))

        day_key = (name, entity_type, DAY_SENTINEL)
        if day_key in override_map:
            display_day = max(1, min(28, int(override_map[day_key])))

        rows.append({
            "name": name,
            "day_of_month": display_day,
            "effective_day": display_day,
            "avg_amount": round(total_open, 2),
            "effective_avg": round(effective_avg, 2),
            "amounts": [round(a, 2) for a in amounts],
            "known_amounts": [round(a, 2) for a in amounts],
            "run_rate_amounts": [0.0] * weeks,
            "known_covered_months": sorted(known_covered_months),
            "sort_order": sort_order,
            "source": "invoice",
            "open_invoices": len(items),
            "total_open_balance": round(total_open, 2),
            "past_due_balance": round(past_due, 2),
            # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-C3) — expected cash past the
            # last grid Friday is excluded from the weekly columns (placing it would fabricate
            # timing) but counted and surfaced so the grid total is never silently short of
            # the open-balance total.
            "beyond_horizon_balance": round(beyond_horizon_balance, 2),
            "beyond_horizon_items": beyond_horizon_items,
            "dso_days": entity_dso,
            "has_historical_data": has_historical_data,
        })

    return rows


def _months_spanned(first: date, last: date) -> int:
    """Calendar months from ``first``'s month through ``last``'s month, inclusive."""
    return (last.year - first.year) * 12 + (last.month - first.month) + 1


def _aggregate_entity_patterns(
    transactions: List[Dict],
    *,
    as_of: Optional[date] = None,
    non_recurring: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Given a list of {'name', 'txn_date', 'amount'}, produce recurring-payment rows with
    name, median day of month, cadence, and average MONTHLY amount.

    Only evidenced recurrences become rows: a quarterly cadence (≥2 payments, median gap
    10–17 weeks, no gap under 8 weeks), or activity in at least MIN_RECURRING_MONTHS
    distinct months for a monthly-or-faster cadence. One-offs, sparse or irregular payees
    and the unattributed lumps are left out — appended to the ``non_recurring`` sink (when
    given) for review — instead of being projected as a monthly run-rate.

    The monthly amount spreads the observed total over every month since the entity's
    first payment (through the last complete month before ``as_of`` when given), so quiet
    months count instead of being skipped. A quarterly entity's monthly amount is its mean
    payment / 3, placed once per quarter from ``anchor_date``.
    """
    by_entity: Dict[str, List[Dict]] = defaultdict(list)
    for transaction in transactions:
        by_entity[transaction["name"]].append(transaction)

    last_complete_month_end = (as_of.replace(day=1) - timedelta(days=1)) if as_of else None

    patterns = []
    for name, txns in sorted(by_entity.items()):
        dates = [transaction["txn_date"] for transaction in txns]
        first, last = min(dates), max(dates)
        distinct_months = len({(d.year, d.month) for d in dates})
        total = sum(transaction["amount"] for transaction in txns)
        gap_weeks = _median_gap_weeks(dates)
        interval_weeks = _infer_interval_weeks(dates)
        distinct_dates = sorted(set(dates))
        min_gap_days = min(
            ((distinct_dates[i + 1] - distinct_dates[i]).days for i in range(len(distinct_dates) - 1)),
            default=None,
        )

        reason = None
        if name in UNATTRIBUTED_HISTORY_NAMES:
            reason = "unattributed"
        elif len(txns) < 2:
            reason = "single_occurrence"
        elif gap_weeks is not None and gap_weeks > _QUARTERLY_MAX_WEEKS:
            reason = "irregular"
        elif interval_weeks == QUARTERLY and (min_gap_days or 0) < _QUARTERLY_MIN_GAP_DAYS:
            reason = "irregular"
        elif interval_weeks != QUARTERLY and distinct_months < MIN_RECURRING_MONTHS:
            reason = "too_few_months"
        if reason is not None:
            if non_recurring is not None:
                non_recurring.append({
                    "name": name,
                    "occurrences": len(txns),
                    "distinct_months": distinct_months,
                    "total_amount": round(total, 2),
                    "last_date": last.isoformat(),
                    "reason": reason,
                })
            continue

        if interval_weeks == QUARTERLY:
            # Mean per payment date (same-day split payments are one occurrence) / 3.
            avg_amount = round(total / len(distinct_dates) / MONTHS_PER_QUARTER, 2)
            day_of_month = last.day
        else:
            span_end = max(last, last_complete_month_end) if last_complete_month_end else last
            avg_amount = round(total / _months_spanned(first, span_end), 2)
            day_of_month = _median_int([d.day for d in dates])

        patterns.append({
            "name": name,
            "day_of_month": day_of_month,
            "interval_weeks": interval_weeks,
            "avg_amount": avg_amount,
            "anchor_date": last.isoformat(),
            "occurrences": len(txns),
            "distinct_months": distinct_months,
        })

    patterns.sort(key=lambda p: p["avg_amount"], reverse=True)
    return patterns


def _infer_run_rate_patterns_from_open_items(
    items: List[Dict],
    dso_map: Dict[str, int],
    entity_type: str,
) -> List[Dict[str, Any]]:
    """
    Infer monthly run-rate from current open AP/AR when cash history does not
    already give us one.
    """
    entity_key = "customer" if entity_type == "customer" else "vendor"
    default_late_buffer = DEFAULT_AR_LATE_BUFFER if entity_type == "customer" else DEFAULT_AP_LATE_BUFFER

    by_entity: Dict[str, List[Dict]] = defaultdict(list)
    for item in items:
        by_entity[item[entity_key]].append(item)

    patterns: List[Dict[str, Any]] = []
    for name, entity_items in sorted(by_entity.items()):
        entity_offset = dso_map.get(name, default_late_buffer)
        monthly_totals: Dict[str, float] = defaultdict(float)
        expected_days: List[int] = []

        for item in entity_items:
            due = item.get("due_date")
            txn = item.get("txn_date")
            if not due:
                if txn:
                    due = txn + timedelta(days=30)
                else:
                    due = date.today() + timedelta(days=14)

            expected_date = due + timedelta(days=entity_offset)
            expected_days.append(expected_date.day)

            amount = float(item.get("balance", 0.0) or 0.0)
            if entity_type == "customer":
                amount *= item.get("collection_pct", 1.0)
            monthly_totals[_month_key(expected_date)] += amount

        avg_amount = round(statistics.mean(monthly_totals.values()), 2) if monthly_totals else 0.0
        if avg_amount <= 0:
            continue

        patterns.append({
            "name": name,
            "day_of_month": max(1, min(28, _median_int(expected_days) if expected_days else 15)),
            "avg_amount": avg_amount,
            "run_rate_source": "open_items",
        })

    patterns.sort(key=lambda p: p["avg_amount"], reverse=True)
    return patterns


def _combined_run_rate_pattern_map(
    historical_patterns: List[Dict[str, Any]],
    inferred_patterns: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    patterns: Dict[str, Dict[str, Any]] = {}
    for pattern in historical_patterns:
        copy = dict(pattern)
        copy.setdefault("run_rate_source", "history")
        patterns[copy["name"]] = copy
    for pattern in inferred_patterns:
        if pattern["name"] not in patterns:
            patterns[pattern["name"]] = dict(pattern)
    return patterns


def _project_into_weeks(
    patterns: List[Dict],
    fridays: List[date],
    override_map: Dict[tuple, float],
    entity_type: str,
) -> List[Dict[str, Any]]:
    """
    For each entity, place avg_amount in the correct weekly column using the
    same logic as the Excel formula.
    """
    weeks = len(fridays)

    rows = []
    for pattern in patterns:
        avg_key = (pattern["name"], entity_type, AVG_SENTINEL)
        effective_avg = override_map.get(avg_key, pattern["avg_amount"])

        day_key = (pattern["name"], entity_type, DAY_SENTINEL)
        # Drop the legacy day-28 clamp (it silently moved 29/30/31 pay-days to the 28th).
        entity_day = max(1, min(31, int(override_map.get(day_key, pattern["day_of_month"]))))
        interval_weeks = int(pattern.get("interval_weeks", MONTHLY))
        anchor_date = _as_date(pattern.get("anchor_date"))
        amounts = [0.0] * weeks

        # Place the recurring amount on the entity's cadence (interval-week model): a bi-weekly
        # entity hits ~2–3 times/month, a weekly one every week, a quarterly one once a quarter.
        for i, amt in project_interval_amounts(
            fridays, interval_weeks=interval_weeks, entity_day=entity_day, monthly_avg=effective_avg,
            anchor_date=anchor_date,
        ).items():
            amounts[i] = amt

        # Cell-level overrides win over the projected cadence.
        for i in range(weeks):
            cell_key = (pattern["name"], entity_type, fridays[i])
            if cell_key in override_map:
                amounts[i] = override_map[cell_key]

        order_key = (pattern["name"], entity_type, ORDER_SENTINEL)
        sort_order = override_map.get(order_key, 999.0)

        rows.append({
            "name": pattern["name"],
            "day_of_month": max(1, min(31, pattern["day_of_month"])),
            "effective_day": entity_day,
            "interval_weeks": interval_weeks,
            "anchor_date": anchor_date.isoformat() if anchor_date else None,
            "avg_amount": pattern["avg_amount"],
            "effective_avg": round(effective_avg, 2),
            "amounts": [round(a, 2) for a in amounts],
            "sort_order": sort_order,
            "run_rate_source": pattern.get("run_rate_source", "history"),
        })

    return rows


def _as_date(value: Any) -> Optional[date]:
    """A date from a date or an ISO string (snapshot rows round-trip through JSON)."""
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None

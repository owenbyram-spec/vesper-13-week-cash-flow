"""1C: cash-flow recurrence uses an interval-week model, not a day-of-month crossing.

The old model aggregated every entity to a MONTHLY mean and placed it once per calendar
month (the day-of-month "crossing" predicate). A bi-weekly entity was therefore collapsed
to ~1 payment/month with a month's worth of amount — wrong cadence AND wrong per-period
amount. The interval-week model derives the cadence from the observed spacing and places a
per-occurrence amount on that cadence, so a bi-weekly entity correctly hits 3 times in a
5-Friday month while the monthly total is preserved.

Per WS-1 (critical review): the placement tests FIX a known interval; the inference is
tested separately. Fixed examples use hand-computed literals; exhaustive checks use an
independent calendar allocation and restrict it to each visible window.
"""
import calendar
from datetime import date, timedelta

import pytest

from app.cashflow.projection import (
    AVG_SENTINEL,
    BIWEEKLY,
    DAY_SENTINEL,
    MONTHLY,
    WEEKLY,
    WEEKS_PER_MONTH,
    _infer_interval_weeks,
    _project_into_weeks,
    project_interval_amounts,
)

# January 2026 has 5 Fridays: 2, 9, 16, 23, 30.
JAN_2026_FRIDAYS = [date(2026, 1, 2), date(2026, 1, 9), date(2026, 1, 16), date(2026, 1, 23), date(2026, 1, 30)]


# ── inference (WS-1: tested separately from placement) ──────────────────────────

def test_infer_interval_weeks_from_spacing():
    weekly = [date(2026, 1, 2) + timedelta(days=7 * i) for i in range(5)]
    biweekly = [date(2026, 1, 2) + timedelta(days=14 * i) for i in range(5)]
    monthly = [date(2026, 1, 2) + timedelta(days=30 * i) for i in range(5)]
    assert _infer_interval_weeks(weekly) == WEEKLY
    assert _infer_interval_weeks(biweekly) == BIWEEKLY
    assert _infer_interval_weeks(monthly) == MONTHLY


def test_infer_defaults_to_monthly_on_sparse_history():
    # <2 observations: never fabricate a frequent cadence from one data point.
    assert _infer_interval_weeks([date(2026, 1, 2)]) == MONTHLY
    assert _infer_interval_weeks([]) == MONTHLY


# ── placement (fixed interval) ──────────────────────────────────────────────────

def test_biweekly_places_three_periods_in_a_five_friday_month():
    placed = project_interval_amounts(JAN_2026_FRIDAYS, interval_weeks=BIWEEKLY, entity_day=2, monthly_avg=1000.0)
    # anchor = week 0 (Fri Jan 2 crosses day 2), then every 2 weeks: indices 0, 2, 4 → 3 periods.
    assert sorted(placed) == [0, 2, 4]
    # per-occurrence = 1000 * 2 / (52/12) = 461.538… → 461.54 (hand-computed).
    assert placed[0] == pytest.approx(461.54)
    assert WEEKS_PER_MONTH == pytest.approx(52 / 12)


def test_weekly_places_every_week():
    placed = project_interval_amounts(JAN_2026_FRIDAYS, interval_weeks=WEEKLY, entity_day=2, monthly_avg=1000.0)
    assert sorted(placed) == [0, 1, 2, 3, 4]
    # per-occurrence = 1000 / (52/12) = 230.769… → 230.77 (hand-computed).
    assert placed[0] == pytest.approx(230.77)


def test_monthly_keeps_legacy_single_calendar_placement():
    # MONTHLY keeps the unchanged calendar-crossing: one placement of the FULL monthly amount.
    placed = project_interval_amounts(JAN_2026_FRIDAYS, interval_weeks=MONTHLY, entity_day=15, monthly_avg=1000.0)
    # Day-15 crosses in the Jan 9→16 week → index 1 only.
    assert sorted(placed) == [1]
    assert placed[1] == pytest.approx(1000.0)


# ── integration through the row builder ─────────────────────────────────────────

def test_project_into_weeks_biweekly_row_has_three_nonzero_periods():
    pattern = {"name": "Payroll", "day_of_month": 2, "interval_weeks": BIWEEKLY, "avg_amount": 1000.0}
    rows = _project_into_weeks([pattern], JAN_2026_FRIDAYS, {}, "vendor")
    assert len(rows) == 1
    amounts = rows[0]["amounts"]
    nonzero = [a for a in amounts if a > 0]
    assert len(nonzero) == 3                          # 3 pay periods in the 5-Friday month
    assert all(a == pytest.approx(461.54) for a in nonzero)
    assert rows[0]["interval_weeks"] == BIWEEKLY


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027])
def test_monthly_covers_every_calendar_day_without_moving_existing_buckets(year):
    # The calendar oracle chooses the latest Friday on/before the payment day, or the
    # first calendar Friday when the payment precedes every Friday. It also covers
    # short months: day 29/30/31 retains the month's final Friday, including leap years.
    for month in range(1, 13):
        fridays = [
            date(year, month, day)
            for day in range(1, calendar.monthrange(year, month)[1] + 1)
            if date(year, month, day).weekday() == 4
        ]
        for entity_day in range(1, 32):
            preceding = [i for i, friday in enumerate(fridays) if friday.day <= entity_day]
            expected_index = preceding[-1] if preceding else 0
            placed = project_interval_amounts(
                fridays, interval_weeks=MONTHLY, entity_day=entity_day, monthly_avg=1000.0,
            )
            assert placed == {expected_index: 1000.0}, (year, month, entity_day)


def test_monthly_early_days_have_exact_first_friday_buckets_across_year_boundary():
    fridays = [date(2026, 12, 4) + timedelta(weeks=i) for i in range(14)]
    # December's first Friday is the 4th, January's is the 1st, February's is the 5th,
    # and March's is the 5th. Day 1 stays in its own month and is never duplicated.
    placed = project_interval_amounts(
        fridays, interval_weeks=MONTHLY, entity_day=1, monthly_avg=8333.33,
    )
    assert placed == {0: 8333.33, 4: 8333.33, 9: 8333.33, 13: 8333.33}


def test_monthly_partial_windows_only_restrict_calendar_buckets_without_catchup():
    fridays = [date(2023, 12, 1) + timedelta(weeks=i) for i in range(18)]
    by_month = {}
    for i, friday in enumerate(fridays):
        by_month.setdefault((friday.year, friday.month), []).append(i)

    for entity_day in range(1, 32):
        expected = {}
        for indices in by_month.values():
            preceding = [i for i in indices if fridays[i].day <= entity_day]
            expected[preceding[-1] if preceding else indices[0]] = 1000.0

        for start_idx in range(len(fridays) + 1):
            for end_idx in range(start_idx, len(fridays) + 1):
                restricted = {i: amount for i, amount in expected.items() if start_idx <= i < end_idx}
                actual = project_interval_amounts(
                    fridays, interval_weeks=MONTHLY, entity_day=entity_day,
                    monthly_avg=1000.0, start_idx=start_idx, end_idx=end_idx,
                )
                assert actual == restricted, (entity_day, start_idx, end_idx)

                # A caller can trim the grid itself instead of providing index bounds.
                # Its first visible Friday must not become an invented payment anchor.
                sliced = project_interval_amounts(
                    fridays[start_idx:end_idx], interval_weeks=MONTHLY,
                    entity_day=entity_day, monthly_avg=1000.0,
                )
                assert sliced == {i - start_idx: amount for i, amount in restricted.items()}


@pytest.mark.parametrize(
    ("interval_weeks", "expected"),
    [(WEEKLY, dict.fromkeys(range(8), 230.77)), (BIWEEKLY, {0: 461.54, 2: 461.54, 4: 461.54, 6: 461.54})],
)
def test_early_day_monthly_repair_preserves_submonthly_fallback_anchor(interval_weeks, expected):
    fridays = [date(2026, 9, 25) + timedelta(weeks=i) for i in range(8)]
    # No Friday is day 1, so the existing submonthly contract starts at index 0.
    # Reusing the new monthly first-Friday rule for anchors would move it to index 1.
    assert project_interval_amounts(
        fridays, interval_weeks=interval_weeks, entity_day=1, monthly_avg=1000.0,
    ) == expected


def test_monthly_early_day_respects_day_average_and_cell_overrides():
    pattern = {"name": "Rent", "day_of_month": 15, "avg_amount": 1000.0}
    overrides = {
        ("Rent", "vendor", DAY_SENTINEL): 1,
        ("Rent", "vendor", AVG_SENTINEL): 2500.0,
        ("Rent", "vendor", date(2026, 1, 2)): 0.0,
        ("Rent", "vendor", date(2026, 1, 16)): 125.0,
    }
    # February 2026 starts on Sunday; its day-1 recurrence belongs to Friday Feb 6.
    fridays = JAN_2026_FRIDAYS + [date(2026, 2, 6)]
    row = _project_into_weeks([pattern], fridays, overrides, "vendor")[0]
    assert row["effective_day"] == 1
    assert row["effective_avg"] == 2500.0
    assert row["amounts"] == [0.0, 0.0, 125.0, 0.0, 0.0, 2500.0]

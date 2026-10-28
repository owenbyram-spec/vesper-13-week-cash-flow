"""Provider-neutral 13-week projection, liquidity, scenario, and variance."""

from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from decimal import Decimal

import pytest

from vesper_cashflow.domain.money import Money
from vesper_cashflow.domain.forecast import (
    ActualCashFlow,
    CashFlowCadence,
    CashFlowDirection,
    CashFlowError,
    CashFlowForecastInput,
    CashFlowItem,
    CashFlowProjection,
    ForecastWeek,
    LiquidityObservation,
    LiquidityObservationBasis,
    LiquiditySummary,
    RecurringCashFlow,
    RunwayStatus,
    ScenarioParameters,
    ScenarioProjection,
    VarianceAssessment,
    VarianceClassification,
    WeeklyCashFlow,
    WeeklyVariance,
    apply_scenario,
    build_13_week_projection,
    build_forecast_weeks,
    build_variance_report,
    classify_variance,
    compute_liquidity_summary,
)


AS_OF = date(2026, 1, 4)


def _money(value: str, currency: str = "USD") -> Money:
    return Money(Decimal(value), currency)


def _weeks(as_of: date = AS_OF) -> tuple[ForecastWeek, ...]:
    first_start = as_of + timedelta(days=1)
    return tuple(
        ForecastWeek(
            start=first_start + timedelta(weeks=index),
            end=first_start + timedelta(weeks=index, days=6),
        )
        for index in range(13)
    )


def _item(
    item_id: str,
    direction: CashFlowDirection,
    expected_date: date,
    amount: str,
    currency: str = "USD",
) -> CashFlowItem:
    return CashFlowItem(
        item_id=item_id,
        label=item_id,
        direction=direction,
        expected_date=expected_date,
        amount=_money(amount, currency),
    )


def _projection_from_weekly_totals(
    cash_in: tuple[str, ...],
    cash_out: tuple[str, ...],
    *,
    opening_cash: str = "0.00",
) -> CashFlowProjection:
    weeks = _weeks()
    items = []
    for index in range(13):
        if Decimal(cash_in[index]) > Decimal(0):
            items.append(
                _item(
                    "in-%s" % index,
                    CashFlowDirection.CASH_IN,
                    weeks[index].start,
                    cash_in[index],
                )
            )
        if Decimal(cash_out[index]) > Decimal(0):
            items.append(
                _item(
                    "out-%s" % index,
                    CashFlowDirection.CASH_OUT,
                    weeks[index].start,
                    cash_out[index],
                )
            )
    return build_13_week_projection(
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=weeks,
            opening_cash=_money(opening_cash),
            items=tuple(items),
        )
    )


def test_boundary_helper_is_explicit_and_deterministic() -> None:
    assert build_forecast_weeks(AS_OF) == _weeks()
    assert build_forecast_weeks(AS_OF)[-1].end == date(2026, 4, 5)
    with pytest.raises(TypeError, match="as_of must be a date"):
        build_forecast_weeks("2026-01-04")


def test_projection_places_dated_items_and_surfaces_exclusions() -> None:
    weeks = _weeks()
    projection = build_13_week_projection(
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=weeks,
            opening_cash=_money("10000.00"),
            items=(
                _item(
                    "past-due-receipt",
                    CashFlowDirection.CASH_IN,
                    AS_OF - timedelta(days=5),
                    "200.00",
                ),
                _item(
                    "current-receipt",
                    CashFlowDirection.CASH_IN,
                    weeks[0].start + timedelta(days=2),
                    "1000.00",
                ),
                _item(
                    "current-payment",
                    CashFlowDirection.CASH_OUT,
                    weeks[0].end,
                    "300.00",
                ),
                _item(
                    "late-receipt",
                    CashFlowDirection.CASH_IN,
                    weeks[-1].end + timedelta(days=1),
                    "700.00",
                ),
                _item(
                    "late-payment",
                    CashFlowDirection.CASH_OUT,
                    weeks[-1].end + timedelta(days=30),
                    "50.00",
                ),
            ),
        )
    )

    first = projection.weekly[0]
    assert first.cash_in == _money("1200.00")
    assert first.cash_out == _money("300.00")
    assert first.ending_cash == _money("10900.00")
    assert projection.audit.past_due_item_ids == ("past-due-receipt",)
    assert projection.audit.beyond_horizon_item_ids == (
        "late-receipt",
        "late-payment",
    )
    assert projection.audit.beyond_horizon_cash_in == _money("700.00")
    assert projection.audit.beyond_horizon_cash_out == _money("50.00")
    assert projection.ending_cash == _money("10900.00")


def test_weekend_item_uses_the_supplied_boundary_without_provider_logic() -> None:
    weeks = _weeks()
    saturday = weeks[2].end - timedelta(days=1)
    projection = build_13_week_projection(
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=weeks,
            opening_cash=_money("100.00"),
            items=(
                _item(
                    "weekend",
                    CashFlowDirection.CASH_IN,
                    saturday,
                    "25.00",
                ),
            ),
        )
    )

    assert projection.weekly[2].cash_in == _money("25.00")
    assert sum(row.cash_in.amount for row in projection.weekly) == Decimal("25.00")


def test_biweekly_run_rate_preserves_monthly_economics() -> None:
    weeks = _weeks()
    projection = build_13_week_projection(
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=weeks,
            opening_cash=_money("10000.00"),
            recurrences=(
                RecurringCashFlow(
                    recurrence_id="payroll",
                    label="Payroll",
                    direction=CashFlowDirection.CASH_OUT,
                    monthly_average=_money("1000.00"),
                    cadence=CashFlowCadence.BIWEEKLY,
                    anchor_date=weeks[0].start,
                ),
            ),
        )
    )

    nonzero = [
        (index, row.cash_out)
        for index, row in enumerate(projection.weekly)
        if row.cash_out.amount > Decimal(0)
    ]
    assert [index for index, _ in nonzero] == [0, 2, 4, 6, 8, 10, 12]
    assert all(amount == _money("461.54") for _, amount in nonzero)


def test_monthly_run_rate_clamps_anchor_day_to_short_month_ends() -> None:
    weeks = _weeks()
    projection = build_13_week_projection(
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=weeks,
            opening_cash=_money("0.00"),
            recurrences=(
                RecurringCashFlow(
                    recurrence_id="month-end",
                    label="Month-end settlement",
                    direction=CashFlowDirection.CASH_IN,
                    monthly_average=_money("900.00"),
                    cadence=CashFlowCadence.MONTHLY,
                    anchor_date=date(2026, 1, 31),
                ),
            ),
        )
    )

    nonzero_indices = [
        index for index, row in enumerate(projection.weekly) if row.cash_in.amount > Decimal(0)
    ]
    assert nonzero_indices == [3, 7, 12]
    assert all(projection.weekly[index].cash_in == _money("900.00") for index in nonzero_indices)


def test_input_is_immutable_and_requires_exact_consecutive_calendar() -> None:
    forecast_input = CashFlowForecastInput(
        as_of=AS_OF,
        weeks=_weeks(),
        opening_cash=_money("1.00"),
    )
    with pytest.raises(FrozenInstanceError):
        forecast_input.as_of = date(2026, 1, 5)

    with pytest.raises(CashFlowError, match="exactly 13"):
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=_weeks()[:-1],
            opening_cash=_money("1.00"),
        )

    broken = list(_weeks())
    broken[1] = ForecastWeek(
        start=broken[1].start + timedelta(days=1),
        end=broken[1].end + timedelta(days=1),
    )
    with pytest.raises(CashFlowError, match="consecutive"):
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=tuple(broken),
            opening_cash=_money("1.00"),
        )


def test_direct_weekly_result_refuses_mixed_currency_and_an_untied_roll() -> None:
    week = _weeks()[0]
    with pytest.raises(CashFlowError, match="forecast currency USD"):
        WeeklyCashFlow(
            week=week,
            cash_in=_money("10.00", "EUR"),
            cash_out=_money("0.00"),
            beginning_cash=_money("100.00"),
            ending_cash=_money("110.00"),
        )
    with pytest.raises(CashFlowError, match="does not tie"):
        WeeklyCashFlow(
            week=week,
            cash_in=_money("10.00"),
            cash_out=_money("0.00"),
            beginning_cash=_money("100.00"),
            ending_cash=_money("109.99"),
        )


def test_projection_refuses_mixed_currency_and_duplicate_identity() -> None:
    weeks = _weeks()
    with pytest.raises(CashFlowError, match="forecast currency USD"):
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=weeks,
            opening_cash=_money("100.00"),
            items=(
                _item(
                    "eur-receipt",
                    CashFlowDirection.CASH_IN,
                    weeks[0].start,
                    "10.00",
                    "EUR",
                ),
            ),
        )

    repeated = _item(
        "same-id",
        CashFlowDirection.CASH_IN,
        weeks[0].start,
        "10.00",
    )
    with pytest.raises(CashFlowError, match="identities must be unique"):
        CashFlowForecastInput(
            as_of=AS_OF,
            weeks=weeks,
            opening_cash=_money("100.00"),
            items=(repeated, repeated),
        )


def test_liquidity_uses_ending_grid_and_counts_full_weeks() -> None:
    projection = _projection_from_weekly_totals(
        ("0.00",) * 13,
        ("10.00",) * 13,
        opening_cash="50.00",
    )
    summary = compute_liquidity_summary(
        projection,
        minimum_cash=_money("25.00"),
    )

    assert summary.runway_status is RunwayStatus.BREACHES_WITHIN_HORIZON
    assert summary.runway_weeks == 2
    assert summary.first_breach_week_ending == _weeks()[2].end
    assert summary.minimum_cash_week_ending == _weeks()[-1].end
    assert summary.minimum_cash_balance == _money("-80.00")


def test_liquidity_reports_no_breach_without_extrapolating() -> None:
    projection = _projection_from_weekly_totals(
        ("10.00",) * 13,
        ("0.00",) * 13,
        opening_cash="50.00",
    )
    summary = compute_liquidity_summary(projection)

    assert summary.runway_status is RunwayStatus.NO_BREACH_WITHIN_HORIZON
    assert summary.runway_weeks is None
    assert summary.first_breach_week_ending is None
    assert summary.minimum_cash_balance == _money("50.00")
    assert summary.minimum_cash_date == AS_OF
    assert summary.minimum_cash_week_ending is None
    assert summary.minimum_cash_observation.basis is LiquidityObservationBasis.AS_OF_OPENING


def test_liquidity_opening_breach_is_not_masked_by_week_one_recovery() -> None:
    projection = _projection_from_weekly_totals(
        ("100.00",) + ("0.00",) * 12,
        ("0.00",) * 13,
        opening_cash="-10.00",
    )
    summary = compute_liquidity_summary(projection)

    assert projection.weekly[0].ending_cash == _money("90.00")
    assert summary.runway_status is RunwayStatus.BREACHED_AT_AS_OF
    assert summary.runway_weeks == 0
    assert summary.first_breach is not None
    assert summary.first_breach.observed_on == AS_OF
    assert summary.first_breach.basis is LiquidityObservationBasis.AS_OF_OPENING
    assert summary.first_breach.balance == _money("-10.00")
    assert summary.first_breach_date == AS_OF
    assert summary.first_breach_week_ending is None
    assert summary.minimum_cash_observation == summary.first_breach


def test_liquidity_refuses_a_floor_in_another_currency() -> None:
    projection = _projection_from_weekly_totals(
        ("0.00",) * 13,
        ("0.00",) * 13,
    )
    with pytest.raises(CashFlowError, match="forecast currency USD"):
        compute_liquidity_summary(
            projection,
            minimum_cash=_money("0.00", "EUR"),
        )


def test_direct_liquidity_result_refuses_an_inconsistent_status() -> None:
    with pytest.raises(CashFlowError, match="cannot report runway"):
        LiquiditySummary(
            as_of=AS_OF,
            opening_cash=_money("10.00"),
            minimum_cash=_money("0.00"),
            runway_status=RunwayStatus.NO_BREACH_WITHIN_HORIZON,
            runway_weeks=2,
            first_breach=None,
            minimum_cash_observation=LiquidityObservation(
                observed_on=AS_OF,
                basis=LiquidityObservationBasis.AS_OF_OPENING,
                balance=_money("10.00"),
            ),
        )

    with pytest.raises(CashFlowError, match="below the floor"):
        LiquiditySummary(
            as_of=AS_OF,
            opening_cash=_money("30.00"),
            minimum_cash=_money("25.00"),
            runway_status=RunwayStatus.NO_BREACH_WITHIN_HORIZON,
            runway_weeks=None,
            first_breach=None,
            minimum_cash_observation=LiquidityObservation(
                observed_on=_weeks()[-1].end,
                basis=LiquidityObservationBasis.WEEK_ENDING,
                balance=_money("24.99"),
            ),
        )


def test_scenario_preserves_anchor_and_reports_delayed_horizon_spill() -> None:
    projection = _projection_from_weekly_totals(
        ("100.00",) * 13,
        ("80.00",) * 13,
        opening_cash="500.00",
    )
    scenario = apply_scenario(
        projection,
        ScenarioParameters(
            name="downside",
            label="Receipts delayed and costs inflated",
            cash_in_haircut_rate=Decimal("0.10"),
            cash_in_delay_weeks=1,
            cash_out_inflation_rate=Decimal("0.10"),
        ),
        minimum_cash=_money("450.00"),
    )

    assert projection.weekly[0].ending_cash == _money("520.00")
    assert scenario.projection.opening_cash == _money("500.00")
    assert scenario.projection.weekly[0].cash_in == _money("0.00")
    assert scenario.projection.weekly[0].cash_out == _money("88.00")
    assert scenario.projection.weekly[0].ending_cash == _money("412.00")
    assert scenario.projection.weekly[1].cash_in == _money("90.00")
    assert scenario.projection.weekly[1].ending_cash == _money("414.00")
    assert scenario.slipped_beyond_horizon == _money("100.00")
    assert scenario.liquidity.runway_weeks == 0


def test_direct_scenario_result_refuses_liquidity_from_another_grid() -> None:
    projection = _projection_from_weekly_totals(
        ("10.00",) * 13,
        ("0.00",) * 13,
        opening_cash="100.00",
    )
    fabricated_liquidity = LiquiditySummary(
        as_of=AS_OF,
        opening_cash=_money("100.00"),
        minimum_cash=_money("0.00"),
        runway_status=RunwayStatus.NO_BREACH_WITHIN_HORIZON,
        runway_weeks=None,
        first_breach=None,
        minimum_cash_observation=LiquidityObservation(
            observed_on=_weeks()[0].end,
            basis=LiquidityObservationBasis.WEEK_ENDING,
            balance=_money("90.00"),
        ),
    )
    with pytest.raises(CashFlowError, match="does not match"):
        ScenarioProjection(
            parameters=ScenarioParameters(name="base", label="Base"),
            projection=projection,
            slipped_beyond_horizon=_money("0.00"),
            liquidity=fabricated_liquidity,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cash_in_haircut_rate": Decimal("1.01")},
        {"cash_in_haircut_rate": Decimal("-0.01")},
        {"cash_out_inflation_rate": Decimal("-0.01")},
        {"cash_in_delay_weeks": -1},
    ],
)
def test_scenario_parameters_refuse_invalid_assumptions(kwargs: dict) -> None:
    with pytest.raises(CashFlowError):
        ScenarioParameters(name="bad", label="Bad case", **kwargs)


def test_scenario_parameters_refuse_float_rates() -> None:
    with pytest.raises(TypeError, match="must be Decimal"):
        ScenarioParameters(
            name="float",
            label="Float assumption",
            cash_in_haircut_rate=0.1,
        )


def test_variance_report_links_timing_and_persistent_patterns() -> None:
    projection = _projection_from_weekly_totals(
        ("100.00", "100.00", "100.00") + ("0.00",) * 10,
        ("50.00", "50.00", "50.00") + ("0.00",) * 10,
    )
    endings = _weeks()
    report = build_variance_report(
        projection,
        (
            ActualCashFlow(
                week_ending=endings[0].end,
                cash_in=_money("0.00"),
                cash_out=_money("40.00"),
            ),
            ActualCashFlow(
                week_ending=endings[1].end,
                cash_in=_money("200.00"),
                cash_out=_money("40.00"),
            ),
            ActualCashFlow(
                week_ending=endings[2].end,
                cash_in=_money("80.00"),
                cash_out=_money("40.00"),
            ),
        ),
    )

    assert report.rows[0].cash_in_variance == _money("-100.00")
    assert report.rows[0].cash_in_assessment.classification is VarianceClassification.TIMING
    assert report.rows[0].cash_in_assessment.timing_offset_amount == _money("100.00")
    assert report.rows[2].cash_out_assessment.classification is VarianceClassification.SYSTEMATIC
    assert report.accuracy.cash_in.mape == Decimal("0.3750")
    assert report.accuracy.cash_in.bias == Decimal("0.0714")
    assert report.accuracy.cash_in.mad == _money("73.33")
    assert report.accuracy.cash_in.weeks_excluded_zero_actual == 1
    assert report.accuracy.cash_out.mape == Decimal("0.2500")


def test_accuracy_excludes_partial_weeks_and_counts_zero_actuals() -> None:
    projection = _projection_from_weekly_totals(
        ("100.00", "100.00", "100.00") + ("0.00",) * 10,
        ("50.00", "50.00", "50.00") + ("0.00",) * 10,
    )
    weeks = _weeks()
    report = build_variance_report(
        projection,
        (
            ActualCashFlow(
                week_ending=weeks[0].end,
                cash_in=_money("80.00"),
                cash_out=_money("50.00"),
            ),
            ActualCashFlow(
                week_ending=weeks[1].end,
                cash_in=_money("1.00"),
                cash_out=_money("1.00"),
                is_partial=True,
            ),
            ActualCashFlow(
                week_ending=weeks[2].end,
                cash_in=_money("100.00"),
                cash_out=_money("50.00"),
            ),
        ),
    )

    assert report.accuracy.weeks_considered == 3
    assert report.accuracy.weeks_excluded_partial == 1
    assert report.accuracy.excluded_partial_week_endings == (weeks[1].end,)
    assert report.accuracy.cash_in.weeks_scored == 2
    assert report.accuracy.cash_in.mape == Decimal("0.1250")
    assert report.accuracy.cash_in.bias == Decimal("0.1111")
    assert report.accuracy.cash_in.mad == _money("10.00")
    partial_row = report.rows[1]
    assert partial_row.is_partial is True
    assert (
        partial_row.cash_in_assessment.classification
        is VarianceClassification.UNCLASSIFIED_INCOMPLETE_ACTUALS
    )
    assert (
        partial_row.cash_out_assessment.classification
        is VarianceClassification.UNCLASSIFIED_INCOMPLETE_ACTUALS
    )
    assert partial_row.cash_in_assessment.is_classified is False
    assert partial_row.cash_out_assessment.is_classified is False

    zero_report = build_variance_report(
        projection,
        (
            ActualCashFlow(
                week_ending=weeks[0].end,
                cash_in=_money("0.00"),
                cash_out=_money("50.00"),
            ),
        ),
    )
    assert zero_report.accuracy.cash_in.mape is None
    assert zero_report.accuracy.cash_in.mad == _money("100.00")
    assert zero_report.accuracy.cash_in.weeks_excluded_zero_actual == 1


def test_variance_refuses_unknown_weeks_duplicates_and_mixed_currency() -> None:
    projection = _projection_from_weekly_totals(
        ("0.00",) * 13,
        ("0.00",) * 13,
    )
    weeks = _weeks()
    actual = ActualCashFlow(
        week_ending=weeks[0].end,
        cash_in=_money("1.00"),
        cash_out=_money("0.00"),
    )
    with pytest.raises(CashFlowError, match="unique"):
        build_variance_report(projection, (actual, actual))

    with pytest.raises(CashFlowError, match="outside"):
        build_variance_report(
            projection,
            (
                ActualCashFlow(
                    week_ending=weeks[-1].end + timedelta(days=7),
                    cash_in=_money("1.00"),
                    cash_out=_money("0.00"),
                ),
            ),
        )

    with pytest.raises(CashFlowError, match="forecast currency USD"):
        build_variance_report(
            projection,
            (
                ActualCashFlow(
                    week_ending=weeks[0].end,
                    cash_in=_money("1.00", "EUR"),
                    cash_out=_money("0.00", "EUR"),
                ),
            ),
        )


def test_classification_refuses_mixed_currency_history() -> None:
    with pytest.raises(CashFlowError, match="forecast currency USD"):
        classify_variance(
            _money("-10.00"),
            prior_variances=(
                _money("-2.00"),
                _money("-3.00", "EUR"),
            ),
        )


def test_direct_variance_results_refuse_inconsistent_values() -> None:
    zero_assessment = VarianceAssessment(
        classification=VarianceClassification.ONE_OFF,
        timing_offset_amount=_money("0.00"),
    )
    with pytest.raises(CashFlowError, match="does not tie"):
        WeeklyVariance(
            week_ending=_weeks()[0].end,
            forecast_cash_in=_money("100.00"),
            actual_cash_in=_money("80.00"),
            cash_in_variance=_money("-19.99"),
            cash_in_assessment=zero_assessment,
            forecast_cash_out=_money("50.00"),
            actual_cash_out=_money("50.00"),
            cash_out_variance=_money("0.00"),
            cash_out_assessment=zero_assessment,
            is_partial=False,
        )

    with pytest.raises(CashFlowError, match="only a timing variance"):
        VarianceAssessment(
            classification=VarianceClassification.ONE_OFF,
            timing_offset_amount=_money("1.00"),
        )

    with pytest.raises(CashFlowError, match="partial week assessments"):
        WeeklyVariance(
            week_ending=_weeks()[0].end,
            forecast_cash_in=_money("100.00"),
            actual_cash_in=_money("80.00"),
            cash_in_variance=_money("-20.00"),
            cash_in_assessment=zero_assessment,
            forecast_cash_out=_money("50.00"),
            actual_cash_out=_money("50.00"),
            cash_out_variance=_money("0.00"),
            cash_out_assessment=zero_assessment,
            is_partial=True,
        )

    incomplete = VarianceAssessment.incomplete_actuals("USD")
    with pytest.raises(CashFlowError, match="complete week assessments"):
        WeeklyVariance(
            week_ending=_weeks()[0].end,
            forecast_cash_in=_money("100.00"),
            actual_cash_in=_money("80.00"),
            cash_in_variance=_money("-20.00"),
            cash_in_assessment=incomplete,
            forecast_cash_out=_money("50.00"),
            actual_cash_out=_money("50.00"),
            cash_out_variance=_money("0.00"),
            cash_out_assessment=incomplete,
            is_partial=False,
        )

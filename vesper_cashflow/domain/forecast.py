"""Pure, provider-neutral 13-week cash-flow modeling.

Accounting connectors stop at the canonical inputs in this module.  Projection,
liquidity, scenario, and variance behavior is deterministic and has no network,
ORM, persistence, or provider-specific fields.  Every forecast supplies its own
``as_of`` date and exact week boundaries; this module never consults the clock.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional, Tuple

from .money import Money


FORECAST_WEEKS = 13
_RATIO_QUANTUM = Decimal("0.0001")
_WEEKS_PER_YEAR = Decimal(52)
_MONTHS_PER_YEAR = Decimal(12)
_SYSTEMATIC_WEEKS = 3


class CashFlowError(ValueError):
    """The supplied inputs cannot produce a reliable cash-flow result."""


class CashFlowDirection(str, Enum):
    """The positive lane in which a cash-flow amount belongs."""

    CASH_IN = "cash_in"
    CASH_OUT = "cash_out"


class CashFlowCadence(str, Enum):
    """Supported recurring forecast cadences."""

    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"

    @property
    def interval_weeks(self) -> Optional[int]:
        if self is CashFlowCadence.WEEKLY:
            return 1
        if self is CashFlowCadence.BIWEEKLY:
            return 2
        return None


class RunwayStatus(str, Enum):
    """Whether the cash roll crosses the selected cash floor."""

    BREACHED_AT_AS_OF = "breached_at_as_of"
    BREACHES_WITHIN_HORIZON = "breaches_within_horizon"
    NO_BREACH_WITHIN_HORIZON = "no_breach_within_horizon"


class LiquidityObservationBasis(str, Enum):
    """The point in the cash roll represented by a liquidity observation."""

    AS_OF_OPENING = "as_of_opening"
    WEEK_ENDING = "week_ending"


class VarianceClassification(str, Enum):
    """Review taxonomy for one forecast-to-actual variance."""

    TIMING = "timing"
    SYSTEMATIC = "systematic"
    ONE_OFF = "one_off"
    UNCLASSIFIED_INCOMPLETE_ACTUALS = "unclassified_incomplete_actuals"


def _require_date(value: object, field_name: str) -> None:
    if type(value) is not date:
        raise TypeError("%s must be a date" % field_name)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError("%s must be a string" % field_name)
    if not value or value != value.strip():
        raise CashFlowError("%s must be non-empty with no surrounding whitespace" % field_name)
    if len(value) > 255:
        raise CashFlowError("%s must be at most 255 characters" % field_name)


def _require_money(value: object, field_name: str) -> Money:
    if not isinstance(value, Money):
        raise TypeError("%s must be Money" % field_name)
    return value


def _require_currency(value: Money, currency: str, field_name: str) -> None:
    if value.currency != currency:
        raise CashFlowError("%s must use the forecast currency %s" % (field_name, currency))


def _require_rate(
    value: object,
    field_name: str,
    *,
    maximum: Optional[Decimal] = None,
) -> None:
    if not isinstance(value, Decimal):
        raise TypeError("%s must be Decimal" % field_name)
    if not value.is_finite():
        raise CashFlowError("%s must be finite" % field_name)
    if value < Decimal(0):
        raise CashFlowError("%s must not be negative" % field_name)
    if maximum is not None and value > maximum:
        raise CashFlowError("%s must not exceed %s" % (field_name, maximum))


def _quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-Money.MINOR_UNITS.get(currency, 2))


def _rounded_money(amount: Decimal, currency: str) -> Money:
    rounded = amount.quantize(_quantum(currency), rounding=ROUND_HALF_UP)
    return Money(rounded, currency)


@dataclass(frozen=True)
class ForecastWeek:
    """One explicit, inclusive seven-day forecast bucket."""

    start: date
    end: date

    def __post_init__(self) -> None:
        _require_date(self.start, "ForecastWeek.start")
        _require_date(self.end, "ForecastWeek.end")
        if self.end != self.start + timedelta(days=6):
            raise CashFlowError("each ForecastWeek must contain exactly seven inclusive days")


def build_forecast_weeks(as_of: date) -> Tuple[ForecastWeek, ...]:
    """Build 13 explicit seven-day buckets beginning after ``as_of``.

    The caller still owns the anchor date.  This convenience only removes
    repetitive date arithmetic; it never reads the current date.
    """

    _require_date(as_of, "as_of")
    first_start = as_of + timedelta(days=1)
    return tuple(
        ForecastWeek(
            start=first_start + timedelta(weeks=index),
            end=first_start + timedelta(weeks=index, days=6),
        )
        for index in range(FORECAST_WEEKS)
    )


@dataclass(frozen=True)
class CashFlowItem:
    """One dated, positive canonical forecast receipt or disbursement."""

    item_id: str
    label: str
    direction: CashFlowDirection
    expected_date: date
    amount: Money

    def __post_init__(self) -> None:
        _require_text(self.item_id, "item_id")
        _require_text(self.label, "label")
        if not isinstance(self.direction, CashFlowDirection):
            raise TypeError("direction must be CashFlowDirection")
        _require_date(self.expected_date, "expected_date")
        amount = _require_money(self.amount, "amount")
        if amount.amount <= Decimal(0):
            raise CashFlowError("cash-flow item amount must be positive")


@dataclass(frozen=True)
class RecurringCashFlow:
    """A recurring run rate anchored to a known occurrence date.

    Weekly and biweekly amounts are derived from ``monthly_average`` using
    ``52 / 12`` weeks per month.  Monthly recurrence follows the anchor's
    calendar day and clamps that day to shorter month ends.
    """

    recurrence_id: str
    label: str
    direction: CashFlowDirection
    monthly_average: Money
    cadence: CashFlowCadence
    anchor_date: date

    def __post_init__(self) -> None:
        _require_text(self.recurrence_id, "recurrence_id")
        _require_text(self.label, "label")
        if not isinstance(self.direction, CashFlowDirection):
            raise TypeError("direction must be CashFlowDirection")
        monthly_average = _require_money(
            self.monthly_average,
            "monthly_average",
        )
        if monthly_average.amount <= Decimal(0):
            raise CashFlowError("monthly_average must be positive")
        if not isinstance(self.cadence, CashFlowCadence):
            raise TypeError("cadence must be CashFlowCadence")
        _require_date(self.anchor_date, "anchor_date")


@dataclass(frozen=True)
class CashFlowForecastInput:
    """Complete immutable input for a new 13-week projection."""

    as_of: date
    weeks: Tuple[ForecastWeek, ...]
    opening_cash: Money
    items: Tuple[CashFlowItem, ...] = ()
    recurrences: Tuple[RecurringCashFlow, ...] = ()

    def __post_init__(self) -> None:
        _require_date(self.as_of, "as_of")
        opening_cash = _require_money(self.opening_cash, "opening_cash")
        if not isinstance(self.weeks, tuple):
            raise TypeError("weeks must be an immutable tuple")
        if len(self.weeks) != FORECAST_WEEKS:
            raise CashFlowError("a cash-flow forecast must contain exactly 13 weeks")
        if any(not isinstance(week, ForecastWeek) for week in self.weeks):
            raise TypeError("weeks must contain ForecastWeek values")
        if self.weeks[0].start != self.as_of + timedelta(days=1):
            raise CashFlowError("the first forecast week must begin one day after as_of")
        for previous, current in zip(self.weeks, self.weeks[1:]):
            if current.start != previous.end + timedelta(days=1):
                raise CashFlowError("forecast weeks must be consecutive")

        if not isinstance(self.items, tuple):
            raise TypeError("items must be an immutable tuple")
        if any(not isinstance(item, CashFlowItem) for item in self.items):
            raise TypeError("items must contain CashFlowItem values")
        if not isinstance(self.recurrences, tuple):
            raise TypeError("recurrences must be an immutable tuple")
        if any(not isinstance(recurrence, RecurringCashFlow) for recurrence in self.recurrences):
            raise TypeError("recurrences must contain RecurringCashFlow values")

        identities = [item.item_id for item in self.items]
        identities.extend(recurrence.recurrence_id for recurrence in self.recurrences)
        if len(identities) != len(set(identities)):
            raise CashFlowError("cash-flow input identities must be unique")

        currency = opening_cash.currency
        for item in self.items:
            _require_currency(item.amount, currency, "item amount")
        for recurrence in self.recurrences:
            _require_currency(
                recurrence.monthly_average,
                currency,
                "recurrence monthly_average",
            )


@dataclass(frozen=True)
class WeeklyCashFlow:
    """Projected cash roll for one explicit week."""

    week: ForecastWeek
    cash_in: Money
    cash_out: Money
    beginning_cash: Money
    ending_cash: Money

    def __post_init__(self) -> None:
        if not isinstance(self.week, ForecastWeek):
            raise TypeError("week must be ForecastWeek")
        cash_in = _require_money(self.cash_in, "cash_in")
        cash_out = _require_money(self.cash_out, "cash_out")
        beginning = _require_money(self.beginning_cash, "beginning_cash")
        ending = _require_money(self.ending_cash, "ending_cash")
        currency = beginning.currency
        _require_currency(cash_in, currency, "cash_in")
        _require_currency(cash_out, currency, "cash_out")
        _require_currency(ending, currency, "ending_cash")
        if cash_in.amount < Decimal(0) or cash_out.amount < Decimal(0):
            raise CashFlowError("weekly flow amounts must not be negative")
        expected_ending = beginning.amount + cash_in.amount - cash_out.amount
        if ending.amount != expected_ending:
            raise CashFlowError("weekly ending cash does not tie")


@dataclass(frozen=True)
class ProjectionAudit:
    """Amounts intentionally re-timed or excluded from the 13-week grid."""

    past_due_item_ids: Tuple[str, ...]
    beyond_horizon_item_ids: Tuple[str, ...]
    beyond_horizon_cash_in: Money
    beyond_horizon_cash_out: Money

    def __post_init__(self) -> None:
        if not isinstance(self.past_due_item_ids, tuple):
            raise TypeError("past_due_item_ids must be an immutable tuple")
        if not isinstance(self.beyond_horizon_item_ids, tuple):
            raise TypeError("beyond_horizon_item_ids must be an immutable tuple")
        if len(self.past_due_item_ids) != len(set(self.past_due_item_ids)):
            raise CashFlowError("past-due item identities must be unique")
        if len(self.beyond_horizon_item_ids) != len(set(self.beyond_horizon_item_ids)):
            raise CashFlowError("beyond-horizon item identities must be unique")
        if set(self.past_due_item_ids) & set(self.beyond_horizon_item_ids):
            raise CashFlowError("an item cannot be both past due and beyond the horizon")
        cash_in = _require_money(
            self.beyond_horizon_cash_in,
            "beyond_horizon_cash_in",
        )
        cash_out = _require_money(
            self.beyond_horizon_cash_out,
            "beyond_horizon_cash_out",
        )
        _require_currency(
            cash_out,
            cash_in.currency,
            "beyond_horizon_cash_out",
        )
        if cash_in.amount < Decimal(0) or cash_out.amount < Decimal(0):
            raise CashFlowError("beyond-horizon amounts must not be negative")


@dataclass(frozen=True)
class CashFlowProjection:
    """A balanced 13-week cash roll and its exclusion audit."""

    as_of: date
    opening_cash: Money
    weekly: Tuple[WeeklyCashFlow, ...]
    audit: ProjectionAudit

    def __post_init__(self) -> None:
        _require_date(self.as_of, "as_of")
        opening_cash = _require_money(self.opening_cash, "opening_cash")
        if not isinstance(self.weekly, tuple):
            raise TypeError("weekly must be an immutable tuple")
        if len(self.weekly) != FORECAST_WEEKS:
            raise CashFlowError("projection must contain exactly 13 weeks")
        if any(not isinstance(row, WeeklyCashFlow) for row in self.weekly):
            raise TypeError("weekly must contain WeeklyCashFlow values")
        if not isinstance(self.audit, ProjectionAudit):
            raise TypeError("audit must be ProjectionAudit")

        currency = opening_cash.currency
        expected_beginning = opening_cash.amount
        previous_week = None
        for index, row in enumerate(self.weekly):
            for value, field_name in (
                (row.cash_in, "cash_in"),
                (row.cash_out, "cash_out"),
                (row.beginning_cash, "beginning_cash"),
                (row.ending_cash, "ending_cash"),
            ):
                _require_money(value, field_name)
                _require_currency(value, currency, field_name)
            if row.cash_in.amount < Decimal(0) or row.cash_out.amount < Decimal(0):
                raise CashFlowError("weekly flow amounts must not be negative")
            if index == 0 and row.week.start != self.as_of + timedelta(days=1):
                raise CashFlowError("the first projected week must begin one day after as_of")
            if previous_week is not None:
                if row.week.start != previous_week.end + timedelta(days=1):
                    raise CashFlowError("projected weeks must be consecutive")
            if row.beginning_cash.amount != expected_beginning:
                raise CashFlowError("weekly beginning cash does not roll forward")
            expected_ending = row.beginning_cash.amount + row.cash_in.amount - row.cash_out.amount
            if row.ending_cash.amount != expected_ending:
                raise CashFlowError("weekly ending cash does not tie")
            expected_beginning = row.ending_cash.amount
            previous_week = row.week

        _require_currency(
            self.audit.beyond_horizon_cash_in,
            currency,
            "audit beyond_horizon_cash_in",
        )
        _require_currency(
            self.audit.beyond_horizon_cash_out,
            currency,
            "audit beyond_horizon_cash_out",
        )

    @property
    def currency(self) -> str:
        return self.opening_cash.currency

    @property
    def ending_cash(self) -> Money:
        return self.weekly[-1].ending_cash


def _monthly_occurrence(anchor: date, year: int, month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(anchor.day, last_day))


def _next_month(year: int, month: int) -> Tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _recurrence_dates(
    recurrence: RecurringCashFlow,
    window_start: date,
    window_end: date,
) -> Tuple[date, ...]:
    interval_weeks = recurrence.cadence.interval_weeks
    if interval_weeks is not None:
        interval_days = interval_weeks * 7
        occurrence = recurrence.anchor_date
        if occurrence < window_start:
            days_to_start = (window_start - occurrence).days
            intervals = (days_to_start + interval_days - 1) // interval_days
            occurrence += timedelta(days=intervals * interval_days)
        dates = []
        while occurrence <= window_end:
            dates.append(occurrence)
            occurrence += timedelta(days=interval_days)
        return tuple(dates)

    year = recurrence.anchor_date.year
    month = recurrence.anchor_date.month
    occurrence = recurrence.anchor_date
    while occurrence < window_start:
        year, month = _next_month(year, month)
        occurrence = _monthly_occurrence(recurrence.anchor_date, year, month)
    dates = []
    while occurrence <= window_end:
        dates.append(occurrence)
        year, month = _next_month(year, month)
        occurrence = _monthly_occurrence(recurrence.anchor_date, year, month)
    return tuple(dates)


def _recurrence_amount(recurrence: RecurringCashFlow) -> Money:
    interval_weeks = recurrence.cadence.interval_weeks
    if interval_weeks is None:
        return recurrence.monthly_average
    amount = recurrence.monthly_average.amount * Decimal(interval_weeks) * _MONTHS_PER_YEAR / _WEEKS_PER_YEAR
    return _rounded_money(amount, recurrence.monthly_average.currency)


def _week_index(weeks: Tuple[ForecastWeek, ...], expected_date: date) -> int:
    for index, week in enumerate(weeks):
        if week.start <= expected_date <= week.end:
            return index
    raise CashFlowError("expected date is not contained in the forecast calendar")


def _build_projection_from_amounts(
    *,
    as_of: date,
    opening_cash: Money,
    weeks: Tuple[ForecastWeek, ...],
    cash_in: Tuple[Money, ...],
    cash_out: Tuple[Money, ...],
    audit: ProjectionAudit,
) -> CashFlowProjection:
    beginning = opening_cash
    rows = []
    for week, week_in, week_out in zip(weeks, cash_in, cash_out):
        ending = Money(
            beginning.amount + week_in.amount - week_out.amount,
            opening_cash.currency,
        )
        rows.append(
            WeeklyCashFlow(
                week=week,
                cash_in=week_in,
                cash_out=week_out,
                beginning_cash=beginning,
                ending_cash=ending,
            )
        )
        beginning = ending
    return CashFlowProjection(
        as_of=as_of,
        opening_cash=opening_cash,
        weekly=tuple(rows),
        audit=audit,
    )


def build_13_week_projection(
    forecast_input: CashFlowForecastInput,
) -> CashFlowProjection:
    """Project canonical dated items and run rates into an exact 13-week grid.

    Items due on or before ``as_of`` are surfaced as past due and placed in the
    first week.  Items after the final boundary are excluded and totaled in the
    audit instead of being silently pulled into the horizon.
    """

    if not isinstance(forecast_input, CashFlowForecastInput):
        raise TypeError("forecast_input must be CashFlowForecastInput")
    currency = forecast_input.opening_cash.currency
    zero = Decimal(0)
    cash_in_amounts = [zero for _ in range(FORECAST_WEEKS)]
    cash_out_amounts = [zero for _ in range(FORECAST_WEEKS)]
    past_due_ids = []
    beyond_ids = []
    beyond_in = zero
    beyond_out = zero
    final_date = forecast_input.weeks[-1].end

    for item in forecast_input.items:
        if item.expected_date <= forecast_input.as_of:
            week_index = 0
            past_due_ids.append(item.item_id)
        elif item.expected_date > final_date:
            beyond_ids.append(item.item_id)
            if item.direction is CashFlowDirection.CASH_IN:
                beyond_in += item.amount.amount
            else:
                beyond_out += item.amount.amount
            continue
        else:
            week_index = _week_index(
                forecast_input.weeks,
                item.expected_date,
            )

        if item.direction is CashFlowDirection.CASH_IN:
            cash_in_amounts[week_index] += item.amount.amount
        else:
            cash_out_amounts[week_index] += item.amount.amount

    for recurrence in forecast_input.recurrences:
        amount = _recurrence_amount(recurrence)
        occurrences = _recurrence_dates(
            recurrence,
            forecast_input.weeks[0].start,
            final_date,
        )
        for occurrence in occurrences:
            week_index = _week_index(forecast_input.weeks, occurrence)
            if recurrence.direction is CashFlowDirection.CASH_IN:
                cash_in_amounts[week_index] += amount.amount
            else:
                cash_out_amounts[week_index] += amount.amount

    cash_in = tuple(Money(amount, currency) for amount in cash_in_amounts)
    cash_out = tuple(Money(amount, currency) for amount in cash_out_amounts)
    audit = ProjectionAudit(
        past_due_item_ids=tuple(past_due_ids),
        beyond_horizon_item_ids=tuple(beyond_ids),
        beyond_horizon_cash_in=Money(beyond_in, currency),
        beyond_horizon_cash_out=Money(beyond_out, currency),
    )
    return _build_projection_from_amounts(
        as_of=forecast_input.as_of,
        opening_cash=forecast_input.opening_cash,
        weeks=forecast_input.weeks,
        cash_in=cash_in,
        cash_out=cash_out,
        audit=audit,
    )


@dataclass(frozen=True)
class LiquidityObservation:
    """One dated cash balance used by the liquidity calculation."""

    observed_on: date
    basis: LiquidityObservationBasis
    balance: Money

    def __post_init__(self) -> None:
        _require_date(self.observed_on, "observed_on")
        if not isinstance(self.basis, LiquidityObservationBasis):
            raise TypeError("basis must be LiquidityObservationBasis")
        _require_money(self.balance, "balance")


@dataclass(frozen=True)
class LiquiditySummary:
    """Cash-floor runway over opening cash and 13 weekly ending balances.

    ``first_breach`` and ``minimum_cash_observation`` identify whether the
    relevant balance is the as-of opening anchor or a forecast week ending.
    An opening breach therefore cannot be hidden by cash received in week one.
    """

    as_of: date
    opening_cash: Money
    minimum_cash: Money
    runway_status: RunwayStatus
    runway_weeks: Optional[int]
    first_breach: Optional[LiquidityObservation]
    minimum_cash_observation: LiquidityObservation
    horizon_weeks: int = FORECAST_WEEKS

    def __post_init__(self) -> None:
        _require_date(self.as_of, "as_of")
        opening = _require_money(self.opening_cash, "opening_cash")
        floor = _require_money(self.minimum_cash, "minimum_cash")
        _require_currency(floor, opening.currency, "minimum_cash")
        if not isinstance(
            self.minimum_cash_observation,
            LiquidityObservation,
        ):
            raise TypeError("minimum_cash_observation must be LiquidityObservation")
        minimum = self.minimum_cash_observation
        _require_currency(minimum.balance, opening.currency, "minimum balance")
        self._validate_observation(minimum, "minimum_cash_observation")
        if minimum.balance.amount > opening.amount:
            raise CashFlowError("minimum cash cannot exceed the included opening balance")
        if not isinstance(self.runway_status, RunwayStatus):
            raise TypeError("runway_status must be RunwayStatus")
        if not isinstance(self.horizon_weeks, int) or isinstance(
            self.horizon_weeks,
            bool,
        ):
            raise TypeError("horizon_weeks must be an integer")
        if self.horizon_weeks != FORECAST_WEEKS:
            raise CashFlowError("liquidity horizon must contain exactly 13 weeks")

        opening_breached = opening.amount < floor.amount
        if self.runway_status is RunwayStatus.BREACHED_AT_AS_OF:
            if not opening_breached:
                raise CashFlowError("breached-at-as-of status requires opening cash below the floor")
            if self.runway_weeks != 0:
                raise CashFlowError("an as-of opening breach has zero runway weeks")
            self._require_first_breach(LiquidityObservationBasis.AS_OF_OPENING)
        elif self.runway_status is RunwayStatus.BREACHES_WITHIN_HORIZON:
            if opening_breached:
                raise CashFlowError("an opening breach must use breached-at-as-of status")
            if not isinstance(self.runway_weeks, int) or isinstance(
                self.runway_weeks,
                bool,
            ):
                raise TypeError("breach runway_weeks must be an integer")
            if not 0 <= self.runway_weeks < self.horizon_weeks:
                raise CashFlowError("breach runway_weeks is outside the horizon")
            self._require_first_breach(LiquidityObservationBasis.WEEK_ENDING)
        else:
            if self.runway_weeks is not None:
                raise CashFlowError("no-breach liquidity cannot report runway_weeks")
            if self.first_breach is not None:
                raise CashFlowError("no-breach liquidity cannot report a first breach")
            if opening_breached or minimum.balance.amount < floor.amount:
                raise CashFlowError("a no-breach summary cannot contain a balance below the floor")

        if self.first_breach is not None:
            if minimum.balance.amount > self.first_breach.balance.amount:
                raise CashFlowError("minimum cash cannot exceed the first breach balance")

    def _validate_observation(
        self,
        observation: LiquidityObservation,
        field_name: str,
    ) -> None:
        if observation.basis is LiquidityObservationBasis.AS_OF_OPENING:
            if observation.observed_on != self.as_of:
                raise CashFlowError("%s must occur on as_of" % field_name)
            if observation.balance != self.opening_cash:
                raise CashFlowError("%s must equal opening_cash" % field_name)
        elif observation.observed_on <= self.as_of:
            raise CashFlowError("%s week ending must follow as_of" % field_name)

    def _require_first_breach(
        self,
        expected_basis: LiquidityObservationBasis,
    ) -> None:
        if not isinstance(self.first_breach, LiquidityObservation):
            raise TypeError("a breach status requires first_breach")
        _require_currency(
            self.first_breach.balance,
            self.opening_cash.currency,
            "first breach balance",
        )
        self._validate_observation(self.first_breach, "first_breach")
        if self.first_breach.basis is not expected_basis:
            raise CashFlowError("first breach basis does not match runway status")
        if self.first_breach.balance.amount >= self.minimum_cash.amount:
            raise CashFlowError("first breach balance must be below the floor")

    @property
    def first_breach_date(self) -> Optional[date]:
        return None if self.first_breach is None else self.first_breach.observed_on

    @property
    def first_breach_week_ending(self) -> Optional[date]:
        if self.first_breach is None or self.first_breach.basis is LiquidityObservationBasis.AS_OF_OPENING:
            return None
        return self.first_breach.observed_on

    @property
    def minimum_cash_date(self) -> date:
        return self.minimum_cash_observation.observed_on

    @property
    def minimum_cash_week_ending(self) -> Optional[date]:
        if self.minimum_cash_observation.basis is LiquidityObservationBasis.AS_OF_OPENING:
            return None
        return self.minimum_cash_observation.observed_on

    @property
    def minimum_cash_balance(self) -> Money:
        return self.minimum_cash_observation.balance


def compute_liquidity_summary(
    projection: CashFlowProjection,
    *,
    minimum_cash: Optional[Money] = None,
) -> LiquiditySummary:
    """Compute full weeks before the first opening-or-ending cash breach."""

    if not isinstance(projection, CashFlowProjection):
        raise TypeError("projection must be CashFlowProjection")
    floor = minimum_cash or Money.zero(projection.currency)
    _require_money(floor, "minimum_cash")
    _require_currency(floor, projection.currency, "minimum_cash")

    opening_observation = LiquidityObservation(
        observed_on=projection.as_of,
        basis=LiquidityObservationBasis.AS_OF_OPENING,
        balance=projection.opening_cash,
    )
    weekly_observations = tuple(
        LiquidityObservation(
            observed_on=row.week.end,
            basis=LiquidityObservationBasis.WEEK_ENDING,
            balance=row.ending_cash,
        )
        for row in projection.weekly
    )
    observations = (opening_observation,) + weekly_observations
    first_breach = next(
        (observation for observation in observations if observation.balance.amount < floor.amount),
        None,
    )
    minimum_observation = min(
        observations,
        key=lambda observation: observation.balance.amount,
    )

    if first_breach is None:
        status = RunwayStatus.NO_BREACH_WITHIN_HORIZON
        runway_weeks = None
    elif first_breach.basis is LiquidityObservationBasis.AS_OF_OPENING:
        status = RunwayStatus.BREACHED_AT_AS_OF
        runway_weeks = 0
    else:
        status = RunwayStatus.BREACHES_WITHIN_HORIZON
        runway_weeks = weekly_observations.index(first_breach)

    return LiquiditySummary(
        as_of=projection.as_of,
        opening_cash=projection.opening_cash,
        minimum_cash=floor,
        runway_status=status,
        runway_weeks=runway_weeks,
        first_breach=first_breach,
        minimum_cash_observation=minimum_observation,
    )


@dataclass(frozen=True)
class ScenarioParameters:
    """Named, explicit stress assumptions applied to one base projection."""

    name: str
    label: str
    cash_in_haircut_rate: Decimal = Decimal(0)
    cash_in_delay_weeks: int = 0
    cash_out_inflation_rate: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.label, "label")
        _require_rate(
            self.cash_in_haircut_rate,
            "cash_in_haircut_rate",
            maximum=Decimal(1),
        )
        _require_rate(
            self.cash_out_inflation_rate,
            "cash_out_inflation_rate",
        )
        if not isinstance(self.cash_in_delay_weeks, int) or isinstance(
            self.cash_in_delay_weeks,
            bool,
        ):
            raise TypeError("cash_in_delay_weeks must be an integer")
        if self.cash_in_delay_weeks < 0:
            raise CashFlowError("cash_in_delay_weeks must not be negative")


@dataclass(frozen=True)
class ScenarioProjection:
    """Read-only stress projection, including liquidity and horizon spill."""

    parameters: ScenarioParameters
    projection: CashFlowProjection
    slipped_beyond_horizon: Money
    liquidity: LiquiditySummary

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, ScenarioParameters):
            raise TypeError("parameters must be ScenarioParameters")
        if not isinstance(self.projection, CashFlowProjection):
            raise TypeError("projection must be CashFlowProjection")
        slipped = _require_money(
            self.slipped_beyond_horizon,
            "slipped_beyond_horizon",
        )
        _require_currency(
            slipped,
            self.projection.currency,
            "slipped_beyond_horizon",
        )
        if slipped.amount < Decimal(0):
            raise CashFlowError("slipped_beyond_horizon must not be negative")
        if not isinstance(self.liquidity, LiquiditySummary):
            raise TypeError("liquidity must be LiquiditySummary")
        _require_currency(
            self.liquidity.minimum_cash,
            self.projection.currency,
            "liquidity minimum_cash",
        )
        expected_liquidity = compute_liquidity_summary(
            self.projection,
            minimum_cash=self.liquidity.minimum_cash,
        )
        if self.liquidity != expected_liquidity:
            raise CashFlowError("scenario liquidity does not match its projection")


def apply_scenario(
    projection: CashFlowProjection,
    parameters: ScenarioParameters,
    *,
    minimum_cash: Optional[Money] = None,
) -> ScenarioProjection:
    """Transform one projection without changing its opening-cash anchor.

    Cash-in is delayed first and then haircutted; delayed amounts that leave the
    horizon are reported at their pre-haircut value.  Cash-out inflation applies
    independently.  The base projection is immutable and is never modified.
    """

    if not isinstance(projection, CashFlowProjection):
        raise TypeError("projection must be CashFlowProjection")
    if not isinstance(parameters, ScenarioParameters):
        raise TypeError("parameters must be ScenarioParameters")
    if minimum_cash is not None:
        _require_money(minimum_cash, "minimum_cash")
        _require_currency(minimum_cash, projection.currency, "minimum_cash")

    raw_cash_in = [row.cash_in.amount for row in projection.weekly]
    shifted_cash_in = [Decimal(0) for _ in range(FORECAST_WEEKS)]
    slipped = Decimal(0)
    for index, amount in enumerate(raw_cash_in):
        destination = index + parameters.cash_in_delay_weeks
        if destination < FORECAST_WEEKS:
            shifted_cash_in[destination] += amount
        else:
            slipped += amount

    haircut_factor = Decimal(1) - parameters.cash_in_haircut_rate
    inflation_factor = Decimal(1) + parameters.cash_out_inflation_rate
    scenario_cash_in = tuple(
        _rounded_money(amount * haircut_factor, projection.currency) for amount in shifted_cash_in
    )
    scenario_cash_out = tuple(
        _rounded_money(
            row.cash_out.amount * inflation_factor,
            projection.currency,
        )
        for row in projection.weekly
    )
    scenario_projection = _build_projection_from_amounts(
        as_of=projection.as_of,
        opening_cash=projection.opening_cash,
        weeks=tuple(row.week for row in projection.weekly),
        cash_in=scenario_cash_in,
        cash_out=scenario_cash_out,
        audit=projection.audit,
    )
    liquidity = compute_liquidity_summary(
        scenario_projection,
        minimum_cash=minimum_cash,
    )
    return ScenarioProjection(
        parameters=parameters,
        projection=scenario_projection,
        slipped_beyond_horizon=Money(slipped, projection.currency),
        liquidity=liquidity,
    )


@dataclass(frozen=True)
class VarianceAssessment:
    """Classification and adjacent-week amount that self-corrects."""

    classification: VarianceClassification
    timing_offset_amount: Money

    def __post_init__(self) -> None:
        if not isinstance(self.classification, VarianceClassification):
            raise TypeError("classification must be VarianceClassification")
        offset = _require_money(
            self.timing_offset_amount,
            "timing_offset_amount",
        )
        if offset.amount < Decimal(0):
            raise CashFlowError("timing_offset_amount must not be negative")
        if self.classification is not VarianceClassification.TIMING and offset.amount != Decimal(0):
            raise CashFlowError("only a timing variance can report a timing offset")

    @classmethod
    def incomplete_actuals(cls, currency: str) -> "VarianceAssessment":
        """Build the only valid assessment for an incomplete actual week."""

        return cls(
            classification=(VarianceClassification.UNCLASSIFIED_INCOMPLETE_ACTUALS),
            timing_offset_amount=Money.zero(currency),
        )

    @property
    def is_classified(self) -> bool:
        """Whether the assessment makes a causal variance claim."""

        return self.classification is not VarianceClassification.UNCLASSIFIED_INCOMPLETE_ACTUALS


def classify_variance(
    variance: Money,
    *,
    next_week_offset: Optional[Money] = None,
    prior_variances: Tuple[Money, ...] = (),
) -> VarianceAssessment:
    """Classify a variance as timing, persistent systematic, or one-off."""

    variance = _require_money(variance, "variance")
    if not isinstance(prior_variances, tuple):
        raise TypeError("prior_variances must be an immutable tuple")
    if any(not isinstance(item, Money) for item in prior_variances):
        raise TypeError("prior_variances must contain Money values")
    offset = next_week_offset or Money.zero(variance.currency)
    _require_money(offset, "next_week_offset")
    _require_currency(offset, variance.currency, "next_week_offset")
    for prior in prior_variances:
        _require_currency(prior, variance.currency, "prior variance")

    zero = Money.zero(variance.currency)
    if variance.amount == Decimal(0):
        return VarianceAssessment(
            classification=VarianceClassification.ONE_OFF,
            timing_offset_amount=zero,
        )
    if variance.amount * offset.amount < Decimal(0):
        timing_amount = min(abs(variance.amount), abs(offset.amount))
        if timing_amount > Decimal(0):
            return VarianceAssessment(
                classification=VarianceClassification.TIMING,
                timing_offset_amount=Money(timing_amount, variance.currency),
            )

    recent = prior_variances[-(_SYSTEMATIC_WEEKS - 1) :]
    if len(recent) == _SYSTEMATIC_WEEKS - 1 and all(
        prior.amount * variance.amount > Decimal(0) for prior in recent
    ):
        return VarianceAssessment(
            classification=VarianceClassification.SYSTEMATIC,
            timing_offset_amount=zero,
        )
    return VarianceAssessment(
        classification=VarianceClassification.ONE_OFF,
        timing_offset_amount=zero,
    )


@dataclass(frozen=True)
class ActualCashFlow:
    """Observed cash totals for one forecast week."""

    week_ending: date
    cash_in: Money
    cash_out: Money
    is_partial: bool = False

    def __post_init__(self) -> None:
        _require_date(self.week_ending, "week_ending")
        cash_in = _require_money(self.cash_in, "cash_in")
        cash_out = _require_money(self.cash_out, "cash_out")
        _require_currency(cash_out, cash_in.currency, "cash_out")
        if cash_in.amount < Decimal(0) or cash_out.amount < Decimal(0):
            raise CashFlowError("actual cash totals must not be negative")
        if not isinstance(self.is_partial, bool):
            raise TypeError("is_partial must be bool")


@dataclass(frozen=True)
class WeeklyVariance:
    """Forecast-to-actual comparison and taxonomy for one week."""

    week_ending: date
    forecast_cash_in: Money
    actual_cash_in: Money
    cash_in_variance: Money
    cash_in_assessment: VarianceAssessment
    forecast_cash_out: Money
    actual_cash_out: Money
    cash_out_variance: Money
    cash_out_assessment: VarianceAssessment
    is_partial: bool

    def __post_init__(self) -> None:
        _require_date(self.week_ending, "week_ending")
        monetary_fields = (
            (self.forecast_cash_in, "forecast_cash_in"),
            (self.actual_cash_in, "actual_cash_in"),
            (self.cash_in_variance, "cash_in_variance"),
            (self.forecast_cash_out, "forecast_cash_out"),
            (self.actual_cash_out, "actual_cash_out"),
            (self.cash_out_variance, "cash_out_variance"),
        )
        first = _require_money(monetary_fields[0][0], monetary_fields[0][1])
        currency = first.currency
        for value, field_name in monetary_fields[1:]:
            money = _require_money(value, field_name)
            _require_currency(money, currency, field_name)
        for amount, field_name in (
            (self.forecast_cash_in, "forecast_cash_in"),
            (self.actual_cash_in, "actual_cash_in"),
            (self.forecast_cash_out, "forecast_cash_out"),
            (self.actual_cash_out, "actual_cash_out"),
        ):
            if amount.amount < Decimal(0):
                raise CashFlowError("%s must not be negative" % field_name)
        if self.cash_in_variance.amount != self.actual_cash_in.amount - self.forecast_cash_in.amount:
            raise CashFlowError("cash_in_variance does not tie")
        if self.cash_out_variance.amount != self.actual_cash_out.amount - self.forecast_cash_out.amount:
            raise CashFlowError("cash_out_variance does not tie")
        if not isinstance(self.cash_in_assessment, VarianceAssessment):
            raise TypeError("cash_in_assessment must be VarianceAssessment")
        if not isinstance(self.cash_out_assessment, VarianceAssessment):
            raise TypeError("cash_out_assessment must be VarianceAssessment")
        _require_currency(
            self.cash_in_assessment.timing_offset_amount,
            currency,
            "cash_in timing offset",
        )
        _require_currency(
            self.cash_out_assessment.timing_offset_amount,
            currency,
            "cash_out timing offset",
        )
        if not isinstance(self.is_partial, bool):
            raise TypeError("is_partial must be bool")
        assessments = (self.cash_in_assessment, self.cash_out_assessment)
        if self.is_partial and any(assessment.is_classified for assessment in assessments):
            raise CashFlowError("partial week assessments must be unclassified incomplete actuals")
        if not self.is_partial and any(not assessment.is_classified for assessment in assessments):
            raise CashFlowError("complete week assessments cannot use incomplete actuals")


@dataclass(frozen=True)
class AccuracyMetric:
    """Magnitude, direction, and dollar error for one cash direction."""

    mape: Optional[Decimal]
    bias: Optional[Decimal]
    mad: Optional[Money]
    weeks_scored: int
    weeks_excluded_zero_actual: int

    def __post_init__(self) -> None:
        for value, field_name in ((self.mape, "mape"), (self.bias, "bias")):
            if value is not None:
                if not isinstance(value, Decimal):
                    raise TypeError("%s must be Decimal or None" % field_name)
                if not value.is_finite():
                    raise CashFlowError("%s must be finite" % field_name)
        if self.mape is not None and self.mape < Decimal(0):
            raise CashFlowError("mape must not be negative")
        if self.mad is not None:
            mad = _require_money(self.mad, "mad")
            if mad.amount < Decimal(0):
                raise CashFlowError("mad must not be negative")
        for value, field_name in (
            (self.weeks_scored, "weeks_scored"),
            (
                self.weeks_excluded_zero_actual,
                "weeks_excluded_zero_actual",
            ),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError("%s must be an integer" % field_name)
            if value < 0:
                raise CashFlowError("%s must not be negative" % field_name)
        if self.weeks_excluded_zero_actual > self.weeks_scored:
            raise CashFlowError("zero-actual exclusions exceed scored weeks")
        if self.weeks_scored == 0 and any(value is not None for value in (self.mape, self.bias, self.mad)):
            raise CashFlowError("an unscored accuracy metric cannot report results")
        if self.weeks_scored > 0 and self.mad is None:
            raise CashFlowError("a scored accuracy metric must report MAD")


@dataclass(frozen=True)
class ForecastAccuracy:
    """Accuracy metrics with incomplete-source weeks explicitly excluded."""

    cash_in: AccuracyMetric
    cash_out: AccuracyMetric
    weeks_considered: int
    weeks_excluded_partial: int
    excluded_partial_week_endings: Tuple[date, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.cash_in, AccuracyMetric):
            raise TypeError("cash_in must be AccuracyMetric")
        if not isinstance(self.cash_out, AccuracyMetric):
            raise TypeError("cash_out must be AccuracyMetric")
        for value, field_name in (
            (self.weeks_considered, "weeks_considered"),
            (self.weeks_excluded_partial, "weeks_excluded_partial"),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError("%s must be an integer" % field_name)
            if value < 0:
                raise CashFlowError("%s must not be negative" % field_name)
        if not isinstance(self.excluded_partial_week_endings, tuple):
            raise TypeError("excluded_partial_week_endings must be an immutable tuple")
        for week_ending in self.excluded_partial_week_endings:
            _require_date(week_ending, "excluded partial week ending")
        if len(self.excluded_partial_week_endings) != len(set(self.excluded_partial_week_endings)):
            raise CashFlowError("excluded partial week endings must be unique")
        if self.weeks_excluded_partial != len(self.excluded_partial_week_endings):
            raise CashFlowError("partial-week count does not tie")
        scored_weeks = self.weeks_considered - self.weeks_excluded_partial
        if scored_weeks < 0:
            raise CashFlowError("partial weeks exceed considered weeks")
        if self.cash_in.weeks_scored != scored_weeks:
            raise CashFlowError("cash-in scored-week count does not tie")
        if self.cash_out.weeks_scored != scored_weeks:
            raise CashFlowError("cash-out scored-week count does not tie")
        if self.cash_in.mad is not None and self.cash_out.mad is not None:
            _require_currency(
                self.cash_out.mad,
                self.cash_in.mad.currency,
                "cash_out MAD",
            )


@dataclass(frozen=True)
class CashFlowVarianceReport:
    """Chronological weekly variances and aggregate frozen-forecast accuracy."""

    rows: Tuple[WeeklyVariance, ...]
    accuracy: ForecastAccuracy

    def __post_init__(self) -> None:
        if not isinstance(self.rows, tuple):
            raise TypeError("rows must be an immutable tuple")
        if any(not isinstance(row, WeeklyVariance) for row in self.rows):
            raise TypeError("rows must contain WeeklyVariance values")
        if not isinstance(self.accuracy, ForecastAccuracy):
            raise TypeError("accuracy must be ForecastAccuracy")
        endings = tuple(row.week_ending for row in self.rows)
        if endings != tuple(sorted(endings)):
            raise CashFlowError("variance rows must be chronological")
        if len(endings) != len(set(endings)):
            raise CashFlowError("variance row week endings must be unique")
        if self.accuracy.weeks_considered != len(self.rows):
            raise CashFlowError("variance row count does not tie to accuracy")
        excluded = tuple(row.week_ending for row in self.rows if row.is_partial)
        if excluded != self.accuracy.excluded_partial_week_endings:
            raise CashFlowError("partial variance rows do not tie to accuracy")
        if self.rows:
            currency = self.rows[0].forecast_cash_in.currency
            for row in self.rows[1:]:
                _require_currency(
                    row.forecast_cash_in,
                    currency,
                    "variance row",
                )
            for metric, field_name in (
                (self.accuracy.cash_in, "cash_in MAD"),
                (self.accuracy.cash_out, "cash_out MAD"),
            ):
                if metric.mad is not None:
                    _require_currency(metric.mad, currency, field_name)
            complete_rows = tuple(row for row in self.rows if not row.is_partial)
            expected_cash_in = _accuracy_metric(
                tuple((row.forecast_cash_in.amount, row.actual_cash_in.amount) for row in complete_rows),
                currency,
            )
            expected_cash_out = _accuracy_metric(
                tuple((row.forecast_cash_out.amount, row.actual_cash_out.amount) for row in complete_rows),
                currency,
            )
            if self.accuracy.cash_in != expected_cash_in:
                raise CashFlowError("cash-in accuracy does not match variance rows")
            if self.accuracy.cash_out != expected_cash_out:
                raise CashFlowError("cash-out accuracy does not match variance rows")


def _accuracy_metric(
    pairs: Tuple[Tuple[Decimal, Decimal], ...],
    currency: str,
) -> AccuracyMetric:
    if not pairs:
        return AccuracyMetric(
            mape=None,
            bias=None,
            mad=None,
            weeks_scored=0,
            weeks_excluded_zero_actual=0,
        )
    absolute_errors = [abs(forecast - actual) for forecast, actual in pairs]
    mad = _rounded_money(
        sum(absolute_errors, Decimal(0)) / Decimal(len(pairs)),
        currency,
    )
    percentage_errors = [
        abs(forecast - actual) / abs(actual) for forecast, actual in pairs if actual != Decimal(0)
    ]
    if percentage_errors:
        mape = (sum(percentage_errors, Decimal(0)) / Decimal(len(percentage_errors))).quantize(
            _RATIO_QUANTUM, rounding=ROUND_HALF_UP
        )
    else:
        mape = None
    forecast_total = sum((forecast for forecast, _ in pairs), Decimal(0))
    actual_total = sum((actual for _, actual in pairs), Decimal(0))
    if actual_total == Decimal(0):
        bias = None
    else:
        bias = ((forecast_total - actual_total) / actual_total).quantize(
            _RATIO_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    return AccuracyMetric(
        mape=mape,
        bias=bias,
        mad=mad,
        weeks_scored=len(pairs),
        weeks_excluded_zero_actual=len(pairs) - len(percentage_errors),
    )


def build_variance_report(
    projection: CashFlowProjection,
    actuals: Tuple[ActualCashFlow, ...],
) -> CashFlowVarianceReport:
    """Compare frozen projected totals with available weekly actuals.

    Partial-source weeks remain visible but are explicitly unclassified and
    excluded from MAPE, bias, and MAD.  Timing and systematic classifications
    use only complete, adjacent weeks; missing or partial weeks break the
    pattern.
    """

    if not isinstance(projection, CashFlowProjection):
        raise TypeError("projection must be CashFlowProjection")
    if not isinstance(actuals, tuple):
        raise TypeError("actuals must be an immutable tuple")
    if any(not isinstance(actual, ActualCashFlow) for actual in actuals):
        raise TypeError("actuals must contain ActualCashFlow values")
    actual_dates = [actual.week_ending for actual in actuals]
    if len(actual_dates) != len(set(actual_dates)):
        raise CashFlowError("actual week endings must be unique")

    forecast_by_date = {row.week.end: (index, row) for index, row in enumerate(projection.weekly)}
    actual_by_index = {}
    for actual in actuals:
        _require_currency(actual.cash_in, projection.currency, "actual cash_in")
        _require_currency(actual.cash_out, projection.currency, "actual cash_out")
        if actual.week_ending not in forecast_by_date:
            raise CashFlowError("actual week ending is outside the frozen projection")
        index = forecast_by_date[actual.week_ending][0]
        actual_by_index[index] = actual

    variances = {}
    for index, actual in actual_by_index.items():
        forecast = projection.weekly[index]
        variances[index] = (
            Money(
                actual.cash_in.amount - forecast.cash_in.amount,
                projection.currency,
            ),
            Money(
                actual.cash_out.amount - forecast.cash_out.amount,
                projection.currency,
            ),
        )

    rows = []
    for index in sorted(actual_by_index):
        actual = actual_by_index[index]
        forecast = projection.weekly[index]
        cash_in_variance, cash_out_variance = variances[index]

        next_actual = actual_by_index.get(index + 1)
        if next_actual is not None and not actual.is_partial and not next_actual.is_partial:
            next_in, next_out = variances[index + 1]
        else:
            next_in = Money.zero(projection.currency)
            next_out = Money.zero(projection.currency)

        prior_in = ()
        prior_out = ()
        previous_two = (actual_by_index.get(index - 2), actual_by_index.get(index - 1))
        if (
            not actual.is_partial
            and all(value is not None for value in previous_two)
            and all(not value.is_partial for value in previous_two if value is not None)
        ):
            prior_in = (variances[index - 2][0], variances[index - 1][0])
            prior_out = (variances[index - 2][1], variances[index - 1][1])

        if actual.is_partial:
            cash_in_assessment = VarianceAssessment.incomplete_actuals(projection.currency)
            cash_out_assessment = VarianceAssessment.incomplete_actuals(projection.currency)
        else:
            cash_in_assessment = classify_variance(
                cash_in_variance,
                next_week_offset=next_in,
                prior_variances=prior_in,
            )
            cash_out_assessment = classify_variance(
                cash_out_variance,
                next_week_offset=next_out,
                prior_variances=prior_out,
            )

        rows.append(
            WeeklyVariance(
                week_ending=actual.week_ending,
                forecast_cash_in=forecast.cash_in,
                actual_cash_in=actual.cash_in,
                cash_in_variance=cash_in_variance,
                cash_in_assessment=cash_in_assessment,
                forecast_cash_out=forecast.cash_out,
                actual_cash_out=actual.cash_out,
                cash_out_variance=cash_out_variance,
                cash_out_assessment=cash_out_assessment,
                is_partial=actual.is_partial,
            )
        )

    complete_rows = tuple(row for row in rows if not row.is_partial)
    cash_in_pairs = tuple((row.forecast_cash_in.amount, row.actual_cash_in.amount) for row in complete_rows)
    cash_out_pairs = tuple(
        (row.forecast_cash_out.amount, row.actual_cash_out.amount) for row in complete_rows
    )
    excluded = tuple(row.week_ending for row in rows if row.is_partial)
    accuracy = ForecastAccuracy(
        cash_in=_accuracy_metric(cash_in_pairs, projection.currency),
        cash_out=_accuracy_metric(cash_out_pairs, projection.currency),
        weeks_considered=len(rows),
        weeks_excluded_partial=len(excluded),
        excluded_partial_week_endings=excluded,
    )
    return CashFlowVarianceReport(rows=tuple(rows), accuracy=accuracy)


__all__ = [
    "FORECAST_WEEKS",
    "AccuracyMetric",
    "ActualCashFlow",
    "CashFlowCadence",
    "CashFlowDirection",
    "CashFlowError",
    "CashFlowForecastInput",
    "CashFlowItem",
    "CashFlowProjection",
    "CashFlowVarianceReport",
    "ForecastAccuracy",
    "ForecastWeek",
    "LiquidityObservation",
    "LiquidityObservationBasis",
    "LiquiditySummary",
    "ProjectionAudit",
    "RecurringCashFlow",
    "RunwayStatus",
    "ScenarioParameters",
    "ScenarioProjection",
    "VarianceAssessment",
    "VarianceClassification",
    "WeeklyCashFlow",
    "WeeklyVariance",
    "apply_scenario",
    "build_13_week_projection",
    "build_forecast_weeks",
    "build_variance_report",
    "classify_variance",
    "compute_liquidity_summary",
]

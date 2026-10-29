"""Canonical semantic references for Peregrine finance metrics (FOS-1 Wave A).

A number without its semantics is a trap: $1.2M "revenue" on a CASH basis is not
comparable to $1.3M "revenue" on an ACCRUAL basis; a July 1-11 month-to-date figure
is not comparable to a full June; a BUDGET line is not an ACTUAL. This module gives
every metric value an explicit, hashable, serializable identity — WHAT (metric_id),
WHO (entity_id), WHEN (Period + grain), HOW (Basis), WHICH WORLD (Scenario), and
IN WHAT TERMS (Unit + currency + dimensions) — so the compat layer
(``app.semantics.compat``) can refuse accidental mixing instead of silently
producing a financially wrong comparison.

Everything here is a frozen value object: hashable (usable as dict keys / in sets)
and round-trippable via ``to_dict()`` / ``from_dict()``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

__all__ = [
    "Basis",
    "ExpectedSign",
    "MetricRef",
    "Period",
    "PeriodGrain",
    "Scenario",
    "ScenarioKind",
    "Statement",
    "Unit",
]


class Basis(StrEnum):
    """Accounting basis. Cash-basis and accrual-basis figures are never comparable.

    ``BASIS_INDEPENDENT`` tags a value whose number does not depend on the reporting
    basis (a bank balance, a debt-tranche schedule) — comparable against either basis.
    """

    ACCRUAL = "accrual"
    CASH = "cash"
    BASIS_INDEPENDENT = "basis_independent"


class ScenarioKind(StrEnum):
    """Which world the number belongs to."""

    ACTUAL = "actual"
    BUDGET = "budget"
    FORECAST = "forecast"
    STRESS = "stress"


@dataclass(frozen=True, slots=True)
class Scenario:
    """A scenario tag; STRESS scenarios carry the stress-case name (e.g. "rate_shock").

    Two stress scenarios with different names are DIFFERENT scenarios — a downside
    case must not silently mix with a rate-shock case.
    """

    kind: ScenarioKind
    stress_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind is ScenarioKind.STRESS:
            if not self.stress_name:
                raise ValueError("a STRESS scenario requires a non-empty stress_name")
        elif self.stress_name is not None:
            raise ValueError(
                f"stress_name is only valid for STRESS scenarios, got kind={self.kind.value!r}"
            )

    # ── named constructors ────────────────────────────────────────────────────
    @classmethod
    def actual(cls) -> Scenario:
        return cls(ScenarioKind.ACTUAL)

    @classmethod
    def budget(cls) -> Scenario:
        return cls(ScenarioKind.BUDGET)

    @classmethod
    def forecast(cls) -> Scenario:
        return cls(ScenarioKind.FORECAST)

    @classmethod
    def stress(cls, stress_name: str) -> Scenario:
        return cls(ScenarioKind.STRESS, stress_name=stress_name)

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"kind": self.kind.value}
        if self.stress_name is not None:
            out["stress_name"] = self.stress_name
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Scenario:
        stress_name = data.get("stress_name")
        return cls(
            kind=ScenarioKind(str(data["kind"])),
            stress_name=None if stress_name is None else str(stress_name),
        )


class PeriodGrain(StrEnum):
    """The canonical shape of a period window.

    ``POINT_IN_TIME`` is a zero-width window (a balance read AT a date, e.g. a
    balance-sheet stock or a week-ending cash balance) — the grain the W2 evidence
    lane emits for stock facts. ``TTM`` is the trailing-twelve-months grain (the
    covenant as-tested window); the W2 emitters spell it ``PeriodGrain.TTM``.
    """

    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    TTM = "ttm"
    WEEK = "week"
    CUSTOM = "custom"
    POINT_IN_TIME = "point_in_time"


def _end_of_month(year: int, month: int) -> date:
    """Last calendar day of (year, month)."""
    if month == 12:
        return date(year + 1, 1, 1) - timedelta(days=1)
    return date(year, month + 1, 1) - timedelta(days=1)


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    """(year, month) advanced by ``months`` whole months."""
    idx = (year * 12 + (month - 1)) + months
    return idx // 12, (idx % 12) + 1


# Whole-month span (in months) each grain canonically covers. Fiscal quarters/years
# may START in any month (fiscal year is company data, not a constant — the
# financial-modeling-correctness skill, principle 8) — completeness is judged by
# whole-month SPAN, never by a hardcoded January start.
_GRAIN_MONTH_SPAN: dict[PeriodGrain, int] = {
    PeriodGrain.MONTH: 1,
    PeriodGrain.QUARTER: 3,
    PeriodGrain.YEAR: 12,
    PeriodGrain.TTM: 12,
}


@dataclass(frozen=True, slots=True)
class Period:
    """A dated window [start, end] (both inclusive) with an explicit grain."""

    start: date
    end: date
    grain: PeriodGrain

    def __post_init__(self) -> None:
        if not isinstance(self.start, date) or isinstance(self.start, bool):
            raise TypeError(f"Period.start must be a date, got {type(self.start).__name__}")
        if not isinstance(self.end, date) or isinstance(self.end, bool):
            raise TypeError(f"Period.end must be a date, got {type(self.end).__name__}")
        if self.start > self.end:
            raise ValueError(
                f"Period start must be <= end, got start={self.start.isoformat()} "
                f"end={self.end.isoformat()}"
            )

    # ── named constructors ────────────────────────────────────────────────────
    @classmethod
    def month(cls, year: int, month: int) -> Period:
        """The full calendar month (year, month)."""
        return cls(date(year, month, 1), _end_of_month(year, month), PeriodGrain.MONTH)

    @classmethod
    def ttm_ending(cls, year: int, month: int) -> Period:
        """The trailing-12-month window whose LAST month is (year, month)."""
        start_y, start_m = _add_months(year, month, -11)
        return cls(date(start_y, start_m, 1), _end_of_month(year, month), PeriodGrain.TTM)

    @classmethod
    def point_in_time(cls, as_of: date) -> Period:
        """A zero-width window AT ``as_of`` — a balance read (stock) at a date."""
        return cls(as_of, as_of, PeriodGrain.POINT_IN_TIME)

    # ── semantics helpers ─────────────────────────────────────────────────────
    def is_partial(self, as_of: date) -> bool:
        """True when this period's end falls beyond the last CLOSED month at ``as_of``.

        The last closed month relative to ``as_of`` is the month BEFORE ``as_of``'s
        month (conservative: the current month is never closed while we are inside
        it, including on its final day — close happens after the month rolls over).
        A period ending inside a not-yet-closed month is month-to-date / in-flight
        data and must not silently compare against a fully closed period.
        """
        last_closed_month_end = date(as_of.year, as_of.month, 1) - timedelta(days=1)
        return self.end > last_closed_month_end

    def spans_full_grain(self) -> bool:
        """Whether [start, end] exactly covers this grain's canonical span.

        MONTH / QUARTER / YEAR / TTM require whole months (start on the 1st, end on
        a month-end, covering exactly 1 / 3 / 12 / 12 months); WEEK requires exactly
        7 days. CUSTOM has no canonical span, so it is vacuously "full" here — two
        CUSTOM windows are instead compared by equal length in the compat layer.
        """
        if self.grain is PeriodGrain.POINT_IN_TIME:
            # A point-in-time read spans its (zero-width) grain exactly when it IS a
            # single date — a stock balance is always "full" at its as-of.
            return self.start == self.end
        if self.grain is PeriodGrain.WEEK:
            return (self.end - self.start).days == 6
        if self.grain is PeriodGrain.CUSTOM:
            return True
        months = _GRAIN_MONTH_SPAN[self.grain]
        if self.start.day != 1:
            return False
        end_y, end_m = _add_months(self.start.year, self.start.month, months - 1)
        return self.end == _end_of_month(end_y, end_m)

    def span_days(self) -> int:
        """Inclusive day count of the window."""
        return (self.end - self.start).days + 1

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "grain": self.grain.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Period:
        return cls(
            start=date.fromisoformat(str(data["start"])),
            end=date.fromisoformat(str(data["end"])),
            grain=PeriodGrain(str(data["grain"])),
        )


class Unit(StrEnum):
    """What kind of quantity the value is. A ratio never compares against dollars."""

    CURRENCY = "currency"
    RATIO = "ratio"
    PERCENT = "percent"
    COUNT = "count"
    DAYS = "days"


class Statement(StrEnum):
    """The financial statement / lane a metric belongs to (registry metadata)."""

    INCOME_STATEMENT = "income_statement"
    BALANCE_SHEET = "balance_sheet"
    CASH_FLOW = "cash_flow"
    CASHFLOW_FORECAST = "cashflow_forecast"
    AR = "ar"
    AP = "ap"
    COVENANT = "covenant"
    BUDGET = "budget"
    LBO = "lbo"
    # For registry rows that predate the statement taxonomy (the terse pl.*/bs.* /
    # ratio.* / cashflow.* / custom.* FOS seed) — statement is unclassified, never
    # fabricated. The validate_catalog prefix rule only applies to classified rows.
    UNSPECIFIED = "unspecified"


class ExpectedSign(StrEnum):
    """The financially-expected sign of a metric value (registry metadata)."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    EITHER = "either"


@dataclass(frozen=True, slots=True)
class MetricRef:
    """The full semantic identity of one metric value.

    ``dimensions`` accepts any str->str mapping at construction and is normalized
    to a sorted tuple of (key, value) pairs internally so the ref stays hashable
    and order-insensitively equal; read it back as a dict via ``dimensions_dict()``.
    """

    metric_id: str
    entity_id: str
    period: Period
    basis: Basis
    scenario: Scenario
    unit: Unit
    currency: str = "USD"
    dimensions: tuple[tuple[str, str], ...] | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.metric_id or not str(self.metric_id).strip():
            raise ValueError("MetricRef.metric_id must be a non-empty string")
        if not self.entity_id or not str(self.entity_id).strip():
            raise ValueError("MetricRef.entity_id must be a non-empty string")
        if not self.currency or not str(self.currency).strip():
            raise ValueError("MetricRef.currency must be a non-empty string")
        # Normalize currency to the upper-case ISO convention so "usd" == "USD".
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        # Normalize dimensions: Mapping | iterable of pairs | None → sorted tuple | None.
        dims = self.dimensions
        if dims is not None:
            if isinstance(dims, Mapping):
                items = dims.items()
            else:
                items = tuple(dims)
            normalized = tuple(sorted((str(k), str(v)) for k, v in items))
            object.__setattr__(self, "dimensions", normalized or None)

    def dimensions_dict(self) -> dict[str, str]:
        """The dimensions as a plain dict ({} when absent)."""
        return dict(self.dimensions) if self.dimensions else {}

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "entity_id": self.entity_id,
            "period": self.period.to_dict(),
            "basis": self.basis.value,
            "scenario": self.scenario.to_dict(),
            "unit": self.unit.value,
            "currency": self.currency,
            "dimensions": self.dimensions_dict() or None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> MetricRef:
        period = data["period"]
        scenario = data["scenario"]
        if not isinstance(period, Mapping):
            raise TypeError("MetricRef.from_dict: 'period' must be a mapping")
        if not isinstance(scenario, Mapping):
            raise TypeError("MetricRef.from_dict: 'scenario' must be a mapping")
        dimensions = data.get("dimensions")
        if dimensions is not None and not isinstance(dimensions, Mapping):
            raise TypeError("MetricRef.from_dict: 'dimensions' must be a mapping or None")
        return cls(
            metric_id=str(data["metric_id"]),
            entity_id=str(data["entity_id"]),
            period=Period.from_dict(period),
            basis=Basis(str(data["basis"])),
            scenario=Scenario.from_dict(scenario),
            unit=Unit(str(data["unit"])),
            currency=str(data.get("currency", "USD")),
            dimensions=dimensions,
        )

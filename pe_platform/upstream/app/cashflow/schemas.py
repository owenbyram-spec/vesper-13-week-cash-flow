"""Strict canonical DTOs for immutable cash-flow forecast exports.

The models in this module are the financial display contract shared by the
screen, persistence layer, and XLSX/PDF renderers.  They deliberately contain
no forecast-building logic: amounts arrive from the finalized weekly payload
and validation only proves that the displayed grid is internally coherent.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    field_validator,
    model_validator,
)


FORECAST_EXPORT_SCHEMA_VERSION = 1
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _decimal_cents(value: object) -> Decimal:
    """Convert one finite money value to canonical HALF_UP Decimal cents."""
    if value is None or isinstance(value, bool):
        raise ValueError("money must be a finite number, not null or boolean")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("money must be a finite decimal number") from exc
    if not amount.is_finite():
        raise ValueError("money must be finite")
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    return ZERO if amount == 0 else amount


def _serialize_money(value: Decimal) -> str:
    return format(ZERO if value == 0 else value, ".2f")


Money = Annotated[
    Decimal,
    BeforeValidator(_decimal_cents),
    PlainSerializer(_serialize_money, return_type=str, when_used="json"),
]

WeekStatus = Literal["Needs pull", "Pending", "Pulled", "Closed", "Reconciled"]
SnapshotState = Literal["ready", "stale"]
RowKind = Literal[
    "beginning_bank_balance",
    "cash_in_section",
    "customer",
    "other_cash_in",
    "total_cash_in",
    "cash_out_section",
    "vendor",
    "non_routine_expenses",
    "total_cash_out",
    "ending_bank_balance",
]
EntityType = Literal["customer", "vendor"]
NoteCode = Literal[
    "freshness",
    "data_quality",
    "opening_cash",
    "lowest_forecast_balance",
    "weeks_to_negative",
    "reanchor",
]

CANONICAL_NON_ENTITY_ROWS: dict[str, tuple[str, str]] = {
    "beginning_bank_balance": ("beginning_bank_balance", "Beginning Bank Balance"),
    "cash_in_section": ("cash_in_section", "Customer Collections (Cash In)"),
    "other_cash_in": ("other_cash_in", "Other Cash In"),
    "total_cash_in": ("total_cash_in", "Total Cash In"),
    "cash_out_section": ("cash_out_section", "Vendor Obligations (Cash Out)"),
    "non_routine_expenses": ("non_routine_expenses", "Non Routine Expenses"),
    "total_cash_out": ("total_cash_out", "Total Cash Out"),
    "ending_bank_balance": ("ending_bank_balance", "Ending Bank Balance"),
}


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ForecastExportWeek(StrictFrozenModel):
    week_ending: date
    is_lookback: bool
    status: WeekStatus | None

    @model_validator(mode="after")
    def _status_applies_only_to_lookback(self) -> "ForecastExportWeek":
        if self.is_lookback and self.status is None:
            raise ValueError("lookback weeks require a visible status")
        if not self.is_lookback and self.status is not None:
            raise ValueError("forecast weeks must have a blank status")
        return self


class ForecastGridRow(StrictFrozenModel):
    row_id: str = Field(min_length=1)
    row_kind: RowKind
    label: str
    entity_type: EntityType | None = None
    entity_key: str | None = None
    effective_day: int | None = Field(default=None, ge=0, le=31)
    effective_avg: Money | None = None
    amounts: tuple[Money | None, ...]

    @model_validator(mode="after")
    def _field_applicability(self) -> "ForecastGridRow":
        is_entity = self.row_kind in {"customer", "vendor"}
        if is_entity:
            if self.entity_type != self.row_kind:
                raise ValueError("entity_type must match the entity row_kind")
            if not self.entity_key:
                raise ValueError("entity rows require a stable entity_key")
            if self.row_id != f"{self.entity_type}:{self.entity_key}":
                raise ValueError("entity row_id must match its type and stable entity_key")
        elif any(
            value is not None
            for value in (
                self.entity_type,
                self.entity_key,
                self.effective_day,
                self.effective_avg,
            )
        ):
            raise ValueError("Day, Avg/Mo, and entity identity apply only to entity rows")
        else:
            expected_identity = CANONICAL_NON_ENTITY_ROWS[self.row_kind]
            if (self.row_id, self.label) != expected_identity:
                raise ValueError(
                    "non-entity row_id and label must match the canonical financial line"
                )

        if self.row_kind in {"cash_in_section", "cash_out_section"}:
            if any(value is not None for value in self.amounts):
                raise ValueError("section-label week cells must be true nulls")
        return self


class ForecastExportError(StrictFrozenModel):
    """Stable user-safe error disclosure (volatile trace IDs are transport-only)."""

    error_code: str = Field(min_length=1)
    user_message: str = Field(min_length=1)
    retryable: bool
    support_hint: str | None = None


class ForecastDataQuality(StrictFrozenModel):
    is_partial: bool
    unavailable_sections: tuple[str, ...] = Field(default_factory=tuple)


class ForecastUnmatchedReason(StrictFrozenModel):
    count: int = Field(ge=0)
    total_amount: Money


class ForecastUnmatchedEntity(StrictFrozenModel):
    entity_name: str
    entity_type: str
    transaction_count: int = Field(ge=0)
    total_amount: Money
    first_week_ending: date | None = None
    last_week_ending: date | None = None


class ForecastUnmatched(StrictFrozenModel):
    count: int = Field(ge=0)
    reasons: dict[str, ForecastUnmatchedReason] = Field(default_factory=dict)
    entities: tuple[ForecastUnmatchedEntity, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _reason_counts_tie(self) -> "ForecastUnmatched":
        if sum(reason.count for reason in self.reasons.values()) != self.count:
            raise ValueError("unmatched reason counts must equal unmatched count")
        return self


class ForecastBeyondHorizon(StrictFrozenModel):
    cash_in: Money
    cash_out: Money
    item_count: int = Field(ge=0)
    policy: Literal["excluded_from_grid_and_reported"] = "excluded_from_grid_and_reported"


class ForecastLiquidity(StrictFrozenModel):
    method: Literal["grid_ending_balances"]
    horizon_weeks: int = Field(ge=0)
    min_cash_floor: Money
    runway_weeks: int | None = Field(default=None, ge=0)
    runway_status: Literal[
        "no_forecast_columns",
        "no_breach_within_horizon",
        "breaches_within_horizon",
    ]
    first_breach_week_ending: date | None = None
    min_cash_week_ending: date | None = None
    min_cash_balance: Money | None = None


class ForecastReconciliationDelta(StrictFrozenModel):
    anchor_as_of: date
    prior_as_of: date
    anchor_bank_balance: Money
    prior_bank_balance: Money
    balance_sheet_change: Money
    actuals_net_change: Money
    delta: Money
    prior_balance_source: str | None = None
    anchor_balance_source: str | None = None

    @model_validator(mode="after")
    def _delta_ties(self) -> "ForecastReconciliationDelta":
        expected_change = self.anchor_bank_balance - self.prior_bank_balance
        if self.balance_sheet_change != expected_change:
            raise ValueError("reconciliation balance_sheet_change does not tie")
        if self.delta != self.balance_sheet_change - self.actuals_net_change:
            raise ValueError("reconciliation delta does not tie")
        return self


class ForecastExportNote(StrictFrozenModel):
    code: NoteCode
    text: str = Field(min_length=1)


EXPECTED_NON_ENTITY_ORDER: tuple[RowKind, ...] = (
    "beginning_bank_balance",
    "cash_in_section",
    "other_cash_in",
    "total_cash_in",
    "cash_out_section",
    "non_routine_expenses",
    "total_cash_out",
    "ending_bank_balance",
)


def _sum_nullable(values: list[Decimal | None]) -> Decimal | None:
    if any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), ZERO)


class ForecastGridExportContent(StrictFrozenModel):
    """Acyclic semantic projection used as the content-addressed hash input."""

    schema_version: Literal[1] = FORECAST_EXPORT_SCHEMA_VERSION
    company_id: str = Field(min_length=1)
    company_display_name: str = Field(min_length=1)
    as_of_week_ending: date
    lookback: int = Field(ge=0)
    snapshot_computed_at: datetime
    snapshot_state: SnapshotState
    weeks: tuple[ForecastExportWeek, ...]
    rows: tuple[ForecastGridRow, ...]

    bank_balance: Money
    bank_balance_as_of: date | None = None
    bank_balance_source: str
    opening_balance_week_ending: date | None = None
    beginning_balance_override: Money | None = None
    anchor_diagnostics: dict[str, JsonValue] | None = None
    reconciliation_delta: ForecastReconciliationDelta | None = None
    forecast_reanchored_to_control: bool

    uncategorized_actuals: int = Field(ge=0)
    review_required_weeks: tuple[date, ...] = Field(default_factory=tuple)
    past_due_in: Money
    past_due_out: Money
    errors: tuple[ForecastExportError, ...] = Field(default_factory=tuple)
    data_quality: ForecastDataQuality
    collection_haircuts: dict[str, JsonValue] | None = None
    unmatched: ForecastUnmatched
    beyond_horizon: ForecastBeyondHorizon | None = None
    liquidity: ForecastLiquidity | None = None
    notes: tuple[ForecastExportNote, ...]

    @field_validator("snapshot_computed_at")
    @classmethod
    def _snapshot_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot_computed_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("anchor_diagnostics", "collection_haircuts")
    @classmethod
    def _diagnostics_are_finite(
        cls, value: dict[str, JsonValue] | None
    ) -> dict[str, JsonValue] | None:
        def walk(item: object) -> None:
            if isinstance(item, float) and (item != item or item in {float("inf"), float("-inf")}):
                raise ValueError("diagnostics and disclosures must contain only finite numbers")
            if isinstance(item, Decimal) and not item.is_finite():
                raise ValueError("diagnostics and disclosures must contain only finite numbers")
            if isinstance(item, dict):
                for nested in item.values():
                    walk(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    walk(nested)

        walk(value)
        return value

    @model_validator(mode="after")
    def _validate_grid(self) -> "ForecastGridExportContent":
        week_count = len(self.weeks)
        if week_count == 0:
            raise ValueError("export requires at least one week")
        if self.lookback >= week_count:
            raise ValueError("lookback must leave at least one forecast week")
        if any(
            self.weeks[index].week_ending >= self.weeks[index + 1].week_ending
            for index in range(week_count - 1)
        ):
            raise ValueError("weeks must be strictly increasing")
        for index, week in enumerate(self.weeks):
            if week.is_lookback != (index < self.lookback):
                raise ValueError("week lookback flags must match the lookback boundary")
        if self.as_of_week_ending != self.weeks[self.lookback].week_ending:
            raise ValueError("as_of_week_ending must be the first forecast week")

        if any(len(row.amounts) != week_count for row in self.rows):
            raise ValueError("every row amount vector must match the week vector")
        if len({row.row_id for row in self.rows}) != len(self.rows):
            raise ValueError("row_id values must be unique")

        kinds = [row.row_kind for row in self.rows]
        expected = ["beginning_bank_balance", "cash_in_section"]
        expected.extend("customer" for _ in filter(lambda row: row.row_kind == "customer", self.rows))
        expected.extend(["other_cash_in", "total_cash_in", "cash_out_section"])
        expected.extend("vendor" for _ in filter(lambda row: row.row_kind == "vendor", self.rows))
        expected.extend(["non_routine_expenses", "total_cash_out", "ending_bank_balance"])
        if kinds != expected:
            raise ValueError("rows do not match the canonical visible-grid order")

        rows_by_kind = {
            kind: next(row for row in self.rows if row.row_kind == kind)
            for kind in EXPECTED_NON_ENTITY_ORDER
        }
        customers = [row for row in self.rows if row.row_kind == "customer"]
        vendors = [row for row in self.rows if row.row_kind == "vendor"]
        beginning = rows_by_kind["beginning_bank_balance"]
        other_in = rows_by_kind["other_cash_in"]
        total_in = rows_by_kind["total_cash_in"]
        other_out = rows_by_kind["non_routine_expenses"]
        total_out = rows_by_kind["total_cash_out"]
        ending = rows_by_kind["ending_bank_balance"]

        for index in range(week_count):
            expected_in = _sum_nullable(
                [other_in.amounts[index], *(row.amounts[index] for row in customers)]
            )
            if total_in.amounts[index] != expected_in:
                raise ValueError(f"Total Cash In does not cross-foot at week index {index}")
            expected_out = _sum_nullable(
                [other_out.amounts[index], *(row.amounts[index] for row in vendors)]
            )
            if total_out.amounts[index] != expected_out:
                raise ValueError(f"Total Cash Out does not cross-foot at week index {index}")
            expected_ending = _sum_nullable(
                [beginning.amounts[index], total_in.amounts[index]]
            )
            if expected_ending is not None and total_out.amounts[index] is not None:
                expected_ending -= total_out.amounts[index]
            else:
                expected_ending = None
            if ending.amounts[index] != expected_ending:
                raise ValueError(f"Ending Bank Balance does not cross-foot at week index {index}")

            if index == 0:
                continue
            if self.forecast_reanchored_to_control and index == self.lookback:
                reconciliation = self.reconciliation_delta
                if reconciliation is None:
                    raise ValueError("re-anchored exports require reconciliation_delta")
                if beginning.amounts[index] != reconciliation.anchor_bank_balance:
                    raise ValueError("re-anchor beginning balance must equal the control balance")
                previous_ending = ending.amounts[index - 1]
                current_beginning = beginning.amounts[index]
                if previous_ending is None or current_beginning is None:
                    raise ValueError("re-anchor boundary balances cannot be null")
                if current_beginning - previous_ending != reconciliation.delta:
                    raise ValueError("re-anchor discontinuity must equal reconciliation delta")
            elif beginning.amounts[index] != ending.amounts[index - 1]:
                raise ValueError(f"balance continuity fails at week index {index}")

        if self.forecast_reanchored_to_control and self.lookback == 0:
            raise ValueError("a re-anchor requires a disclosed lookback boundary")

        liquidity = self.liquidity
        if liquidity is not None:
            forecast_balances = ending.amounts[self.lookback :]
            if any(balance is None for balance in forecast_balances):
                raise ValueError(
                    "liquidity summary requires complete forecast ending balances"
                )
            complete_balances = tuple(
                balance for balance in forecast_balances if balance is not None
            )
            if liquidity.horizon_weeks != len(complete_balances):
                raise ValueError("liquidity horizon does not match the forecast grid")
            if liquidity.runway_status == "no_forecast_columns":
                raise ValueError("liquidity cannot report no forecast columns for this grid")

            minimum_offset, minimum_balance = min(
                enumerate(complete_balances),
                key=lambda item: item[1],
            )
            minimum_week = self.weeks[self.lookback + minimum_offset].week_ending
            if liquidity.min_cash_balance != minimum_balance:
                raise ValueError("liquidity minimum balance does not match the forecast grid")
            if liquidity.min_cash_week_ending != minimum_week:
                raise ValueError("liquidity minimum week does not match the forecast grid")

            breach_offset = next(
                (
                    offset
                    for offset, balance in enumerate(complete_balances)
                    if balance < liquidity.min_cash_floor
                ),
                None,
            )
            if breach_offset is None:
                if liquidity.runway_status != "no_breach_within_horizon":
                    raise ValueError("liquidity breach status does not match the forecast grid")
                if liquidity.runway_weeks is not None:
                    raise ValueError("liquidity runway must be null when no breach occurs")
                if liquidity.first_breach_week_ending is not None:
                    raise ValueError("liquidity breach week must be null when no breach occurs")
            else:
                breach_week = self.weeks[self.lookback + breach_offset].week_ending
                if liquidity.runway_status != "breaches_within_horizon":
                    raise ValueError("liquidity breach status does not match the forecast grid")
                if liquidity.runway_weeks != breach_offset:
                    raise ValueError("liquidity runway does not match the forecast grid")
                if liquidity.first_breach_week_ending != breach_week:
                    raise ValueError("liquidity breach week does not match the forecast grid")

        note_codes = [note.code for note in self.notes]
        if len(note_codes) != len(set(note_codes)):
            raise ValueError("deterministic note codes must be unique")
        return self


class ForecastGridExport(ForecastGridExportContent):
    """Persisted immutable envelope; only ``ForecastGridExportContent`` is hashed."""

    export_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    organization_id: int = Field(gt=0)
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def _capture_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    def content(self) -> ForecastGridExportContent:
        fields = ForecastGridExportContent.model_fields
        return ForecastGridExportContent.model_validate(
            self.model_dump(include=set(fields), mode="python")
        )

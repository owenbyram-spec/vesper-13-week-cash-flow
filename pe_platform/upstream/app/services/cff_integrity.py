# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-5 (F-4 #5 — CFF integrity).
"""Cash-flow-forecast integrity — categorized-but-unmapped entity detection (W-5, F-4).

THE GAP (F-4, live-confirmed): categorizing a bank transaction to a customer/vendor that has
no forecast row makes the money silently vanish from the 13-week forecast. The display-time
auto-create (``_apply_live_layer`` Phase 2b in ``app/routers/cashflow_forecast.py``) only
covers actuals whose ``week_ending`` sits inside the CURRENT lookback window — the row it
builds is ephemeral (per-request, zero forward projection), so the entity disappears the
moment the window rolls past its transactions.

An entity is VISIBLE in the forecast when it either
  • has a row in the stored ``CashForecastSnapshot`` grid (``customer_rows``/``vendor_rows``), or
  • was user-created via the DAY/AVG sentinel overrides (the existing add-row path),
and is NOT hidden (the HIDDEN sentinel — hiding filters the row out even when a snapshot
row exists).

Two consumers, one predicate:
  • ``list_unmapped_categorized_entities`` — the per-realm detection feed for the CFF page
    banner (GET /cashflow-forecast/integrity/unmapped). HIDDEN entities are EXCLUDED from
    this list: hiding is an explicit modeling decision the user already owns — the ambient
    banner must not nag against it.
  • ``ForecastRowIndex.is_visible`` — the point-of-action check behind the additive
    ``cff_unmapped`` flag on POST /actuals/categorize(+bulk). This one DOES flag hidden
    entities: at categorize-time the user is actively routing money to the entity, and the
    truthful statement is "this won't appear in the cash forecast."

No silent auto-create anywhere — the user owns the modeling decision (plan W-5, option C+B;
option A rejected).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TypedDict

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..cashflow.projection import (
    AVG_SENTINEL,
    DAY_SENTINEL,
    HIDDEN_SENTINEL,
)
from ..models import CashForecastActual, CashForecastOverride, CashForecastSnapshot

# Names that are grid plumbing, not real customer/vendor entities — never report these.
SKIP_ENTITY_NAMES: set[str] = {
    "Other Cash In",
    "Non Routine Expenses",
    "__excluded",
    "__beginning_balance",
}

# Detection horizon default: how far back categorized actuals are scanned. Bounded so the
# query stays index-served; wide enough that a row lost weeks ago still surfaces.
DEFAULT_SINCE_WEEKS = 26


class UnmappedEntity(TypedDict):
    """One categorized-but-unmapped entity — the banner row the frontend renders."""

    entity_name: str
    entity_type: str  # "customer" | "vendor"
    transaction_count: int
    total_amount: float
    first_week_ending: str | None  # ISO date
    last_week_ending: str | None  # ISO date


@dataclass
class ForecastRowIndex:
    """The per-company sets that decide whether an entity is visible in the forecast."""

    customer_names: set[str] = field(default_factory=set)
    vendor_names: set[str] = field(default_factory=set)
    hidden: set[tuple[str, str]] = field(default_factory=set)  # (entity_name, entity_type)

    def has_row(self, entity_name: str, entity_type: str) -> bool:
        """Snapshot row OR user-created (DAY/AVG sentinel) row — ignores hidden state."""
        if entity_type == "customer":
            return entity_name in self.customer_names
        if entity_type == "vendor":
            return entity_name in self.vendor_names
        return True  # non-customer/vendor types (balance, other_cash_in, …) are not entity rows

    def is_hidden(self, entity_name: str, entity_type: str) -> bool:
        return (entity_name, entity_type) in self.hidden

    def is_visible(self, entity_name: str, entity_type: str) -> bool:
        """True when the entity will actually render in the forecast grid."""
        return self.has_row(entity_name, entity_type) and not self.is_hidden(
            entity_name, entity_type
        )


def load_forecast_row_index(db: Session, company_id: str) -> ForecastRowIndex:
    """Build the visibility index for a company: snapshot row names + sentinel-created
    entities + hidden entities. One snapshot read + one sentinel-only override read."""
    index = ForecastRowIndex()

    snapshot: CashForecastSnapshot | None = (
        db.query(CashForecastSnapshot)
        .filter(CashForecastSnapshot.company_id == company_id)
        .first()
    )
    if snapshot is not None and snapshot.snapshot_data:
        data = snapshot.snapshot_data  # stored JSON grid; row shape owned by the CFF builder
        index.customer_names.update(
            row.get("name") for row in (data.get("customer_rows") or []) if row.get("name")
        )
        index.vendor_names.update(
            row.get("name") for row in (data.get("vendor_rows") or []) if row.get("name")
        )

    sentinel_rows: list[CashForecastOverride] = (
        db.query(CashForecastOverride)
        .filter(
            CashForecastOverride.company_id == company_id,
            CashForecastOverride.week_ending.in_(
                [DAY_SENTINEL, AVG_SENTINEL, HIDDEN_SENTINEL]
            ),
        )
        .all()
    )
    for row in sentinel_rows:
        week_ending = row.week_ending
        if hasattr(week_ending, "date"):
            week_ending = week_ending.date()
        if week_ending == HIDDEN_SENTINEL:
            index.hidden.add((row.entity_name, row.entity_type))
        elif row.entity_type == "customer":
            index.customer_names.add(row.entity_name)
        elif row.entity_type == "vendor":
            index.vendor_names.add(row.entity_name)
    return index


def list_unmapped_categorized_entities(
    db: Session,
    company_id: str,
    *,
    since_weeks: int = DEFAULT_SINCE_WEEKS,
) -> list[UnmappedEntity]:
    """Entities (customer/vendor) with categorized bank actuals in the trailing
    *since_weeks* window but NO visible forecast row — the exact F-4 silent-drop set.

    ``category == "categorized"`` matches precisely the rows the forecast reconciliation
    consumes: top-level categorized actuals AND decomposed Bill.com children (decomposed
    parents carry category="decomposed" and excluded rows "excluded" — both out).
    HIDDEN entities are excluded here (explicit user decision; see module docstring).
    Ordered by total amount at stake, descending — biggest silent hole first.
    """
    index = load_forecast_row_index(db, company_id)
    window_floor = date.today() - timedelta(weeks=since_weeks)

    grouped = (
        db.query(
            CashForecastActual.entity_name,
            CashForecastActual.entity_type,
            func.count(CashForecastActual.id).label("transaction_count"),
            func.sum(CashForecastActual.amount).label("total_amount"),
            func.min(CashForecastActual.week_ending).label("first_week_ending"),
            func.max(CashForecastActual.week_ending).label("last_week_ending"),
        )
        .filter(
            CashForecastActual.company_id == company_id,
            CashForecastActual.category == "categorized",
            CashForecastActual.entity_type.in_(["customer", "vendor"]),
            CashForecastActual.entity_name.isnot(None),
            CashForecastActual.entity_name.notin_(SKIP_ENTITY_NAMES),
            CashForecastActual.week_ending >= window_floor,
        )
        .group_by(CashForecastActual.entity_name, CashForecastActual.entity_type)
        .all()
    )

    unmapped: list[UnmappedEntity] = []
    for row in grouped:
        entity_name: str = row.entity_name
        entity_type: str = row.entity_type
        if index.has_row(entity_name, entity_type):
            continue
        if index.is_hidden(entity_name, entity_type):
            continue  # explicit hide — the banner respects the user's modeling decision
        first_week = row.first_week_ending
        last_week = row.last_week_ending
        if hasattr(first_week, "date"):
            first_week = first_week.date()
        if hasattr(last_week, "date"):
            last_week = last_week.date()
        unmapped.append(
            UnmappedEntity(
                entity_name=entity_name,
                entity_type=entity_type,
                transaction_count=int(row.transaction_count or 0),
                total_amount=round(float(row.total_amount or 0.0), 2),
                first_week_ending=first_week.isoformat() if first_week else None,
                last_week_ending=last_week.isoformat() if last_week else None,
            )
        )

    unmapped.sort(key=lambda item: item["total_amount"], reverse=True)
    return unmapped

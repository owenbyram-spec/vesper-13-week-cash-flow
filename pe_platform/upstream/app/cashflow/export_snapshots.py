"""Canonical mapping and isolated persistence for cash-flow forecast exports."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Organization, OrganizationCompany
from ..tenant_rls import tenant_scope_live
from .schemas import (
    FORECAST_EXPORT_SCHEMA_VERSION,
    ForecastBeyondHorizon,
    ForecastDataQuality,
    ForecastExportError,
    ForecastExportNote,
    ForecastExportWeek,
    ForecastGridExport,
    ForecastGridExportContent,
    ForecastGridRow,
    ForecastLiquidity,
    ForecastReconciliationDelta,
    ForecastUnmatched,
    ForecastUnmatchedEntity,
    ForecastUnmatchedReason,
)


class ExportSnapshotError(RuntimeError):
    """Base class for fail-closed export snapshot failures."""


class ExportSnapshotNotReadyError(ExportSnapshotError):
    pass


class ExportSnapshotNotFoundError(ExportSnapshotError):
    pass


class ExportSnapshotContextMismatchError(ExportSnapshotError):
    pass


class ExportSnapshotSchemaUnsupportedError(ExportSnapshotError):
    pass


class ExportSnapshotIntegrityError(ExportSnapshotError):
    pass


class _SnapshotRow(Protocol):
    id: object
    organization_id: object
    company_id: object
    schema_version: object
    content_hash: object
    payload: object
    captured_at: object


STATUS_LABELS: dict[str, str] = {
    "not_pulled": "Needs pull",
    "pending_categorization": "Pending",
    "pulled": "Pulled",
    "closed": "Closed",
    "reconciled": "Reconciled",
}


def _snapshot_model():
    # Lazy because the model and this service are landed by separate Wave-3 owners.
    from ..models import CashForecastExportSnapshot

    return CashForecastExportSnapshot


def canonical_content_bytes(content: ForecastGridExportContent) -> bytes:
    """Return the one stable UTF-8 JSON representation used for identity."""
    return json.dumps(
        content.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_content_hash(content: ForecastGridExportContent) -> str:
    return hashlib.sha256(canonical_content_bytes(content)).hexdigest()


def _required_sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    value = payload.get(key)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"weekly payload field {key!r} must be an ordered vector")
    return value


def _money_vector(payload: Mapping[str, object], key: str, size: int) -> tuple[object, ...]:
    values = tuple(_required_sequence(payload, key))
    if len(values) != size:
        raise ValueError(f"weekly payload field {key!r} has the wrong vector length")
    return values


def _stable_entity_identity(
    row: Mapping[str, object],
    *,
    entity_type: str,
    name: str,
    occurrence: int,
) -> tuple[str, str]:
    for key in ("entity_key", "entity_id", "qbo_id", "id"):
        value = row.get(key)
        if value is not None and str(value):
            entity_key = str(value)
            return entity_key, f"{entity_type}:{entity_key}"
    seed = f"{entity_type}\x00{name}\x00{occurrence}".encode("utf-8")
    entity_key = hashlib.sha256(seed).hexdigest()[:24]
    return entity_key, f"{entity_type}:{entity_key}"


def _entity_rows(
    raw_rows: object,
    *,
    entity_type: str,
    week_count: int,
) -> list[ForecastGridRow]:
    if not isinstance(raw_rows, (list, tuple)):
        raise ValueError(f"weekly payload {entity_type} rows must be ordered")
    occurrences: dict[str, int] = {}
    rows: list[ForecastGridRow] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError(f"weekly payload {entity_type} row must be an object")
        if "name" not in raw:
            raise ValueError(f"weekly payload {entity_type} row is missing name")
        name = str(raw["name"])
        occurrence = occurrences.get(name, 0)
        occurrences[name] = occurrence + 1
        entity_key, row_id = _stable_entity_identity(
            raw,
            entity_type=entity_type,
            name=name,
            occurrence=occurrence,
        )
        amounts = raw.get("amounts")
        if not isinstance(amounts, (list, tuple)) or len(amounts) != week_count:
            raise ValueError(f"weekly payload {entity_type} row has the wrong amount length")
        effective_day = raw.get("effective_day", raw.get("day_of_month"))
        effective_avg = raw.get("effective_avg", raw.get("avg_amount"))
        rows.append(
            ForecastGridRow(
                row_id=row_id,
                row_kind=entity_type,
                label=name,
                entity_type=entity_type,
                entity_key=entity_key,
                effective_day=effective_day,
                effective_avg=effective_avg,
                amounts=tuple(amounts),
            )
        )
    return rows


def _error_disclosures(raw_errors: object) -> tuple[ForecastExportError, ...]:
    if raw_errors is None:
        return ()
    if not isinstance(raw_errors, (list, tuple)):
        raise ValueError("weekly payload errors must be an ordered vector")
    errors: list[ForecastExportError] = []
    for raw in raw_errors:
        if not isinstance(raw, Mapping):
            raise ValueError("weekly payload error must be an object")
        errors.append(
            ForecastExportError(
                error_code=raw.get("error_code"),
                user_message=raw.get("user_message"),
                retryable=raw.get("retryable", False),
                support_hint=raw.get("support_hint"),
            )
        )
    return tuple(errors)


def _unmatched_disclosure(raw: object) -> ForecastUnmatched:
    if not isinstance(raw, Mapping):
        raise ValueError("weekly payload unmatched disclosure must be an object")
    raw_reasons = raw.get("reasons") or {}
    if not isinstance(raw_reasons, Mapping):
        raise ValueError("weekly payload unmatched reasons must be an object")
    reasons: dict[str, ForecastUnmatchedReason] = {}
    for key, value in raw_reasons.items():
        if not isinstance(value, Mapping):
            raise ValueError("weekly payload unmatched reason must be an object")
        reasons[str(key)] = ForecastUnmatchedReason(
            count=value.get("count", 0),
            total_amount=value.get("total_amount", 0),
        )
    raw_entities = raw.get("entities") or []
    if not isinstance(raw_entities, (list, tuple)):
        raise ValueError("weekly payload unmatched entities must be ordered")
    entities = tuple(
        ForecastUnmatchedEntity.model_validate(entity)
        for entity in raw_entities
    )
    return ForecastUnmatched(
        count=raw.get("count", 0),
        reasons=reasons,
        entities=entities,
    )


def _status_weeks(
    payload: Mapping[str, object], week_dates: tuple[date, ...], lookback: int
) -> tuple[ForecastExportWeek, ...]:
    raw_statuses = payload.get("week_statuses") or []
    if not isinstance(raw_statuses, (list, tuple)):
        raise ValueError("weekly payload week_statuses must be ordered")
    status_by_date: dict[date, str] = {}
    for raw in raw_statuses:
        if not isinstance(raw, Mapping):
            raise ValueError("weekly payload week status must be an object")
        week = date.fromisoformat(str(raw.get("week_ending"))[:10])
        if week in status_by_date:
            raise ValueError("weekly payload has duplicate week statuses")
        status = str(raw.get("status"))
        if status not in STATUS_LABELS:
            raise ValueError(f"unsupported weekly status {status!r}")
        status_by_date[week] = STATUS_LABELS[status]
    expected_lookback_dates = set(week_dates[:lookback])
    if set(status_by_date) != expected_lookback_dates:
        raise ValueError("weekly statuses must cover exactly the lookback weeks")
    return tuple(
        ForecastExportWeek(
            week_ending=week,
            is_lookback=index < lookback,
            status=status_by_date.get(week),
        )
        for index, week in enumerate(week_dates)
    )


def _money_text(value: Decimal) -> str:
    value = Decimal("0.00") if value == 0 else value
    absolute = f"${abs(value):,.2f}"
    return f"({absolute})" if value < 0 else absolute


def _deterministic_notes(
    *,
    snapshot_state: str,
    data_quality: ForecastDataQuality,
    beginning_row: ForecastGridRow,
    ending_row: ForecastGridRow,
    weeks: tuple[ForecastExportWeek, ...],
    lookback: int,
    forecast_reanchored: bool,
    reconciliation: ForecastReconciliationDelta | None,
) -> tuple[ForecastExportNote, ...]:
    notes = [
        ForecastExportNote(
            code="freshness",
            text=(
                "Snapshot state: ready."
                if snapshot_state == "ready"
                else "Snapshot state: stale; values reproduce the stale grid shown."
            ),
        ),
        ForecastExportNote(
            code="data_quality",
            text=(
                "Data quality: partial; review the disclosed unavailable sections."
                if data_quality.is_partial
                else "Data quality: complete for the disclosed forecast inputs."
            ),
        ),
    ]
    opening = beginning_row.amounts[0]
    if opening is not None:
        notes.append(
            ForecastExportNote(code="opening_cash", text=f"Opening cash: {_money_text(opening)}.")
        )

    forecast_candidates = [
        (index, amount)
        for index, amount in enumerate(ending_row.amounts[lookback:], start=lookback)
        if amount is not None
    ]
    if forecast_candidates:
        minimum_index, minimum_amount = min(forecast_candidates, key=lambda item: item[1])
        notes.append(
            ForecastExportNote(
                code="lowest_forecast_balance",
                text=(
                    f"Lowest forecast balance: {_money_text(minimum_amount)} on "
                    f"{weeks[minimum_index].week_ending.isoformat()}."
                ),
            )
        )
        negative = next(
            ((index, amount) for index, amount in forecast_candidates if amount < 0),
            None,
        )
        if negative is not None:
            negative_index, _ = negative
            notes.append(
                ForecastExportNote(
                    code="weeks_to_negative",
                    text=(
                        f"Weeks to negative cash: {negative_index - lookback}; first negative "
                        f"week ends {weeks[negative_index].week_ending.isoformat()}."
                    ),
                )
            )
    if forecast_reanchored and reconciliation is not None:
        notes.append(
            ForecastExportNote(
                code="reanchor",
                text=(
                    "The first forecast week was re-anchored to the authoritative control "
                    f"balance of {_money_text(reconciliation.anchor_bank_balance)}; the disclosed "
                    f"lookback discontinuity is {_money_text(reconciliation.delta)}."
                ),
            )
        )
    return tuple(notes)


def content_from_weekly_payload(
    payload: Mapping[str, object],
    *,
    company_id: str,
    company_display_name: str | None,
    snapshot_computed_at: datetime | None = None,
) -> ForecastGridExportContent:
    """Map one finalized served weekly payload to the canonical visible DTO.

    ``company_display_name`` and ``snapshot_computed_at`` are server-owned metadata;
    they are explicit inputs because neither may be looked up again by an exact-ID
    renderer.  A building payload is not capturable and is never turned into a
    zero-filled report.
    """
    snapshot_state = payload.get("snapshot_state")
    if snapshot_state == "building":
        raise ExportSnapshotNotReadyError("a building forecast cannot be captured")
    if snapshot_state not in {"ready", "stale"}:
        raise ValueError("weekly payload snapshot_state must be ready or stale")
    computed_at = snapshot_computed_at or payload.get("snapshot_computed_at")
    if not isinstance(computed_at, (datetime, str)):
        raise ValueError("stable snapshot_computed_at metadata is required")

    week_dates = tuple(
        date.fromisoformat(str(raw)[:10])
        for raw in _required_sequence(payload, "week_headers")
    )
    lookback_raw = payload.get("lookback")
    if isinstance(lookback_raw, bool) or not isinstance(lookback_raw, int):
        raise ValueError("weekly payload lookback must be an integer")
    lookback = lookback_raw
    weeks = _status_weeks(payload, week_dates, lookback)
    week_count = len(weeks)

    customer_rows = _entity_rows(
        payload.get("customers"), entity_type="customer", week_count=week_count
    )
    vendor_rows = _entity_rows(
        payload.get("vendors"), entity_type="vendor", week_count=week_count
    )
    nulls = tuple(None for _ in range(week_count))
    beginning_row = ForecastGridRow(
        row_id="beginning_bank_balance",
        row_kind="beginning_bank_balance",
        label="Beginning Bank Balance",
        amounts=_money_vector(payload, "beginning_balances", week_count),
    )
    ending_row = ForecastGridRow(
        row_id="ending_bank_balance",
        row_kind="ending_bank_balance",
        label="Ending Bank Balance",
        amounts=_money_vector(payload, "ending_balances", week_count),
    )
    rows = (
        beginning_row,
        ForecastGridRow(
            row_id="cash_in_section",
            row_kind="cash_in_section",
            label="Customer Collections (Cash In)",
            amounts=nulls,
        ),
        *customer_rows,
        ForecastGridRow(
            row_id="other_cash_in",
            row_kind="other_cash_in",
            label="Other Cash In",
            amounts=_money_vector(payload, "other_cash_in", week_count),
        ),
        ForecastGridRow(
            row_id="total_cash_in",
            row_kind="total_cash_in",
            label="Total Cash In",
            amounts=_money_vector(payload, "total_cash_in", week_count),
        ),
        ForecastGridRow(
            row_id="cash_out_section",
            row_kind="cash_out_section",
            label="Vendor Obligations (Cash Out)",
            amounts=nulls,
        ),
        *vendor_rows,
        ForecastGridRow(
            row_id="non_routine_expenses",
            row_kind="non_routine_expenses",
            label="Non Routine Expenses",
            amounts=_money_vector(payload, "other_cash_out", week_count),
        ),
        ForecastGridRow(
            row_id="total_cash_out",
            row_kind="total_cash_out",
            label="Total Cash Out",
            amounts=_money_vector(payload, "total_cash_out", week_count),
        ),
        ending_row,
    )

    raw_data_quality = payload.get("data_quality")
    if not isinstance(raw_data_quality, Mapping):
        raise ValueError("weekly payload data_quality disclosure is required")
    data_quality = ForecastDataQuality(
        is_partial=raw_data_quality.get("is_partial", False),
        unavailable_sections=tuple(raw_data_quality.get("unavailable_sections") or ()),
    )
    raw_reconciliation = payload.get("reconciliation_delta")
    reconciliation = (
        ForecastReconciliationDelta.model_validate(raw_reconciliation)
        if raw_reconciliation is not None
        else None
    )
    raw_beyond = payload.get("beyond_horizon")
    beyond = (
        ForecastBeyondHorizon.model_validate(raw_beyond)
        if raw_beyond is not None
        else None
    )
    raw_liquidity = payload.get("liquidity")
    liquidity = (
        ForecastLiquidity.model_validate(raw_liquidity)
        if raw_liquidity is not None
        else None
    )
    reanchored = payload.get("forecast_reanchored_to_control")
    if not isinstance(reanchored, bool):
        raise ValueError("weekly payload re-anchor flag must be boolean")
    notes = _deterministic_notes(
        snapshot_state=str(snapshot_state),
        data_quality=data_quality,
        beginning_row=beginning_row,
        ending_row=ending_row,
        weeks=weeks,
        lookback=lookback,
        forecast_reanchored=reanchored,
        reconciliation=reconciliation,
    )

    raw_anchor = payload.get("anchor_diagnostics")
    if raw_anchor is not None and not isinstance(raw_anchor, dict):
        raise ValueError("weekly payload anchor_diagnostics must be an object or null")
    raw_haircuts = payload.get("collection_haircuts")
    if raw_haircuts is not None and not isinstance(raw_haircuts, dict):
        raise ValueError("weekly payload collection_haircuts must be an object or null")

    return ForecastGridExportContent(
        company_id=str(company_id),
        company_display_name=(
            company_display_name
            if company_display_name is not None and company_display_name.strip()
            else str(company_id)
        ),
        as_of_week_ending=week_dates[lookback],
        lookback=lookback,
        snapshot_computed_at=computed_at,
        snapshot_state=snapshot_state,
        weeks=weeks,
        rows=rows,
        bank_balance=payload.get("bank_balance"),
        bank_balance_as_of=payload.get("bank_balance_as_of"),
        bank_balance_source=str(payload.get("bank_balance_source") or "unknown"),
        opening_balance_week_ending=payload.get("opening_balance_week_ending"),
        beginning_balance_override=payload.get("beginning_balance_override"),
        anchor_diagnostics=raw_anchor,
        reconciliation_delta=reconciliation,
        forecast_reanchored_to_control=reanchored,
        uncategorized_actuals=payload.get("uncategorized_actuals", 0),
        review_required_weeks=tuple(payload.get("review_required_weeks") or ()),
        past_due_in=payload.get("past_due_in", 0),
        past_due_out=payload.get("past_due_out", 0),
        errors=_error_disclosures(payload.get("errors")),
        data_quality=data_quality,
        collection_haircuts=raw_haircuts,
        unmatched=_unmatched_disclosure(payload.get("unmatched")),
        beyond_horizon=beyond,
        liquidity=liquidity,
        notes=notes,
    )


def resolve_company_display_name(
    db: Session, *, organization_id: int, company_id: str
) -> str:
    """Resolve and verify the authoritative local company→organization mapping."""
    company = (
        db.query(OrganizationCompany)
        .filter(
            OrganizationCompany.organization_id == organization_id,
            OrganizationCompany.realm_id == str(company_id),
        )
        .first()
    )
    if company is not None:
        candidate = company.display_name or company.billing_name
        return candidate if candidate is not None and candidate.strip() else str(company_id)
    organization = (
        db.query(Organization)
        .filter(
            Organization.id == organization_id,
            Organization.primary_realm_id == str(company_id),
        )
        .first()
    )
    if organization is not None:
        return str(company_id)
    raise PermissionError("company is not mapped to the authorized organization")


def _utc_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ExportSnapshotIntegrityError(f"persisted {field} is not a datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_forecast_export_from_authorized_row(
    row: _SnapshotRow,
    *,
    expected_company_id: str | None = None,
    expected_organization_id: int | None = None,
) -> ForecastGridExport:
    """Pure integrity check after the router has authorized the row's owner."""
    try:
        row_schema = int(row.schema_version)
    except (TypeError, ValueError) as exc:
        raise ExportSnapshotIntegrityError("persisted schema version is invalid") from exc
    if row_schema != FORECAST_EXPORT_SCHEMA_VERSION:
        raise ExportSnapshotSchemaUnsupportedError(
            f"cash-flow export schema {row_schema} is unsupported"
        )
    if not isinstance(row.payload, dict):
        raise ExportSnapshotIntegrityError("persisted export payload is not an object")
    try:
        row_organization_id = int(row.organization_id)
    except (TypeError, ValueError) as exc:
        raise ExportSnapshotIntegrityError("persisted organization ID is invalid") from exc
    payload_schema = row.payload.get("schema_version")
    if payload_schema != row_schema:
        raise ExportSnapshotIntegrityError("persisted row and envelope schema disagree")
    try:
        export = ForecastGridExport.model_validate(row.payload)
    except Exception as exc:
        raise ExportSnapshotIntegrityError("persisted export envelope failed strict validation") from exc

    row_id = str(row.id)
    if str(export.export_id) != row_id:
        raise ExportSnapshotIntegrityError("persisted row and envelope IDs disagree")
    if str(row.company_id) != export.company_id:
        raise ExportSnapshotIntegrityError("persisted row and envelope companies disagree")
    if str(row.content_hash) != export.content_hash:
        raise ExportSnapshotIntegrityError("persisted row and envelope hashes disagree")
    if canonical_content_hash(export.content()) != export.content_hash:
        raise ExportSnapshotIntegrityError("persisted export content hash does not verify")
    if _utc_datetime(row.captured_at, field="captured_at") != export.captured_at:
        raise ExportSnapshotIntegrityError("persisted row and envelope capture times disagree")
    if expected_company_id is not None and export.company_id != str(expected_company_id):
        raise ExportSnapshotContextMismatchError("export belongs to a different company")
    if (
        expected_organization_id is not None
        and row_organization_id != expected_organization_id
    ):
        raise ExportSnapshotNotFoundError("export snapshot not found")
    # Organization ownership is deliberately outside the content hash and is the
    # one row field the audited company-reassignment lifecycle may update.  The
    # immutable persisted envelope can therefore carry the capture-time owner;
    # after entity-owner authorization, return the row's current owner without
    # rewriting any financial/content identity bytes.
    if export.organization_id != row_organization_id:
        export = export.model_copy(update={"organization_id": row_organization_id})
    return export


def _query_identity(session: Session, content: ForecastGridExportContent):
    model = _snapshot_model()
    return (
        session.query(model)
        .filter(
            model.company_id == content.company_id,
            model.schema_version == content.schema_version,
            model.content_hash == canonical_content_hash(content),
        )
        .first()
    )


def capture_forecast_export(
    content: ForecastGridExportContent,
    *,
    organization_id: int,
    session_factory: Callable[[], Session] | None = None,
    captured_at: datetime | None = None,
) -> ForecastGridExport:
    """Durably materialize/reuse content in an independently owned transaction."""
    factory = session_factory or SessionLocal
    session = factory()
    try:
        with tenant_scope_live(
            session,
            org_id=organization_id,
            realm_id=content.company_id,
        ):
            authoritative_name = resolve_company_display_name(
                session,
                organization_id=organization_id,
                company_id=content.company_id,
            )
            if authoritative_name != content.company_display_name:
                raise PermissionError("export company display metadata is not authoritative")

            existing = _query_identity(session, content)
            if existing is not None:
                export = load_forecast_export_from_authorized_row(
                    existing,
                    expected_company_id=content.company_id,
                    expected_organization_id=organization_id,
                )
                session.commit()
                return export

            capture_time = captured_at or datetime.now(timezone.utc)
            if capture_time.tzinfo is None or capture_time.utcoffset() is None:
                raise ValueError("captured_at must be timezone-aware")
            capture_time = capture_time.astimezone(timezone.utc)
            export = ForecastGridExport(
                **content.model_dump(mode="python"),
                export_id=uuid4(),
                content_hash=canonical_content_hash(content),
                organization_id=organization_id,
                captured_at=capture_time,
            )
            model = _snapshot_model()
            row = model(
                id=export.export_id,
                organization_id=organization_id,
                company_id=content.company_id,
                schema_version=content.schema_version,
                content_hash=export.content_hash,
                payload=export.model_dump(mode="json"),
                captured_at=capture_time,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                winner = _query_identity(session, content)
                if winner is None:
                    raise ExportSnapshotIntegrityError(
                        "concurrent export winner is not visible in authorized scope"
                    )
                export = load_forecast_export_from_authorized_row(
                    winner,
                    expected_company_id=content.company_id,
                    expected_organization_id=organization_id,
                )
            session.commit()
            return export
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def load_forecast_export(
    export_id: UUID | str,
    *,
    organization_id: int,
    expected_company_id: str,
    session_factory: Callable[[], Session] | None = None,
) -> ForecastGridExport:
    """Load one exact ID inside the authorized tenant scope and verify its bytes."""
    try:
        normalized_id = UUID(str(export_id))
    except (TypeError, ValueError) as exc:
        raise ExportSnapshotNotFoundError("export snapshot not found") from exc
    factory = session_factory or SessionLocal
    session = factory()
    try:
        with tenant_scope_live(
            session,
            org_id=organization_id,
            realm_id=expected_company_id,
        ):
            model = _snapshot_model()
            row = (
                session.query(model)
                .filter(
                    model.id == normalized_id,
                    model.organization_id == organization_id,
                )
                .first()
            )
            if row is None:
                raise ExportSnapshotNotFoundError("export snapshot not found")
            export = load_forecast_export_from_authorized_row(
                row,
                expected_company_id=expected_company_id,
                expected_organization_id=organization_id,
            )
            session.rollback()
            return export
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "ExportSnapshotContextMismatchError",
    "ExportSnapshotError",
    "ExportSnapshotIntegrityError",
    "ExportSnapshotNotFoundError",
    "ExportSnapshotNotReadyError",
    "ExportSnapshotSchemaUnsupportedError",
    "canonical_content_bytes",
    "canonical_content_hash",
    "capture_forecast_export",
    "content_from_weekly_payload",
    "load_forecast_export",
    "load_forecast_export_from_authorized_row",
    "resolve_company_display_name",
]

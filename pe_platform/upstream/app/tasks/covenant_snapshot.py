# PLAN-EXEMPT: AC-6/AC-7 two-lens covenant snapshot + dual alerts under ratified plan 20260630-covenant-redesign; surface_scope enumerates "app/tasks/" (the dir) but the gate matches exact paths, so this task file (named in the Wave-4 dispatch) takes the audited doorway.
"""Monthly two-lens covenant snapshot + dual transition alerting (covenant-redesign Wave 4).

Beat-scheduled fan-out (AP-CEL-9): ``snapshot_all_covenants`` enumerates mapped realms under
bypass and dispatches one realm-scoped child ``snapshot_realm_covenants`` each (mirrors
``capture_all_metric_snapshots`` / ``build_all_realm_contexts``), so a single realm's QBO work
stays well under the broker visibility_timeout.

Each child, for ONE realm and the last CLOSED month, snapshots each active ``DebtCovenant`` via
the redesigned TWO-LENS engine (``app/services/covenant_compute.compute_monthly_series``):

  AC-6  writes ONE ``CovenantRecord`` per active covenant (period = the closed month). BOTH
        lenses are persisted with NO migration in the existing JSON ``calculation_context``:
          • the rolling-TTM lens (the true-default test) → ``actual_value`` + ``status``
          • the per-month run-rate lens (early warning) → ``calculation_context["run_rate"]``
        Idempotent on (covenant_id, period): a re-run UPDATES the same row (safe under
        acks_late at-least-once delivery), never a duplicate.

  AC-7  fires DUAL alerts on a lens TRANSITION vs the prior period's stored per-lens verdict:
          • a RUN-RATE crossing INTO danger (WARN/BREACH) → ONE ``COVENANT_RUN_RATE_WARNING``
          • a TTM crossing INTO breach                    → ONE ``COVENANT_BREACH``
        Deduped per (covenant, period, lens): the two lenses use DISTINCT ``alert_type`` values,
        and the watchdog fingerprint keys on (alert_type, period, related_entity_id) — so a
        steady-state period (danger→danger, breach→breach) does NOT re-fire (no alert storms).

The child pins the tenant RLS scope (``tenant_scope_for_realm``) so the org-keyed reads/writes
pass FORCE RLS, and every query carries its company_id predicate.
"""
from __future__ import annotations

import logging
import random
# PLAN-EXEMPT: CI-red hotfix (Steven-directed) — datetime/timezone for the explicit
# updated_at touch in _apply_snapshot_fields (freshness-window re-arm).
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import requests
from sqlalchemy.orm import Session
from sqlalchemy.exc import DisconnectionError, OperationalError, TimeoutError as SQLAlchemyTimeoutError

from ..celery_app import celery_app
from ..db import SessionLocal
from ..qbo_errors import (
    QBO_NOT_CONNECTED_CODE,
    QBO_RECONNECT_REQUIRED_CODE,
    is_qbo_connection_required_exception,
)
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

if TYPE_CHECKING:  # typing-only imports — avoid an import cycle at runtime
    from ..models import DebtCovenant
    from ..services.covenant_compute import CovenantSeries

logger = logging.getLogger(__name__)

# A WARN or BREACH verdict is a "danger" state — the set whose ENTRY (a non-danger → danger
# transition) triggers the run-rate EARLY-WARNING alert.
_DANGER_STATES = {"WARN", "BREACH"}

# Distinct alert types = the per-lens dedupe axis (AC-7): the watchdog fingerprint keys on
# (alert_type, period, related_entity_id), so two lenses on the same covenant+period never collide.
_ALERT_TTM_BREACH = "COVENANT_BREACH"
_ALERT_RUN_RATE_WARNING = "COVENANT_RUN_RATE_WARNING"

# No migration is needed for the interactive warm lifecycle: every (covenant, period) row already
# owns a JSON ``calculation_context``.  This nested block is the tenant-bound job ledger shared by
# GET /covenants/status and this worker.  The outer snapshot fields remain the existing financial
# contract; this block carries only progress/correlation/error metadata.
COVENANT_COMPUTATION_KEY = "computation"
COMPUTATION_COMPUTING = "COMPUTING"
COMPUTATION_POPULATED = "POPULATED"
COMPUTATION_NO_DATA = "NO_DATA"
COMPUTATION_ERROR = "ERROR"

_QBO_CONNECTION_RECOVERY_CODES = {
    "qbo_connection_missing",
    QBO_NOT_CONNECTED_CODE,
    QBO_RECONNECT_REQUIRED_CODE,
}


class CovenantSnapshotProviderError(RuntimeError):
    """A classified provider/compute failure that either retries or becomes visible ERROR."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool,
        reason_code: str = "covenant_compute_failed",
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.reason_code = reason_code


class CovenantSnapshotPersistenceError(RuntimeError):
    """A snapshot write could not be proven under the current tenant scope."""


def _provider_exception_is_transient(exc: Exception) -> bool:
    """Classify known network/rate-limit/server failures, including wrapped provider causes."""
    from fastapi import HTTPException

    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, requests.HTTPError):
            status_code = getattr(getattr(current, "response", None), "status_code", None)
            if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
                return True
        elif isinstance(current, requests.RequestException):
            return True
        elif isinstance(current, (TimeoutError, ConnectionError)):
            return True
        elif isinstance(current, HTTPException):
            detail = current.detail if isinstance(current.detail, dict) else {}
            if bool(detail.get("retryable")) and (
                current.status_code == 429 or current.status_code >= 500
            ):
                return True
        current = current.__cause__ or current.__context__
    return False


def _qbo_connection_reason_code(exc: Exception) -> str | None:
    """Return a structured QBO connection-precondition code through wrapped causes."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        if is_qbo_connection_required_exception(current):
            detail = current.detail if isinstance(current.detail, dict) else {}
            code = detail.get("error_code")
            return str(code) if code else None
        current = current.__cause__ or current.__context__
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _computation_metadata(rec) -> dict | None:
    """Return a defensive copy of one record's persisted warm metadata, if present."""
    context = getattr(rec, "calculation_context", None)
    if not isinstance(context, dict):
        return None
    metadata = context.get(COVENANT_COMPUTATION_KEY)
    return dict(metadata) if isinstance(metadata, dict) else None


def _with_computation_metadata(context: dict | None, metadata: dict) -> dict:
    """Preserve financial snapshot context while replacing only the warm-state block."""
    return {**(context if isinstance(context, dict) else {}), COVENANT_COMPUTATION_KEY: metadata}


# ─────────────────────────────────────────────────────────────────────────────
# Period helpers
# ─────────────────────────────────────────────────────────────────────────────

def period_for_date(today: date | None = None) -> str:
    """The closed month a snapshot is taken for: the last fully-closed calendar month,
    ``YYYY-MM``. ``today`` is injectable for deterministic tests."""
    today = today or date.today()
    last_month_end = date(today.year, today.month, 1) - timedelta(days=1)
    return f"{last_month_end.year}-{last_month_end.month:02d}"


def _prior_period(period: str) -> str:
    """The ``YYYY-MM`` immediately before ``period``."""
    year, month = (int(p) for p in period.split("-"))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# Two-lens compute (reuses the redesigned compute_monthly_series verbatim)
# ─────────────────────────────────────────────────────────────────────────────

def _status_of(
    value: float | None, cov: "DebtCovenant", *, not_meaningful: str | None = None,
) -> str:
    """The tri-state verdict for a ratio value against this covenant's threshold + cushion.
    Reuses the SAME ``/covenants/status`` helpers so a snapshot verdict ties to what /status shows.
    ``not_meaningful`` (EBITDA <= 0 on a leverage ratio) makes a maximum covenant BREACH."""
    from ..routers.covenants import _compute_status, _cov_warn_cushion

    return _compute_status(
        value, float(cov.threshold), cov.breach_direction, cushion=_cov_warn_cushion(cov),
        not_meaningful=not_meaningful,
    )


def _compute_series(
    cov: "DebtCovenant", client: object, company_addbacks: list | None,
    start_date: str, end_date: str,
) -> "CovenantSeries":  # CRAFT-OK: client is the opaque QBOClient provider boundary
    """Compute one two-lens series or raise a retryable provider-boundary error.

    A returned ``CovenantSeries(available=False)`` is a legitimate, serveable NO_DATA result.  A
    missing client or an exception is different: it must not be converted into the old ``lens=None``
    stub and then reported as worker ``ok``.  Raising keeps the task retry contract intact; the
    terminal task arm persists a safe ERROR for the status endpoint.
    """
    from ..services.covenant_compute import compute_monthly_series

    if not client:
        raise CovenantSnapshotProviderError(
            "No QuickBooks connection is available for the covenant snapshot.",
            transient=False,
            reason_code="qbo_connection_missing",
        )
    try:
        return compute_monthly_series(cov, client, company_addbacks, start_date, end_date)
    except Exception as exc:  # noqa: BLE001 — opaque provider boundary; re-raised as a typed task error
        connection_reason = _qbo_connection_reason_code(exc)
        raise CovenantSnapshotProviderError(
            "QuickBooks covenant data could not be computed.",
            transient=_provider_exception_is_transient(exc),
            reason_code=connection_reason or "covenant_compute_failed",
        ) from exc


def _latest_run_rate(series: "CovenantSeries | None") -> tuple[float | None, str | None]:
    """The most-recent month's run-rate lens result as ``(ratio, not_meaningful_code)``.

    A month whose run-rate is not meaningful (EBITDA <= 0 on a leverage ratio) IS that month's
    result — it is returned, never skipped in favour of an older month's passing ratio. Months
    with no data are skipped; (None, None) when no month carries a result."""
    if series is None:
        return None, None
    for month in reversed(series.months):
        if month.run_rate_ratio is not None:
            return month.run_rate_ratio, None
        not_meaningful = getattr(month, "run_rate_not_meaningful", None)
        if not_meaningful:
            return None, not_meaningful
    return None, None


def _build_context(
    series: "CovenantSeries | None",
    ttm_status: str,
    run_rate_status: str,
    run_rate_value: float | None,
    projection: dict | None = None,
    computation: dict | None = None,
    *,
    run_rate_not_meaningful: str | None = None,
) -> dict:
    """The stored ``calculation_context`` — BOTH lenses + the per-month grid, no migration.

    ``projection`` (W-2 F-2, plan 20260702-qa-remediation): the covenant's periods-until-breach
    trend dict (``_covenant_projection``), stored so the snapshot-served /covenants/status needs
    ZERO live QBO calls. None on a legacy write path → /status heals it in place on first read."""
    if computation is None:
        computation = {
            "state": (
                COMPUTATION_POPULATED
                if series is not None and series.available and (
                    series.ttm_ratio is not None or series.ttm_not_meaningful is not None
                )
                else COMPUTATION_NO_DATA
            ),
            "request_id": None,
            "task_id": None,
            "retryable": False,
            "error": None,
            "finished_at": _utc_now_iso(),
        }
    if series is None:
        return {
            "lens": None, "available": False,
            "reason": "Covenant data could not be loaded from QuickBooks.",
            "ttm": {"ratio": None, "status": ttm_status, "overage": None},
            "run_rate": {"ratio": run_rate_value, "status": run_rate_status},
            "months": [],
            "projection": projection,
            COVENANT_COMPUTATION_KEY: computation,
        }
    return {
        "lens": series.lens,
        "threshold": series.threshold,
        "breach_direction": str(series.breach_direction or "").upper(),
        "available": series.available,
        "reason": series.reason,
        "ttm": {
            "ratio": series.ttm_ratio, "status": ttm_status, "overage": series.ttm_overage,
            "not_meaningful": series.ttm_not_meaningful,
        },
        "run_rate": {
            "ratio": run_rate_value, "status": run_rate_status,
            "not_meaningful": run_rate_not_meaningful,
        },
        "months": [m.__dict__ for m in series.months],
        "projection": projection,
        COVENANT_COMPUTATION_KEY: computation,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Prior-verdict lookup + idempotent upsert
# ─────────────────────────────────────────────────────────────────────────────

def _prior_record(db: Session, *, company_id: str, covenant_id: int, prior_period: str):
    """The covenant's stored snapshot for the PRIOR period, or None. Scoped by company_id
    (the realm-string tenant predicate) AND covenant_id."""
    from ..models import CovenantRecord

    return (
        db.query(CovenantRecord)
        .filter(
            CovenantRecord.company_id == company_id,
            CovenantRecord.covenant_id == covenant_id,
            CovenantRecord.period == prior_period,
        )
        .first()
    )


def _prior_lens_status(prior, lens: str) -> str | None:
    """The prior period's stored verdict for ``lens`` ("ttm" | "run_rate"), from the stored
    two-lens ``calculation_context``; None if no prior snapshot exists."""
    if prior is None:
        return None
    ctx = prior.calculation_context or {}
    if lens == "ttm":
        # A prior row's TTM verdict is the row-level ``status`` (the as-tested value).
        return prior.status
    lens_block = ctx.get(lens) if isinstance(ctx, dict) else None
    if isinstance(lens_block, dict):
        return lens_block.get("status")
    return None


def _apply_snapshot_fields(rec, *, value: float | None, status: str, context: dict) -> None:
    """Copy the freshly-computed verdict onto an existing/just-won CovenantRecord row."""
    rec.actual_value = value
    rec.status = status
    rec.is_compliant = status == "PASS"
    rec.calculation_context = context
    # PLAN-EXEMPT: CI-red hotfix (Steven-directed) — touch updated_at explicitly: when the
    # recomputed verdict is byte-identical to the stored one, SQLAlchemy emits NO UPDATE, so
    # the column's onupdate never fires and the row stays past the freshness window forever —
    # every subsequent GET recomputes live (cache thrash) instead of re-arming the snapshot.
    rec.updated_at = datetime.now(timezone.utc)


def _upsert_record(
    db: Session, *, covenant_id: int, company_id: str, period: str,
    value: float | None, status: str, context: dict,
) -> None:
    """Idempotent, RACE-SAFE write of one (covenant, period) snapshot.

    UNIQUE(covenant_id, period) (``uq_covenant_record_covenant_period``) means two concurrent
    inserts of the SAME (covenant, period) — the exact shape of a covenant page load, where
    GET /status and each per-card GET /series both write-back the snapshot — race: the second
    INSERT raises ``IntegrityError`` (UniqueViolation on PG). A plain check-then-insert
    (SELECT .first() → db.add) surfaces that as an unhandled 500. So the INSERT runs inside a
    SAVEPOINT (``begin_nested``, dialect-portable across PG + the sqlite test lane): on the
    conflict we roll back ONLY the savepoint (not the caller's outer transaction), expunge the
    losing instance, re-SELECT the row the winner committed, and UPDATE it in place. Net result:
    no duplicate row, no exception out of this function, last-write-wins on a genuine re-run —
    the same idempotent contract, now concurrency-safe."""
    from sqlalchemy.exc import IntegrityError

    from ..models import CovenantRecord

    def _existing():
        return (
            db.query(CovenantRecord)
            .filter(
                CovenantRecord.company_id == company_id,
                CovenantRecord.covenant_id == covenant_id,
                CovenantRecord.period == period,
            )
            .first()
        )

    existing = _existing()
    if existing is not None:
        _apply_snapshot_fields(existing, value=value, status=status, context=context)
        return

    new_rec = None
    try:
        with db.begin_nested():  # SAVEPOINT — only the INSERT rolls back on a unique-conflict
            # Keep the pending INSERT inside the savepoint. SQLAlchemy flushes all pending state
            # while ENTERING begin_nested(); adding it before the context would let a PostgreSQL
            # UniqueViolation escape before the nested transaction became current and poison the
            # outer transaction.
            new_rec = CovenantRecord(
                covenant_id=covenant_id,
                company_id=company_id,
                period=period,
                actual_value=value,
                status=status,
                is_compliant=status == "PASS",
                calculation_context=context,
            )
            db.add(new_rec)
            db.flush()
    except IntegrityError:
        # A concurrent request already inserted (covenant_id, period). Discard our losing
        # instance, re-read the winner, and UPDATE it — the row still ends up current, and the
        # conflict never propagates as a 500.
        if new_rec is not None and new_rec in db:
            db.expunge(new_rec)
        winner = _existing()
        if winner is not None:
            _apply_snapshot_fields(winner, value=value, status=status, context=context)
        else:
            # A conflict without a visible winner is a scope/persistence anomaly. Reporting this
            # row as written would be a false success and could conceal a tenant-scope defect.
            raise CovenantSnapshotPersistenceError(
                "Concurrent covenant snapshot winner is not visible under the current scope "
                f"(covenant={covenant_id}, period={period})."
            )


def _records_for_warm(
    db: Session, *, company_id: str, period: str, covenant_ids: list[int],
) -> dict[int, object]:
    """Load only this realm+period's holder rows, indexed by covenant id."""
    if not covenant_ids:
        return {}
    from ..models import CovenantRecord

    rows = (
        db.query(CovenantRecord)
        .filter(
            CovenantRecord.company_id == company_id,
            CovenantRecord.period == period,
            CovenantRecord.covenant_id.in_(covenant_ids),
        )
        .all()
    )
    return {int(row.covenant_id): row for row in rows}


def warm_metadata_for_covenant(
    db: Session, *, company_id: str, covenant_id: int, period: str,
) -> dict | None:
    """Read one tenant+period holder's job metadata without consulting Celery's global backend."""
    records = _records_for_warm(
        db, company_id=company_id, period=period, covenant_ids=[covenant_id]
    )
    rec = records.get(covenant_id)
    return _computation_metadata(rec) if rec is not None else None


def mark_warm_computing(
    db: Session,
    *,
    company_id: str,
    period: str,
    covenant_ids: list[int],
    request_id: str | None,
    task_id: str,
    attempt: int = 0,
) -> None:
    """Persist/reconcile the shared COMPUTING holder without erasing prior financial context.

    The caller owns the commit.  The request path commits this mutation *before* enqueue, making
    the task identity observable and making repeated polls dedupe from tenant-scoped database state
    rather than from an unauthenticated Celery-result lookup.
    """
    now = _utc_now_iso()
    records = _records_for_warm(
        db, company_id=company_id, period=period, covenant_ids=covenant_ids
    )
    for covenant_id in covenant_ids:
        rec = records.get(covenant_id)
        prior = _computation_metadata(rec) if rec is not None else None
        metadata = {
            "state": COMPUTATION_COMPUTING,
            "request_id": request_id or (prior or {}).get("request_id"),
            "task_id": task_id,
            "retryable": False,
            "error": None,
            "attempt": attempt,
            "started_at": (prior or {}).get("started_at") or now,
            "updated_at": now,
        }
        if rec is None:
            _upsert_record(
                db,
                covenant_id=covenant_id,
                company_id=company_id,
                period=period,
                value=None,
                status="UNKNOWN",
                context={COVENANT_COMPUTATION_KEY: metadata},
            )
            continue
        rec.calculation_context = _with_computation_metadata(rec.calculation_context, metadata)
        rec.updated_at = datetime.now(timezone.utc)


def mark_warm_error(
    db: Session,
    *,
    company_id: str,
    period: str,
    covenant_ids: list[int],
    request_id: str | None,
    task_id: str,
    error: dict,
) -> None:
    """Persist a safe retryable terminal ERROR for this tenant+period warm."""
    now = _utc_now_iso()
    records = _records_for_warm(
        db, company_id=company_id, period=period, covenant_ids=covenant_ids
    )
    for covenant_id in covenant_ids:
        rec = records.get(covenant_id)
        prior = _computation_metadata(rec) if rec is not None else None
        metadata = {
            "state": COMPUTATION_ERROR,
            "request_id": request_id or (prior or {}).get("request_id"),
            "task_id": task_id or (prior or {}).get("task_id"),
            "retryable": bool(error.get("retryable")),
            "error": dict(error),
            "attempt": (prior or {}).get("attempt", 0),
            "started_at": (prior or {}).get("started_at"),
            "finished_at": now,
            "updated_at": now,
        }
        if rec is None:
            _upsert_record(
                db,
                covenant_id=covenant_id,
                company_id=company_id,
                period=period,
                value=None,
                status="UNKNOWN",
                context={COVENANT_COMPUTATION_KEY: metadata},
            )
            continue
        rec.calculation_context = _with_computation_metadata(rec.calculation_context, metadata)
        rec.updated_at = datetime.now(timezone.utc)


def _completed_computation(
    prior: dict | None, *, task_id: str | None, state: str,
) -> dict:
    """Carry enqueue correlation into a terminal serveable snapshot context."""
    return {
        "state": state,
        "request_id": (prior or {}).get("request_id"),
        "task_id": task_id or (prior or {}).get("task_id"),
        "retryable": False,
        "error": None,
        "attempt": (prior or {}).get("attempt", 0),
        "started_at": (prior or {}).get("started_at"),
        "finished_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
    }


def _active_supported_covenants(
    db: Session, *, company_id: str, lock: bool = False,
) -> list["DebtCovenant"]:
    """Load the ordered full-realm single-flight surface, optionally FOR UPDATE."""
    from ..models import DebtCovenant
    from ..services.covenant_compute import SUPPORTED_METRICS

    query = (
        db.query(DebtCovenant)
        .filter(
            DebtCovenant.company_id == company_id,
            DebtCovenant.active == True,  # noqa: E712
        )
        .order_by(DebtCovenant.id)
    )
    if lock:
        query = query.with_for_update()
    return [
        covenant
        for covenant in query.all()
        if str(covenant.metric_type or "").upper() in SUPPORTED_METRICS
    ]


def _claim_warm_task(
    db: Session,
    *,
    company_id: str,
    period: str,
    task_id: str,
    attempt: int,
) -> tuple[list[int], str | None, str | None]:
    """Claim/adopt a realm warm under the full-realm lock, fenced by task identity.

    Returns ``(supported_ids, originating_request_id, skip_reason)``. A task whose ID differs
    from the persisted owner is obsolete and must never overwrite the replacement worker.
    """
    supported = _active_supported_covenants(db, company_id=company_id, lock=True)
    supported_ids = [int(covenant.id) for covenant in supported]
    if not supported_ids:
        db.commit()
        return [], None, None

    records = _records_for_warm(
        db, company_id=company_id, period=period, covenant_ids=supported_ids
    )
    active_owner_ids = {
        str(metadata.get("task_id"))
        for covenant_id in supported_ids
        if (
            (metadata := _computation_metadata(records.get(covenant_id))) is not None
            and metadata.get("state") == COMPUTATION_COMPUTING
            and metadata.get("task_id")
        )
    }
    # The full realm is one coordination surface. If an interactive warm already owns even one
    # COMPUTING row, a scheduled contender must not claim a metadata-free sibling and split the
    # realm across two workers. Terminal rows' historical task IDs are deliberately ignored.
    if any(owner_task_id != task_id for owner_task_id in active_owner_ids):
        db.commit()
        return supported_ids, None, "superseded"

    claimed_ids: list[int] = []
    for covenant_id in supported_ids:
        metadata = _computation_metadata(records.get(covenant_id))
        if metadata is None:
            # Scheduled work or a covenant added after the realm task was queued.
            claimed_ids.append(covenant_id)
            continue
        if metadata.get("state") != COMPUTATION_COMPUTING:
            # Fresh/terminal rows retain their financial result and historical task correlation.
            continue
        owner_task_id = str(metadata.get("task_id") or "")
        if not owner_task_id or owner_task_id == task_id:
            claimed_ids.append(covenant_id)

    if not claimed_ids:
        db.commit()
        return supported_ids, None, "superseded"

    originating_request_id = next(
        (
            str(metadata.get("request_id"))
            for covenant_id in supported_ids
            if (
                (metadata := _computation_metadata(records.get(covenant_id))) is not None
                and metadata.get("state") == COMPUTATION_COMPUTING
                and str(metadata.get("task_id") or "") == task_id
                and metadata.get("request_id")
            )
        ),
        None,
    )
    mark_warm_computing(
        db,
        company_id=company_id,
        period=period,
        covenant_ids=claimed_ids,
        request_id=originating_request_id,
        task_id=task_id,
        attempt=attempt,
    )
    db.commit()
    return claimed_ids, originating_request_id, None


def _mark_warm_error_if_owner(
    db: Session,
    *,
    company_id: str,
    period: str,
    task_id: str,
    covenant_ids: list[int],
    error: dict,
) -> bool:
    """CAS-style terminal write: only the currently persisted task owner may publish ERROR."""
    _active_supported_covenants(db, company_id=company_id, lock=True)
    records = _records_for_warm(
        db, company_id=company_id, period=period, covenant_ids=covenant_ids
    )
    owned_ids = [
        covenant_id
        for covenant_id in covenant_ids
        if (
            (metadata := _computation_metadata(records.get(covenant_id))) is not None
            and metadata.get("state") == COMPUTATION_COMPUTING
            and str(metadata.get("task_id") or "") == task_id
        )
    ]
    if not owned_ids:
        db.commit()
        return False
    mark_warm_error(
        db,
        company_id=company_id,
        period=period,
        covenant_ids=owned_ids,
        request_id=None,
        task_id=task_id,
        error=error,
    )
    db.commit()
    return True


def _is_transient_warm_failure(exc: Exception) -> bool:
    """Only typed provider/connection failures consume the Celery retry budget."""
    return isinstance(
        exc,
        (
            OperationalError,
            DisconnectionError,
            SQLAlchemyTimeoutError,
        ),
    ) or (isinstance(exc, CovenantSnapshotProviderError) and exc.transient)


def _retry_countdown(retries: int) -> int:
    """Capped exponential backoff plus bounded jitter (patchable for deterministic tests)."""
    base = min(180 * (2**retries), 540)
    jitter = random.randint(0, min(60, max(1, base // 4)))
    return min(base + jitter, 600)


# ─────────────────────────────────────────────────────────────────────────────
# Dual two-lens transition alerts (AC-7)
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_alert(
    db: Session, *, cov: "DebtCovenant", company_id: str, period: str, alert_type: str,
    status: str, prior_status: str | None, value: float | None, lens_label: str,
    not_meaningful: str | None = None,
) -> bool:
    """Fire ONE alert when ``status`` TRANSITIONS into danger versus ``prior_status`` (AC-7).

    Transition rule: alert only when the NEW status is a danger (WARN/BREACH) AND the prior status
    was NOT the same danger — PASS/UNKNOWN/None→WARN, PASS/UNKNOWN/None→BREACH, or WARN→BREACH
    (escalation). A steady-state danger→same-danger does NOT re-alert; a WARN after a BREACH is a
    recovery → no alert. The write routes through ``create_watchdog_alert_if_new``, deduped per
    (covenant, period, lens) by the DISTINCT ``alert_type`` + the watchdog fingerprint — no storms.
    Returns True if a NEW alert was created."""
    if status not in _DANGER_STATES:
        return False
    if prior_status == status:
        return False
    if status == "WARN" and prior_status == "BREACH":
        return False

    from ..services.coa_formula import NOT_MEANINGFUL_REASONS
    from .proactive import create_watchdog_alert_if_new

    severity = "critical" if status == "BREACH" else "warning"
    verb = "breached" if status == "BREACH" else "near breach"
    if value is not None:
        shown = "%.4f" % value
    elif not_meaningful in NOT_MEANINGFUL_REASONS:
        shown = f"not meaningful ({NOT_MEANINGFUL_REASONS[not_meaningful]})"
    else:
        shown = "n/a"
    created = create_watchdog_alert_if_new(
        db,
        company_id=company_id,
        alert_type=alert_type,
        period=period,
        severity=severity,
        title=f"Covenant {lens_label} {verb}: {cov.name}",
        description=(
            f"{cov.loan_name} — {cov.name} ({lens_label}): "
            f"{shown} vs threshold "
            f"{float(cov.threshold):.2f} ({status}) for {period}."
        ),
        metric_value=Decimal(str(value)) if value is not None else None,
        threshold_value=cov.threshold,
        metric_unit="score",  # a covenant ratio (DSCR / Debt-EBITDA), rendered as a number not $
        related_entity_type="covenant",
        related_entity_id=cov.id,
        context={"loan_name": cov.loan_name, "metric_type": cov.metric_type,
                 "period": period, "lens": lens_label},
    )
    return bool(created)


# ─────────────────────────────────────────────────────────────────────────────
# The unit of work (task + tests both drive this)
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_covenants_for_realm(
    db: Session,
    *,
    company_id: str,
    period: str | None = None,
    commit: bool = True,
    task_id: str | None = None,
    covenant_ids: list[int] | None = None,
) -> dict:
    """Snapshot every active covenant for ONE company for ONE closed month via the two-lens engine
    + fire dual transition alerts. The unit of work both the task and the tests drive.

    AC-6: one ``CovenantRecord`` per active covenant for ``period`` (TTM → status/value, run-rate →
    context), idempotent on (covenant_id, period). AC-7: one run-rate early-warning alert per
    run-rate transition-into-danger + one breach alert per TTM transition-into-breach, deduped
    per (covenant, period, lens)."""
    from ..models import DebtCovenant
    from ..qbo_client import get_qbo_client_from_db
    from ..routers.covenants import (
        _cov_own_addbacks,
        _canonical_covenant_direction,
        _covenant_projection,
        _get_date_range,
        _load_company_ebitda_addbacks,
    )
    from ..services.covenant_compute import SUPPORTED_METRICS

    period = period or period_for_date()
    covenants = (
        db.query(DebtCovenant)
        .filter(DebtCovenant.company_id == company_id, DebtCovenant.active == True)  # noqa: E712
        .all()
    )
    # Quiet empty realms do not need a QBO client.  This also keeps beat/manual invocations aligned
    # with GET /status's NO_COVENANTS contract.
    requested_ids = set(covenant_ids) if covenant_ids is not None else None
    supported = [
        cov
        for cov in covenants
        if str(cov.metric_type or "").upper() in SUPPORTED_METRICS
        and (requested_ids is None or int(cov.id) in requested_ids)
    ]
    if not supported:
        return {"company_id": company_id, "period": period, "snapshotted": 0, "alerts": 0}

    # Reject corrupt persisted configuration before any provider work. Lowercase legacy values are
    # normalized by the canonicalizer; every other token becomes a retry/terminal worker failure,
    # never the old implicit "anything non-BELOW means ABOVE" financial interpretation.
    for covenant in supported:  # SA-OK: bounded in-memory active-covenant validation
        _canonical_covenant_direction(covenant.breach_direction)

    client = get_qbo_client_from_db(db, company_id)
    company_addbacks = _load_company_ebitda_addbacks(db, company_id)
    # The requested historical period is the compute window.  The former no-argument call drifted
    # every worker invocation to the current last-closed month while writing under the requested
    # ``period`` key, making historical rows mislabeled and financially wrong.
    start_date, end_date = _get_date_range(as_of=period)
    prior_period = _prior_period(period)

    computed: list[tuple] = []
    for cov in supported:  # SA-OK: bounded-loop per-realm active covenants; keyed record lookups only
        series = _compute_series(cov, client, company_addbacks, start_date, end_date)

        ttm_value = series.ttm_ratio if series is not None else None
        ttm_not_meaningful = series.ttm_not_meaningful if series is not None else None
        ttm_status = _status_of(ttm_value, cov, not_meaningful=ttm_not_meaningful)
        run_rate_value, run_rate_not_meaningful = _latest_run_rate(series)
        run_rate_status = _status_of(run_rate_value, cov, not_meaningful=run_rate_not_meaningful)

        # W-2 F-2 (plan 20260702-qa-remediation): store the periods-until-breach trend alongside
        # the grid so the snapshot-served /covenants/status makes ZERO live QBO calls. Best-effort
        # by construction — _covenant_projection degrades to {available: False, reason} internally.
        projection = _covenant_projection(
            cov,
            client,
            addbacks=_cov_own_addbacks(cov, company_addbacks),
            as_of_date=date.fromisoformat(end_date),
        )
        terminal_state = (
            COMPUTATION_POPULATED
            if series.available and (ttm_value is not None or ttm_not_meaningful is not None)
            else COMPUTATION_NO_DATA
        )
        computed.append(
            (
                cov,
                series,
                ttm_value,
                ttm_status,
                run_rate_value,
                run_rate_status,
                projection,
                terminal_state,
                run_rate_not_meaningful,
            )
        )

    records: dict[int, object] = {}
    if task_id:
        # Finalization fence: provider work runs without a DB row lock, then the worker reacquires
        # the full realm coordination surface and verifies it still owns the holder. An explicit
        # retry can replace a >30m task while it is computing; that obsolete task must discard its
        # result instead of overwriting the replacement's state.
        _active_supported_covenants(db, company_id=company_id, lock=True)
        computed_ids = [int(item[0].id) for item in computed]
        records = _records_for_warm(
            db, company_id=company_id, period=period, covenant_ids=computed_ids
        )
        owned_ids = {
            covenant_id
            for covenant_id in computed_ids
            if (
                (metadata := _computation_metadata(records.get(covenant_id))) is not None
                and metadata.get("state") == COMPUTATION_COMPUTING
                and str(metadata.get("task_id") or "") == task_id
            )
        }
        if not owned_ids:
            db.rollback()
            return {
                "status": "superseded",
                "company_id": company_id,
                "period": period,
                "snapshotted": 0,
                "alerts": 0,
            }
        computed = [item for item in computed if int(item[0].id) in owned_ids]

    written = 0
    alerts = 0
    for (
        cov,
        series,
        ttm_value,
        ttm_status,
        run_rate_value,
        run_rate_status,
        projection,
        terminal_state,
        run_rate_not_meaningful,
    ) in computed:
        prior_computation = _computation_metadata(records.get(int(cov.id))) if task_id else None
        context = _build_context(
            series,
            ttm_status,
            run_rate_status,
            run_rate_value,
            projection=projection,
            computation=_completed_computation(
                prior_computation, task_id=task_id, state=terminal_state
            ),
            run_rate_not_meaningful=run_rate_not_meaningful,
        )
        _upsert_record(
            db, covenant_id=cov.id, company_id=company_id, period=period,
            value=ttm_value, status=ttm_status, context=context,
        )
        written += 1

        prior = _prior_record(db, company_id=company_id, covenant_id=cov.id, prior_period=prior_period)
        # TTM lens → breach alert; run-rate lens → early-warning alert. Each deduped by its own type.
        if _maybe_alert(
            db, cov=cov, company_id=company_id, period=period, alert_type=_ALERT_TTM_BREACH,
            status=ttm_status, prior_status=_prior_lens_status(prior, "ttm"),
            value=ttm_value, lens_label="TTM", not_meaningful=series.ttm_not_meaningful,
        ):
            alerts += 1
        if _maybe_alert(
            db, cov=cov, company_id=company_id, period=period, alert_type=_ALERT_RUN_RATE_WARNING,
            status=run_rate_status, prior_status=_prior_lens_status(prior, "run_rate"),
            value=run_rate_value, lens_label="run-rate", not_meaningful=run_rate_not_meaningful,
        ):
            alerts += 1

    if commit:
        db.commit()
    return {"company_id": company_id, "period": period, "snapshotted": written, "alerts": alerts}


# ─────────────────────────────────────────────────────────────────────────────
# Celery tasks (beat-scheduled fan-out)
# ─────────────────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=2, name="app.tasks.covenant_snapshot.snapshot_realm_covenants")
def snapshot_realm_covenants(self, qbo_realm_id: str | None = None, period: str | None = None) -> dict:
    """Snapshot every active covenant for ONE realm for the last CLOSED month (two-lens) + fire dual
    transition alerts (AC-6 + AC-7). Strictly realm-scoped; never touches another realm.

    IDEMPOTENT-OK: the per-(covenant, period) upsert means a redelivery UPDATES the same rows rather
    than duplicating, and each lens's alert is deduped per (covenant, period, lens) by the distinct
    alert_type + watchdog fingerprint — so at-least-once delivery is safe."""
    if not qbo_realm_id:
        logger.warning("[covenant_snapshot] missing qbo_realm_id; skipping")
        return {"status": "skipped", "reason": "missing_realm"}

    period = period or period_for_date()
    db = SessionLocal()
    task_id = str(self.request.id or "")
    originating_request_id: str | None = None
    claimed_covenant_ids: list[int] = []
    if not task_id:
        db.close()
        logger.error(
            "[covenant_snapshot] missing task_id realm=%s period=%s; skipping unobservable work",
            qbo_realm_id,
            period,
        )
        return {"status": "skipped", "reason": "missing_task_id"}
    try:
        # Pin org+realm (resolved from the realm) so the org-keyed reads/writes pass FORCE RLS
        # — the systemic RLS-write lesson (metric_snapshots / llm_usage).
        with tenant_scope_for_realm(db, qbo_realm_id):
            claimed_covenant_ids, originating_request_id, skip_reason = _claim_warm_task(
                db,
                company_id=qbo_realm_id,
                period=period,
                task_id=task_id,
                attempt=int(self.request.retries),
            )
            if skip_reason is not None:
                logger.info(
                    "[covenant_snapshot] skipped realm=%s period=%s request_id=%s "
                    "task_id=%s reason=%s",
                    qbo_realm_id,
                    period,
                    originating_request_id,
                    task_id,
                    skip_reason,
                )
                return {"status": "skipped", "reason": skip_reason}
            logger.info(
                "[covenant_snapshot] start realm=%s period=%s request_id=%s task_id=%s attempt=%s",
                qbo_realm_id,
                period,
                originating_request_id,
                task_id,
                int(self.request.retries),
            )
            summary = snapshot_covenants_for_realm(
                db,
                company_id=qbo_realm_id,
                period=period,
                task_id=task_id or None,
                covenant_ids=claimed_covenant_ids,
            )
        if summary.get("status") == "superseded":
            logger.info(
                "[covenant_snapshot] superseded realm=%s period=%s request_id=%s task_id=%s",
                qbo_realm_id,
                period,
                originating_request_id,
                task_id,
            )
            return summary
        logger.info(
            "[covenant_snapshot] success realm=%s period=%s request_id=%s task_id=%s "
            "snapshotted=%s alerts=%s",
            qbo_realm_id,
            period,
            originating_request_id,
            task_id,
            summary["snapshotted"],
            summary["alerts"],
        )
        return {"status": "ok", **summary}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error(
            "[covenant_snapshot] failure realm=%s period=%s request_id=%s task_id=%s "
            "attempt=%s error=%s",
            qbo_realm_id,
            period,
            originating_request_id,
            task_id,
            int(self.request.retries),
            redact_to_string(str(exc), max_length=500),
        )
        max_retries = int(self.max_retries or 0)
        is_transient = _is_transient_warm_failure(exc)
        if not is_transient or int(self.request.retries) >= max_retries:
            # Permanent failures terminalize immediately; typed transient provider/connection
            # failures terminalize only at their retry ceiling. The CAS write is fenced so an
            # obsolete worker can never overwrite a replacement task's COMPUTING/POPULATED state.
            from fastapi import HTTPException
            from ..services.errors import safe_error_detail

            try:
                with tenant_scope_for_realm(db, qbo_realm_id):
                    if isinstance(exc, ValueError):
                        error_code = "covenant_configuration_invalid"
                        user_message = (
                            "A covenant configuration is invalid. Update it before retrying."
                        )
                    elif (
                        isinstance(exc, CovenantSnapshotProviderError)
                        and exc.reason_code == "qbo_connection_missing"
                    ):
                        error_code = "qbo_connection_missing"
                        user_message = (
                            "No QuickBooks connection is available for this covenant calculation."
                        )
                    elif (
                        isinstance(exc, CovenantSnapshotProviderError)
                        and exc.reason_code
                        in {QBO_NOT_CONNECTED_CODE, QBO_RECONNECT_REQUIRED_CODE}
                    ):
                        error_code = exc.reason_code
                        user_message = (
                            "QuickBooks must be connected again before covenant calculations "
                            "can be refreshed."
                        )
                    elif isinstance(exc, HTTPException):
                        error_code = "covenant_warm_request_invalid"
                        user_message = "The requested covenant period is invalid."
                    else:
                        error_code = "covenant_warm_failed"
                        user_message = (
                            "Covenant calculations could not be refreshed from QuickBooks. "
                            "Retry when the connection is available."
                        )
                    # Celery transience and user recovery are separate axes. Missing QBO and an
                    # invalid covenant configuration must not auto-retry, but after a user
                    # reconnects/fixes the row the explicit dashboard Retry is a valid recovery.
                    user_retryable = is_transient or isinstance(exc, ValueError) or (
                        isinstance(exc, CovenantSnapshotProviderError)
                        and exc.reason_code in _QBO_CONNECTION_RECOVERY_CODES
                    )
                    terminal_error = safe_error_detail(
                        error_code=error_code,
                        user_message=user_message,
                        exc=exc,
                        retryable=user_retryable,
                        log_context="covenant_warm_worker_error",
                    )
                    persisted = _mark_warm_error_if_owner(
                        db,
                        company_id=qbo_realm_id,
                        period=period,
                        task_id=task_id,
                        covenant_ids=claimed_covenant_ids,
                        error=terminal_error,
                    )
                    if persisted:
                        logger.error(
                            "[covenant_snapshot] terminal realm=%s period=%s request_id=%s "
                            "task_id=%s trace_id=%s retryable=%s",
                            qbo_realm_id,
                            period,
                            originating_request_id,
                            task_id,
                            terminal_error["trace_id"],
                            user_retryable,
                        )
                    else:
                        logger.info(
                            "[covenant_snapshot] terminal write fenced realm=%s period=%s "
                            "request_id=%s task_id=%s",
                            qbo_realm_id,
                            period,
                            originating_request_id,
                            task_id,
                        )
            except Exception as state_exc:  # noqa: BLE001 — terminal-state write failure is logged, never substituted
                db.rollback()
                logger.error(
                    "[covenant_snapshot] terminal state persist failed realm=%s period=%s "
                    "request_id=%s task_id=%s error=%s",
                    qbo_realm_id,
                    period,
                    originating_request_id,
                    task_id,
                    redact_to_string(str(state_exc), max_length=500),
                )
            raise
        countdown = _retry_countdown(int(self.request.retries))
        logger.warning(
            "[covenant_snapshot] retry realm=%s period=%s request_id=%s task_id=%s "
            "next_attempt=%s countdown=%s",
            qbo_realm_id,
            period,
            originating_request_id,
            task_id,
            int(self.request.retries) + 1,
            countdown,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.covenant_snapshot.snapshot_all_covenants")
def snapshot_all_covenants(period: str | None = None) -> dict:
    """Monthly fan-out (AP-CEL-9): enqueue ``snapshot_realm_covenants`` for every mapped realm, each
    strictly scoped to its own realm. Runs after month-end so it captures the just-closed month."""
    from ..models import OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    period = period or period_for_date()
    db = SessionLocal()
    try:
        # SWEEP orchestration: the cross-tenant enumeration runs under bypass; each fanned-out child
        # re-pins its own realm scope (mirrors capture_all_metric_snapshots).
        with tenant_scope(bypass=True):  # CROSS-TENANT-OK: vetted monthly sweep over OrganizationCompany (RLS-excluded mapping); each child re-pins its realm
            companies = (
                db.query(OrganizationCompany)
                .filter(OrganizationCompany.realm_id.isnot(None))
                .all()
            )
            dispatchable = [
                company
                for company in companies
                if not company_is_suspended(db, organization_company=company)
            ]
    finally:
        db.close()

    dispatched = 0
    for company in dispatchable:
        if not company.realm_id:
            continue
        snapshot_realm_covenants.delay(qbo_realm_id=company.realm_id, period=period)
        dispatched += 1
    logger.info("[covenant_snapshot] period=%s dispatched=%s", period, dispatched)
    return {"period": period, "dispatched": dispatched}

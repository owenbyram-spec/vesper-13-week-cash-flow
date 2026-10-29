# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-3 #16 — CFF snapshot precompute).
"""Background cash-flow-forecast snapshot refresh (QA-remediation W-2, F-3).

The 13-week forecast's snapshot-MISS path used to run the full synchronous QBO build (open
invoices + open bills + DSO + DPO + deposits/outflows history — the live-confirmed multi-second
stall) inside the GET /cashflow-forecast/weekly request. F-3 moves that build here:

  • ``refresh_realm_forecast_snapshot`` — rebuild ONE realm's ``CashForecastSnapshot`` from QBO
    and save it (the exact ``_build_forecast_from_qbo`` + ``_save_snapshot`` pair the explicit
    POST /weekly/refresh endpoint runs — one engine, no fork). Enqueued by the router on a
    snapshot miss; also the nightly fan-out child. Self-deduping: a snapshot computed within the
    last 10 minutes short-circuits, so a burst of page loads on a missing snapshot enqueues many
    tasks but builds once.

  • ``refresh_all_forecast_snapshots`` — nightly beat fan-out (AP-CEL-9): one realm-scoped child
    per company that HAS a snapshot row (i.e. actually uses the CFF), mirroring
    ``snapshot_all_covenants``. Keeps every active forecast <24h fresh so the interactive path
    never needs an in-request QBO build.

Each child pins the tenant RLS scope (``tenant_scope_for_realm``) so the realm-keyed reads/writes
pass FORCE RLS, and every query carries its company_id predicate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)

# A snapshot computed within this window is fresh enough that a queued rebuild is redundant
# (dedupes the enqueue-per-page-load burst on a snapshot miss).
_JUST_BUILT_SECONDS = 600


def _snapshot_age_seconds(db, company_id: str) -> float | None:
    """Age of the company's stored snapshot in seconds, or None when no snapshot exists."""
    from ..models import CashForecastSnapshot

    snapshot = (
        db.query(CashForecastSnapshot)
        .filter(CashForecastSnapshot.company_id == company_id)
        .first()
    )
    if snapshot is None or snapshot.computed_at is None:
        return None
    computed_at = snapshot.computed_at
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - computed_at).total_seconds()


def refresh_forecast_snapshot_for_realm(
    db, *, company_id: str, weeks: int = 13, lookback: int = 1, force: bool = False,
) -> dict:
    """Rebuild + save ONE realm's forecast snapshot — the unit of work the task and tests drive.

    Reuses the router's ``_build_forecast_from_qbo`` + ``_save_snapshot`` verbatim (the same pair
    POST /weekly/refresh runs), so the background build and the explicit refresh cannot drift.
    ``force=False`` short-circuits when a snapshot was computed within the last 10 minutes.

    After saving the serve snapshot it also FREEZES this week's snapshot version
    (plan 20260706-cfo-grade-financial-workflows AC-2B.1): the live layer is applied
    to the just-built grid and the served view is captured into
    ``CashForecastSnapshotVersion`` — the forecast-as-made provenance that accuracy
    scoring reads (the serve snapshot itself stays latest-only, contract unchanged).
    A freeze failure never fails the refresh; it is logged + surfaced in the summary."""
    from ..routers.cashflow_forecast import (
        _apply_live_layer,
        _build_forecast_from_qbo,
        _freeze_version_best_effort,
        _save_snapshot,
    )

    if not force:
        age = _snapshot_age_seconds(db, company_id)
        if age is not None and age < _JUST_BUILT_SECONDS:
            return {"status": "skipped", "reason": "recently_built", "age_seconds": round(age, 1)}

    snapshot_data, bank_balance, bank_balance_as_of, bank_balance_source, anchor_diag = (
        _build_forecast_from_qbo(company_id, weeks, lookback, db)
    )
    _save_snapshot(db, company_id, snapshot_data, bank_balance)
    payload = _apply_live_layer(
        snapshot_data,
        bank_balance,
        company_id,
        weeks,
        lookback,
        db,
        bank_balance_as_of=bank_balance_as_of,
        bank_balance_source=bank_balance_source,
        anchor_diagnostics=anchor_diag,
    )
    version_summary = _freeze_version_best_effort(db, company_id, payload)
    return {"status": "refreshed", "company_id": company_id, "snapshot_version": version_summary}


@celery_app.task(bind=True, max_retries=2, name="app.tasks.cash_forecast_refresh.refresh_realm_forecast_snapshot")
def refresh_realm_forecast_snapshot(
    self, qbo_realm_id: str | None = None, weeks: int = 13, lookback: int = 1, force: bool = False,
) -> dict:
    """Rebuild ONE realm's CFF snapshot from QBO in the background. Strictly realm-scoped.

    IDEMPOTENT-OK: ``_save_snapshot`` upserts the single per-company row, so an at-least-once
    redelivery overwrites the same snapshot rather than duplicating; the 10-minute
    recently-built short-circuit collapses enqueue bursts to one build."""
    if not qbo_realm_id:
        logger.warning("[cash_forecast_refresh] missing qbo_realm_id; skipping")
        return {"status": "skipped", "reason": "missing_realm"}

    db = SessionLocal()
    try:
        # Pin org+realm (resolved from the realm) so the realm-keyed reads/writes pass FORCE RLS.
        with tenant_scope_for_realm(db, qbo_realm_id):
            summary = refresh_forecast_snapshot_for_realm(
                db, company_id=qbo_realm_id, weeks=weeks, lookback=lookback, force=force,
            )
        logger.info("[cash_forecast_refresh] realm=%s %s", qbo_realm_id, summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[cash_forecast_refresh] realm=%s failed: %s",
                     qbo_realm_id, redact_to_string(str(exc), max_length=500))
        # Capped exponential backoff (AP-CEL-4): 180s, 360s, bounded at 600s.
        raise self.retry(exc=exc, countdown=min(180 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.cash_forecast_refresh.refresh_all_forecast_snapshots")
def refresh_all_forecast_snapshots() -> dict:
    """Nightly fan-out (AP-CEL-9): enqueue ``refresh_realm_forecast_snapshot`` for every
    non-suspended company that HAS a forecast snapshot (only realms actually using the CFF),
    each strictly scoped to its own realm."""
    from ..models import CashForecastSnapshot, OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    db = SessionLocal()
    try:
        # SWEEP orchestration: the cross-tenant enumeration runs under bypass; each fanned-out
        # child re-pins its own realm scope (mirrors snapshot_all_covenants).
        with tenant_scope(bypass=True):  # CROSS-TENANT-OK: vetted nightly sweep; each child re-pins its realm
            snapshot_realms = {
                row[0] for row in db.query(CashForecastSnapshot.company_id).distinct().all()
            }
            companies = (
                db.query(OrganizationCompany)
                .filter(OrganizationCompany.realm_id.isnot(None))
                .all()
            )
            dispatchable = [
                company.realm_id
                for company in companies
                if company.realm_id in snapshot_realms
                and not company_is_suspended(db, organization_company=company)
            ]
    finally:
        db.close()

    dispatched = 0
    for realm_id in dispatchable:
        refresh_realm_forecast_snapshot.delay(qbo_realm_id=realm_id, force=True)
        dispatched += 1
    logger.info("[cash_forecast_refresh] nightly fan-out dispatched=%s", dispatched)
    return {"dispatched": dispatched}

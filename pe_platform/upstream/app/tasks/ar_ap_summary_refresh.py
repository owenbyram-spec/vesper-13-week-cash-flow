# PLAN-EXEMPT: market-ready ar-ap latency fix (Owen-ratified 2026-07-06)
"""Background AR/AP summary snapshot warm.

GET /ar-ap/summary's snapshot-MISS path used to run the full synchronous
three-report QBO compute (AR aging + AP aging + trailing-12-month P&L) on the
request thread. The router now serves a ``data_quality: "computing"`` payload
and enqueues this task instead (the cash-flow F-3 / ``cash_forecast_refresh``
shape):

  * ``warm_ar_ap_summary`` — compute ONE realm's summary from QBO and upsert
    its ``ARAPSummarySnapshot`` row. Self-deduping: a snapshot updated within
    the last 10 minutes short-circuits, so a burst of page loads on a missing
    snapshot enqueues many tasks but computes once.

The task pins the tenant RLS scope (``tenant_scope_for_realm``) so the
realm-keyed snapshot write and the org-keyed QBO-token read both pass FORCE
RLS, and every query carries its company_id predicate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope_for_realm

logger = logging.getLogger(__name__)

# A snapshot updated within this window is fresh enough that a queued warm is
# redundant (dedupes the enqueue-per-page-load burst on a snapshot miss).
_JUST_BUILT_SECONDS = 600


def _snapshot_age_seconds(db, company_id: str, period: str) -> float | None:
    """Age of the (company, period) snapshot in seconds, or None when absent."""
    from ..models import ARAPSummarySnapshot

    snapshot = (
        db.query(ARAPSummarySnapshot)
        .filter(
            ARAPSummarySnapshot.company_id == company_id,
            ARAPSummarySnapshot.period == period,
        )
        .first()
    )
    if snapshot is None:
        return None
    stamped = snapshot.updated_at or snapshot.created_at
    if stamped is None:
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamped).total_seconds()


def warm_summary_for_realm(db, *, company_id: str, period: str | None = None, force: bool = False) -> dict:
    """Compute + upsert ONE realm's summary snapshot — the unit of work the task
    and tests drive.

    Reuses the service's ``compute_summary_payload`` + ``upsert_snapshot``
    verbatim (the same pair POST /ar-ap/refresh runs), so the background warm
    and the explicit refresh cannot drift. ``force=False`` short-circuits when
    the snapshot was updated within the last 10 minutes."""
    from ..services.ar_ap_summary import compute_summary_payload, current_period, upsert_snapshot

    period = period or current_period()

    if not force:
        age = _snapshot_age_seconds(db, company_id, period)
        if age is not None and age < _JUST_BUILT_SECONDS:
            return {"status": "skipped", "reason": "recently_built", "age_seconds": round(age, 1)}

    payload = compute_summary_payload(db, company_id)
    upsert_snapshot(db, company_id, period, payload)
    db.commit()
    return {"status": "refreshed", "company_id": company_id, "period": period}


@celery_app.task(bind=True, max_retries=2, name="app.tasks.ar_ap_summary_refresh.warm_ar_ap_summary")
def warm_ar_ap_summary(self, company_id: str | None = None, period: str | None = None, force: bool = False) -> dict:
    """Warm ONE realm's AR/AP summary snapshot from QBO. Strictly realm-scoped.

    IDEMPOTENT-OK: ``upsert_snapshot`` upserts the single (company, period) row
    (race-safe SAVEPOINT on the unique-conflict), so an at-least-once redelivery
    overwrites the same snapshot rather than duplicating; the 10-minute
    recently-built short-circuit collapses enqueue bursts to one compute."""
    if not company_id:
        logger.warning("[ar_ap_summary_refresh] missing company_id; skipping")
        return {"status": "skipped", "reason": "missing_company"}

    db = SessionLocal()
    try:
        # Pin org+realm (resolved from the realm) so the realm-keyed snapshot
        # write and the org-keyed token read pass FORCE RLS.
        with tenant_scope_for_realm(db, company_id):
            summary = warm_summary_for_realm(db, company_id=company_id, period=period, force=force)
        logger.info("[ar_ap_summary_refresh] realm=%s %s", company_id, summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("[ar_ap_summary_refresh] realm=%s failed: %s",
                     company_id, redact_to_string(str(exc), max_length=500))
        # Capped exponential backoff (AP-CEL-4): 180s, 360s, bounded at 600s.
        raise self.retry(exc=exc, countdown=min(180 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()

# PLAN-EXEMPT: Deliverable C search-index reconcile task (plan 20260625-intent-routing-receivables, ratified Steven 2026-06-25)
"""Periodic reconcile of the QBOSearchRecord FTS index for recent invoices/bills.

The webhook path (routers/webhooks.py:_sync_search_index_entities) indexes an
Invoice/Bill the moment QBO fires a Create/Update event, but a dropped/missed
webhook would leave that row unfindable forever — ``sync_search_index`` otherwise
runs ONLY inside the full ``sync_all_data`` pass, which has no beat entry. This is
the bounded periodic backstop.

The parent fan-out (``reconcile_all_search_indexes``) enumerates mapped companies
under bypass and dispatches ONE realm-scoped child (``reconcile_realm_search_index``)
each (AP-CEL-9: never an unbounded all-realms loop inside one task that could run
past the broker visibility_timeout). The child re-indexes a TRAILING WINDOW of
recent invoices/bills (not the whole history) under ``tenant_scope_for_realm`` so
the org-keyed writes pass FORCE RLS, and the indexer upserts by
(realm_id, entity_type, qbo_id) so the task is idempotent (AP-CEL-3) — a redelivery
under at-least-once (acks_late) refreshes the same rows rather than duplicating.
"""
import logging
from datetime import date, timedelta

from ..celery_app import celery_app
from ..db import SessionLocal
from ..qbo_client import get_qbo_client_from_db
from ..services import sync
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)

# Trailing window re-indexed each run. Bounds the QBO query + write volume per
# realm so a single child stays well under the broker visibility_timeout, while
# still covering recent invoices/bills a missed webhook would have left unindexed.
RECONCILE_WINDOW_DAYS = 35


@celery_app.task(
    bind=True,
    max_retries=2,
    name="app.tasks.search_reconcile.reconcile_realm_search_index",
)
def reconcile_realm_search_index(self, qbo_realm_id: str | None = None) -> dict:
    """Re-index the trailing ``RECONCILE_WINDOW_DAYS`` of invoices/bills (and the
    other search entities) for ONE realm into ``qbo_search_records``. Strictly
    realm-scoped; never touches another realm."""
    if not qbo_realm_id:
        logger.warning("[search_reconcile] missing qbo_realm_id; skipping")
        return {"status": "skipped", "reason": "missing_realm"}

    end = date.today()
    start = end - timedelta(days=RECONCILE_WINDOW_DAYS)
    db = SessionLocal()
    try:
        # Pin org+realm (resolved from the realm) so the org-keyed qbo_search_records
        # write passes FORCE RLS — the systemic RLS-write lesson.
        with tenant_scope_for_realm(db, qbo_realm_id):
            client = get_qbo_client_from_db(db, qbo_realm_id)
            if client is None:
                logger.info("[search_reconcile] realm=%s skipped: no QBO client", qbo_realm_id)
                return {"status": "skipped", "reason": "no_client", "qbo_realm_id": qbo_realm_id}
            # IDEMPOTENT-OK: sync_search_index -> index_qbo_entity -> upsert_search_record
            # upserts by (realm_id, entity_type, qbo_id); a redelivery refreshes the
            # same rows rather than duplicating, so at-least-once delivery is safe.
            indexed = sync.sync_search_index(db, client, start_date=start, end_date=end)
            logger.info("[search_reconcile] realm=%s indexed=%s", qbo_realm_id, indexed)
            return {"status": "ok", "qbo_realm_id": qbo_realm_id, "indexed": indexed}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error(
            "[search_reconcile] realm=%s failed: %s",
            qbo_realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        # Capped exponential backoff (AP-CEL-4): 180s, 360s, … bounded at 600s.
        raise self.retry(exc=exc, countdown=min(180 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.search_reconcile.reconcile_all_search_indexes")
def reconcile_all_search_indexes() -> dict:
    """Periodic fan-out: enqueue ``reconcile_realm_search_index`` for every mapped
    realm, each strictly scoped to its own realm (AP-CEL-9 — one short child per
    realm, never one unbounded all-realms loop)."""
    from ..models import OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    db = SessionLocal()
    try:
        # SWEEP orchestration: the cross-tenant enumeration of mapped companies runs
        # under bypass; each fanned-out child re-pins its own realm scope.
        with tenant_scope(bypass=True):  # CROSS-TENANT-OK: vetted sweep over OrganizationCompany (RLS-excluded mapping); each child re-pins its realm
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
        reconcile_realm_search_index.delay(qbo_realm_id=company.realm_id)
        dispatched += 1
    logger.info("[search_reconcile] dispatched=%s", dispatched)
    return {"dispatched": dispatched}

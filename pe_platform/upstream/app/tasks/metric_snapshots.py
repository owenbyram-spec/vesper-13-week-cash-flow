# PLAN-EXEMPT: item-4 MetricSnapshot daily capture task, ratified 2026-06-25 ("proceed with 1 and 2")
"""Daily capture of point-in-time metric snapshots (item-4 custom reporting).

The parent fan-out (``capture_all_metric_snapshots``) enumerates mapped companies under
bypass and dispatches one realm-scoped child (``capture_realm_metric_snapshots``) each —
mirrors ``build_all_realm_contexts`` so a single realm's QBO work stays well under the broker
visibility_timeout (AP-CEL-9). The child writes under ``tenant_scope_for_realm`` so the
org-keyed ``metric_snapshots`` INSERT passes FORCE RLS, and the upsert is idempotent on
(company_id, metric, as_of_date).
"""
import logging
from datetime import date

from ..celery_app import celery_app
from ..db import SessionLocal
from ..qbo_client import get_qbo_client_from_db
from ..services.metric_snapshots import compute_metrics_for_realm, upsert_metric_snapshots
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, name="app.tasks.metric_snapshots.capture_realm_metric_snapshots")
def capture_realm_metric_snapshots(
    self,
    organization_id: int | None = None,
    organization_company_id: int | None = None,
    qbo_realm_id: str | None = None,
) -> dict:
    """Capture AP/AR/blended-cash (+ per-account cash) for ONE realm as of today, upserting one
    MetricSnapshot per metric. Strictly realm-scoped; never touches another realm."""
    if not qbo_realm_id:
        logger.warning("[metric_snapshots] missing qbo_realm_id; skipping")
        return {"status": "skipped", "reason": "missing_realm"}
    as_of = date.today()
    db = SessionLocal()
    try:
        # Pin org+realm (resolved from the realm) so the org-keyed metric_snapshots write passes
        # FORCE RLS — the systemic RLS-write lesson (llm_usage / correction_capture this batch).
        with tenant_scope_for_realm(db, qbo_realm_id):
            client = get_qbo_client_from_db(db, qbo_realm_id)
            if client is None:
                logger.info("[metric_snapshots] realm=%s skipped: no QBO client", qbo_realm_id)
                return {"status": "skipped", "reason": "no_client", "qbo_realm_id": qbo_realm_id}
            metrics = compute_metrics_for_realm(client, as_of)
            written = upsert_metric_snapshots(
                db,
                realm_id=qbo_realm_id,
                organization_id=organization_id,
                organization_company_id=organization_company_id,
                metrics=metrics,
                as_of_date=as_of,
                source="scheduled",
            )
            # IDEMPOTENT-OK: upsert-by-(company_id, metric, as_of_date) — a redelivery updates the
            # same-day rows rather than duplicating, so at-least-once delivery is safe.
            db.commit()
            logger.info("[metric_snapshots] realm=%s captured=%s", qbo_realm_id, written)
            return {"status": "ok", "qbo_realm_id": qbo_realm_id, "captured": written}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error(
            "[metric_snapshots] realm=%s failed: %s",
            qbo_realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        # Capped exponential backoff (AP-CEL-4): 180s, 360s, … bounded at 600s.
        raise self.retry(exc=exc, countdown=min(180 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.metric_snapshots.capture_all_metric_snapshots")
def capture_all_metric_snapshots() -> dict:
    """Daily fan-out: enqueue ``capture_realm_metric_snapshots`` for every mapped realm, each
    strictly scoped by its own triple."""
    from ..models import OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    db = SessionLocal()
    try:
        # SWEEP orchestration: the cross-tenant enumeration of mapped companies runs under
        # bypass; each fanned-out child re-pins its own realm scope (mirrors build_all_realm_contexts).
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
        capture_realm_metric_snapshots.delay(
            organization_id=company.organization_id,
            organization_company_id=company.id,
            qbo_realm_id=company.realm_id,
        )
        dispatched += 1
    logger.info("[metric_snapshots] dispatched=%s", dispatched)
    return {"dispatched": dispatched}

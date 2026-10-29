"""
Celery tasks for QBO webhook stale-processing recovery.

If a BackgroundTasks worker dies mid-flight, a QBOWebhookEvent can be stuck in
status="processing" forever. This sweep mirrors the Stripe webhook ledger's
15-minute stale-reclaim policy (app/services/stripe.py) so recovery does not
depend on a new webhook arriving for the same entity. It also re-runs events
whose last attempt FAILED (a QBO auth or transport failure the handlers raise
instead of swallowing) and events whose in-request background task never ran,
both bounded by the same retry cap.

All claiming is done through the concurrency-safe conditional UPDATE in
app.routers.webhooks._claim_qbo_event_for_processing, so the sweep is
idempotent: re-running it (or running it concurrently with live processing)
cannot double-claim an event or exceed the retry cap.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import and_, or_

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import QBOWebhookEvent
from ..tenant_rls import tenant_scope

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2)
def sweep_stale_stripe_webhook_events(self):
    """
    Periodic task: reclaim Stripe webhook events stuck in status="processing"
    past the stale cutoff and re-apply them by re-fetching each event from the
    Stripe API. Parity with the QBO sweep below — without it a dead worker can
    strand a billing-state update until the next Stripe webhook happens to
    arrive for the same entity.
    """
    from ..services.stripe import recover_stale_stripe_webhook_events

    db = SessionLocal()
    try:
        result = recover_stale_stripe_webhook_events(db)
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()
    return result


@celery_app.task(bind=True, max_retries=3)
def process_qbo_webhook_event(self, event_id: int) -> dict:
    """Process a single QBO webhook event.

    Resolve-then-pin (discover the realm by a transient-bypass PK lookup → resolve the
    owning org → pin both GUCs) now lives INSIDE ``_process_qbo_webhook_event`` itself,
    shared with the live FastAPI BackgroundTasks path, so this Celery task is a thin
    entrypoint over the one scoped processor. Idempotent: the claim is enforced inside.
    """
    from ..routers.webhooks import _process_qbo_webhook_event

    _process_qbo_webhook_event(event_id)  # IDEMPOTENT-OK: claim is a conditional UPDATE inside the processor
    return {"status": "done", "event_id": event_id}


@celery_app.task(bind=True, max_retries=2)
def sweep_stale_qbo_webhook_events(self):
    """
    Periodic task: re-enqueue QBO webhook events that need another attempt:
      - stuck in status="processing" past the stale cutoff (a dead worker);
      - status="failed" with retry budget left, once the retry backoff passed
        (the handler raised on a QBO auth/transport failure);
      - status="pending" past the stale cutoff (the request's background task
        never ran).

    Only event ids are selected here — the actual claim (including the
    retry-cap-to-failed transition) happens via the row-level conditional
    UPDATE inside _process_qbo_webhook_event, keeping the sweep idempotent.
    """
    from ..routers.webhooks import (
        QBO_WEBHOOK_FAILED_RETRY_AFTER,
        QBO_WEBHOOK_MAX_RETRIES,
        _qbo_stale_processing_cutoff,
    )

    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() unchanged below (re-indent only)
    try:
        stale_cutoff = _qbo_stale_processing_cutoff()
        retry_cutoff = datetime.now(timezone.utc) - QBO_WEBHOOK_FAILED_RETRY_AFTER
        needs_attempt = or_(
            and_(
                QBOWebhookEvent.status == "processing",
                QBOWebhookEvent.processing_started_at.isnot(None),
                QBOWebhookEvent.processing_started_at < stale_cutoff,
            ),
            and_(
                QBOWebhookEvent.status == "failed",
                QBOWebhookEvent.retry_count < QBO_WEBHOOK_MAX_RETRIES,
                or_(
                    QBOWebhookEvent.processing_started_at.is_(None),
                    QBOWebhookEvent.processing_started_at < retry_cutoff,
                ),
            ),
            and_(
                QBOWebhookEvent.status == "pending",
                QBOWebhookEvent.received_at < stale_cutoff,
            ),
        )
        # SWEEP orchestration: enumerating stale events across all realms is cross-tenant →
        # bypass. Each fanned-out process_qbo_webhook_event child re-pins its own realm scope.
        with tenant_scope(bypass=True):
            stale_ids = [
                row[0]
                for row in (
                    db.query(QBOWebhookEvent.id)  # CROSS-TENANT-OK: sweep selects stale event ids across every realm under tenant_scope(bypass=True); each is re-pinned in its child task
                    .filter(needs_attempt)
                    .order_by(QBOWebhookEvent.id.asc())
                    .all()
                )
            ]
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()

    for event_id in stale_ids:
        process_qbo_webhook_event.delay(event_id)

    if stale_ids:
        logger.warning(
            "QBO webhook recovery sweep re-enqueued %s event(s): %s",
            len(stale_ids),
            stale_ids,
        )
    return {"status": "done", "re_enqueued": len(stale_ids)}

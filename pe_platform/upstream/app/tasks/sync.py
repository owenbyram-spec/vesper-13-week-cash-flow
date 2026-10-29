import logging
import random
from datetime import date, timedelta

from fastapi import HTTPException

from sqlalchemy import func

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import ReconciliationSession
from ..services import sync
from ..qbo_client import get_qbo_client_from_db, qbo_realm_sync_lock
from ..services.bank_recon.session_status import STATUS_ABANDONED, STATUS_COMPLETE
from ..services.cache import clear_company_cache
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def sync_all_data(self, realm_id: str) -> "dict | None":
    """
    Background task to sync all data entities from QBO.

    B3.3 — a per-realm Redis lock makes a second ``sync_all_data`` for the same
    realm a NO-OP while one is running, so overlapping triggers do not double the
    QBO load. The lock is NOT what keeps an overlap correct (it yields when Redis
    is down, and the other sync entry points never take it): the mirror tables'
    database unique identities with ON CONFLICT upserts do. B3.4 — a 429 is now absorbed by the per-REQUEST backoff in the
    QBO client (the multi-entity task is NOT restarted for a throttle blip); the
    task-level retry below is bounded and reserved for genuinely transient
    failures, and a permanent precondition (reconnect / not-connected) is NOT
    retried.
    """
    db = SessionLocal()  # SA-OK: closed-elsewhere — the function-level `finally: db.close()` below owns the close
    try:
        # B3.3 per-realm exclusivity: acquire-or-skip. If another sync for this
        # realm holds the lock, this trigger no-ops (the in-flight one covers it).
        with qbo_realm_sync_lock(realm_id) as acquired:
            if not acquired:
                logger.info(
                    "[sync_all_data] realm=%s already syncing; skipping duplicate trigger",
                    realm_id,
                )
                return {"status": "skipped", "reason": "sync_already_running", "realm_id": realm_id}

            # RLS tenant-scope: this is a SINGLE-tenant task keyed on the realm_id
            # arg. Re-pin the per-transaction GUC inside the worker (no request
            # middleware here); no-op on SQLite.
            with tenant_scope_for_realm(db, realm_id):
                # Resolve client for the specific realm_id
                client = get_qbo_client_from_db(db, realm_id)

                # Run sync functions
                sync.sync_company_info(db, client)
                sync.sync_accounts(db, client)
                sync.sync_vendors(db, client)
                sync.sync_customers(db, client)
                sync.sync_items(db, client)
                sync.sync_transactions(db, client)
                sync.sync_journal_entries(db, client)
                sync.sync_search_index(db, client)

                # Normalized GL detail cache (last 90 days) for the drilldown/assistant fast
                # path. Keeps qbo_gl_lines warm so the freshness-gated drilldown read serves
                # from cache instead of a live GL fetch; a stale/empty cache falls through to
                # the live path which backfills. (Previously only run_full_sync — dead/uncalled
                # — refreshed this cache, so it never got warmed on the live sync path.)
                _gl_end = date.today()
                sync.sync_gl_lines(db, client, start_date=_gl_end - timedelta(days=90), end_date=_gl_end)

                # Reports
                sync.sync_report(db, client, "ProfitAndLoss", {"date_macro": "ThisFiscalYearToDate", "summarize_column_by": "Month"})
                sync.sync_report(db, client, "BalanceSheet", {"date_macro": "ThisFiscalYear"})
                sync.sync_report(db, client, "CashFlow", {"date_macro": "ThisFiscalYearToDate", "summarize_column_by": "Month"})

                # Clear dashboard and KPI cache
                clear_company_cache(realm_id)

                # Post-sync precompute hook (audit item S6): rebuild the realm's isolated
                # context profile + onboarding-recommendation artifacts off the request
                # path. Resolve the full scope triple here so the enqueued task is
                # strictly realm-scoped; if the realm is not mapped to an org + company,
                # enqueue with just the realm id and let the builder no-op safely.
                try:
                    from ..services.tenant_companies import (
                        organization_company_for_realm,
                        organization_for_company,
                    )
                    from .realm_context import build_realm_context

                    organization = organization_for_company(db, realm_id)
                    organization_company = (
                        organization_company_for_realm(
                            db, organization_id=organization.id, realm_id=realm_id
                        )
                        if organization
                        else None
                    )
                    build_realm_context.delay(
                        organization_id=organization.id if organization else None,
                        organization_company_id=(
                            organization_company.id if organization_company else None
                        ),
                        qbo_realm_id=realm_id,
                    )
                except Exception:  # noqa: BLE001 — never let precompute scheduling fail the sync
                    logger.exception(
                        "[sync_all_data] failed to enqueue build_realm_context for realm=%s",
                        realm_id,
                    )

        # Normal completion (lock acquired, sync ran).
        return None

    except HTTPException:
        # B3.4 — a precondition fault is PERMANENT: 401 (refresh-token expired →
        # reconnect) and 409 (qbo_not_connected) will never self-heal on a retry,
        # so re-raise without burning the bounded task-retry budget. A 429 never
        # reaches here — the per-request backoff (B3.1) already absorbed it.
        raise
    except Exception as exc:
        # B3.4 — bounded task-level retry reserved for a genuinely transient
        # failure (a 429 is handled at the call by B3.1, not by restarting the
        # whole multi-entity task). Exponential backoff + full jitter (AP-CEL-4):
        # space retries so a struggling downstream can recover and de-sync many
        # realms' tasks retrying at once, capped at 600s.
        backoff = min(60 * (2 ** self.request.retries), 600)
        countdown = random.uniform(0, backoff)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        db.close()


# ``sync_transactions`` re-reads the trailing 90 days by default, so a transaction
# deleted, voided or moved in QBO after it aged out of that window would stay a
# phantom book row in a reconciliation still open for an older period. This
# backstop re-syncs (and so retires) the book side from the oldest such open
# period up to where the default window begins.
BOOK_SYNC_DEFAULT_WINDOW_DAYS = 90


def open_recon_window_outside_default_sync(db, realm_id: str, today: date | None = None):
    """``(start, end)`` covering the realm's open reconciliation periods older than
    the default book-sync window, or None when there are none."""
    cutoff = (today or date.today()) - timedelta(days=BOOK_SYNC_DEFAULT_WINDOW_DAYS)
    oldest = (
        db.query(func.min(ReconciliationSession.period_start))
        .filter(
            ReconciliationSession.realm_id == realm_id,
            ReconciliationSession.status.notin_([STATUS_COMPLETE, STATUS_ABANDONED]),
            ReconciliationSession.period_start < cutoff,
        )
        .scalar()
    )
    return (oldest, cutoff) if oldest is not None else None


@celery_app.task(bind=True, max_retries=3, name="app.tasks.sync.reconcile_open_recon_book_window")
def reconcile_open_recon_book_window(self, realm_id: str) -> dict:
    """Re-sync one realm's book side over its open reconciliation periods older than
    the default window (see ``open_recon_window_outside_default_sync``)."""
    db = SessionLocal()  # closed by the finally below
    try:
        with qbo_realm_sync_lock(realm_id) as acquired:
            if not acquired:
                return {"status": "skipped", "reason": "sync_already_running", "realm_id": realm_id}
            with tenant_scope_for_realm(db, realm_id):
                window = open_recon_window_outside_default_sync(db, realm_id)
                if window is None:
                    return {"status": "skipped", "reason": "no_open_period_outside_window", "realm_id": realm_id}
                client = get_qbo_client_from_db(db, realm_id)
                result = sync.sync_transactions(db, client, start_date=window[0], end_date=window[1])
                return {
                    "status": "ok", "realm_id": realm_id,
                    "start": window[0].isoformat(), "end": window[1].isoformat(), **result,
                }
    except HTTPException:
        # Not connected / reconnect required: permanent until the user acts.
        raise
    except Exception as exc:
        db.rollback()
        backoff = min(60 * (2 ** self.request.retries), 600)
        raise self.retry(exc=exc, countdown=random.uniform(0, backoff)) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.sync.reconcile_all_open_recon_book_windows")
def reconcile_all_open_recon_book_windows() -> dict:
    """Periodic fan-out: one ``reconcile_open_recon_book_window`` child per realm
    with an open reconciliation older than the default book-sync window."""
    cutoff = date.today() - timedelta(days=BOOK_SYNC_DEFAULT_WINDOW_DAYS)
    db = SessionLocal()
    try:
        # SWEEP orchestration: enumerating realms is cross-tenant; each child re-pins its realm.
        with tenant_scope(bypass=True):
            realm_ids = [
                realm_id
                for (realm_id,) in db.query(ReconciliationSession.realm_id)
                .filter(
                    ReconciliationSession.status.notin_([STATUS_COMPLETE, STATUS_ABANDONED]),
                    ReconciliationSession.period_start < cutoff,
                )
                .distinct()
            ]
    finally:
        db.close()
    for realm_id in realm_ids:
        reconcile_open_recon_book_window.delay(realm_id)
    logger.info("[reconcile_open_recon_book_windows] dispatched=%s", len(realm_ids))
    return {"dispatched": len(realm_ids)}

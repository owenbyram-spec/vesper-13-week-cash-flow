"""
app/tasks/onboarding.py
─────────────────────────
Celery task wrapper for the company cold-start bootstrap pipeline.
Dispatched asynchronously from the QBO OAuth callback or the manual
POST /sync/bootstrap endpoint.
"""
import logging
from ..celery_app import celery_app
from ..db import SessionLocal
from ..tenant_rls import tenant_scope_for_realm

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, name="app.tasks.onboarding.run_company_bootstrap")
def run_company_bootstrap(self, realm_id: str):
    """
    Run the company cold-start bootstrap for the given realm_id.
    Pulls 6-month GL, runs GPT-4o analysis, writes CompanyMemory + ClassificationRules.
    """
    db = SessionLocal()  # SA-OK: closed-elsewhere — finally: db.close() below (unchanged)
    try:
        from ..qbo_client import get_qbo_client_from_db
        from ..services.onboarding import bootstrap_company_memory

        # RLS backstop: this task is single-tenant (keyed on the realm arg); pin the
        # realm GUC so every query in the bootstrap pipeline is realm-scoped.
        with tenant_scope_for_realm(db, realm_id):
            client = get_qbo_client_from_db(db, realm_id=realm_id)
            result = bootstrap_company_memory(db, client, realm_id)
        logger.info(f"[BootstrapTask] realm={realm_id} result={result}")
        return result
    except Exception as exc:
        logger.error(f"[BootstrapTask] Failed for realm={realm_id}: {exc}")
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()

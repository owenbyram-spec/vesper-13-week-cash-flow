"""
Celery tasks for refreshing the AI assistant's context cache.
"""
from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.context_service import ContextCacheService
from ..models import QBOCompanyInfo
from ..tenant_rls import tenant_scope, tenant_scope_for_realm


@celery_app.task(bind=True, max_retries=2)
def refresh_context_cache_task(self, realm_id: str) -> dict:
    """Refresh the executive summary cache for a single company."""
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            service = ContextCacheService(db)
            service.refresh_executive_summary(realm_id)
            return {"status": "done", "realm_id": realm_id}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=300)  # RETRY-FIXED-OK: pre-existing legacy cadence, unchanged by this RLS-wiring edit
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=1)
def refresh_all_context_caches(self) -> dict:
    """
    Periodic task: refresh executive summaries for ALL active companies.
    Schedule via celery-beat (e.g., daily at 2 AM).
    """
    db = SessionLocal()
    try:
        # Cross-tenant orchestration: list every company, then fan out a
        # per-tenant child (which self-scopes) — no per-tenant query here.
        with tenant_scope(bypass=True):
            companies = db.query(QBOCompanyInfo).all()
        for company in companies:
            # Fan out individual tasks to avoid timeout
            refresh_context_cache_task.delay(company.realm_id)
        return {"status": "dispatched", "count": len(companies)}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=600)  # RETRY-FIXED-OK: pre-existing legacy cadence, unchanged by this RLS-wiring edit
    finally:
        db.close()

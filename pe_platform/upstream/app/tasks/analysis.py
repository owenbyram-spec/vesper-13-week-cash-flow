"""
Celery task wrappers for the AI analysis pipeline.
"""
from ..celery_app import celery_app
from ..db import SessionLocal
from ..orchestration.pipeline import run_analysis_pipeline, process_pending_events
from ..tenant_rls import tenant_scope_for_realm


@celery_app.task(bind=True, max_retries=3)
def run_analysis_task(self, company_id: str, period: str) -> dict:
    """
    Run the AI analysis pipeline for a specific company + period.
    Usually chained from a sync task or triggered manually.
    """
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, company_id):
            result = run_analysis_pipeline(db, company_id, period)  # noqa: F841
            return {"status": result.get("status", "partial"), "company_id": company_id, "period": period, "result": result, "failed_stages": result.get("failed_stages", [])}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=60)  # RETRY-FIXED-OK: pre-existing legacy cadence, unchanged by this RLS-wiring edit
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2)
def process_pending_sync_events(self) -> None:
    """
    Periodic task: scan for PENDING SyncEvents and run the pipeline.
    Can be called via celery-beat on a schedule (e.g., every 5 minutes).
    """
    db = SessionLocal()
    try:
        process_pending_events(db)
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=120)  # RETRY-FIXED-OK: pre-existing legacy cadence, unchanged by this RLS-wiring edit
    finally:
        db.close()

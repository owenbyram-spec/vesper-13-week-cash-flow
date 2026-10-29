"""
app/tasks/digest.py
───────────────────
Weekly Celery beat task: sends a financial digest email for every company
that has an email_approver configured in SystemSettings.

Schedule: every Monday at 08:00 UTC (configured in Celery beat schedule).
"""
from __future__ import annotations

import logging

from app.services.errors import safe_error_detail
from app.services.redaction import redact_to_string

logger = logging.getLogger(__name__)


def send_weekly_digest_all() -> dict:
    """
    Celery task — iterate all SystemSettings rows with email_approver set
    and call email_service.send_weekly_digest for each.
    """
    try:
        from app.db import SessionLocal
        from app import models
        from app.services.email_service import send_weekly_digest
        from app.tenant_rls import tenant_scope, tenant_scope_for_realm

        db = SessionLocal()
        try:
            # SWEEP orchestration: enumerating every SystemSettings row is cross-tenant → bypass.
            with tenant_scope(bypass=True):
                settings_rows = (
                    db.query(models.SystemSettings)  # CROSS-TENANT-OK: sweep enumerates every tenant's settings under tenant_scope(bypass=True); each digest is re-pinned per-realm below
                    .filter(models.SystemSettings.email_approver.isnot(None))
                    .all()
                )
            sent = 0
            errors = 0
            for row in settings_rows:
                # Per-tenant unit re-scoped to this realm; never left under bypass.
                with tenant_scope_for_realm(db, row.realm_id):
                    try:
                        send_weekly_digest(row.realm_id, row.email_approver)
                        sent += 1
                        logger.info(f"Weekly digest sent for realm {row.realm_id} → {row.email_approver}")
                    except Exception as e:
                        errors += 1
                        logger.error(
                            "Failed digest for realm %s: %s",
                            row.realm_id,
                            redact_to_string(str(e), max_length=500),
                        )
            return {"sent": sent, "errors": errors}
        finally:
            db.close()

    except Exception as exc:
        logger.error(
            "send_weekly_digest_all failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
        return {
            "status": "error",
            "error": safe_error_detail(
                error_code="weekly_digest_failed",
                user_message="Weekly digest dispatch failed.",
                exc=exc,
                retryable=True,
                log_context="weekly_digest_failed",
            ),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Celery task registration
# ─────────────────────────────────────────────────────────────────────────────
# The beat schedule entry ("weekly-digest-monday-8am", Monday 08:00 UTC) lives
# in app/celery_app.py alongside every other beat entry. This module must be
# imported by app/tasks/__init__.py so workers register the task.
try:
    from app.celery_app import celery_app  # type: ignore

    @celery_app.task(name="tasks.send_weekly_digest_all", bind=True, max_retries=2)
    def weekly_digest_task(self):
        return send_weekly_digest_all()

except (ImportError, AttributeError):
    # Celery not available (e.g. in test/dev without a broker) — that's fine
    pass

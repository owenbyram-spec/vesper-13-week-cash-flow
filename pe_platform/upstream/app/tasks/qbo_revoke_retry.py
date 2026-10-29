"""
Celery task: bounded background retry of failed Intuit token revokes.

When DELETE /qbo/disconnect cannot revoke the grant at Intuit, the QBOToken
row is kept with revoke_status="revoke_failed" so the revoke can be retried.
Without this task the retry only happened if someone called disconnect again,
so an incomplete disconnect could linger indefinitely and invisibly.

This sweep retries every revoke_failed row up to MAX_REVOKE_RETRY_ATTEMPTS
total failed attempts. Beyond the cap the row is left alone (still surfaced
as "disconnect_incomplete" in connection metadata, and still retried by any
explicit disconnect call) so a permanently broken grant cannot generate
unbounded Intuit traffic.

Log hygiene: only realm ids, attempt counts, and short error categories are
logged — never token material or Intuit response bodies.
"""
import logging
from datetime import datetime, timezone

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import QBOToken
from ..services.auth_migration import record_auth_event
from ..services.qbo_token_revoke import revoke_intuit_token
from ..services.tenant_companies import organization_for_company
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)

# Total failed attempts (including the original disconnect failure) after
# which the background sweep stops retrying.
MAX_REVOKE_RETRY_ATTEMPTS = 5


def retry_failed_revokes(db) -> dict:
    """Core sweep, separated from the Celery wrapper for testability."""
    # SWEEP orchestration: enumerating every revoke_failed token is cross-tenant → bypass.
    with tenant_scope(bypass=True):
        rows = db.query(QBOToken).filter(QBOToken.revoke_status == "revoke_failed").all()  # CROSS-TENANT-OK: sweep enumerates every tenant's revoke_failed tokens under tenant_scope(bypass=True); each is re-pinned per-realm below
    summary = {"eligible": 0, "revoked": 0, "still_failed": 0, "exhausted": 0}

    for row in rows:
        attempts = row.revoke_attempts or 0
        if attempts >= MAX_REVOKE_RETRY_ATTEMPTS:
            summary["exhausted"] += 1
            continue
        summary["eligible"] += 1

        realm_id = row.realm_id
        # Per-tenant unit re-scoped to this realm; never left under bypass.
        with tenant_scope_for_realm(db, realm_id):
            org = organization_for_company(db, realm_id)
            revoked, category = revoke_intuit_token(row.refresh_token)
            if revoked:
                db.delete(row)
                record_auth_event(
                    db,
                    event_type="qbo_token_revoked",
                    organization=org,
                    details={
                        "realm_id": realm_id,
                        "revoke_result": category or "revoked",
                        "retried_by": "background_revoke_retry",
                        "attempts_before_success": attempts,
                    },
                )
                record_auth_event(
                    db,
                    event_type="qbo_token_deleted",
                    organization=org,
                    details={"realm_id": realm_id, "retried_by": "background_revoke_retry"},
                )
                summary["revoked"] += 1
                logger.info(
                    "Background revoke retry succeeded for realm_id=%s after %s failed attempt(s)",
                    realm_id,
                    attempts,
                )
            else:
                row.revoke_attempts = attempts + 1
                row.revoke_failed_reason = category
                row.revoke_attempted_at = datetime.now(timezone.utc)
                summary["still_failed"] += 1
                logger.warning(
                    "Background revoke retry failed for realm_id=%s (attempt %s/%s, reason=%s)",
                    realm_id,
                    attempts + 1,
                    MAX_REVOKE_RETRY_ATTEMPTS,
                    category,
                )

    # Multi-tenant flush of the per-row mutations above → under bypass.
    with tenant_scope(bypass=True):
        db.commit()
    return summary


@celery_app.task(bind=True, max_retries=1)
def retry_failed_qbo_revokes(self):
    """Periodic bounded retry of Intuit revokes that failed on disconnect."""
    db = SessionLocal()
    try:
        summary = retry_failed_revokes(db)
        if summary["eligible"] or summary["exhausted"]:
            logger.info("QBO revoke retry sweep: %s", summary)
        return summary
    finally:
        db.close()

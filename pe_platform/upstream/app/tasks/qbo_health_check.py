# PLAN-EXEMPT: QBO connection-health revocation-detection (AC-6) — the proactive health sweep.
"""Proactive QBO connection-health check (revocation-detection AC-6).

A QuickBooks OAuth grant can be revoked out-of-band on Intuit's side while the stored
token is still clock-fresh (``access_expires_at`` in the future). Nothing on the read path
notices until a customer hits a broken screen. This beat-scheduled sweep pings each
connected realm with a CHEAP call (``SELECT * FROM CompanyInfo``) so a dead grant is
DISCOVERED — flagged ``reconnect_required`` + an auth event recorded — before a user does.

Shape (mirrors ``covenant_snapshot`` / ``metric_snapshots``):
  - ``check_all_qbo_connections`` — the beat-scheduled fan-out (AP-CEL-9): enumerate mapped
    realms under a vetted bypass and dispatch one realm-scoped child each, so a single
    realm's QBO work stays well under the broker visibility_timeout.
  - ``check_qbo_connection_health`` — ONE realm: pins ``tenant_scope_for_realm`` (org-keyed
    ``qbo_tokens`` needs the org GUC) and probes the connection.

Detection semantics (``_probe_realm_health``):
  - not connected / already-flagged → skip (recovery is driven by the /qbo/authorize
    reconnect flow, which clears the flag; the sweep is for NEWLY-dead grants).
  - refresh token itself expired → dead → flag it (no Intuit call with a dead token).
  - otherwise probe: ``get_qbo_client_from_db`` (self-heals a merely-stale token) + a
    CompanyInfo query. A DEAD grant surfaces as the 409 reconnect signal — the self-heal
    seam has already flagged ``reconnect_required`` + recorded the ``qbo_token_revoked``
    event by then (the transition record). A TRANSIENT 503 (network / 5xx) is NOT treated
    as dead — a blip must never mass-flag every connection.

Bounded + idempotent: setting the flag is idempotent (a re-run of a dead realm is a no-op),
and a health-check failure for one realm NEVER crashes the beat (each realm is isolated).
Log hygiene: only realm ids, statuses, and short error CATEGORIES — never token material or
Intuit response bodies.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import HTTPException

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)

# A cheap, read-only liveness probe. CompanyInfo is one row and is the same entity the
# OAuth callback syncs first, so a healthy grant always answers it.
_HEALTH_PROBE_QUERY = "SELECT * FROM CompanyInfo"


def _probe_realm_health(db, realm_id: str) -> dict:
    """Probe ONE realm's connection and set/clear the dead-grant flag. Pure unit of work
    (the task + tests both drive this). Assumes the caller has pinned the realm scope."""
    from ..models import QBOToken
    from ..qbo_client import _mark_token_reconnect_required, get_qbo_client_from_db

    realm = str(realm_id)
    token = db.query(QBOToken).filter(QBOToken.realm_id == realm).first()
    if token is None:
        return {"realm_id": realm, "status": "not_connected"}
    if getattr(token, "revoke_status", None) == "revoke_failed":
        # Mid-disconnect (Intuit revoke failed); the revoke-retry sweep owns this row.
        return {"realm_id": realm, "status": "disconnect_incomplete"}
    if token.reconnect_required:
        # Already known-dead — recovery is driven by the /qbo/authorize reconnect flow,
        # which clears the flag; re-probing a dead grant only wastes Intuit calls.
        return {"realm_id": realm, "status": "already_flagged"}

    now = datetime.utcnow()
    if token.refresh_expires_at and token.refresh_expires_at <= now:
        # The refresh token itself has expired → the grant is dead. Flag it WITHOUT an
        # Intuit call (a dead refresh token would only invalid_grant).
        _mark_token_reconnect_required(db, token, "refresh_token_expired")
        return {"realm_id": realm, "status": "newly_dead", "reason": "refresh_token_expired"}

    try:
        client = get_qbo_client_from_db(db, realm)
        client.query(_HEALTH_PROBE_QUERY)
    except HTTPException as exc:
        # The self-heal seam (force-refresh → retry) already flagged reconnect_required +
        # recorded the qbo_token_revoked transition event for a DEAD grant (409). A 503 is
        # a TRANSIENT blip (network / 5xx) — do NOT treat it as dead.
        db.rollback()
        db.refresh(token)
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if token.reconnect_required:
            return {"realm_id": realm, "status": "newly_dead",
                    "reason": token.last_health_error or detail.get("error_code")}
        return {"realm_id": realm, "status": "transient_error",
                "error_code": detail.get("error_code") or exc.status_code}

    # Healthy — record the successful liveness time.
    db.refresh(token)
    token.health_checked_at = datetime.utcnow()
    db.commit()
    return {"realm_id": realm, "status": "healthy"}


@celery_app.task(bind=True, max_retries=2, name="app.tasks.qbo_health_check.check_qbo_connection_health")
def check_qbo_connection_health(self, qbo_realm_id: str | None = None) -> dict:
    """Probe ONE realm's QBO connection for a revoked/dead grant. Strictly realm-scoped.

    IDEMPOTENT-OK: setting/leaving the reconnect_required flag is idempotent — a redelivery
    re-probes and converges to the same flag, never duplicating work or side effects."""
    if not qbo_realm_id:
        logger.warning("[qbo_health] missing qbo_realm_id; skipping")
        return {"status": "skipped", "reason": "missing_realm"}

    db = SessionLocal()
    try:
        # Pin org+realm (resolved from the realm) so the org-keyed qbo_tokens read/write
        # passes FORCE RLS — the systemic RLS-write lesson (metric_snapshots / covenants).
        with tenant_scope_for_realm(db, qbo_realm_id):
            result = _probe_realm_health(db, qbo_realm_id)
        logger.info("[qbo_health] realm=%s status=%s", qbo_realm_id, result.get("status"))
        return result
    except Exception as exc:  # noqa: BLE001 — one realm's failure must never crash the beat
        db.rollback()
        logger.error("[qbo_health] realm=%s probe failed: %s",
                     qbo_realm_id, redact_to_string(str(exc), max_length=300))
        # Capped exponential backoff (AP-CEL-4): 120s, 240s, bounded at 480s.
        raise self.retry(exc=exc, countdown=min(120 * (2 ** self.request.retries), 480)) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.qbo_health_check.check_all_qbo_connections")
def check_all_qbo_connections() -> dict:
    """Beat-scheduled fan-out (AP-CEL-9): enqueue ``check_qbo_connection_health`` for every
    mapped realm, each strictly scoped to its own realm. Mirrors ``snapshot_all_covenants``."""
    from ..models import OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    db = SessionLocal()
    try:
        # SWEEP orchestration: the cross-tenant enumeration runs under bypass; each fanned-out
        # child re-pins its own realm scope (mirrors capture_all_metric_snapshots).
        with tenant_scope(bypass=True):  # CROSS-TENANT-OK: vetted health sweep over OrganizationCompany (RLS-excluded mapping); each child re-pins its realm
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
        check_qbo_connection_health.delay(qbo_realm_id=company.realm_id)
        dispatched += 1
    logger.info("[qbo_health] dispatched=%s health probes", dispatched)
    return {"dispatched": dispatched}

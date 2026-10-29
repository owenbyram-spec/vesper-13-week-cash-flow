"""
app/tasks/provisioning.py
─────────────────────────
Asynchronous execution of tenant provisioning requests.

``execute_provisioning_run`` loads the persisted ``TenantProvisioningRequest``
row by id and executes ``ProvisioningService.run_request``. The task never
assumes ambient admin identity: the acting user is resolved from the persisted
``requested_by_user_id`` on the request row (which was set by the
platform-admin-gated endpoint that created the request). If that context is
missing the task fails the request explicitly rather than running unattributed.

The run itself is step-resumable: ``run_request`` skips steps that already
succeeded in a prior run, so a retried/failed request resumes at the failed
step without duplicating Stytch orgs, admin users, or billing rows.
"""
import logging

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..tenant_rls import clear_tenant_scope, set_tenant_scope

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=0, name="app.tasks.provisioning.execute_provisioning_run")
def execute_provisioning_run(self, request_id: int):
    """Execute (or resume) one tenant provisioning request in the background.

    All context is loaded from the persisted request row; nothing is taken
    from the enqueuing process beyond the request id. Safe to enqueue more
    than once: an already-succeeded request is a no-op (``reused``), and a
    partially-completed request resumes at its first incomplete step.
    """
    from ..models import TenantProvisioningRequest, User
    from ..services.tenant_provisioning import (
        ProvisioningBlocked,
        ProvisioningFailed,
        ProvisioningService,
        utcnow,
    )

    db = SessionLocal()
    try:
        # Vetted system bypass: provisioning CREATES the org and writes almost
        # entirely to the excluded resolution/auth chain (Organization /
        # OrganizationMembership / User / TenantBilling — not RLS'd), and
        # request.organization_id is None until assigned mid-run. Scoping by a
        # null org would fail-close to zero rows.
        # set (emit-next-begin) is CORRECT here, NOT pin (emit-now): `db` is a fresh
        # SessionLocal() with no query run before this line, so the FIRST query below
        # (the request lookup) opens the transaction and the begin-listener emits this
        # bypass scope onto it. Do NOT convert to pin_tenant_scope_live — the
        # INCIDENT-2 bug is set-on-an-ALREADY-OPEN transaction, not this case.
        set_tenant_scope(bypass=True)
        request = (
            db.query(TenantProvisioningRequest)
            .filter(TenantProvisioningRequest.id == request_id)
            .first()
        )
        if not request:
            logger.warning("[execute_provisioning_run] request_id=%s not found; skipping", request_id)
            return {"status": "skipped", "reason": "request_not_found", "request_id": request_id}

        if request.status == "succeeded" and request.organization_id:
            logger.info("[execute_provisioning_run] request_id=%s already succeeded; no-op", request_id)
            return {"status": "succeeded", "request_id": request_id, "reused": True}

        actor = (
            db.query(User).filter(User.id == request.requested_by_user_id).first()
            if request.requested_by_user_id
            else None
        )
        if actor is None or not actor.is_priority_admin:
            # No persisted platform-admin context — fail explicitly instead of
            # running provisioning without an attributable, authorized actor.
            from ..models import AuthAuditEvent

            request.status = "failed"
            request.error = "missing or unauthorized requesting admin context"
            request.updated_at = utcnow()
            db.add(
                AuthAuditEvent(
                    user_id=None,
                    organization_id=request.organization_id,
                    event_type="tenant_provisioning_failed",
                    event_status="failure",
                    details={"request_id": request.id, "mode": request.mode, "reason": "missing_actor"},
                )
            )
            db.commit()
            logger.error(
                "[execute_provisioning_run] request_id=%s has no valid platform-admin actor; failed",
                request_id,
            )
            return {"status": "failed", "reason": "missing_actor", "request_id": request_id}

        service = ProvisioningService(db, actor=actor)
        try:
            result = service.run_request(request)
            return {"status": result.get("status"), "request_id": request_id, "reused": bool(result.get("reused"))}
        except ProvisioningBlocked as exc:
            # Terminal policy block — already persisted/audited by the service.
            logger.warning(
                "[execute_provisioning_run] request_id=%s blocked: %s",
                request_id,
                redact_to_string(str(exc), max_length=500),
            )
            return {"status": "blocked", "request_id": request_id}
        except ProvisioningFailed as exc:
            # Failure state + failed step are persisted/audited by the service;
            # repair happens via the admin retry endpoint, not blind re-runs.
            logger.error(
                "[execute_provisioning_run] request_id=%s failed: %s",
                request_id,
                redact_to_string(str(exc), max_length=500),
            )
            return {"status": "failed", "request_id": request_id}
    finally:
        clear_tenant_scope()
        db.close()

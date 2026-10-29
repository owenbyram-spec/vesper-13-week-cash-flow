"""
app/tasks/data_deletion.py
───────────────────────────
Celery task that HARD-deletes one organization's data after a confirmed,
grace-elapsed (or platform-admin-forced) deletion request.

Order of operations:
  1. Re-validate the deletion request is confirmed and (grace elapsed OR forced).
  2. Delete the org's QBO OAuth tokens (and one-time OAuth states); their grants
     are revoked at Intuit when the transaction commits.
  3. Delete every org-scoped row in FK-safe order (leaf → root) via the shared
     ``tenant_data_scope`` registry — the SAME scope used by export, so the set
     of rows considered "this tenant's" is identical and another tenant's rows
     are never touched.
  4. Delete the organization row, its memberships, and its export records, and
     COMMIT (which fires the Intuit revokes).
  5. Delete the org's export bundle objects and document originals from storage,
     and the Stytch organization.

External side effects run only after the commit: a database failure rolls the
whole delete back and leaves the tenant's QuickBooks grants intact, so a failed
request can be confirmed again from the admin flow and retried.

Strict tenant isolation: every delete is filtered by organization scope, so a
deletion can never reach into another tenant's rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..celery_app import celery_app
from ..db import SessionLocal
from .. import models
from ..services import object_storage
from ..services import tenant_export_storage as storage
from ..services.stytch_b2b import stytch_b2b_client
from ..services.tenant_data_scope import MODEL_SCOPES, realm_in, resolve_tenant_scope, scoped_query
from ..services.qbo_token_revoke import revoke_intuit_token, revoke_purged_grants_on_commit
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_live

logger = logging.getLogger(__name__)

# Statuses from which the admin confirm step may (re)start a hard delete. A
# "failed" request rolled back completely, so confirming it again is a retry.
CONFIRMABLE_STATUSES = frozenset({"pending_deletion", "confirmed", "failed"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def deletion_is_ready(request: "models.TenantDeletionRequest") -> bool:
    """Whether a confirmed request may proceed to hard delete now."""
    if request.status not in {"confirmed"}:
        return False
    if request.forced:
        return True
    grace_until = _as_aware(request.grace_until)
    return grace_until is not None and _now() >= grace_until


def _revoke_qbo_tokens(db, scope) -> int:
    """
    Delete the org's QBO OAuth tokens and durable OAuth states in the current
    transaction, and revoke their grants at Intuit once — and only if — that
    transaction commits (best effort: revoke failures are logged, never raised).
    Returns the number of token rows deleted.
    """
    # A QBO token belongs to this tenant if it is bound to the org OR to one of
    # the org's realms. Both predicates are inside the tenant boundary, so the
    # union can never reach another tenant's tokens.
    predicate = (models.QBOToken.organization_id == scope.organization_id) | realm_in(
        models.QBOToken.realm_id, scope.realm_ids
    )
    rows = db.query(models.QBOToken).filter(predicate).all()
    revoke_purged_grants_on_commit(
        db,
        [(row.realm_id, row.refresh_token) for row in rows],
        log_prefix="Tenant deletion",
        revoke=revoke_intuit_token,
    )
    for row in rows:
        db.delete(row)

    db.query(models.QBOOAuthState).filter(
        models.QBOOAuthState.organization_id == scope.organization_id
    ).delete(synchronize_session=False)
    return len(rows)


def run_hard_delete(db, deletion_request_id: int) -> dict[str, Any]:
    """Synchronous core of the deletion task (callable directly in tests)."""
    # RLS backstop: a tenant hard-delete loads its request entity and then sweeps
    # both org-scoped tenant tables AND the excluded resolution/auth chain
    # (organizations, organization_memberships, users, tenant_data_exports — none
    # RLS'd). Per the deletion caveat, run the whole unit under the vetted
    # cross-tenant bypass so the policy never zero-rows a legitimate sweep.
    with tenant_scope(bypass=True):
        request = (
            db.query(models.TenantDeletionRequest)
            .filter(models.TenantDeletionRequest.id == deletion_request_id)
            .first()
        )
        if request is None:
            raise ValueError(f"TenantDeletionRequest {deletion_request_id} not found")

        if not deletion_is_ready(request):
            raise ValueError(
                "Deletion request is not ready: must be confirmed and past the grace "
                "period (or forced by a platform admin)."
            )

        organization_id = request.organization_id
        org = (
            db.query(models.Organization)
            .filter(models.Organization.id == organization_id)
            .with_for_update()
            .first()
        )
        stytch_org_id = org.stytch_organization_id if org else None

        deleted_counts: dict[str, int] = {}

        try:
            scope = resolve_tenant_scope(db, organization_id)

            # 1. Delete QBO tokens + OAuth states (the Intuit revoke fires on commit).
            deleted_counts["qbo_tokens"] = _revoke_qbo_tokens(db, scope)
            db.flush()

            # 2. Delete export records for this org (and their bundles afterwards).
            deleted_counts["tenant_data_exports"] = (
                db.query(models.TenantDataExport)
                .filter(models.TenantDataExport.organization_id == organization_id)
                .delete(synchronize_session=False)
            )

            # 2b. Break the cash_forecast_actuals self-referential parent_id FK
            #     (no ON DELETE rule) before bulk-deleting the rows, so a parent row
            #     is never removed while a sibling child still points at it.
            db.query(models.CashForecastActual).filter(
                realm_in(models.CashForecastActual.company_id, scope.realm_ids)
            ).update({models.CashForecastActual.parent_id: None}, synchronize_session=False)
            db.flush()

            # 3. Delete every org-scoped row in FK-safe order (leaf → root).
            #    The org's auth_audit_events are intentionally PRESERVED for the
            #    compliance trail; their organization_id FK is ON DELETE SET NULL, so
            #    deleting the org row below detaches them rather than removing them.
            for spec in MODEL_SCOPES:
                table = spec.model.__tablename__
                if spec.review_owned:
                    continue  # preflight proved absence; no neutral purge is authorized
                if table == "qbo_tokens":
                    continue  # already handled above
                if table == "auth_audit_events":
                    continue  # preserve audit trail
                # SA-OK: bounded-loop over the fixed MODEL_SCOPES registry — one bulk DELETE per scoped table, not a per-row N+1 (pre-existing logic, re-indented under tenant_scope)
                if spec.model is models.FirmOperationsSnapshot:
                    with tenant_scope_live(db, org_id=organization_id, realm_id=None, bypass=False):
                        count = scoped_query(db, spec, scope).delete(synchronize_session=False)
                else:
                    count = scoped_query(db, spec, scope).delete(synchronize_session=False)
                deleted_counts[table] = deleted_counts.get(table, 0) + int(count)
            db.flush()

            # 4. Stamp THIS deletion request completed and record counts BEFORE the
            #    org row is removed. The request row's organization_id FK is
            #    ON DELETE CASCADE, so deleting the org will cascade-delete every
            #    deletion request for this org at DB flush — the durable audit trail
            #    lives in AuthAuditEvent (written by the confirm endpoint). We still
            #    stamp the request so an in-process caller / return value is accurate.
            request.status = "completed"
            request.completed_at = _now()
            request.deleted_row_counts_json = deleted_counts
            db.flush()

            # 5. Detach shared User rows that still point at this org via the
            #    no-cascade default_organization_id FK, so deleting the org cannot
            #    violate that constraint. Users are shared identity rows and are NOT
            #    deleted here.
            db.query(models.User).filter(
                models.User.default_organization_id == organization_id
            ).update({models.User.default_organization_id: None}, synchronize_session=False)
            db.flush()

            # 6. Delete the organization row LAST. This cascades (DB-level) to the
            #    org's remaining FK-CASCADE children, including deletion requests and
            #    memberships, leaving zero rows for this organization.
            if org is not None:
                db.delete(org)
                deleted_counts["organizations"] = 1
            db.flush()

            db.commit()
        except Exception as exc:
            db.rollback()
            req = (
                db.query(models.TenantDeletionRequest)
                .filter(models.TenantDeletionRequest.id == deletion_request_id)
                .first()
            )
            if req is not None:
                req.status = "failed"
                req.error = redact_to_string(str(exc), max_length=1000)
                db.commit()
            logger.error(
                "[TenantDeletion] hard delete for org %s failed: %s",
                organization_id,
                redact_to_string(str(exc), max_length=500),
            )
            raise

        # 7. External side effects AFTER the DB transaction commits.
        storage.delete_org_bundles(organization_id)

        # 7b. GDPR: purge the org's stored original document objects from object
        #     storage. Idempotent + unconfigured-safe: delete_prefix returns False
        #     when storage is off (or on any error) and must NOT fail the deletion —
        #     the tenant's rows are already gone; the originals purge is best-effort.
        object_storage.delete_prefix(f"tenants/{organization_id}/")

        if stytch_org_id and stytch_b2b_client.enabled:
            try:
                stytch_b2b_client.delete_organization(stytch_org_id)
            except Exception as exc:  # pragma: no cover - external best effort
                logger.warning(
                    "[TenantDeletion] Stytch org delete failed for %s: %s",
                    stytch_org_id,
                    redact_to_string(str(exc), max_length=300),
                )

        return {"organization_id": organization_id, "deleted_counts": deleted_counts}


@celery_app.task(bind=True, max_retries=2, name="app.tasks.data_deletion.hard_delete_tenant")
def hard_delete_tenant(self, deletion_request_id: int):
    """Celery entrypoint: hard-delete an organization's data."""
    db = SessionLocal()
    try:
        return run_hard_delete(db, deletion_request_id)
    except ValueError:
        raise
    except Exception as exc:
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()

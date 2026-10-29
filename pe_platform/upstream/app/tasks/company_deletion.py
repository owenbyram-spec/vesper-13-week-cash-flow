"""
app/tasks/company_deletion.py
─────────────────────────────
Hard-delete ONE connected company (realm) from its tenant.

Distinct from QB token disconnect (``DELETE /qbo/disconnect``), which only
revokes the OAuth grant and deletes the QBOToken row: this purges EVERY
realm-scoped row using the shared ``tenant_data_scope`` registry, so the
company's cached financial / chat / agent / usage data does not linger after
removal. The company's TENANT (organization) and its OTHER companies are
untouched.

Mirrors app/tasks/data_deletion.py (tenant hard-delete) but scoped to one
realm instead of one organization. As there, the Intuit revoke runs only after
the purge commits, so a failed purge never leaves a revoked company behind.
"""
from __future__ import annotations

import logging
from typing import Any

from ..celery_app import celery_app
from ..db import SessionLocal
from .. import models
from ..services.tenant_data_scope import MODEL_SCOPES
from ..services.qbo_token_revoke import revoke_intuit_token, revoke_purged_grants_on_commit
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope

logger = logging.getLogger(__name__)

# Tenant-level tables that belong to the ORGANIZATION, not to a single company;
# a per-company delete must never touch these.
_ORG_LEVEL_TABLES = {
    "organization_memberships",
    "tenant_billing",
    "auth_audit_events",
    "auth_sessions",
    "qbo_oauth_states",
    "organization_companies",
    "tenant_provisioning_requests",
    "tenant_provisioning_runs",
    "tenant_provisioning_steps",
    "tenant_provisioning_external_resources",
    "tenant_provisioning_notifications",
    "api_keys",
    "tenant_llm_credentials",
    "tenant_task_autonomy",
}

# Handled explicitly (with side effects / FK ordering), so excluded from the
# generic realm-scoped delete loop.
_EXPLICIT_TABLES = {"qbo_tokens", "entity_billing", "user_company_access"}


def run_delete_company(db, *, realm_id: str, cancel_billing: bool = False) -> dict[str, Any]:
    """Synchronous core of the per-company delete (callable directly in tests)."""
    realm_id = str(realm_id).strip()
    # RLS backstop: a per-company hard delete sweeps realm-scoped rows AND touches
    # the excluded resolution/auth chain (organization_companies, qbo_oauth_states,
    # organizations, auth_audit_events — none RLS'd). Run the whole delete under the
    # vetted cross-tenant bypass so the policy never zero-rows a legitimate sweep.
    with tenant_scope(bypass=True):
        company = (
            db.query(models.OrganizationCompany)
            .filter(models.OrganizationCompany.realm_id == realm_id)
            .with_for_update()
            .first()
        )
        if company is None:
            raise ValueError(f"No connected company for realm_id {realm_id}")
        org_id = company.organization_id
        org_company_id = company.id
        deleted: dict[str, int] = {}
        billing_canceled = False

        # Neutral review retention is deliberately unsupported in this tranche.
        # This includes review children of QBO companies: provider type does not
        # grant permission to discard their rows or retained object originals.
        # Hold the company lock through the successful legacy delete transaction.
        try:
            review_retention_preflight(
                db, organization_id=org_id, organization_company_ids=(org_company_id,),
            )
        except ReviewRetentionLifecycleRequired as exc:
            db.rollback()
            db.add(models.AuthAuditEvent(
                organization_id=org_id, event_type="company_deletion_blocked",
                event_status="blocked", actor_email="system:company_deletion",
                details={
                    "state": REVIEW_RETENTION_STATE, "reason": exc.reason,
                    "organization_company_id": org_company_id,
                    "review_row_counts": exc.row_counts,
                    "review_object_prefixes": list(exc.object_prefixes),
                },
            ))
            db.commit()
            raise

        try:
            # 0. Cancel the company's Stripe subscription FIRST (best-effort) so the
            #    purge below does not orphan an active subscription that keeps
            #    charging. Only when explicitly acked (cancel_billing=True).
            entity_billing = (
                db.query(models.EntityBilling)
                .filter(models.EntityBilling.organization_company_id == org_company_id)
                .first()
            )
            if cancel_billing and entity_billing and entity_billing.stripe_subscription_id:
                from ..services.stripe import cancel_subscription

                billing_canceled = cancel_subscription(entity_billing.stripe_subscription_id)
                if not billing_canceled:
                    logger.warning(
                        "[CompanyDeletion] Stripe subscription cancel failed/no-op realm=%s sub=%s "
                        "(local billing row still purged)",
                        realm_id,
                        entity_billing.stripe_subscription_id,
                    )

            # 1. Purge QBO tokens for this realm; their grants are revoked at Intuit
            #    when — and only if — this transaction commits.
            token_rows = db.query(models.QBOToken).filter(models.QBOToken.realm_id == realm_id).all()
            revoke_purged_grants_on_commit(
                db,
                [(tok.realm_id, tok.refresh_token) for tok in token_rows],
                log_prefix="[CompanyDeletion]",
                revoke=revoke_intuit_token,
            )
            for tok in token_rows:
                db.delete(tok)
            deleted["qbo_tokens"] = len(token_rows)

            # 2. One-time OAuth states intended for this realm.
            deleted["qbo_oauth_states"] = (
                db.query(models.QBOOAuthState)
                .filter(models.QBOOAuthState.intended_realm_id == realm_id)
                .delete(synchronize_session=False)
            )
            db.flush()

            # 2b. Break the cash_forecast_actuals self-referential parent_id FK before
            #     bulk-deleting (mirrors data_deletion.py).
            db.query(models.CashForecastActual).filter(
                models.CashForecastActual.company_id == realm_id
            ).update({models.CashForecastActual.parent_id: None}, synchronize_session=False)
            db.flush()

            # 3. Delete every realm-scoped row from the shared registry (leaf → root).
            #    Filter by realm only (realm is globally unique to this company), so
            #    sibling companies and tenant-level rows are untouched.
            from sqlalchemy import or_

            for spec in MODEL_SCOPES:
                table = spec.model.__tablename__
                if spec.review_owned:
                    continue  # preflight proved absence; never purge review history
                if table in _ORG_LEVEL_TABLES or table in _EXPLICIT_TABLES:
                    continue
                if not spec.realm_attrs:
                    continue  # not a per-company table
                realm_conditions = [getattr(spec.model, attr) == realm_id for attr in spec.realm_attrs]
                # SA-OK: bounded-loop over the fixed MODEL_SCOPES registry — one bulk DELETE per scoped table, not a per-row N+1 (pre-existing logic, re-indented under tenant_scope)
                count = db.query(spec.model).filter(or_(*realm_conditions)).delete(synchronize_session=False)
                deleted[table] = deleted.get(table, 0) + int(count)
            db.flush()

            # 4. EntityBilling (after Stripe cancel above) and the members' per-company
            #    grants on this company, then the OrganizationCompany.
            deleted["entity_billing"] = (
                db.query(models.EntityBilling)
                .filter(models.EntityBilling.organization_company_id == org_company_id)
                .delete(synchronize_session=False)
            )
            deleted["user_company_access"] = (
                db.query(models.UserCompanyAccess)
                .filter(models.UserCompanyAccess.organization_company_id == org_company_id)
                .delete(synchronize_session=False)
            )

            # 5. Repoint the org's primary_realm_id if this realm was its primary.
            org = db.query(models.Organization).filter(models.Organization.id == org_id).first()
            if org and org.primary_realm_id == realm_id:
                nxt = (
                    db.query(models.OrganizationCompany.realm_id)
                    .filter(
                        models.OrganizationCompany.organization_id == org_id,
                        models.OrganizationCompany.realm_id != realm_id,
                    )
                    .order_by(models.OrganizationCompany.is_primary.desc(), models.OrganizationCompany.realm_id.asc())
                    .first()
                )
                org.primary_realm_id = nxt.realm_id if nxt else None

            db.delete(company)
            deleted["organization_companies"] = 1

            # 6. Completion audit on the org (preserved; matches tenant-delete policy).
            db.add(
                models.AuthAuditEvent(
                    organization_id=org_id,
                    event_type="company_deletion_completed",
                    event_status="success",
                    actor_email="system:company_deletion",
                    details={
                        "realm_id": realm_id,
                        "organization_id": org_id,
                        "stripe_subscription_canceled": billing_canceled,
                        "deleted_row_counts": deleted,
                    },
                )
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error(
                "[CompanyDeletion] realm %s failed: %s",
                realm_id,
                redact_to_string(str(exc), max_length=500),
            )
            raise

    return {
        "realm_id": realm_id,
        "organization_id": org_id,
        "stripe_subscription_canceled": billing_canceled,
        "deleted_counts": deleted,
    }


@celery_app.task(bind=True, max_retries=2, name="app.tasks.company_deletion.hard_delete_company")
def hard_delete_company(self, realm_id: str, cancel_billing: bool = False):
    """Celery entrypoint: hard-delete one connected company's data."""
    db = SessionLocal()
    try:
        return run_delete_company(db, realm_id=realm_id, cancel_billing=cancel_billing)
    except ValueError:
        raise
    except Exception as exc:
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()

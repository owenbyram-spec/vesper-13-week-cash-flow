"""
app/tasks/realm_context.py
──────────────────────────
Background precompute of per-realm context (audit item S6).

``build_realm_context`` runs ``RealmContextBuilder`` for ONE realm — refreshing
its isolated ``RealmContextProfile`` and the precomputed onboarding-recommendation
``RealmContextArtifact`` rows off the request path.

Wiring:
  * chained after ``sync_all_data`` completes for the just-synced realm, and
  * scheduled nightly (celery beat) for every mapped realm via
    ``build_all_realm_contexts``.

The full scope triple is resolved from the realm id inside the task; if the realm
is not mapped to an organization + organization_company the task is a safe no-op
(it never writes loosely-scoped context).
"""
import logging

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.errors import safe_error_detail
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, name="app.tasks.realm_context.build_realm_context")
def build_realm_context(
    self,
    organization_id: int | None = None,
    organization_company_id: int | None = None,
    qbo_realm_id: str | None = None,
):
    """Build / refresh the isolated context profile + artifacts for one realm.

    Accepts either the full pre-resolved triple (preferred — the post-sync hook
    passes it) or just ``qbo_realm_id`` (the triple is then resolved from the
    realm mapping). Strictly realm-scoped; never touches another realm."""
    from ..services.realm_context_builder import (
        RealmContextBuilder,
        RealmIsolationError,
    )
    from ..services.tenant_companies import (
        company_is_suspended,
        organization_company_for_realm,
        organization_for_company,
    )

    if not qbo_realm_id:
        logger.warning("[build_realm_context] missing qbo_realm_id; skipping")
        return {"status": "skipped", "reason": "missing_realm"}

    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() unchanged at end of this try (re-indent only)
    try:
        # ORG-keyed SINGLE: pin RLS to this realm (and org when pre-resolved) for the
        # whole build. realm id is always present; org id is None until resolved
        # (tenant_scope tolerates that). Strictly realm-scoped — never another realm.
        with tenant_scope(org_id=organization_id, realm_id=qbo_realm_id):
            organization_company = None
            if organization_id is not None and organization_company_id is not None:
                organization_company = organization_company_for_realm(
                    db,
                    organization_id=organization_id,
                    realm_id=qbo_realm_id,
                )
                if organization_company and organization_company.id != organization_company_id:
                    raise RealmIsolationError(
                        f"realm {qbo_realm_id} maps to organization_company {organization_company.id}, "
                        f"not {organization_company_id}"
                    )
            else:
                organization = organization_for_company(db, qbo_realm_id)
                if organization is not None:
                    organization_company = organization_company_for_realm(
                        db,
                        organization_id=organization.id,
                        realm_id=qbo_realm_id,
                    )
            if company_is_suspended(db, organization_company=organization_company):
                logger.info("[build_realm_context] realm=%s skipped: company suspended", qbo_realm_id)
                return {"status": "skipped", "reason": "company_suspended", "qbo_realm_id": qbo_realm_id}

            if organization_id is not None and organization_company_id is not None:
                builder = RealmContextBuilder(
                    db,
                    organization_id=organization_id,
                    organization_company_id=organization_company_id,
                    qbo_realm_id=qbo_realm_id,
                )
            else:
                builder = RealmContextBuilder.for_realm(db, qbo_realm_id)
            result = builder.build()
            logger.info("[build_realm_context] realm=%s result=%s", qbo_realm_id, result)
            return result
    except RealmIsolationError as exc:
        # Realm not mapped to a full triple — safe no-op, do not retry.
        logger.warning(
            "[build_realm_context] realm=%s not buildable: %s",
            qbo_realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        return {
            "status": "skipped",
            "reason": "realm_not_buildable",
            "qbo_realm_id": qbo_realm_id,
            "errors": [
                safe_error_detail(
                    error_code="realm_context_not_buildable",
                    user_message="Realm context could not be built because the company is not fully mapped.",
                    exc=exc,
                    retryable=False,
                    log_context="realm_context_not_buildable",
                )
            ],
        }
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[build_realm_context] realm=%s failed: %s",
            qbo_realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        raise self.retry(exc=exc, countdown=180)
    finally:
        db.close()


@celery_app.task(name="app.tasks.realm_context.build_all_realm_contexts")
def build_all_realm_contexts():
    """Nightly fan-out: enqueue ``build_realm_context`` for every mapped realm,
    each strictly scoped by its own triple."""
    from ..models import OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() unchanged below (re-indent only)
    try:
        # SWEEP orchestration: the cross-tenant enumeration of all mapped companies runs
        # under bypass; each fanned-out build_realm_context child re-pins its own realm scope.
        with tenant_scope(bypass=True):
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
        build_realm_context.delay(
            organization_id=company.organization_id,
            organization_company_id=company.id,
            qbo_realm_id=company.realm_id,
        )
        dispatched += 1
    logger.info("[build_all_realm_contexts] dispatched=%s", dispatched)
    return {"dispatched": dispatched}

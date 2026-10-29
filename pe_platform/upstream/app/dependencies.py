import json
import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Type

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import (  # noqa: F401
    AuthContext,
    OrgSwitchRequired,
    bind_session_organization,
    get_current_auth_context,
    get_current_user_from_token,
    require_session_organization,
)
from .config import is_dev_environment, settings
from .db import get_db
from .roles import (
    ROLE_WEIGHTS as _ROLE_WEIGHTS,
    is_tenant_admin,
    role_permissions as _role_permissions_canonical,
)
from .models import AuthAuditEvent, Organization, OrganizationCompany, OrganizationMembership, QBOCompanyInfo, QBOToken, User
from .tenant_rls import pin_tenant_scope_live, tenant_scope_live
from .services.tenant_companies import (
    assert_company_belongs_to_organization,
    company_is_suspended,
    granted_org_company_ids,
    organization_company_for_realm,
    organization_for_company,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantContext:
    user: User
    organization: Organization
    membership: Optional[OrganizationMembership]
    organization_id: int
    stytch_organization_id: str
    role: str
    permissions: list[str]
    is_platform_admin: bool
    organization_company: Optional[OrganizationCompany] = None
    organization_company_id: Optional[int] = None
    realm_id: Optional[str] = None
    company_id: Optional[str] = None


class RoleChecker:
    def __init__(self, allowed_roles: List[str]):
        self.allowed_roles = allowed_roles

    def __call__(self, ctx: AuthContext = Depends(get_current_auth_context)):
        if ctx.user.role not in self.allowed_roles and not ctx.user.is_priority_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operation not permitted")
        return ctx.user


require_admin = RoleChecker(["admin"])
require_editor = RoleChecker(["admin", "editor"])


def require_realm_id(company_id: Optional[str] = Query(None), realm_id: Optional[str] = Query(None)) -> str:
    rid = company_id or realm_id
    if not rid or not rid.strip():
        raise HTTPException(status_code=400, detail="company_id or realm_id query parameter is required")
    return rid.strip()


def get_active_company_name(db: Session, realm_id: str = None) -> str:
    if realm_id:
        info = db.query(QBOCompanyInfo).filter(QBOCompanyInfo.realm_id == str(realm_id)).first()
        if info and info.company_name:
            return info.company_name
    info = db.query(QBOCompanyInfo).first()
    if info and info.company_name:
        return info.company_name
    token = db.query(QBOToken).first()
    if token:
        return f"Company {token.realm_id}"
    return "Vesper Bot"


# Canonical role weights + capability mapping live in app/roles.py (single source
# of truth). ROLE_WEIGHTS is re-exported here so existing importers (route guards,
# the agent tool policy, the static-authorization test) keep working unchanged.
ROLE_WEIGHTS = _ROLE_WEIGHTS


def _membership_for_company(db: Session, user: User, company_id: str) -> Optional[tuple[OrganizationMembership, Organization]]:
    org = organization_for_company(db, str(company_id))
    if not org:
        return None
    query = (
        db.query(OrganizationMembership, Organization)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .filter(
            OrganizationMembership.user_id == user.id,
            Organization.id == org.id,
        )
    )
    return query.first()


def user_has_tenant_bindings(db: Session, user: User) -> bool:
    return (
        db.query(OrganizationMembership.id)
        .filter(OrganizationMembership.user_id == user.id)
        .first()
        is not None
    )


def user_can_bypass_tenant_scope(db: Session, user: User) -> bool:
    if user.is_priority_admin:
        return True
    if user.role.upper() != "ADMIN":
        return False
    if not is_dev_environment(settings.environment):
        return False
    return not user_has_tenant_bindings(db, user)


def _audit_tenant_scope_bypass(db: Session, *, user: User, company_id: str, minimum_role: str, reason: str) -> None:
    try:
        db.add(
            AuthAuditEvent(
                user_id=user.id,
                event_type="platform_admin_tenant_scope_bypass",
                event_status="success",
                actor_email=user.email,
                details={
                    "realm_id": company_id,
                    "minimum_role": minimum_role,
                    "reason": reason,
                },
            )
        )
        db.commit()
    except SQLAlchemyError as e:
        # FAIL-CLOSED (DIM-033): a platform-admin tenant-scope bypass MUST leave a
        # forensic record. If the audit row cannot be committed, DENY the bypass
        # rather than grant un-audited cross-tenant access. Previously this
        # swallowed the error and the caller returned the grant anyway.
        db.rollback()
        logger.error("Tenant-scope-bypass audit write failed for user_id=%s; denying bypass", user.id)
        _detail = "Bypass denied: the required audit record could not be written."
        raise HTTPException(status_code=503, detail=_detail) from e


TERMS_ACCEPTANCE_REQUIRED_MESSAGE = "Accept the current Terms of Service to access this organization."


class TermsAcceptanceRequired(HTTPException):
    """Clickwrap Gate #1 refusal on the realm and tenant-admin paths.

    Carries the same code and remediation as the QuickBooks-connect refusal so the
    client can send the member to /onboarding/legal, which stays reachable.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=403,
            detail={
                "code": "assent_required",
                "message": TERMS_ACCEPTANCE_REQUIRED_MESSAGE,
                "remediation": "/onboarding/legal",
            },
        )


def require_accepted_terms(org: Organization) -> None:
    """Clickwrap Gate #1: only an `active` organization reaches tenant data.

    `pending_acceptance`, `reaccept_required` and `assent_backfill_required` orgs are
    refused. Callers exempt platform admins before calling, as the org-context gate
    does; the legal accept/status flow uses require_organization_context_allow_pending.
    """
    if org.status != "active":
        raise TermsAcceptanceRequired()


# Refusals the caller can act on (switch organization, accept the Terms); the
# tenant guard passes them through instead of folding them into a generic 403.
_ACTIONABLE_TENANT_REFUSALS = (OrgSwitchRequired, TermsAcceptanceRequired)


def _check_company_access(db: Session, user: User, company_id: str, minimum_role: str) -> dict:
    if user_can_bypass_tenant_scope(db, user):
        _audit_tenant_scope_bypass(
            db,
            user=user,
            company_id=company_id,
            minimum_role=minimum_role,
            reason="configured_platform_admin" if user.is_priority_admin else "local_legacy_unbound_admin",
        )
        # Vetted, audited cross-tenant bypass → RLS backstop sees app.rls_bypass='on'.
        # pin (emit-now) not set (emit-next-begin): this guard already opened the
        # request transaction resolving access, so a plain set_tenant_scope would
        # never reach the in-flight transaction the handler reads/writes in.
        pin_tenant_scope_live(db, bypass=True)
        return {
            "user": user,
            "role": "INTERNAL_SUPER_ADMIN" if user.is_priority_admin else "LEGACY_ADMIN",
            "company_id": company_id,
        }

    membership_org = _membership_for_company(db, user, company_id)
    if membership_org:
        membership, org = membership_org
        if not org.is_active:
            raise HTTPException(status_code=403, detail="Forbidden: This tenant is suspended.")
        if membership.status != "active":
            raise HTTPException(status_code=403, detail="Forbidden: Your membership is not active.")
        user_weight = ROLE_WEIGHTS.get((membership.role or "read_only").upper(), 0)
        req_weight = ROLE_WEIGHTS.get(minimum_role.upper(), 10)
        if user_weight < req_weight:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: Minimum role {minimum_role} required. You are a {membership.role}.",
            )
        org_company = organization_company_for_realm(db, organization_id=org.id, realm_id=company_id)
        if company_is_suspended(db, organization_company=org_company):
            raise HTTPException(status_code=403, detail="This company is suspended")
        # Per-company least-privilege gate (intra-org access scoping). A member
        # with grant rows is RESTRICTED to exactly those companies; a member with
        # zero grants (None) is unrestricted — the backward-compatible default.
        # FAIL-CLOSED: granted_org_company_ids RAISES on a DB error (it never
        # degrades to all-access), so a failure here propagates to a 5xx rather
        # than silently widening the member. When the member is restricted AND
        # this company is not in the grant set, 404 (hide existence cross-grant,
        # mirroring the cross-tenant 404 convention) — never 403, which would
        # confirm the company exists.
        granted = granted_org_company_ids(db, user, org)
        if granted is not None and (org_company is None or org_company.id not in granted):
            raise HTTPException(status_code=404, detail="Resource not found")
        # The member is entitled to this company, but only through a session issued
        # for its organization: the org's MFA and auth-method policy apply to that
        # session alone, so another org's session must switch first (Stytch exchange).
        require_session_organization(user, org)
        # Clickwrap Gate #1 holds on the realm path too, not only org-context routes.
        require_accepted_terms(org)
        # Pin the RLS backstop GUCs to the session-resolved, authorized tenant.
        # pin (emit-now) onto the already-open request transaction — the access
        # resolution above (membership/org/org_company queries) opened it, so a
        # plain set_tenant_scope would emit only at the NEXT begin and the
        # handler's feature reads/writes would run GUC-less → zero rows / denied
        # writes under FORCE-RLS (INCIDENT-2).
        pin_tenant_scope_live(db, org_id=org.id, realm_id=str(company_id))
        return {"user": user, "role": membership.role, "company_id": company_id}

    raise HTTPException(status_code=403, detail="Forbidden: You do not have access to this company.")


def _role_permissions(role: str) -> list[str]:
    # Capability set is derived from the canonical weight ladder in app/roles.py.
    return _role_permissions_canonical(role)


def _active_org_from_context(
    db: Session, ctx: AuthContext, *, allow_pending: bool = False
) -> tuple[Organization, OrganizationMembership | None, str]:
    if ctx.organization:
        org = ctx.organization
        membership = ctx.membership
    elif ctx.user.default_organization_id:
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.user_id == ctx.user.id,
                OrganizationMembership.organization_id == ctx.user.default_organization_id,
            )
            .first()
        )
        org = db.query(Organization).filter(Organization.id == ctx.user.default_organization_id).first()
    else:
        membership = (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.user_id == ctx.user.id)
            .order_by(OrganizationMembership.is_default.desc(), OrganizationMembership.id.asc())
            .first()
        )
        org = db.query(Organization).filter(Organization.id == membership.organization_id).first() if membership else None

    if not org:
        raise HTTPException(status_code=400, detail="Select an organization before accessing tenant data")
    if not org.is_active and not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail="This tenant is suspended")
    # Clickwrap Gate #1 (plan 20260720-clickwrap-backend-spine AC-6): a non-`active`
    # lifecycle `status` blocks tenant-data access at this shared org-context choke
    # point — an org that has not recorded acceptance of the current Tier-1
    # ToS+Privacy (status `pending_acceptance` / `reaccept_required` /
    # `assent_backfill_required`) cannot reach tenant data. This is the lifecycle
    # axis, ORTHOGONAL to is_active / billing_status (the suspension / billing axes —
    # never overload those). `allow_pending=True` is passed ONLY by the legal-accept +
    # assent-status flow (require_organization_context_allow_pending) so a pending org
    # can reach the accept endpoint and activate — SPEC §9.2 keeps the accept / read /
    # cancel flow always reachable (the chicken-and-egg). Platform super-admins bypass
    # for support, mirroring the is_active exemption above.
    if not allow_pending and org.status != "active" and not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail=TERMS_ACCEPTANCE_REQUIRED_MESSAGE)
    if membership and membership.status != "active":
        raise HTTPException(status_code=403, detail="Your membership is not active")
    if not membership and not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail="You do not belong to this organization")
    role = "internal_super_admin" if ctx.user.is_priority_admin and not membership else (membership.role if membership else "internal_super_admin")
    return org, membership, role


def _tenant_context_from(
    ctx: AuthContext, org: Organization, membership: OrganizationMembership | None, role: str
) -> TenantContext:
    return TenantContext(
        user=ctx.user,
        organization=org,
        membership=membership,
        organization_id=org.id,
        stytch_organization_id=org.stytch_organization_id,
        role=role,
        permissions=_role_permissions(role),
        is_platform_admin=ctx.user.is_priority_admin,
    )


def require_active_organization_context(
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_current_auth_context),
) -> TenantContext:
    # Enforces the clickwrap Gate #1 status block (allow_pending defaults False).
    org, membership, role = _active_org_from_context(db, ctx)
    return _tenant_context_from(ctx, org, membership, role)


def require_review_company_scope(minimum_role: str = "VIEWER"):
    """Dependency for neutral review selections; company_id keeps QBO meaning.

    The selection is not authority. The resolver revalidates current membership,
    assent, grants and composite ownership before pinning numeric company scope.
    W1 registers no route and this feature is disabled by default.
    """
    async def dependency(
        organization_company_id: int = Query(..., gt=0),
        book_id: int = Query(..., gt=0),
        db: Session = Depends(get_db),
        ctx: AuthContext = Depends(get_current_auth_context),
    ):
        if settings.monthly_review_enabled is not True:
            raise HTTPException(status_code=404, detail="Resource not found")
        from .services.review_company_scope import resolve_review_company_scope

        return resolve_review_company_scope(
            db, auth=ctx, company_id=organization_company_id,
            book_id=book_id, minimum_role=minimum_role,
        )

    return dependency


def require_organization_context_allow_pending(db: Session, ctx: AuthContext) -> TenantContext:
    """Resolve org context WITHOUT the clickwrap Gate #1 status block.

    ONLY the legal-accept + assent-status endpoints call this: a `pending_acceptance`
    org MUST be able to reach the accept endpoint to record its Tier-1 acceptance and
    activate (SPEC §9.2 keeps the accept / read / cancel flow always reachable —
    without this the org could never leave `pending_acceptance`, the chicken-and-egg).
    Deliberately NOT a FastAPI dependency (no `Depends`), so the pending-bypass can
    never be reached through a request-supplied value — the legal router calls it
    directly with the server-authoritative session context.
    """
    org, membership, role = _active_org_from_context(db, ctx, allow_pending=True)
    return _tenant_context_from(ctx, org, membership, role)


def activate_org_on_tier1_assent(db: Session, *, organization_id: int, stytch_organization_id: str) -> bool:
    """Clickwrap Gate #1 activation (AC-7): flip a `pending_acceptance` org to
    `active` IFF current Tier-1 (`tos`+`privacy`) assent is complete for it.

    This is the ONLY path that sets `status='active'`. Called from the legal-accept
    handler AFTER `record_assent` succeeds. The status mutation + commit live here in
    the shared dependency layer (keeping the legal router thin — no ORM mutation in a
    handler). No-ops (returns False) when the org is not `pending_acceptance` or Tier-1
    is incomplete (e.g. a Privacy-only accept). Returns True iff it flipped.
    """
    from .services.legal_gate import assent_gate

    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if org is None or org.status != "pending_acceptance":
        return False
    if not assent_gate(db, stytch_organization_id).get("ok"):
        return False
    org.status = "active"
    db.commit()
    return True


def require_entity_access(minimum_role: str = "VIEWER"):
    def _dependency(
        realm_id: str = Depends(require_realm_id),
        db: Session = Depends(get_db),
        ctx: AuthContext = Depends(get_current_auth_context),
    ) -> TenantContext:
        bind_session_organization(ctx)
        access = _check_company_access(db, ctx.user, str(realm_id), minimum_role)
        org = organization_for_company(db, str(realm_id))
        membership = None
        if org:
            membership = (
                db.query(OrganizationMembership)
                .filter(
                    OrganizationMembership.user_id == ctx.user.id,
                    OrganizationMembership.organization_id == org.id,
                )
                .first()
            )
        elif ctx.organization:
            org = ctx.organization
            membership = ctx.membership
        if not org:
            raise HTTPException(status_code=403, detail="Company is not mapped to an organization")
        try:
            org_company = assert_company_belongs_to_organization(db, organization=org, realm_id=str(realm_id))
        except PermissionError:
            raise HTTPException(status_code=403, detail="Company does not belong to active organization")
        if not ctx.user.is_priority_admin and company_is_suspended(db, organization_company=org_company):
            raise HTTPException(status_code=403, detail="This company is suspended")
        role = str(access["role"])
        return TenantContext(
            user=ctx.user,
            organization=org,
            membership=membership,
            organization_id=org.id,
            stytch_organization_id=org.stytch_organization_id,
            role=role,
            permissions=_role_permissions(role),
            is_platform_admin=ctx.user.is_priority_admin,
            organization_company=org_company,
            organization_company_id=org_company.id,
            realm_id=str(realm_id),
            company_id=str(realm_id),
        )

    return _dependency


require_entity_context = require_entity_access("VIEWER")


def require_org_admin_context(
    tenant: TenantContext = Depends(require_active_organization_context),
) -> TenantContext:
    # Weight-based so canonical tenant_admin/platform_super_admin (and every
    # legacy org-admin alias) resolve, while ACCOUNTING and below are rejected.
    if not tenant.is_platform_admin and not is_tenant_admin(tenant.role):
        raise HTTPException(status_code=403, detail="Organization admin role required")
    return tenant


def assert_tenant_admin(db: Session, user: User, organization_id: int, *, terms_gate: bool = True) -> Organization:
    """Authorize a tenant-admin action against ONE specific organization.

    Allows the request iff the caller is a PLATFORM_SUPER_ADMIN (the
    is_priority_admin break-glass) OR holds a TENANT_ADMIN-weight membership in
    *that* organization. The target org is resolved from the caller-supplied
    organization_id and membership is validated against it — a body/prompt
    supplied org is never trusted without this check.

    ``terms_gate=False`` is for the QuickBooks connect flow only: it records the
    entity attestation before the Terms step, and Gate #2 at /qbo/authorize
    refuses a non-active org with its own 409 assent_required.

    Returns the resolved Organization so callers can reuse it.
    """
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if user.is_priority_admin:
        return org
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    if not membership or membership.status != "active":
        raise HTTPException(status_code=403, detail="You do not belong to this organization")
    if not is_tenant_admin(membership.role):
        raise HTTPException(status_code=403, detail="Tenant admin role required for this organization")
    # Exports, API keys and spend reports are served only to a session issued for
    # this organization, and only once it has accepted the current Terms.
    require_session_organization(user, org)
    if terms_gate:
        require_accepted_terms(org)
    return org


def require_tenant_admin(
    organization_id: int,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_current_auth_context),
) -> AuthContext:
    """FastAPI dependency: gate a route on tenant-admin authority for the
    {organization_id} in its path. Platform super-admins always pass; otherwise
    the caller must be a TENANT_ADMIN of that exact organization.
    """
    assert_tenant_admin(db, ctx.user, organization_id)
    return ctx


def require_debug_endpoint_access(
    ctx: AuthContext = Depends(get_current_auth_context),
) -> AuthContext:
    if is_dev_environment(settings.environment):
        return ctx
    if ctx.user.is_priority_admin:
        return ctx
    raise HTTPException(status_code=403, detail="Debug endpoint access requires platform admin")


def require_entity_admin_context(
    tenant: TenantContext = Depends(require_entity_access("ORG_ADMIN")),
) -> TenantContext:
    return tenant


def require_tenant_access(minimum_role: str = "VIEWER"):
    def _dependency(
        company_id: str,
        db: Session = Depends(get_db),
        ctx: AuthContext = Depends(get_current_auth_context),
    ):
        return _check_company_access(db, ctx.user, company_id, minimum_role)

    return _dependency


def check_tenant_access(minimum_role: str, *, company_id: str, db: Session, user: User) -> dict:
    return _check_company_access(db, user, company_id, minimum_role)


def check_entity_tenant_access(
    minimum_role: str,
    *,
    db: Session,
    user: User,
    model: Type[Any],
    entity_id: Any,
    id_attr: str = "id",
    tenant_attr: str = "company_id",
    not_found_detail: str = "Resource not found",
) -> Any:
    id_column = getattr(model, id_attr)
    # P0 (read-before-scope): resolve the row's owning tenant under a transient RLS
    # bypass. On a FORCE-RLS table a request that never pinned scope (the guard could
    # not infer a realm — e.g. /lbo/scenarios/{id}/... with no company_id param) would
    # read ZERO rows here and 404 spuriously. The bypass covers ONLY this PK lookup;
    # _check_company_access below re-pins the correct NON-bypass scope (org+realm for a
    # member, audited bypass for a priority admin), so cross-tenant isolation holds.
    with tenant_scope_live(db, bypass=True):
        entity = db.query(model).filter(id_column == entity_id).first()
    if not entity:
        raise HTTPException(status_code=404, detail=not_found_detail)

    company_id = getattr(entity, tenant_attr, None)
    if company_id is None and tenant_attr != "realm_id":
        company_id = getattr(entity, "realm_id", None)
    if not company_id:
        raise HTTPException(status_code=500, detail=f"{model.__name__} is missing tenant ownership metadata")

    _check_company_access(db, user, str(company_id), minimum_role)
    org = organization_for_company(db, str(company_id))
    if org:
        row_org_id = getattr(entity, "organization_id", None)
        if row_org_id not in (None, org.id):
            raise HTTPException(status_code=404, detail=not_found_detail)
        row_org_company_id = getattr(entity, "organization_company_id", None)
        if row_org_company_id is not None:
            try:
                org_company = assert_company_belongs_to_organization(db, organization=org, realm_id=str(company_id))
            except PermissionError:
                raise HTTPException(status_code=404, detail=not_found_detail)
            if row_org_company_id != org_company.id:
                raise HTTPException(status_code=404, detail=not_found_detail)
    return entity


async def global_tenant_guard(
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_current_auth_context),
):
    if request.url.path.startswith(("/auth", "/api/docs", "/api/openapi", "/webhook", "/static")):
        return
    if request.url.path.startswith(("/actions/approve-token/", "/actions/reject-token/")):
        return
    # The read-only Pull API authenticates by per-tenant API key (not a human
    # session) and self-enforces stricter key-scoped isolation via
    # require_pull_api_tenant + tenant_data_scope. This human-session guard would
    # otherwise 401 every API-key caller, so skip it here.
    if request.url.path.startswith("/v1/pull"):
        return
    if ctx.user.force_password_reset:
        raise HTTPException(
            status_code=403,
            detail={"code": "password_reset_required", "reason": ctx.user.password_reset_required_reason},
        )

    company_id = request.query_params.get("company_id") or request.query_params.get("realm_id")
    if not company_id:
        company_id = request.path_params.get("company_id") or request.path_params.get("realm_id")

    if not company_id:
        for part in request.url.path.split("/"):
            if part.isdigit() and len(part) > 10:
                company_id = part
                break

    if not company_id and request.method in ["POST", "PUT", "PATCH"]:
        try:
            body_bytes = await request.body()
            if body_bytes:
                data = json.loads(body_bytes)
                if isinstance(data, dict):
                    company_id = data.get("company_id") or data.get("realm_id")
        except Exception:
            pass

    if not company_id:
        lbo_id = request.path_params.get("lbo_company_id") or request.query_params.get("lbo_company_id")
        if lbo_id and str(lbo_id).isdigit():
            from .models import LboCompany

            # P0 (read-before-scope): this realm-inference lookup runs BEFORE any scope
            # is pinned; under FORCE-RLS on lbo_companies it would read None and the
            # realm would never be inferred (silent fall-through, no scope pinned).
            # Resolve under a transient bypass; the `if company_id:` block below pins
            # the real scope (audited bypass for an admin, or _check_company_access for
            # a member). The bypass covers ONLY this lookup.
            with tenant_scope_live(db, bypass=True):
                lbo_comp = db.query(LboCompany).get(int(lbo_id))  # CROSS-TENANT-OK: guard realm-inference resolves the owning realm by PK under a transient RLS bypass, before any tenant scope is known; the if company_id: block below pins the authorized scope
            if lbo_comp:
                company_id = lbo_comp.company_id

    if company_id:
        if user_can_bypass_tenant_scope(db, ctx.user):
            # Pin the RLS bypass GUC for the vetted platform-admin. Without this the
            # GUCs stay unset and the live RLS policies fail-close this admin to ZERO
            # rows on any read route that relies solely on this guard (no in-handler
            # _check_company_access). This restores the pre-RLS admin-read behavior;
            # it does NOT weaken DIM-033 — the break-glass audit row is written by
            # _check_company_access (dependencies.py:150) on the in-handler assert
            # path, exactly as before; the guard layer never audited and still does
            # not authorize on its own. pin (emit-now) not set: this guard runs
            # AFTER the request transaction is already open (the company/realm
            # inference above queries the DB), so set_tenant_scope would emit only
            # at the next begin and leave this read route GUC-less (INCIDENT-2).
            pin_tenant_scope_live(db, bypass=True)
            return
        try:
            _check_company_access(db, ctx.user, str(company_id), "VIEWER")
        except _ACTIONABLE_TENANT_REFUSALS:
            raise
        except HTTPException as exc:
            # PLAN-EXEMPT: prod-incident diagnostic (Steven-authorized 2026-07-01) — the guard
            # masked the real denial reason, blinding tenant-admin 403 triage. Surface the true
            # cause + the ACTUAL principal the guard resolved (which may differ from the handler's).
            logger.warning(  # OBS-OK: non-secret app values only (int user id, realm id, fixed HTTP status/detail) — no token/PII
                "tenant_guard denied: realm=%s guard_user_id=%s orig_status=%s orig_detail=%s",
                company_id,
                getattr(ctx.user, "id", None),
                getattr(exc, "status_code", None),
                getattr(exc, "detail", None),
            )
            if (
                (request.method, request.url.path)
                in {
                    ("POST", "/agent/close-runs/start"),
                    ("POST", "/accounting/calendar/close"),
                }
                and (
                    (
                        exc.status_code == 403
                        and exc.detail == "Forbidden: You do not have access to this company."
                    )
                    or (exc.status_code == 404 and exc.detail == "Resource not found")
                )
            ):
                raise HTTPException(status_code=404, detail="Resource not found") from exc
            blocked = HTTPException(
                status_code=403,
                detail=f"Forbidden: Cross-Tenant Access Blocked for realm {company_id}.",
            )
            raise blocked from exc
    # No realm could be inferred (the documented defense-in-depth silent-pass that
    # predates RLS). Deliberately NOT converted to a hard raise: under the live RLS
    # backstop an unset scope fail-closes to ZERO rows (the safe direction, not a
    # leak), and some genuinely tenant-agnostic routes legitimately reach this
    # fall-through. A read route that needs tenant data MUST still re-assert
    # in-handler (check_tenant_access / check_entity_tenant_access) — that is the
    # load-bearing gate and the only thing that pins scope for such a route. If a
    # solely-guard read route returns empty under RLS, the fix is the in-handler
    # assert, not removing this fail-closed fall-through.
    return

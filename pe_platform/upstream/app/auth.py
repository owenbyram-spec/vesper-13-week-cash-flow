from dataclasses import dataclass
from datetime import timedelta, timezone
import secrets
from typing import Any, Callable, Optional

import jwt
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status  # noqa: F401
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from sqlalchemy.orm import Session

from .rate_limit import limiter
from .auth_passwords import MIN_PASSWORD_LENGTH, hash_password, validate_password_strength, verify_password  # noqa: F401
from .auth_schemas import (
    AuthUserOut,
    EmailChangeCompleteRequest,
    EmailChangeRequest,
    LoginRequest,
    MagicLinkAuthenticateRequest,
    MemberInviteRequest,
    MemberPasswordResetRequest,
    MemberUpdateRequest,
    OrganizationMfaPolicyUpdate,
    OrganizationSummary,
    OrganizationSwitchRequest,
    PasswordResetCompleteRequest,
    PasswordResetStartRequest,
    PlatformOrganizationCreate,
    PlatformOrganizationUpdate,
    ProfileUpdate,
    SmsChallengeRequest,
    TOTPChallengeRequest,
    TOTPEnrollmentStartRequest,
    UserCreate,  # noqa: F401 — re-exported as part of the auth schema facade
    UserOut,
    UserUpdate,  # noqa: F401 — re-exported as part of the auth schema facade
)
from .auth_session import (
    ACCESS_TOKEN_EXPIRE_MINUTES,  # noqa: F401
    ALGORITHM,
    SECRET_KEY,
    AuthContext,
    _auth_response_payload,
    _delete_auth_cookie,
    _hash_token,
    _new_csrf_token,
    _set_auth_cookie,
    _set_csrf_cookie,
    create_access_token,
)
from .auth_stytch import (
    _SMS_PHONE_ERROR,  # noqa: F401
    _can_send_stytch_password_reset,
    _compact_stytch_payload,
    _delete_stytch_member_for_org,
    _has_real_stytch_member_ref,
    _normalize_sms_phone_number,
    _sms_stytch_http_exception,
    _stytch_error_message,
    _stytch_error_status,
    _stytch_error_type,
    _stytch_member_management_error,
    _stytch_member_update_payload,
    _stytch_mfa_challenge_detail,
    _stytch_session_token_for_organization,
    _validate_stytch_authenticated_context,
)
from .config import is_dev_environment, settings
from .db import get_db
from .models import Organization, OrganizationMembership, QBOToken, QBOCompanyInfo, User
from .roles import CANONICAL_ROLES, LEGACY_ROLE_TO_CANONICAL, canonical_role, is_tenant_admin
from .services.auth_migration import (
    build_org_payload,
    build_accessible_organizations,
    compute_cookie_expiry,
    ensure_membership,
    ensure_priority_admin_flag,
    get_user_by_email,
    normalize_global_role,  # noqa: F401
    normalize_stytch_member_role,
    OrganizationSelectionRequired,
    record_auth_event,
    resolve_org_for_login,
    slugify,
    stytch_payload_role,
    sync_stytch_session,
    upsert_auth_session,
    utcnow,
)
from .services.company_connections import qbo_connection_metadata
from .services.errors import safe_error_detail
from .services.tenant_companies import (
    company_ids_for_organization,
    member_company_ids_for_organization,
    organization_for_company,
)
from .services.login_backoff import (
    clear_failed_login_backoff,
    enforce_failed_login_backoff,
    record_failed_login,
)
from .services.stytch_b2b import (
    StytchB2BError,
    evict_cached_session_identity,
    get_cached_session_identity,
    set_cached_session_identity,
    stytch_b2b_client,
)

router = APIRouter(prefix="/auth", tags=["auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


def _is_production_like() -> bool:
    return not is_dev_environment(settings.environment)


def _legacy_fallback_local_allowed() -> bool:
    return bool(settings.auth_allow_legacy_fallback_local_only) and not _is_production_like()


def _dev_session_token_allowed() -> bool:
    # DEV/TEST-ONLY seam: in production the cookie/JWT path is rejected and Stytch
    # is the only accepted auth path. In non-production environments (where the
    # test suite cannot run real Stytch) we accept the locally-minted JWT cookie.
    return _legacy_fallback_local_allowed()


def _legacy_password_login_allowed() -> bool:
    return _legacy_fallback_local_allowed() and not stytch_b2b_client.enabled


@dataclass(frozen=True)
class SessionOrganization:
    """The organization a request's session was issued for (None: it names none)."""

    organization_id: Optional[int]


_SESSION_ORGANIZATION_ATTR = "_session_organization"


def bind_session_organization(ctx: AuthContext) -> AuthContext:
    """Record on the request's User which organization its session belongs to.

    Realm and organization gates take only the User, so the binding travels with
    it. A User loaded outside a request (a worker, a script) carries no binding:
    there is no session to compare against.
    """
    organization_id = ctx.organization.id if ctx.organization is not None else None
    setattr(ctx.user, _SESSION_ORGANIZATION_ATTR, SessionOrganization(organization_id))
    return ctx


def session_organization(user: Any) -> Optional[SessionOrganization]:
    binding = getattr(user, _SESSION_ORGANIZATION_ATTR, None)
    return binding if isinstance(binding, SessionOrganization) else None


class OrgSwitchRequired(HTTPException):
    """403 for a member whose session was issued for another of their organizations.

    An organization's data is served only to a session issued for that
    organization, so its MFA and auth-method policy always applied. The detail
    names the organization so the client can offer /auth/organizations/switch,
    which goes through Stytch's exchange and that organization's policy.
    """

    def __init__(self, organization: Organization) -> None:
        detail = safe_error_detail(
            error_code="org_switch_required",
            user_message=f"This belongs to {organization.name}. Switch to that organization to continue.",
        )
        detail["organization_id"] = organization.stytch_organization_id
        detail["organization_name"] = organization.name
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_session_organization(user: Any, organization: Organization) -> None:
    """Refuse a session-bound caller whose session belongs to a different organization."""
    binding = session_organization(user)
    if binding is not None and binding.organization_id != organization.id:
        raise OrgSwitchRequired(organization)


def _organization_for_identifier(db: Session, identifier: str | None) -> Optional[Organization]:
    if not identifier:
        return None
    normalized = str(identifier).strip()
    if not normalized:
        return None
    if normalized.isdigit():
        org = db.query(Organization).filter(Organization.id == int(normalized)).first()
        if org:
            return org
    for column in (
        Organization.stytch_organization_id,
        Organization.slug,
        Organization.external_id,
        Organization.primary_realm_id,
    ):
        org = db.query(Organization).filter(column == normalized).first()
        if org:
            return org
    return None


def _first_reset_membership(
    db: Session,
    *,
    user: Optional[User],
    email: str,
    organization: Optional[Organization] = None,
) -> tuple[Optional[Organization], Optional[OrganizationMembership]]:
    email = email.lower()
    query = db.query(OrganizationMembership).filter(OrganizationMembership.status != "deleted")
    if organization:
        query = query.filter(OrganizationMembership.organization_id == organization.id)
    if user:
        membership = query.filter(OrganizationMembership.user_id == user.id).first()
        if membership:
            org = organization or db.query(Organization).filter(Organization.id == membership.organization_id).first()
            return org, membership
    membership = query.filter(OrganizationMembership.email == email).first()
    if membership:
        org = organization or db.query(Organization).filter(Organization.id == membership.organization_id).first()
        return org, membership
    return organization, None


def _resolve_password_reset_target(
    db: Session,
    *,
    email: str,
    requested_organization_id: Optional[str] = None,
) -> tuple[Optional[User], Optional[Organization], Optional[OrganizationMembership]]:
    user = get_user_by_email(db, email)
    organization = _organization_for_identifier(db, requested_organization_id)
    if organization:
        org, membership = _first_reset_membership(db, user=user, email=email, organization=organization)
        return user, org, membership

    if not user:
        return None, None, None

    if user.default_organization_id:
        organization = db.query(Organization).filter(
            Organization.id == user.default_organization_id,
            Organization.is_active.is_(True),
        ).first()
        if organization:
            org, membership = _first_reset_membership(db, user=user, email=email, organization=organization)
            return user, org, membership

    membership = (
        db.query(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .filter(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.status != "deleted",
            Organization.is_active.is_(True),
        )
        .order_by(
            (OrganizationMembership.status == "active").desc(),
            OrganizationMembership.id.asc(),
        )
        .first()
    )
    if membership:
        organization = db.query(Organization).filter(Organization.id == membership.organization_id).first()
        return user, organization, membership

    try:
        organization, membership, _options = resolve_org_for_login(db, user=user)
        return user, organization, membership
    except ValueError:
        return user, None, None


def _password_reset_redirect_url() -> str:
    return settings.password_reset_redirect_url or f"{settings.frontend_url}/password-reset"


def _login_redirect_url() -> str:
    return f"{settings.frontend_url}/login"


def _email_change_redirect_url() -> str:
    return settings.email_change_redirect_url or f"{settings.frontend_url}/settings/profile?email_change=complete"


# Accept canonical role names plus every legacy alias as input; the endpoints
# canonicalize before storing so the DB only holds canonical strings.
ORG_ROLES = set(CANONICAL_ROLES) | set(LEGACY_ROLE_TO_CANONICAL)
BILLING_STATUSES = {"trialing", "active", "past_due", "suspended", "canceled"}


def validate_org_role(role: str) -> None:
    if role not in ORG_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid organization role. Allowed roles: {', '.join(sorted(ORG_ROLES))}",
        )


def validate_billing_status(status_value: str) -> None:
    if status_value not in BILLING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid billing status. Allowed statuses: {', '.join(sorted(BILLING_STATUSES))}",
        )


def _unique_org_slug(db: Session, desired_slug: str, existing_org_id: Optional[int] = None) -> str:
    base = slugify(desired_slug)
    candidate = base
    suffix = 2
    while True:
        query = db.query(Organization).filter(Organization.slug == candidate)
        if existing_org_id is not None:
            query = query.filter(Organization.id != existing_org_id)
        if not query.first():
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def _serialize_organization(org: Organization) -> dict[str, Any]:
    return {
        "id": org.id,
        "stytch_organization_id": org.stytch_organization_id,
        "slug": org.slug,
        "name": org.name,
        "external_id": org.external_id,
        "primary_realm_id": org.primary_realm_id,
        "mfa_policy": org.mfa_policy,
        "mfa_methods": org.mfa_methods,
        "allowed_mfa_methods": org.allowed_mfa_methods,
        "is_active": org.is_active,
        "billing_status": org.billing_status,
        "billing_tier": org.billing_tier,
        "billing_customer_id": org.billing_customer_id,
        "suspended_at": org.suspended_at.isoformat() if org.suspended_at else None,
        "suspended_reason": org.suspended_reason,
    }


def _empty_user_payload(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "company_ids": [],
        "auth_source": user.auth_source,
        "legacy_login_enabled": user.legacy_login_enabled,
        "is_priority_admin": user.is_priority_admin,
        "default_organization_id": user.default_organization_id,
        "force_password_reset": user.force_password_reset,
        "password_reset_required_reason": user.password_reset_required_reason,
        "active_organization": None,
        "organizations": [],
    }


def _serialize_user(db: Session, user: User, organization: Optional[Organization] = None) -> dict[str, Any]:
    ensure_priority_admin_flag(user)

    def _active_memberships() -> list[OrganizationMembership]:
        return (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.status == "active",
            )
            .all()
        )

    def _organization_summaries(
        memberships: list[OrganizationMembership],
    ) -> tuple[list[OrganizationSummary], list[str], Optional[OrganizationSummary]]:
        orgs: list[OrganizationSummary] = []
        company_ids: list[str] = []
        active_org: Optional[OrganizationSummary] = None
        org_lookup = {
            org.id: org
            for org in db.query(Organization).filter(
                Organization.id.in_([m.organization_id for m in memberships] or [-1]),
                Organization.is_active.is_(True),
            ).all()
        }
        for membership in memberships:
            org = org_lookup.get(membership.organization_id)
            if not org:
                continue
            summary = OrganizationSummary(
                id=org.id,
                stytch_organization_id=org.stytch_organization_id,
                slug=org.slug,
                name=org.name,
                primary_realm_id=org.primary_realm_id,
                role=membership.role,
                status=membership.status,
                mfa_policy=org.mfa_policy,
                mfa_methods=org.mfa_methods,
                allowed_mfa_methods=list(org.allowed_mfa_methods or []),
                is_active=org.is_active,
                billing_status=org.billing_status,
                billing_tier=org.billing_tier,
                suspended_reason=org.suspended_reason,
            )
            orgs.append(summary)
            # Members see the companies their grants open; platform admins reach every
            # company through the audited bypass, so their list stays whole.
            company_ids.extend(
                company_ids_for_organization(db, org)
                if user.is_priority_admin
                else member_company_ids_for_organization(db, user, org)
            )
            if organization and org.id == organization.id:
                active_org = summary
            elif user.default_organization_id == org.id and active_org is None:
                active_org = summary
        return orgs, company_ids, active_org

    memberships = _active_memberships()
    orgs, company_ids, active_org = _organization_summaries(memberships)

    if user.is_priority_admin:
        # Platform (priority) admins manage every tenant, even ones they hold no
        # membership in. Without this, an explicit membership (e.g. the internal
        # Peregrine org) shadows cross-tenant access: the membership-only list above
        # collapses the switcher to that single org. Mirror list_companies: union all
        # other active orgs in as internal_super_admin. Membership orgs keep their
        # real role; this only ADDS the tenants the admin isn't a member of.
        member_org_ids = {summary.id for summary in orgs}
        extra_orgs = (
            db.query(Organization)  # CROSS-TENANT-OK: platform-admin (is_priority_admin) tenant enumeration, gated above
            .filter(
                Organization.is_active.is_(True),
                Organization.id.notin_(member_org_ids or [-1]),
            )
            .order_by(Organization.name.asc())
            .all()
        )
        extra_company_ids = {org.id: company_ids_for_organization(db, org) for org in extra_orgs}
        for org in extra_orgs:
            summary = OrganizationSummary(
                id=org.id,
                stytch_organization_id=org.stytch_organization_id,
                slug=org.slug,
                name=org.name,
                primary_realm_id=org.primary_realm_id,
                role="internal_super_admin",
                status="active",
                mfa_policy=org.mfa_policy,
                mfa_methods=org.mfa_methods,
                allowed_mfa_methods=list(org.allowed_mfa_methods or []),
                is_active=org.is_active,
                billing_status=org.billing_status,
                billing_tier=org.billing_tier,
                suspended_reason=org.suspended_reason,
            )
            orgs.append(summary)
            company_ids.extend(extra_company_ids[org.id])
            if organization and org.id == organization.id and active_org is None:
                active_org = summary

    if not orgs:
        fallback_orgs = build_accessible_organizations(db, user)
        if not fallback_orgs:
            return _empty_user_payload(user)
        for org in fallback_orgs:
            ensure_membership(db, user=user, organization=org, legacy_role=user.role)
        db.flush()
        orgs, company_ids, active_org = _organization_summaries(_active_memberships())
        if not orgs:
            return _empty_user_payload(user)

    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "company_ids": sorted(set(company_ids)),
        "auth_source": user.auth_source,
        "legacy_login_enabled": user.legacy_login_enabled,
        "is_priority_admin": user.is_priority_admin,
        "default_organization_id": user.default_organization_id,
        "force_password_reset": user.force_password_reset,
        "password_reset_required_reason": user.password_reset_required_reason,
        "active_organization": active_org,
        "organizations": orgs,
    }


def _resolve_dev_session_context(db: Session, user: User, request: Optional[Request]) -> AuthContext:
    # DEV/TEST-ONLY: resolve the user's active org/membership purely from existing
    # OrganizationMembership rows. No legacy access-table backfill or lookups.
    ensure_priority_admin_flag(user)
    active_identifier = None
    if request is not None:
        active_identifier = (
            request.headers.get("x-active-organization")
            or request.query_params.get("organization_id")
            or request.query_params.get("company_id")
            or request.query_params.get("realm_id")
            or request.path_params.get("organization_id")
            or request.path_params.get("company_id")
            or request.path_params.get("realm_id")
        )

    memberships = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.user_id == user.id)
        .all()
    )
    org_lookup = {
        org.id: org
        for org in db.query(Organization).filter(
            Organization.id.in_([m.organization_id for m in memberships] or [-1])
        ).all()
    }
    membership = None
    organization = None
    if active_identifier:
        for candidate in memberships:
            org = org_lookup.get(candidate.organization_id)
            if org and active_identifier in {str(org.id), org.stytch_organization_id, org.slug, org.primary_realm_id or ""}:
                membership = candidate
                organization = org
                break
    if membership is None and user.default_organization_id:
        membership = next((m for m in memberships if m.organization_id == user.default_organization_id), None)
        organization = org_lookup.get(user.default_organization_id) if membership else None
    if membership is None and memberships:
        membership = memberships[0]
        organization = org_lookup.get(membership.organization_id)
    return AuthContext(user=user, organization=organization, membership=membership, auth_source="dev", token="")


def _auth_context_from_cached_identity(db: Session, token: str) -> AuthContext | None:
    """Re-build an AuthContext from a short-TTL session cache hit, or None.

    Returns None (a fail-closed signal to perform the real Stytch
    authenticate_session) on a cache miss, a decode error, or any cached PK that
    no longer resolves to a live row — we NEVER authenticate off a miss. On a
    hit, fresh ORM rows are loaded by PK from the request's own ``db`` session,
    so the returned context (user/organization/membership/auth_source/token) is
    identical to a freshly-validated one — and NO Stytch network call and NO
    session-mirror ``db.commit()`` run.
    """
    cached = get_cached_session_identity(token)
    if not cached:
        return None

    # CROSS-TENANT-OK: by-PK identity lookup at the single auth edge from a server-validated
    # session cache (key = SHA-256 of the verified session token, never client input). The
    # AuthContext IS the tenant scope this would otherwise gate on, so a membership check here
    # is circular; the cache only holds PKs already proven by a prior Stytch authenticate.
    user = db.query(User).filter(User.id == cached["user_id"]).first()
    if user is None:
        # The mirrored user row is gone — re-validate against Stytch (the truth).
        return None

    organization = None
    organization_id = cached.get("organization_id")
    if organization_id is not None:
        # CROSS-TENANT-OK: by-PK org lookup at the auth edge from the validated session cache.
        organization = db.query(Organization).filter(Organization.id == organization_id).first()
        if organization is None:
            return None

    membership = None
    membership_id = cached.get("membership_id")
    if membership_id is not None:
        # CROSS-TENANT-OK: by-PK membership lookup at the auth edge from the validated session cache.
        membership = (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.id == membership_id)
            .first()
        )
        if membership is None:
            return None

    return AuthContext(
        user=user,
        organization=organization,
        membership=membership,
        auth_source="stytch",
        token=token,
        stytch_session=cached.get("stytch_session"),
    )


def get_current_auth_context(
    request: Request,
    db: Session = Depends(get_db),
    access_token: Optional[str] = Cookie(None, alias=settings.auth_cookie_name),
    header_token: Optional[str] = Depends(oauth2_scheme),
) -> AuthContext:
    if header_token and (request.headers.get("origin") or request.headers.get("referer")) and not settings.auth_enable_browser_bearer_fallback:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Browser bearer fallback is disabled")
    token = header_token or access_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        if not _dev_session_token_allowed():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Stytch session required in production")
        ctx = _resolve_dev_session_context(db, user, request)
        ctx.token = token
        return bind_session_organization(ctx)
    except HTTPException:
        raise
    except (InvalidTokenError, TypeError, ValueError):
        pass

    # Fast path: a short-TTL cache HIT for this exact session token lets us skip
    # BOTH the blocking Stytch authenticate network call AND the per-request
    # session-mirror db.commit(). FAIL-CLOSED: any miss / decode error / a
    # cached PK that no longer resolves to a live row falls through to the real
    # authenticate_session below — we NEVER authenticate on a cache miss. The
    # returned AuthContext is re-materialized from the request's own db session
    # by PK, so it is identical to a freshly-validated one.
    cached_ctx = _auth_context_from_cached_identity(db, token)
    if cached_ctx is not None:
        return bind_session_organization(cached_ctx)

    try:
        session_resp = stytch_b2b_client.authenticate_session(
            {
                "session_token": token,
                "session_duration_minutes": settings.auth_session_minutes,
            }
        )
        _validate_stytch_authenticated_context(session_resp)
        user, organization, membership = sync_stytch_session(db, session_payload=session_resp)
        db.commit()
        set_cached_session_identity(
            token,
            user_id=user.id,
            organization_id=organization.id if organization else None,
            membership_id=membership.id if membership else None,
            stytch_session=session_resp,
        )
        return bind_session_organization(
            AuthContext(
                user=user,
                organization=organization,
                membership=membership,
                auth_source="stytch",
                token=token,
                stytch_session=session_resp,
            )
        )
    except StytchB2BError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def get_current_user_from_token(ctx: AuthContext = Depends(get_current_auth_context)) -> User:
    return ctx.user


def require_role(allowed_roles: list[str]) -> Callable:
    def _check_role(ctx: AuthContext = Depends(get_current_auth_context)) -> User:
        if ctx.user.role not in allowed_roles and not ctx.user.is_priority_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role: {' or '.join(allowed_roles)}",
            )
        return ctx.user

    return _check_role


def require_admin_context(ctx: AuthContext = Depends(get_current_auth_context)) -> AuthContext:
    if ctx.user.role != "admin" and not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return ctx


def require_platform_admin_context(ctx: AuthContext = Depends(get_current_auth_context)) -> AuthContext:
    if not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail="Peregrine platform admin role required")
    return ctx


def _require_org_admin(db: Session, ctx: AuthContext, organization_id: int) -> OrganizationMembership:
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    if ctx.user.is_priority_admin:
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == ctx.user.id,
            )
            .first()
        )
        if membership:
            return membership
        return OrganizationMembership(
            organization_id=organization_id,
            user_id=ctx.user.id,
            stytch_member_id="platform-admin",
            email=ctx.user.email,
            role="internal_super_admin",
            status="active",
        )
    if not organization.is_active:
        raise HTTPException(status_code=403, detail="This tenant is suspended")

    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == ctx.user.id,
        )
        .first()
    )
    if not membership or membership.status != "active":
        raise HTTPException(status_code=403, detail="You do not belong to this organization")
    # Weight-based so the canonical tenant_admin / platform_super_admin roles
    # (and legacy org_admin/owner/admin) all resolve after role normalization.
    if not is_tenant_admin(membership.role):
        raise HTTPException(status_code=403, detail="Organization admin role required")
    # Members and MFA policy are administered only from a session issued for this
    # organization; otherwise another org's weaker sign-in could change its policy.
    require_session_organization(ctx.user, organization)
    return membership


def _handle_successful_login(
    db: Session,
    response: Response,
    *,
    user: User,
    organization: Organization,
    membership: OrganizationMembership,
    session_token: str,
    stytch_session_id: Optional[str],
    auth_source: str,
) -> dict[str, Any]:
    if user.force_password_reset:
        record_auth_event(
            db,
            event_type="password_reset_required",
            user=user,
            organization=organization,
            membership=membership,
            details={"reason": user.password_reset_required_reason, "auth_source": auth_source},
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "password_reset_required", "reason": user.password_reset_required_reason},
        )
    user.default_organization_id = organization.id
    user.auth_source = "stytch" if auth_source == "stytch" else user.auth_source
    upsert_auth_session(
        db,
        user=user,
        organization=organization,
        membership=membership,
        auth_source=auth_source,
        session_token=session_token,
        stytch_session_id=stytch_session_id,
        expires_at=compute_cookie_expiry(),
    )
    record_auth_event(
        db,
        event_type="login",
        user=user,
        organization=organization,
        membership=membership,
        details={"auth_source": auth_source},
    )
    db.commit()
    csrf_token = _new_csrf_token()
    _set_auth_cookie(response, session_token)
    _set_csrf_cookie(response, csrf_token)
    return _auth_response_payload(_serialize_user(db, user, organization), session_token, csrf_token)


@router.post("/login", response_model=AuthUserOut)  # public-route: login endpoint — credential entry that establishes the session (no prior session required)
@limiter.limit("5/minute")
def login(request: Request, payload: LoginRequest, response: Response, db: Session = Depends(get_db)):  # CRAFT-OK: FastAPI route output typed by response_model=AuthUserOut
    # Per-ACCOUNT failed-attempt backoff: the slowapi bucket above keys on
    # client-rotatable material (cookie/Authorization header), so it cannot stop
    # a header-rotating brute force on one account. Only 401s count; 202/409/403
    # legitimate-flow exits never increment, and success clears the window.
    enforce_failed_login_backoff(payload.email)
    try:
        result = _login_flow(payload, response, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            record_failed_login(payload.email)
        raise
    clear_failed_login_backoff(payload.email)
    return result


def _verify_password_before_organization_selection(
    db: Session,
    *,
    user: User,
    password: str,
    candidates: list[tuple[Organization, OrganizationMembership]],
) -> None:
    """Check the primary factor for a user who still has to pick an organization.

    Stytch passwords are verified per organization, so each candidate is tried until
    one accepts the password; the session that proves it is revoked, never returned
    (the real session is minted for the org the user then selects). Raises the same
    401 as any failed login when nothing accepts it."""
    if stytch_b2b_client.enabled:
        for organization, _membership in candidates:
            try:
                auth_resp = stytch_b2b_client.authenticate_password(
                    {
                        "organization_id": organization.stytch_organization_id,
                        "email_address": user.email.lower(),
                        "password": password,
                        "session_duration_minutes": settings.auth_session_minutes,
                    }
                )
            except StytchB2BError:
                continue
            if auth_resp.get("session_token"):
                try:
                    stytch_b2b_client.revoke_session({"session_token": auth_resp["session_token"]})
                except StytchB2BError:
                    pass  # the token was never handed out; it lapses with its Stytch TTL
            return
        auth_source = "stytch"
    else:
        if (
            _legacy_password_login_allowed()
            and user.password_hash
            and verify_password(password, user.password_hash)
        ):
            return
        auth_source = "dev"
    record_auth_event(
        db,
        event_type="failed_login",
        user=user,
        event_status="failed",
        details={"auth_source": auth_source, "reason": "organization_selection_password"},
    )
    db.commit()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")


def _login_flow(payload: LoginRequest, response: Response, db: Session):  # CRAFT-OK: FastAPI route output typed by response_model=AuthUserOut
    user = get_user_by_email(db, payload.email)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    ensure_priority_admin_flag(user)
    try:
        organization, membership, _org_options = resolve_org_for_login(
            db,
            user=user,
            requested_organization_id=payload.organization_id,
        )
    except OrganizationSelectionRequired as selection:
        # Which organizations an account can enter is only revealed once its
        # password has been verified, and then only what the picker needs.
        _verify_password_before_organization_selection(db, user=user, password=payload.password, candidates=selection.candidates)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "organization_selection_required",
                "organizations": [
                    {
                        "id": org.id,
                        "stytch_organization_id": org.stytch_organization_id,
                        "slug": org.slug,
                        "name": org.name,
                    }
                    for org, _membership in sorted(selection.candidates, key=lambda pair: pair[0].name.lower())
                ],
            },
        )

    if stytch_b2b_client.enabled and organization and membership:
        try:
            auth_resp = stytch_b2b_client.authenticate_password(
                {
                    "organization_id": organization.stytch_organization_id,
                    "email_address": user.email.lower(),
                    "password": payload.password,
                    "session_duration_minutes": settings.auth_session_minutes,
                }
            )
            if not auth_resp.get("member_authenticated", True):
                record_auth_event(
                    db,
                    event_type="login_mfa_challenge_started",
                    user=user,
                    organization=organization,
                    membership=membership,
                    details={"member_id": auth_resp.get("member_id")},
                )
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_202_ACCEPTED,
                    detail={
                        "code": "mfa_required",
                        "organization_id": organization.stytch_organization_id,
                        "member_id": auth_resp.get("member_id"),
                        "intermediate_session_token": auth_resp.get("intermediate_session_token"),
                        "primary_required": auth_resp.get("primary_required"),
                        "mfa_required": auth_resp.get("mfa_required"),
                    },
                )
            _validate_stytch_authenticated_context(auth_resp)
            synced_user, synced_org, synced_membership = sync_stytch_session(db, session_payload=auth_resp, fallback_user=user)
            return _handle_successful_login(
                db,
                response,
                user=synced_user,
                organization=synced_org,
                membership=synced_membership,
                session_token=auth_resp["session_token"],
                stytch_session_id=(auth_resp.get("member_session") or {}).get("member_session_id"),
                auth_source="stytch",
            )
        except StytchB2BError:
            record_auth_event(
                db,
                event_type="failed_login",
                user=user,
                organization=organization,
                membership=membership,
                event_status="failed",
                details={"auth_source": "stytch"},
            )
            db.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    # Production is Stytch-only. If Stytch is disabled (local/test environments
    # only), fall back to a dev session cookie so the suite can authenticate.
    if not _legacy_password_login_allowed():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    if not user.password_hash or not verify_password(payload.password, user.password_hash):
        record_auth_event(
            db,
            event_type="failed_login",
            user=user,
            organization=organization,
            membership=membership,
            event_status="failed",
            details={"auth_source": "dev", "reason": "bad_password"},
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    if user.force_password_reset:
        record_auth_event(
            db,
            event_type="password_reset_required",
            user=user,
            organization=organization,
            membership=membership,
            details={"reason": user.password_reset_required_reason, "auth_source": "dev"},
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "password_reset_required", "reason": user.password_reset_required_reason},
        )

    token = create_access_token({"sub": str(user.id)})
    user.last_login_at = utcnow()
    record_auth_event(db, event_type="login", user=user, organization=organization, membership=membership, details={"auth_source": "dev"})
    db.commit()
    csrf_token = _new_csrf_token()
    _set_auth_cookie(response, token)
    _set_csrf_cookie(response, csrf_token)
    return _auth_response_payload(_serialize_user(db, user, organization), token, csrf_token)


@router.post("/magic-link/authenticate", response_model=AuthUserOut)
@limiter.limit("10/minute")  # credential-establishing endpoint: throttle magic-link token guessing (same posture as /login's 5/minute)
def authenticate_magic_link(request: Request, payload: MagicLinkAuthenticateRequest, response: Response, db: Session = Depends(get_db)):
    if not stytch_b2b_client.enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stytch is not configured")
    try:
        auth_resp = stytch_b2b_client.authenticate_magic_link(
            {
                "magic_links_token": payload.token,
                "session_duration_minutes": settings.auth_session_minutes,
            }
        )
        if not auth_resp.get("member_authenticated", True):
            record_auth_event(
                db,
                event_type="login_mfa_challenge_started",
                event_status="pending",
                actor_email=(auth_resp.get("member") or {}).get("email_address"),
                details={"auth_source": "stytch_magic_link", "member_id": auth_resp.get("member_id")},
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_202_ACCEPTED,
                detail={
                    "code": "mfa_required",
                    "organization_id": auth_resp.get("organization_id"),
                    "member_id": auth_resp.get("member_id"),
                    "intermediate_session_token": auth_resp.get("intermediate_session_token"),
                    "primary_required": auth_resp.get("primary_required"),
                    "mfa_required": auth_resp.get("mfa_required"),
                },
            )
        _validate_stytch_authenticated_context(auth_resp)
        user, organization, membership = sync_stytch_session(db, session_payload=auth_resp)
        return _handle_successful_login(
            db,
            response,
            user=user,
            organization=organization,
            membership=membership,
            session_token=auth_resp["session_token"],
            stytch_session_id=(auth_resp.get("member_session") or {}).get("member_session_id"),
            auth_source="stytch",
        )
    except StytchB2BError as exc:
        record_auth_event(
            db,
            event_type="failed_login",
            event_status="failed",
            details={"auth_source": "stytch_magic_link", "reason": str(exc.detail)[:300]},
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired login link")


@router.post("/logout")
def logout(response: Response, ctx: AuthContext = Depends(get_current_auth_context), db: Session = Depends(get_db)):
    if ctx.auth_source == "stytch":
        try:
            stytch_b2b_client.revoke_session({"session_token": ctx.token})
        except StytchB2BError:
            pass
        # Evict the short-TTL session cache REGARDLESS of the revoke outcome: a
        # revoked (or revocation-attempted) token must not keep authenticating
        # off a stale cache hit for the remainder of the ≤5-minute TTL.
        evict_cached_session_identity(ctx.token)
    record_auth_event(db, event_type="logout", user=ctx.user, organization=ctx.organization, membership=ctx.membership)
    db.commit()
    _delete_auth_cookie(response)
    return {"message": "Logged out"}


@router.get("/me", response_model=UserOut)
def me(
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    csrf_token = request.cookies.get(settings.csrf_cookie_name)
    if not csrf_token:
        csrf_token = _new_csrf_token()
        _set_csrf_cookie(response, csrf_token)
    return {**_serialize_user(db, ctx.user, ctx.organization), "csrf_token": csrf_token}


@router.put("/me", response_model=UserOut)
@limiter.limit("10/minute")  # re-authenticates with the current password: throttle guessing it
def update_me(
    request: Request,
    payload: ProfileUpdate,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    user = ctx.user
    if payload.password:
        # A Stytch-managed account's real credential lives in Stytch; a local hash
        # would never be used, so the change goes through the verified reset flow,
        # which is also the only place force_password_reset is cleared. Runs before
        # any other field changes: the failed-attempt audit below commits.
        if user.auth_source in {"stytch", "hybrid"} or ctx.auth_source == "stytch":
            raise HTTPException(status_code=400, detail="Use the password reset flow to change the password of a Stytch-managed account")
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="Enter your current password to set a new one")
        if not user.password_hash or not verify_password(payload.current_password, user.password_hash):
            record_auth_event(
                db,
                event_type="password_change_failed",
                user=user,
                organization=ctx.organization,
                membership=ctx.membership,
                event_status="failed",
                details={"reason": "bad_current_password"},
            )
            db.commit()
            raise HTTPException(status_code=403, detail="Current password is incorrect")
        validate_password_strength(payload.password)
        user.password_hash = hash_password(payload.password)
        user.password_last_changed_at = utcnow()
    if payload.email:
        if user.auth_source in {"stytch", "hybrid"}:
            raise HTTPException(status_code=400, detail="Use the verified email change flow for Stytch-managed users")
        new_email = payload.email.lower()
        existing = get_user_by_email(db, new_email)
        if existing and existing.id != user.id:
            raise HTTPException(status_code=400, detail="Email is already registered")
        user.email = new_email
    if payload.default_organization_id is not None:
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.organization_id == payload.default_organization_id,
            )
            .first()
        )
        if not membership:
            raise HTTPException(status_code=403, detail="You do not belong to that organization")
        user.default_organization_id = payload.default_organization_id
    if not payload.email and not payload.password and payload.default_organization_id is None:
        raise HTTPException(status_code=400, detail="Email, password, or default_organization_id is required")
    record_auth_event(db, event_type="profile_updated", user=user, organization=ctx.organization, membership=ctx.membership)
    db.commit()
    db.refresh(user)
    return _serialize_user(db, user, ctx.organization)


@router.get("/companies")
def list_companies(
    org_scope: Optional[int] = None,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
) -> list[dict]:
    from .models import OrganizationCompany
    from .tenant_rls import pin_tenant_scope_live

    # Platform-admin tenant scoping (plan 20260702-tenant-scope-selector, AC-3).
    # `org_scope` (a numeric Organization.id) narrows the cross-tenant enumeration
    # to ONE tenant's companies — the "drop into a tenant" view. Validated BEFORE
    # any enumeration: platform-admin only (a non-admin's list is already scoped
    # to their own memberships, so the parameter is refused, not ignored), and the
    # org must exist. Absent, this handler is byte-identical to its prior self.
    scoped_org: Optional[Organization] = None
    if org_scope is not None:
        if not ctx.user.is_priority_admin:
            raise HTTPException(
                status_code=403,
                detail=(
                    "org_scope is a platform-admin tenant filter. Your company list is "
                    "already scoped to your own organization memberships — omit the parameter."
                ),
            )
        # organizations / organization_companies are RLS-excluded lookup tables,
        # so this pre-pin read is safe and cannot leak tenant rows.
        scoped_org = db.query(Organization).filter(Organization.id == org_scope).first()
        if scoped_org is None:
            raise HTTPException(status_code=404, detail=f"Unknown organization id: {org_scope}")

    # RLS: read-only enumeration of the caller's companies. realm_ids is built
    # from the caller's own memberships (or the caller is a priority admin), and
    # qbo_tokens (org) / qbo_company_info (realm) are FORCE-RLS — without a GUC the
    # per-realm reads fail closed to an empty list. The enumeration set is the
    # authorization boundary.  # CROSS-TENANT-OK: read-only enumeration already restricted to the caller's accessible realms
    pin_tenant_scope_live(db, bypass=True)

    companies = []
    realm_ids: set[str] = set()
    if scoped_org is not None:
        # Platform admin viewing ONE tenant: exactly that tenant's company set
        # (OrganizationCompany rows + primary realm) — server-side, above the RLS backstop.
        realm_ids.update(company_ids_for_organization(db, scoped_org))
    elif ctx.user.is_priority_admin:
        realm_ids.update(row.realm_id for row in db.query(QBOToken.realm_id).all() if row.realm_id)
    else:
        for org in build_accessible_organizations(db, ctx.user):
            realm_ids.update(member_company_ids_for_organization(db, ctx.user, org))

    for realm_id in sorted(realm_ids):
        info = db.query(QBOCompanyInfo).filter(QBOCompanyInfo.realm_id == realm_id).first()
        token = db.query(QBOToken).filter(QBOToken.realm_id == realm_id).first()
        org = organization_for_company(db, realm_id)
        org_company_id = None
        if org:
            org_company_id = (
                db.query(OrganizationCompany.id)
                .filter(OrganizationCompany.organization_id == org.id, OrganizationCompany.realm_id == realm_id)
                .scalar()
            )
        companies.append(
            {
                "realm_id": realm_id,
                "organization_company_id": org_company_id,
                "company_name": info.company_name if info and info.company_name else (org.name if org else f"Company {realm_id}"),
                "organization_id": org.stytch_organization_id if org else None,
                "organization_slug": org.slug if org else None,
                **qbo_connection_metadata(token=token, info=info),
            }
        )
    return companies


@router.get("/organizations")
def list_organizations(ctx: AuthContext = Depends(get_current_auth_context), db: Session = Depends(get_db)):
    return _serialize_user(db, ctx.user, ctx.organization)["organizations"]


@router.get("/platform/organizations")
def list_platform_organizations(
    ctx: AuthContext = Depends(require_platform_admin_context),
    db: Session = Depends(get_db),
):
    organizations = db.query(Organization).order_by(Organization.name.asc()).all()
    return [
        {
            **_serialize_organization(org),
            # company_count feeds the platform-admin tenant-scope selector
            # (plan 20260702-tenant-scope-selector, AC-2): each tenant option shows
            # name + how many companies scoping to it will reveal. Additive field;
            # organization_companies is an RLS-excluded lookup table, so the count
            # is correct without a bypass pin.
            "company_count": len(company_ids_for_organization(db, org)),
        }
        for org in organizations
    ]


@router.post("/platform/organizations")
def create_platform_organization(
    payload: PlatformOrganizationCreate,
    ctx: AuthContext = Depends(require_platform_admin_context),
    db: Session = Depends(get_db),
):
    if payload.billing_status:
        validate_billing_status(payload.billing_status)
    slug = _unique_org_slug(db, payload.slug or payload.name)
    external_id = payload.external_id or payload.primary_realm_id or slug
    if payload.primary_realm_id and db.query(Organization).filter(Organization.primary_realm_id == payload.primary_realm_id).first():
        raise HTTPException(status_code=400, detail="An organization already uses that primary_realm_id")
    if db.query(Organization).filter(Organization.external_id == external_id).first():
        raise HTTPException(status_code=400, detail="An organization already uses that external_id")

    # Clickwrap Gate #1 (plan 20260720-clickwrap-backend-spine, DEC-F5): both branches
    # below inherit the model default status="pending_acceptance" (they pass NO status),
    # so a platform-admin-created org is also gated until Tier-1 assent is recorded. The
    # billing axis (org.is_active / billing_status) is set separately below and is
    # orthogonal to this clickwrap lifecycle axis — do NOT set status="active" here.
    if stytch_b2b_client.enabled:
        stytch_payload = build_org_payload(payload.name.strip(), slug, payload.primary_realm_id)
        stytch_payload["organization_external_id"] = external_id
        resp = stytch_b2b_client.create_organization(stytch_payload)
        org_data = resp["organization"]
        org = Organization(
            stytch_organization_id=org_data["organization_id"],
            slug=org_data.get("organization_slug") or slug,
            name=org_data.get("organization_name") or payload.name.strip(),
            external_id=org_data.get("organization_external_id") or external_id,
            primary_realm_id=payload.primary_realm_id,
            auth_methods=org_data.get("auth_methods") or "RESTRICTED",
            allowed_auth_methods=org_data.get("allowed_auth_methods") or [],
            mfa_policy=org_data.get("mfa_policy") or settings.stytch_default_mfa_policy,
            mfa_methods=org_data.get("mfa_methods") or "RESTRICTED",
            allowed_mfa_methods=org_data.get("allowed_mfa_methods") or [],
        )
    else:
        org = Organization(
            stytch_organization_id=f"legacy-org-{slug}",
            slug=slug,
            name=payload.name.strip(),
            external_id=external_id,
            primary_realm_id=payload.primary_realm_id,
            auth_methods="RESTRICTED",
            allowed_auth_methods=[],
            mfa_policy=settings.stytch_default_mfa_policy,
            mfa_methods="RESTRICTED",
            allowed_mfa_methods=[],
        )
    org.billing_status = payload.billing_status
    org.billing_tier = payload.billing_tier
    org.billing_customer_id = payload.billing_customer_id
    org.is_active = payload.billing_status not in {"past_due", "suspended", "canceled"}
    if not org.is_active:
        org.suspended_at = utcnow()
        org.suspended_reason = f"Initial billing status: {payload.billing_status}"
    db.add(org)
    db.flush()
    record_auth_event(db, event_type="tenant_created", user=ctx.user, organization=org, details=_serialize_organization(org))
    db.commit()
    db.refresh(org)
    return _serialize_organization(org)


@router.put("/platform/organizations/{organization_id}")
def update_platform_organization(
    organization_id: int,
    payload: PlatformOrganizationUpdate,
    ctx: AuthContext = Depends(require_platform_admin_context),
    db: Session = Depends(get_db),
):
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    old_state = _serialize_organization(org)
    if payload.name is not None:
        org.name = payload.name.strip()
    if payload.slug is not None:
        org.slug = _unique_org_slug(db, payload.slug, existing_org_id=org.id)
    if payload.primary_realm_id is not None:
        conflict = (
            db.query(Organization)
            .filter(Organization.primary_realm_id == payload.primary_realm_id, Organization.id != org.id)
            .first()
        )
        if conflict:
            raise HTTPException(status_code=400, detail="An organization already uses that primary_realm_id")
        org.primary_realm_id = payload.primary_realm_id or None
    if payload.external_id is not None:
        conflict = (
            db.query(Organization)
            .filter(Organization.external_id == payload.external_id, Organization.id != org.id)
            .first()
        )
        if conflict:
            raise HTTPException(status_code=400, detail="An organization already uses that external_id")
        org.external_id = payload.external_id or None
    if payload.billing_status is not None:
        validate_billing_status(payload.billing_status)
        org.billing_status = payload.billing_status
        if payload.billing_status in {"past_due", "suspended", "canceled"}:
            org.is_active = False
            org.suspended_at = org.suspended_at or utcnow()
            org.suspended_reason = payload.suspended_reason or org.suspended_reason or f"Billing status: {payload.billing_status}"
        elif payload.is_active is None:
            org.is_active = True
            org.suspended_at = None
            org.suspended_reason = None
    if payload.billing_tier is not None:
        org.billing_tier = payload.billing_tier
    if payload.billing_customer_id is not None:
        org.billing_customer_id = payload.billing_customer_id or None
    if payload.is_active is not None:
        org.is_active = payload.is_active
        if not payload.is_active:
            org.suspended_at = org.suspended_at or utcnow()
            org.suspended_reason = payload.suspended_reason or org.suspended_reason or "Suspended by platform admin"
            if org.billing_status not in {"past_due", "suspended", "canceled"}:
                org.billing_status = "suspended"
        else:
            org.suspended_at = None
            org.suspended_reason = None
            if org.billing_status in {"past_due", "suspended", "canceled"}:
                org.billing_status = "active"
    elif payload.suspended_reason is not None:
        org.suspended_reason = payload.suspended_reason or None

    if stytch_b2b_client.enabled and any(value is not None for value in (payload.name, payload.slug, payload.primary_realm_id, payload.external_id)):
        stytch_payload = build_org_payload(org.name, org.slug, org.primary_realm_id)
        if org.external_id:
            stytch_payload["organization_external_id"] = org.external_id
        stytch_b2b_client.update_organization(org.stytch_organization_id, stytch_payload)

    record_auth_event(
        db,
        event_type="tenant_updated",
        user=ctx.user,
        organization=org,
        details={"before": old_state, "after": _serialize_organization(org)},
    )
    db.commit()
    db.refresh(org)
    return _serialize_organization(org)


@router.put("/organizations/{organization_id}/mfa-policy")
def update_organization_mfa_policy(
    organization_id: int,
    payload: OrganizationMfaPolicyUpdate,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    if payload.mfa_policy not in {"REQUIRED_FOR_ALL", "OPTIONAL"}:
        raise HTTPException(status_code=400, detail="mfa_policy must be REQUIRED_FOR_ALL or OPTIONAL")
    allowed = [method for method in payload.allowed_mfa_methods if method in {"totp", "sms_otp"}]
    if not allowed:
        raise HTTPException(status_code=400, detail="At least one allowed MFA method is required")
    if payload.mfa_methods not in {"ALL_ALLOWED", "RESTRICTED"}:
        raise HTTPException(status_code=400, detail="mfa_methods must be ALL_ALLOWED or RESTRICTED")

    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    membership = _require_org_admin(db, ctx, organization_id)
    old_state = {
        "mfa_policy": organization.mfa_policy,
        "mfa_methods": organization.mfa_methods,
        "allowed_mfa_methods": organization.allowed_mfa_methods,
    }
    stytch_payload = {
        "mfa_policy": payload.mfa_policy,
        "mfa_methods": payload.mfa_methods,
        "allowed_mfa_methods": allowed,
    }
    if stytch_b2b_client.enabled:
        stytch_b2b_client.update_organization(organization.stytch_organization_id, stytch_payload)
    organization.mfa_policy = payload.mfa_policy
    organization.mfa_methods = payload.mfa_methods
    organization.allowed_mfa_methods = allowed
    organization.roles_last_synced_at = utcnow()
    record_auth_event(
        db,
        event_type="mfa_policy_updated",
        user=ctx.user,
        organization=organization,
        membership=membership,
        details={"before": old_state, "after": stytch_payload},
    )
    db.commit()
    return _serialize_organization(organization)


@router.post("/password-reset/start")
@limiter.limit("10/minute")  # sends email: throttle reset-mail spam / account probing
def password_reset_start(request: Request, payload: PasswordResetStartRequest, db: Session = Depends(get_db)):
    user, organization, membership = _resolve_password_reset_target(
        db,
        email=payload.email,
        requested_organization_id=payload.organization_id,
    )
    reset_email = (user.email if user else payload.email).lower()
    if _can_send_stytch_password_reset(user=user, organization=organization, membership=membership):
        try:
            stytch_b2b_client.password_reset_by_email_start(
                {
                    "organization_id": organization.stytch_organization_id,
                    "email_address": reset_email,
                    "login_redirect_url": _login_redirect_url(),
                    "reset_password_redirect_url": _password_reset_redirect_url(),
                }
            )
            record_auth_event(
                db,
                event_type="password_reset_email_sent",
                user=user,
                organization=organization,
                membership=membership,
                target_email=None if user else reset_email,
            )
        except StytchB2BError as exc:
            record_auth_event(
                db,
                event_type="password_reset_failed",
                user=user,
                organization=organization,
                membership=membership,
                target_email=None if user else reset_email,
                event_status="failed",
                details={"reason": str(exc.detail)[:300]},
            )
    elif user:
        record_auth_event(db, event_type="password_reset_requested", user=user, organization=organization, membership=membership)
    db.commit()
    return {"ok": True, "message": "If the account exists, password reset instructions have been sent."}


@router.post("/password-reset/complete")
@limiter.limit("10/minute")  # credential-establishing endpoint: throttle reset-token guessing
def password_reset_complete(request: Request, payload: PasswordResetCompleteRequest, db: Session = Depends(get_db)):
    validate_password_strength(payload.new_password)
    user = get_user_by_email(db, payload.email) if payload.email else None
    organization = None
    membership = None
    if payload.token and stytch_b2b_client.enabled:
        try:
            body = {
                "password_reset_token": payload.token,
                "password": payload.new_password,
                "session_duration_minutes": settings.auth_session_minutes,
            }
            resp = stytch_b2b_client.password_reset_by_email_complete(body)
            mfa_challenge = _stytch_mfa_challenge_detail(resp)
            if mfa_challenge:
                if resp.get("member") and resp.get("organization"):
                    user, organization, membership = sync_stytch_session(db, session_payload=resp, fallback_user=user)
                    user.force_password_reset = False
                    user.password_last_changed_at = utcnow()
                    user.password_reset_required_reason = None
                    record_auth_event(
                        db,
                        event_type="password_reset_completed",
                        user=user,
                        organization=organization,
                        membership=membership,
                        details={"mfa_required": True},
                    )
                else:
                    record_auth_event(
                        db,
                        event_type="password_reset_completed",
                        user=user,
                        organization=organization,
                        membership=membership,
                        details={"mfa_required": True},
                    )
                db.commit()
                raise HTTPException(status_code=status.HTTP_202_ACCEPTED, detail=mfa_challenge)
            _validate_stytch_authenticated_context(resp)
            user, organization, membership = sync_stytch_session(db, session_payload=resp, fallback_user=user)
        except StytchB2BError as exc:
            record_auth_event(
                db,
                event_type="password_reset_failed",
                user=user,
                organization=organization,
                membership=membership,
                event_status="failed",
                details={"reason": str(exc.detail)[:300]},
            )
            db.commit()
            raise HTTPException(status_code=400, detail="Password reset failed")
    else:
        raise HTTPException(status_code=400, detail="Password reset token is required")

    if not user:
        raise HTTPException(status_code=400, detail="Password reset failed")
    user.force_password_reset = False
    user.password_last_changed_at = utcnow()
    user.password_reset_required_reason = None
    record_auth_event(db, event_type="password_reset_completed", user=user, organization=organization, membership=membership)
    db.commit()
    return {"ok": True}


@router.post("/email-change/request")
def request_email_change(
    payload: EmailChangeRequest,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    new_email = payload.new_email.lower()
    existing = get_user_by_email(db, new_email)
    if existing and existing.id != ctx.user.id:
        record_auth_event(db, event_type="email_change_failed", user=ctx.user, organization=ctx.organization, membership=ctx.membership, event_status="failed", details={"reason": "email_in_use"})
        db.commit()
        raise HTTPException(status_code=400, detail="Email is already registered")
    token = secrets.token_urlsafe(32)
    ctx.user.pending_email = new_email
    ctx.user.pending_email_token_hash = _hash_token(token)
    ctx.user.pending_email_expires_at = utcnow() + timedelta(hours=24)
    if ctx.user.auth_source in {"stytch", "hybrid"} and ctx.organization and ctx.membership and stytch_b2b_client.enabled:
        stytch_b2b_client.update_member_email(
            ctx.organization.stytch_organization_id,
            ctx.membership.stytch_member_id,
            {
                "email_address": new_email,
                "email_update_redirect_url": _email_change_redirect_url(),
            },
        )
    record_auth_event(db, event_type="email_change_requested", user=ctx.user, organization=ctx.organization, membership=ctx.membership, target_email=new_email)
    db.commit()
    return {"ok": True, "verification_token": token if is_dev_environment(settings.environment) else None}


@router.post("/email-change/complete")
def complete_email_change(
    payload: EmailChangeCompleteRequest,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    if not ctx.user.pending_email or not ctx.user.pending_email_token_hash:
        raise HTTPException(status_code=400, detail="No pending email change")
    expires_at = ctx.user.pending_email_expires_at
    if expires_at and expires_at.replace(tzinfo=timezone.utc) < utcnow():
        record_auth_event(db, event_type="email_change_failed", user=ctx.user, organization=ctx.organization, membership=ctx.membership, event_status="failed", details={"reason": "expired"})
        db.commit()
        raise HTTPException(status_code=400, detail="Email change token expired")
    if not secrets.compare_digest(ctx.user.pending_email_token_hash, _hash_token(payload.token)):
        record_auth_event(db, event_type="email_change_failed", user=ctx.user, organization=ctx.organization, membership=ctx.membership, event_status="failed", details={"reason": "bad_token"})
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid email change token")
    new_email = ctx.user.pending_email
    if get_user_by_email(db, new_email):
        raise HTTPException(status_code=400, detail="Email is already registered")
    ctx.user.email = new_email
    if ctx.membership:
        ctx.membership.email = new_email
    ctx.user.pending_email = None
    ctx.user.pending_email_token_hash = None
    ctx.user.pending_email_expires_at = None
    record_auth_event(db, event_type="email_change_completed", user=ctx.user, organization=ctx.organization, membership=ctx.membership, target_email=new_email)
    db.commit()
    return {"ok": True}


@router.post("/organizations/switch", response_model=UserOut)
def switch_organization(
    payload: OrganizationSwitchRequest,
    response: Response,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    membership = (
        db.query(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .filter(
            OrganizationMembership.user_id == ctx.user.id,
            Organization.stytch_organization_id == payload.organization_id,
        )
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Organization membership not found")
    organization = db.query(Organization).filter(Organization.id == membership.organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not organization.is_active and not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail="This tenant is suspended")
    if membership.status != "active" and not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail="Your membership is not active")

    if ctx.auth_source == "stytch":
        exchange = stytch_b2b_client.exchange_session(
            {
                "session_token": ctx.token,
                "organization_id": organization.stytch_organization_id,
                "session_duration_minutes": settings.auth_session_minutes,
            }
        )
        if exchange.get("intermediate_session_token"):
            raise HTTPException(
                status_code=status.HTTP_202_ACCEPTED,
                detail={
                    "code": "mfa_required",
                    "organization_id": organization.stytch_organization_id,
                    "member_id": exchange.get("member_id"),
                    "intermediate_session_token": exchange.get("intermediate_session_token"),
                },
            )
        _validate_stytch_authenticated_context(exchange)
        synced_user, synced_org, synced_membership = sync_stytch_session(db, session_payload=exchange, fallback_user=ctx.user)
        record_auth_event(db, event_type="organization_switched", user=synced_user, organization=synced_org, membership=synced_membership)
        return _handle_successful_login(
            db,
            response,
            user=synced_user,
            organization=synced_org,
            membership=synced_membership,
            session_token=exchange["session_token"],
            stytch_session_id=(exchange.get("member_session") or {}).get("member_session_id"),
            auth_source="stytch",
        )

    ctx.user.default_organization_id = organization.id
    record_auth_event(db, event_type="organization_switched", user=ctx.user, organization=organization, membership=membership)
    db.commit()
    # PLAN-EXEMPT: P1 fix (org-switch CSRF 403) per Steven 2026-06-23. The Stytch branch rotates +
    # returns the CSRF token via _handle_successful_login; this non-Stytch branch returned a
    # UserOut with csrf_token=null, which made the SPA delete its stored token (the cross-domain
    # cookie is unreadable from the web origin) → every subsequent POST 403'd until a refresh.
    # Rotate + set + return the token here too, mirroring /auth/me (auth.py:830-834).
    csrf_token = _new_csrf_token()
    _set_csrf_cookie(response, csrf_token)
    return {**_serialize_user(db, ctx.user, organization), "csrf_token": csrf_token}


@router.get("/organizations/{organization_id}/members")
def list_members(organization_id: int, ctx: AuthContext = Depends(get_current_auth_context), db: Session = Depends(get_db)):
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    _require_org_admin(db, ctx, organization_id)
    local_rows = (
        db.query(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == organization_id)
        .all()
    )
    visible_local_rows = [(membership, user) for membership, user in local_rows if membership.status != "deleted"]
    local_by_member_id = {membership.stytch_member_id: (membership, user) for membership, user in local_rows}
    local_by_email = {user.email.lower(): (membership, user) for membership, user in local_rows}

    if stytch_b2b_client.enabled:
        try:
            resp = stytch_b2b_client.search_members(organization.stytch_organization_id)
            members = []
            seen_member_ids: set[str] = set()
            for member in resp.get("members", []):
                member_id = member.get("member_id")
                email = (member.get("email_address") or "").lower()
                local_membership, _local_user = local_by_member_id.get(member_id) or local_by_email.get(email) or (None, None)
                if member_id:
                    seen_member_ids.add(member_id)
                members.append(
                    {
                        "member_id": member_id,
                        "email_address": member.get("email_address") or email,
                        "status": member.get("status") or (local_membership.status if local_membership else "active"),
                        "roles": [normalize_stytch_member_role(member, local_membership.role if local_membership else "read_only")],
                        "mfa_enrolled": bool(member.get("mfa_enrolled", local_membership.mfa_enrolled if local_membership else False)),
                    }
                )
            for membership, user in visible_local_rows:
                if membership.stytch_member_id in seen_member_ids:
                    continue
                members.append(
                    {
                        "member_id": membership.stytch_member_id,
                        "email_address": user.email,
                        "status": membership.status,
                        "roles": [membership.role],
                        "mfa_enrolled": membership.mfa_enrolled,
                    }
                )
            return members
        except StytchB2BError:
            pass
    return [
        {
            "member_id": membership.stytch_member_id,
            "email_address": user.email,
            "status": membership.status,
            "roles": [membership.role],
            "mfa_enrolled": membership.mfa_enrolled,
        }
        for membership, user in visible_local_rows
    ]


@router.post("/organizations/{organization_id}/members/invite")
def invite_member(
    organization_id: int,
    payload: MemberInviteRequest,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    validate_org_role(payload.role)
    role = canonical_role(payload.role)  # store canonical regardless of input vocabulary
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    _require_org_admin(db, ctx, organization_id)
    if not stytch_b2b_client.enabled:
        raise HTTPException(status_code=503, detail="Stytch invite flow is not configured")

    invite_payload = {
        "organization_id": organization.stytch_organization_id,
        "email_address": payload.email.lower(),
        "roles": [stytch_payload_role(role)],
        "trusted_metadata": {"invited_by_email": ctx.user.email},
    }
    if payload.name:
        invite_payload["name"] = payload.name
    if payload.invite_redirect_url:
        invite_payload["invite_redirect_url"] = payload.invite_redirect_url
    if (
        ctx.membership
        and ctx.membership.organization_id == organization.id
        and ctx.membership.stytch_member_id != "platform-admin"
    ):
        invite_payload["invited_by_member_id"] = ctx.membership.stytch_member_id

    try:
        resp = stytch_b2b_client.invite_member(
            invite_payload,
            session_token=_stytch_session_token_for_organization(ctx, organization),
        )
    except StytchB2BError as exc:
        record_auth_event(
            db,
            event_type="invite_failed",
            event_status="failed",
            actor_email=ctx.user.email,
            target_email=payload.email,
            organization=organization,
            details={
                "reason": _stytch_member_management_error(exc),
                "stytch_error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
            },
        )
        db.commit()
        raise HTTPException(
            status_code=_stytch_error_status(exc),
            detail=_stytch_member_management_error(exc),
        )
    invited_user = get_user_by_email(db, payload.email)
    if invited_user:
        if invited_user.auth_source == "legacy":
            invited_user.auth_source = "hybrid"
        if payload.name and not invited_user.display_name:
            invited_user.display_name = payload.name
        invited_user.stytch_last_sync_at = utcnow()
    else:
        invited_user = User(
            email=payload.email.lower(),
            password_hash="stytch-managed",
            role="user",
            auth_source="stytch",
            legacy_login_enabled=False,
            display_name=payload.name,
            stytch_last_sync_at=utcnow(),
        )
    if invited_user.id is None:
        db.add(invited_user)
        db.flush()
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization.id,
            OrganizationMembership.user_id == invited_user.id,
        )
        .first()
    )
    if not membership:
        membership = OrganizationMembership(
            organization_id=organization.id,
            user_id=invited_user.id,
            stytch_member_id=resp["member"]["member_id"],
            email=invited_user.email,
            role=role,
            status=resp["member"].get("status", "invited"),
        )
        db.add(membership)
    else:
        membership.stytch_member_id = resp["member"]["member_id"]
    membership.role = role
    membership.status = resp["member"].get("status", "invited")
    record_auth_event(db, event_type="invite_sent", actor_email=ctx.user.email, target_email=payload.email, organization=organization, membership=membership)
    db.commit()
    return resp


@router.put("/organizations/{organization_id}/members/{member_id}")
def update_member(
    organization_id: int,
    member_id: str,
    payload: MemberUpdateRequest,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    if payload.role:
        validate_org_role(payload.role)
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    _require_org_admin(db, ctx, organization_id)
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.stytch_member_id == member_id,
        )
        .first()
    )
    if not membership:
        if not stytch_b2b_client.enabled:
            raise HTTPException(status_code=404, detail="Member not found")
        try:
            if payload.reactivate:
                resp = stytch_b2b_client.reactivate_member(organization.stytch_organization_id, member_id)
            else:
                resp = stytch_b2b_client.update_member(
                    organization.stytch_organization_id,
                    member_id,
                    _stytch_member_update_payload(payload),
                )
        except StytchB2BError as exc:
            record_auth_event(
                db,
                event_type="member_update_failed",
                event_status="failed",
                actor_email=ctx.user.email,
                organization=organization,
                details={
                    "member_id": member_id,
                    "reason": _stytch_member_management_error(exc),
                    "stytch_error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
                },
            )
            db.commit()
            raise HTTPException(status_code=_stytch_error_status(exc), detail=_stytch_member_management_error(exc))
        record_auth_event(
            db,
            event_type="role_changed" if payload.role else "member_updated",
            actor_email=ctx.user.email,
            organization=organization,
            details={**payload.model_dump(exclude_none=True), "member_id": member_id, "source": "stytch_remote_only"},
        )
        db.commit()
        return resp or {"ok": True}

    resp = None
    try:
        if payload.reactivate and stytch_b2b_client.enabled:
            resp = stytch_b2b_client.reactivate_member(organization.stytch_organization_id, member_id)
        elif stytch_b2b_client.enabled:
            resp = stytch_b2b_client.update_member(
                organization.stytch_organization_id,
                member_id,
                _stytch_member_update_payload(payload),
            )
    except StytchB2BError as exc:
        record_auth_event(
            db,
            event_type="member_update_failed",
            event_status="failed",
            actor_email=ctx.user.email,
            organization=organization,
            membership=membership,
            details={
                "member_id": member_id,
                "reason": _stytch_member_management_error(exc),
                "stytch_error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
            },
        )
        db.commit()
        raise HTTPException(status_code=_stytch_error_status(exc), detail=_stytch_member_management_error(exc))

    if payload.role:
        membership.role = canonical_role(payload.role)
    if payload.mfa_enrolled is not None:
        membership.mfa_enrolled = payload.mfa_enrolled
    if resp and "member" in resp:
        membership.status = resp["member"].get("status", membership.status)
    membership.last_synced_at = utcnow()
    record_auth_event(db, event_type="role_changed" if payload.role else "member_updated", actor_email=ctx.user.email, organization=organization, membership=membership, details=payload.model_dump(exclude_none=True))
    db.commit()
    return resp or {"ok": True}


@router.post("/organizations/{organization_id}/members/{member_id}/password-reset")
def send_member_password_reset(
    organization_id: int,
    member_id: str,
    payload: MemberPasswordResetRequest,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    _require_org_admin(db, ctx, organization_id)
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.stytch_member_id == member_id,
            OrganizationMembership.status != "deleted",
        )
        .first()
    )
    target_email = (membership.email if membership else payload.email)
    if not target_email:
        raise HTTPException(status_code=400, detail="Member email is required to send a password reset")
    if not stytch_b2b_client.enabled or organization.stytch_organization_id.startswith("legacy-org-"):
        raise HTTPException(status_code=503, detail="Stytch password reset flow is not configured")

    target_email = target_email.lower()
    try:
        stytch_b2b_client.password_reset_by_email_start(
            {
                "organization_id": organization.stytch_organization_id,
                "email_address": target_email,
                "login_redirect_url": _login_redirect_url(),
                "reset_password_redirect_url": _password_reset_redirect_url(),
            }
        )
    except StytchB2BError as exc:
        record_auth_event(
            db,
            event_type="password_reset_failed",
            event_status="failed",
            actor_email=ctx.user.email,
            target_email=target_email,
            organization=organization,
            membership=membership,
            details={
                "member_id": member_id,
                "source": "organization_member_action",
                "reason": _stytch_member_management_error(exc),
                "stytch_error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
            },
        )
        db.commit()
        raise HTTPException(status_code=_stytch_error_status(exc), detail=_stytch_member_management_error(exc))

    record_auth_event(
        db,
        event_type="password_reset_email_sent",
        actor_email=ctx.user.email,
        target_email=target_email,
        organization=organization,
        membership=membership,
        details={"member_id": member_id, "source": "organization_member_action"},
    )
    db.commit()
    return {"ok": True}


@router.delete("/organizations/{organization_id}/members/{member_id}", status_code=204)
def remove_member(
    organization_id: int,
    member_id: str,
    ctx: AuthContext = Depends(get_current_auth_context),
    db: Session = Depends(get_db),
):
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    _require_org_admin(db, ctx, organization_id)
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.stytch_member_id == member_id,
        )
        .first()
    )
    if not membership:
        if not stytch_b2b_client.enabled or not _has_real_stytch_member_ref(organization, member_id):
            raise HTTPException(status_code=404, detail="Member not found")
        try:
            _delete_stytch_member_for_org(organization, member_id)
        except StytchB2BError as exc:
            record_auth_event(
                db,
                event_type="member_remove_failed",
                event_status="failed",
                actor_email=ctx.user.email,
                organization=organization,
                details={
                    "member_id": member_id,
                    "reason": _stytch_member_management_error(exc),
                    "stytch_error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
                },
            )
            db.commit()
            raise HTTPException(status_code=_stytch_error_status(exc), detail=_stytch_member_management_error(exc))
        record_auth_event(
            db,
            event_type="member_removed",
            actor_email=ctx.user.email,
            organization=organization,
            details={"member_id": member_id, "source": "stytch_remote_only"},
        )
        db.commit()
        return
    try:
        _delete_stytch_member_for_org(organization, member_id)
    except StytchB2BError as exc:
        record_auth_event(
            db,
            event_type="member_remove_failed",
            event_status="failed",
            actor_email=ctx.user.email,
            organization=organization,
            membership=membership,
            details={
                "member_id": member_id,
                "reason": _stytch_member_management_error(exc),
                "stytch_error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
            },
        )
        db.commit()
        raise HTTPException(status_code=_stytch_error_status(exc), detail=_stytch_member_management_error(exc))
    membership.status = "deleted"
    membership.is_default = False
    membership.last_synced_at = utcnow()
    removed_user = db.query(User).filter(User.id == membership.user_id).first()
    if removed_user and removed_user.default_organization_id == organization.id:
        # A default pointing at a removed membership strands a multi-org user at
        # login; clear it so the next login resolves (or asks) from live memberships.
        removed_user.default_organization_id = None
    record_auth_event(db, event_type="member_removed", actor_email=ctx.user.email, organization=organization, membership=membership)
    db.commit()
    return


@router.post("/mfa/totp/start")
def start_totp_enrollment(ctx: AuthContext = Depends(get_current_auth_context), db: Session = Depends(get_db)):
    if not ctx.organization or not ctx.membership:
        raise HTTPException(status_code=400, detail="No active organization context")
    resp = stytch_b2b_client.create_totp(
        {
            "organization_id": ctx.organization.stytch_organization_id,
            "member_id": ctx.membership.stytch_member_id,
        },
        session_token=ctx.token if ctx.auth_source == "stytch" else None,
    )
    record_auth_event(db, event_type="mfa_enrollment_started", user=ctx.user, organization=ctx.organization, membership=ctx.membership, details={"method": "totp"})
    db.commit()
    return resp


@router.post("/mfa/totp/start-challenge")
@limiter.limit("10/minute")  # pre-session MFA endpoint: throttle enrollment-challenge abuse
def start_totp_enrollment_challenge(request: Request, payload: TOTPEnrollmentStartRequest, db: Session = Depends(get_db)):
    if not payload.intermediate_session_token and not payload.session_token:
        raise HTTPException(status_code=400, detail="A session token is required to start MFA enrollment")
    resp = stytch_b2b_client.create_totp(
        _compact_stytch_payload(
            {
                "organization_id": payload.organization_id,
                "member_id": payload.member_id,
                "intermediate_session_token": payload.intermediate_session_token,
                "session_token": payload.session_token,
            }
        )
    )
    record_auth_event(
        db,
        event_type="mfa_enrollment_started",
        actor_email=(resp.get("member") or {}).get("email_address"),
        details={"method": "totp", "organization_id": payload.organization_id, "member_id": payload.member_id},
    )
    db.commit()
    return resp


@router.post("/mfa/totp/verify", response_model=AuthUserOut)
@limiter.limit("15/minute")  # credential-establishing endpoint: throttle 6-digit TOTP code guessing
def verify_totp(
    request: Request,
    payload: TOTPChallengeRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    resp = stytch_b2b_client.authenticate_totp(
        _compact_stytch_payload(
            {
                "organization_id": payload.organization_id,
                "member_id": payload.member_id,
                "code": payload.code,
                "intermediate_session_token": payload.intermediate_session_token,
                "session_token": payload.session_token,
                "session_duration_minutes": settings.auth_session_minutes,
                "set_mfa_enrollment": "enroll",
                "set_default_mfa": True,
            }
        )
    )
    _validate_stytch_authenticated_context(resp)
    user, organization, membership = sync_stytch_session(db, session_payload=resp)
    membership.mfa_enrolled = True
    record_auth_event(db, event_type="mfa_enrollment_completed", user=user, organization=organization, membership=membership, details={"method": "totp"})
    return _handle_successful_login(
        db,
        response,
        user=user,
        organization=organization,
        membership=membership,
        session_token=resp["session_token"],
        stytch_session_id=(resp.get("member_session") or {}).get("member_session_id"),
        auth_source="stytch",
    )


@router.post("/mfa/sms/send")
def send_sms_otp(payload: SmsChallengeRequest, ctx: AuthContext = Depends(get_current_auth_context), db: Session = Depends(get_db)):
    phone_number = _normalize_sms_phone_number(payload.phone_number, required=False)
    try:
        resp = stytch_b2b_client.send_sms_otp(
            _compact_stytch_payload(
                {
                    "organization_id": payload.organization_id,
                    "member_id": payload.member_id,
                    "mfa_phone_number": phone_number,
                    "intermediate_session_token": payload.intermediate_session_token,
                    "session_token": payload.session_token,
                }
            ),
            session_token=ctx.token if ctx.auth_source == "stytch" else None,
        )
    except StytchB2BError as exc:
        raise _sms_stytch_http_exception(exc)
    record_auth_event(db, event_type="login_mfa_challenge_started", user=ctx.user, organization=ctx.organization, membership=ctx.membership, details={"method": "sms"})
    db.commit()
    return resp


@router.post("/mfa/sms/send-challenge")
@limiter.limit("5/minute")  # sends SMS (per-message cost): tightest of the auth limits
def send_sms_otp_challenge(request: Request, payload: SmsChallengeRequest, db: Session = Depends(get_db)):
    if not payload.intermediate_session_token and not payload.session_token:
        raise HTTPException(status_code=400, detail="A session token is required to send an SMS code")
    phone_number = _normalize_sms_phone_number(payload.phone_number)
    try:
        resp = stytch_b2b_client.send_sms_otp(
            _compact_stytch_payload(
                {
                    "organization_id": payload.organization_id,
                    "member_id": payload.member_id,
                    "mfa_phone_number": phone_number,
                    "intermediate_session_token": payload.intermediate_session_token,
                    "session_token": payload.session_token,
                }
            )
        )
    except StytchB2BError as exc:
        record_auth_event(
            db,
            event_type="mfa_enrollment_failed",
            event_status="failed",
            details={
                "method": "sms",
                "organization_id": payload.organization_id,
                "member_id": payload.member_id,
                "reason": _stytch_error_type(exc) or _stytch_error_message(exc)[:120],
            },
        )
        db.commit()
        raise _sms_stytch_http_exception(exc)
    record_auth_event(
        db,
        event_type="mfa_enrollment_started",
        actor_email=(resp.get("member") or {}).get("email_address"),
        details={"method": "sms", "organization_id": payload.organization_id, "member_id": payload.member_id},
    )
    db.commit()
    return resp


@router.post("/mfa/sms/verify", response_model=AuthUserOut)
@limiter.limit("15/minute")  # credential-establishing endpoint: throttle SMS OTP code guessing
def verify_sms_otp(request: Request, payload: SmsChallengeRequest, response: Response, db: Session = Depends(get_db)):
    if not payload.code:
        raise HTTPException(status_code=400, detail="Code is required")
    resp = stytch_b2b_client.authenticate_sms_otp(
        _compact_stytch_payload(
            {
                "organization_id": payload.organization_id,
                "member_id": payload.member_id,
                "code": payload.code,
                "intermediate_session_token": payload.intermediate_session_token,
                "session_token": payload.session_token,
                "session_duration_minutes": settings.auth_session_minutes,
                "set_mfa_enrollment": "enroll",
            }
        )
    )
    _validate_stytch_authenticated_context(resp)
    user, organization, membership = sync_stytch_session(db, session_payload=resp)
    membership.mfa_enrolled = True
    record_auth_event(db, event_type="mfa_enrollment_completed", user=user, organization=organization, membership=membership, details={"method": "sms"})
    return _handle_successful_login(
        db,
        response,
        user=user,
        organization=organization,
        membership=membership,
        session_token=resp["session_token"],
        stytch_session_id=(resp.get("member_session") or {}).get("member_session_id"),
        auth_source="stytch",
    )

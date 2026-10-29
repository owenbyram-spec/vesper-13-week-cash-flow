import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AuthAuditEvent,
    AuthSession,
    Organization,
    OrganizationCompany,
    OrganizationMembership,
    QBOCompanyInfo,
    QBOToken,
    User,
)
from ..roles import (
    PLATFORM_SUPER_ADMIN,
    VIEWER,
    canonical_role,
    role_weight,
)
from .stytch_b2b import StytchB2BError, stytch_b2b_client


# Stytch's RBAC policy is keyed by its own role_ids. We keep the canonical app
# role authoritative locally and translate to the Stytch role_id at the API
# boundary so member creation keeps using the role_ids the Stytch project
# already defines. Canonical -> Stytch role_id (send direction); the receive
# direction canonicalizes whatever Stytch returns (see normalize_stytch_member_role).
_CANONICAL_TO_STYTCH_ROLE = {
    "viewer": "read_only",
    "analyst": "analyst",
    "accounting": "manager",
    "tenant_admin": "org_admin",
    "platform_super_admin": "stytch_admin",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationSelectionRequired(ValueError):
    """Two or more active memberships and no usable default: the user must pick one.

    ``candidates`` holds the (organization, membership) pairs the user may enter."""

    def __init__(self, candidates: list[tuple[Organization, OrganizationMembership]]):
        super().__init__("organization_selection_required")
        self.candidates = candidates


def platform_admin_emails() -> set[str]:
    configured = {
        item.strip().lower()
        for item in settings.auth_platform_admin_emails.split(",")
        if item.strip()
    }
    if settings.auth_priority_admin_email:
        configured.add(settings.auth_priority_admin_email.lower())
    return configured


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return value or "organization"


def normalize_global_role(role: str, email: str, is_priority_admin: bool) -> str:
    """Resolve a user's canonical tenant role for a new membership.

    Platform staff (is_priority_admin or a configured platform-admin email) map
    to PLATFORM_SUPER_ADMIN; everyone else collapses to a canonical role via the
    legacy alias map (admin->tenant_admin, manager/cfo/editor->accounting,
    user->viewer, ...). Unknown values default to least-privilege VIEWER.
    """
    if is_priority_admin or email.lower() in platform_admin_emails():
        return PLATFORM_SUPER_ADMIN
    return canonical_role(role or VIEWER)


def stytch_payload_role(role: str) -> str:
    """Translate a canonical (or legacy) app role to the Stytch role_id to send.

    The canonical app roles are app-only and are not part of Stytch's RBAC
    policy, so we map each to the built-in/legacy role_id the Stytch project
    already accepts. Authorization always uses the local canonical role +
    is_priority_admin, never the Stytch role.
    """
    return _CANONICAL_TO_STYTCH_ROLE.get(canonical_role(role), "read_only")


def normalize_stytch_member_role(member: dict[str, Any], fallback: str = VIEWER) -> str:
    """Canonicalize the role a Stytch member carries into a canonical app role.

    Stytch returns its own role_ids (read_only/manager/org_admin/stytch_admin/…)
    AND always includes the implicit base role ``stytch_member``. We canonicalize
    EVERY role Stytch reports and keep the HIGHEST-privilege one by weight, so a
    member carrying ``[stytch_member, org_admin]`` resolves to ``tenant_admin``
    instead of being collapsed to ``viewer`` by the always-first ``stytch_member``
    entry. Empty/unknown -> fallback (default least-privilege VIEWER).
    """
    roles = member.get("roles") or []
    role_ids: list[str] = []
    for entry in roles:
        if isinstance(entry, dict):
            rid = entry.get("role_id") or entry.get("id")
        elif isinstance(entry, str):
            rid = entry
        else:
            rid = None
        if rid:
            role_ids.append(rid)
    if not role_ids:
        return canonical_role(fallback)
    # Pick the highest-weight canonical role across everything Stytch reports.
    best = max(role_ids, key=lambda rid: role_weight(canonical_role(rid)))
    return canonical_role(best)


def _allowed_auth_methods() -> list[str]:
    return [item.strip() for item in settings.stytch_default_auth_methods.split(",") if item.strip()]


def _allowed_mfa_methods() -> list[str]:
    return [item.strip() for item in settings.stytch_default_allowed_mfa_methods.split(",") if item.strip()]


def record_auth_event(
    db: Session,
    *,
    event_type: str,
    user: Optional[User] = None,
    organization: Optional[Organization] = None,
    membership: Optional[OrganizationMembership] = None,
    event_status: str = "success",
    actor_email: Optional[str] = None,
    target_email: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    db.add(
        AuthAuditEvent(
            user_id=user.id if user else None,
            organization_id=organization.id if organization else None,
            membership_id=membership.id if membership else None,
            event_type=event_type,
            event_status=event_status,
            actor_email=actor_email or (user.email if user else None),
            target_email=target_email,
            details=details or {},
        )
    )


def ensure_priority_admin_flag(user: User) -> None:
    if user.email.lower() in platform_admin_emails():
        user.is_priority_admin = True
        if user.role != "admin":
            user.role = "admin"


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email.lower()).first()


def build_org_payload(name: str, slug: str, realm_id: Optional[str]) -> dict[str, Any]:
    payload = {
        "organization_name": name,
        "organization_slug": slug,
        "organization_external_id": realm_id or slug,
        "auth_methods": "RESTRICTED",
        "allowed_auth_methods": _allowed_auth_methods(),
        "mfa_policy": settings.stytch_default_mfa_policy,
        "mfa_methods": "RESTRICTED",
        "allowed_mfa_methods": _allowed_mfa_methods(),
        "email_invites": "ALL_ALLOWED",
    }
    if realm_id:
        payload["trusted_metadata"] = {"primary_realm_id": realm_id}
    return payload


def ensure_local_org_for_realm(db: Session, realm_id: str) -> Organization:
    org = db.query(Organization).filter(Organization.primary_realm_id == realm_id).first()
    if org:
        if not db.query(OrganizationCompany).filter(OrganizationCompany.realm_id == realm_id).first():
            db.add(OrganizationCompany(organization_id=org.id, realm_id=realm_id, is_primary=True))
            db.flush()
        return org

    mapped = (
        db.query(Organization)
        .join(OrganizationCompany, OrganizationCompany.organization_id == Organization.id)
        .filter(OrganizationCompany.realm_id == realm_id)
        .first()
    )
    if mapped:
        return mapped

    info = db.query(QBOCompanyInfo).filter(QBOCompanyInfo.realm_id == realm_id).first()
    name = (info.company_name if info and info.company_name else f"Company {realm_id}").strip()
    slug = slugify(name)

    # Clickwrap Gate #1 (plan 20260720-clickwrap-backend-spine, DEC-F5): this is a
    # LOGIN-PATH org creator — it mints an org as a side effect of QBO/Stytch auth.
    # New orgs MUST start status="pending_acceptance" so a self-serve login cannot
    # bypass the clickwrap; only the Tier-1 accept path flips status→"active".
    # (status is a separate lifecycle axis from is_active/billing_status — do not overload.)
    if stytch_b2b_client.enabled:
        resp = stytch_b2b_client.create_organization(build_org_payload(name, slug, realm_id))
        org_data = resp["organization"]
        org = Organization(
            stytch_organization_id=org_data["organization_id"],
            slug=org_data.get("organization_slug") or slug,
            name=org_data.get("organization_name") or name,
            external_id=org_data.get("organization_external_id") or realm_id,
            primary_realm_id=realm_id,
            auth_methods=org_data.get("auth_methods") or "RESTRICTED",
            allowed_auth_methods=org_data.get("allowed_auth_methods") or _allowed_auth_methods(),
            mfa_policy=org_data.get("mfa_policy") or settings.stytch_default_mfa_policy,
            mfa_methods=org_data.get("mfa_methods") or "RESTRICTED",
            allowed_mfa_methods=org_data.get("allowed_mfa_methods") or _allowed_mfa_methods(),
            status="pending_acceptance",
        )
    else:
        org = Organization(
            stytch_organization_id=f"legacy-org-{realm_id}",
            slug=slug,
            name=name,
            external_id=realm_id,
            primary_realm_id=realm_id,
            auth_methods="RESTRICTED",
            allowed_auth_methods=_allowed_auth_methods(),
            mfa_policy=settings.stytch_default_mfa_policy,
            mfa_methods="RESTRICTED",
            allowed_mfa_methods=_allowed_mfa_methods(),
            status="pending_acceptance",
        )

    db.add(org)
    db.flush()
    db.add(OrganizationCompany(organization_id=org.id, realm_id=realm_id, is_primary=True))
    db.flush()
    return org


def ensure_membership(
    db: Session,
    *,
    user: User,
    organization: Organization,
    legacy_role: Optional[str] = None,
) -> OrganizationMembership:
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.organization_id == organization.id,
        )
        .first()
    )
    if membership:
        return membership

    role = normalize_global_role(legacy_role or user.role, user.email, user.is_priority_admin)
    member_email = user.email.lower()

    if stytch_b2b_client.enabled:
        payload = {
            "email_address": member_email,
            "name": user.display_name,
            "external_id": f"user-{user.id}",
            "roles": [stytch_payload_role(role)],
            "is_breakglass": bool(user.is_priority_admin),
            "trusted_metadata": {
                "local_user_id": user.id,
                "legacy_role": user.role,
            },
        }
        try:
            resp = stytch_b2b_client.create_member(organization.stytch_organization_id, payload)
            member = resp["member"]
        except StytchB2BError as exc:
            if exc.status_code != 400:
                raise
            existing = stytch_b2b_client.search_members(organization.stytch_organization_id)
            candidate = next(
                (item for item in existing.get("members", []) if item.get("email_address", "").lower() == member_email),
                None,
            )
            if not candidate:
                raise
            # The member already exists in Stytch (create conflicted on a 400). Re-assert
            # the role we intended so local and Stytch don't silently drift — adopting the
            # candidate as-is would leave Stytch on whatever role it already had. A
            # role-policy gap (a custom role_id missing from Stytch's RBAC policy) raises
            # StytchB2BError here, surfacing loudly at provisioning time instead of as
            # later silent drift.
            updated = stytch_b2b_client.update_member(
                organization.stytch_organization_id,
                candidate["member_id"],
                {"roles": [stytch_payload_role(role)]},
            )
            member = updated.get("member", candidate)
    else:
        member = {"member_id": f"legacy-member-{organization.id}-{user.id}", "email_address": member_email, "status": "active"}

    membership = OrganizationMembership(
        organization_id=organization.id,
        user_id=user.id,
        stytch_member_id=member["member_id"],
        email=member_email,
        role=role,
        status=member.get("status") or "active",
        is_default=user.default_organization_id in (None, organization.id),
        mfa_enrolled=bool(member.get("mfa_enrolled", False)),
        last_synced_at=utcnow(),
    )
    if user.default_organization_id is None:
        user.default_organization_id = organization.id
    db.add(membership)
    db.flush()
    return membership


def build_accessible_organizations(db: Session, user: User) -> list[Organization]:
    orgs = (
        db.query(Organization)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .filter(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.status == "active",
            Organization.is_active.is_(True),
        )
        .all()
    )
    if orgs:
        return orgs

    # Non-priority users with no memberships have no accessible organizations.
    if not user.is_priority_admin:
        return []

    realm_ids = {row.realm_id for row in db.query(QBOToken).all()}
    return [ensure_local_org_for_realm(db, realm_id) for realm_id in sorted(realm_ids)]


def backfill_user_memberships(db: Session, user: User) -> list[OrganizationMembership]:
    ensure_priority_admin_flag(user)
    memberships: list[OrganizationMembership] = []
    for org in build_accessible_organizations(db, user):
        memberships.append(
            ensure_membership(
                db,
                user=user,
                organization=org,
                legacy_role=user.role,
            )
        )
    # NOTE: platform admins are intentionally NOT auto-joined to every organization.
    # The is_priority_admin flag already grants cross-tenant access (see
    # app/dependencies.py), so force-membering them into every tenant only clutters
    # the tenant roster and triggers Stytch member creation with an internal-only role.
    return memberships


def migrate_password_for_membership(db: Session, *, user: User, membership: OrganizationMembership, organization: Organization) -> None:
    if not stytch_b2b_client.enabled:
        return
    stytch_b2b_client.migrate_password(
        {
            "email_address": user.email.lower(),
            "hash": user.password_hash,
            "hash_type": "bcrypt",
            "organization_id": organization.stytch_organization_id,
            "external_id": f"user-{user.id}",
        }
    )
    user.auth_source = "hybrid"
    user.migrated_to_stytch_at = utcnow()
    user.stytch_last_sync_at = utcnow()
    membership.last_synced_at = utcnow()
    record_auth_event(
        db,
        event_type="legacy_user_migration_completed",
        user=user,
        organization=organization,
        membership=membership,
        details={"strategy": "password_hash_migrate"},
    )


def upsert_auth_session(
    db: Session,
    *,
    user: User,
    organization: Optional[Organization],
    membership: Optional[OrganizationMembership],
    auth_source: str,
    session_token: Optional[str],
    stytch_session_id: Optional[str],
    expires_at: Optional[datetime],
) -> AuthSession:
    token_hint = session_token[-8:] if session_token else None
    record = AuthSession(
        user_id=user.id,
        organization_id=organization.id if organization else None,
        membership_id=membership.id if membership else None,
        auth_source=auth_source,
        cookie_token_hint=token_hint,
        stytch_session_id=stytch_session_id,
        last_seen_at=utcnow(),
        expires_at=expires_at,
    )
    db.add(record)
    user.last_login_at = utcnow()
    return record


def resolve_org_for_login(
    db: Session,
    *,
    user: User,
    requested_organization_id: Optional[str] = None,
) -> tuple[Optional[Organization], Optional[OrganizationMembership], list[dict[str, Any]]]:
    memberships = backfill_user_memberships(db, user)
    memberships = [
        membership
        for membership in memberships
        if membership and membership.status == "active"
    ]
    org_map = {
        membership.organization_id: db.query(Organization).filter(Organization.id == membership.organization_id).first()
        for membership in memberships
    }
    if not user.is_priority_admin:
        memberships = [
            membership
            for membership in memberships
            if org_map.get(membership.organization_id) and org_map[membership.organization_id].is_active
        ]
    options = [
        {
            "id": org.stytch_organization_id,
            "local_id": org.id,
            "slug": org.slug,
            "name": org.name,
            "primary_realm_id": org.primary_realm_id,
            "role": membership.role,
        }
        for membership, org in ((m, org_map.get(m.organization_id)) for m in memberships)
        if org
    ]

    if not memberships:
        return None, None, options

    if requested_organization_id:
        for membership in memberships:
            org = org_map.get(membership.organization_id)
            if org and requested_organization_id in {org.stytch_organization_id, org.slug, str(org.id), org.primary_realm_id or ""}:
                return org, membership, options

    default_membership = next((m for m in memberships if m.organization_id == user.default_organization_id), None)
    if default_membership:
        return org_map[default_membership.organization_id], default_membership, options
    if len(memberships) == 1:
        membership = memberships[0]
        return org_map[membership.organization_id], membership, options
    raise OrganizationSelectionRequired(
        [(org_map[m.organization_id], m) for m in memberships if org_map.get(m.organization_id)]
    )


def sync_stytch_session(
    db: Session,
    *,
    session_payload: dict[str, Any],
    fallback_user: Optional[User] = None,
) -> tuple[User, Organization, OrganizationMembership]:
    member = session_payload["member"]
    org_data = session_payload["organization"]
    member_email = member["email_address"].lower()
    membership = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.stytch_member_id == member["member_id"])
        .first()
    )
    user = get_user_by_email(db, member_email)
    if not user and membership:
        user = db.query(User).filter(User.id == membership.user_id).first()
    if not user and fallback_user and fallback_user.email.lower() == member_email:
        user = fallback_user
    if not user:
        user = User(
            email=member_email,
            password_hash="stytch-managed",
            role="user",
            auth_source="stytch",
            legacy_login_enabled=False,
            display_name=member.get("name"),
        )
        db.add(user)
        db.flush()

    ensure_priority_admin_flag(user)
    org = db.query(Organization).filter(Organization.stytch_organization_id == org_data["organization_id"]).first()
    if not org:
        # Clickwrap Gate #1 (plan 20260720-clickwrap-backend-spine, DEC-F5): sync_stytch_session
        # runs on every Stytch login and mints the local org on first sight — a LOGIN-PATH
        # creator. New orgs MUST start status="pending_acceptance" so a self-serve login cannot
        # bypass the clickwrap; only the Tier-1 accept path flips status→"active". status is a
        # separate lifecycle axis from is_active/billing_status — do not overload those.
        org = Organization(
            stytch_organization_id=org_data["organization_id"],
            slug=org_data.get("organization_slug") or slugify(org_data.get("organization_name") or member["email_address"]),
            name=org_data.get("organization_name") or "Organization",
            external_id=org_data.get("organization_external_id"),
            primary_realm_id=(org_data.get("trusted_metadata") or {}).get("primary_realm_id"),
            auth_methods=org_data.get("auth_methods") or "ALL_ALLOWED",
            allowed_auth_methods=org_data.get("allowed_auth_methods") or [],
            mfa_policy=org_data.get("mfa_policy") or "OPTIONAL",
            mfa_methods=org_data.get("mfa_methods") or "ALL_ALLOWED",
            allowed_mfa_methods=org_data.get("allowed_mfa_methods") or [],
            status="pending_acceptance",
        )
        db.add(org)
        db.flush()
    else:
        org.name = org_data.get("organization_name") or org.name
        org.slug = org_data.get("organization_slug") or org.slug
        org.external_id = org_data.get("organization_external_id") or org.external_id
        org.primary_realm_id = org.primary_realm_id or (org_data.get("trusted_metadata") or {}).get("primary_realm_id")
        org.mfa_policy = org_data.get("mfa_policy") or org.mfa_policy

    if not membership:
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == org.id,
                OrganizationMembership.user_id == user.id,
            )
            .first()
        )
    if not membership:
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == org.id,
                OrganizationMembership.email == member_email,
            )
            .first()
        )
    if not membership:
        membership = OrganizationMembership(
            organization_id=org.id,
            user_id=user.id,
            stytch_member_id=member["member_id"],
            email=member_email,
            role=normalize_stytch_member_role(member),
            status=member.get("status") or "active",
            mfa_enrolled=bool(member.get("mfa_enrolled", False)),
        )
        db.add(membership)
    else:
        membership.organization_id = org.id
        membership.user_id = user.id
        membership.stytch_member_id = member["member_id"]
        membership.email = member_email
        membership.role = normalize_stytch_member_role(member, membership.role)
        membership.status = member.get("status") or membership.status
        membership.mfa_enrolled = bool(member.get("mfa_enrolled", membership.mfa_enrolled))
    membership.last_authenticated_at = utcnow()
    membership.last_synced_at = utcnow()
    user.auth_source = "stytch"
    user.stytch_last_sync_at = utcnow()
    if user.default_organization_id is None:
        user.default_organization_id = org.id
    return user, org, membership


def compute_cookie_expiry() -> datetime:
    return utcnow() + timedelta(minutes=settings.auth_session_minutes)

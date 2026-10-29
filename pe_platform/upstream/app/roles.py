"""Canonical tenant RBAC role model.

Single source of truth for tenant role names, their weight ladder, and the
capability (permission) set each role grants. Route guards and the agent tool
policy compare *weights*; product code that needs to reason about capabilities
should read the *permission* strings emitted by :func:`role_permissions`.

Five canonical tenant roles, cumulative capabilities:

    VIEWER (10)                read
    ANALYST (20)               + analyze            (NO QuickBooks writebacks)
    ACCOUNTING (30)            + propose/execute QBO writebacks, schedules,
                                 entity/operational finance work
    TENANT_ADMIN (40)          + manage members & roles, billing, MFA policy,
                                 QBO connection, tenant settings, data export
                                 (everything a platform super-admin can do, but
                                 ONLY within the admin's own tenant)
    PLATFORM_SUPER_ADMIN (100) + cross-tenant platform operations

Legacy role strings (member/editor/cfo/manager/owner/org_admin/admin/…) and
Stytch's built-in role names are kept as weight aliases so historical
``organization_memberships.role`` values keep resolving while existing rows are
normalized to the canonical set by Alembic migration. The platform-staff
break-glass remains the global ``User.is_priority_admin`` boolean; tenant-admin
power derives from ``membership.role`` scoped to the org, not from any role
string alone.
"""
from __future__ import annotations

# ── Canonical role names (stored lowercase in organization_memberships.role) ──
VIEWER = "viewer"
ANALYST = "analyst"
ACCOUNTING = "accounting"
TENANT_ADMIN = "tenant_admin"
PLATFORM_SUPER_ADMIN = "platform_super_admin"

CANONICAL_ROLES: tuple[str, ...] = (
    VIEWER,
    ANALYST,
    ACCOUNTING,
    TENANT_ADMIN,
    PLATFORM_SUPER_ADMIN,
)

# ── Canonical weight ladder ──
VIEWER_WEIGHT = 10
ANALYST_WEIGHT = 20
ACCOUNTING_WEIGHT = 30
TENANT_ADMIN_WEIGHT = 40
PLATFORM_SUPER_ADMIN_WEIGHT = 100

# ── Permission (capability) strings ──
READ = "read"
ANALYZE = "analyze"
QBO_WRITEBACK = "qbo_writeback"
MANAGE_SCHEDULES = "manage_schedules"
MANAGE_ENTITY = "manage_entity"
MANAGE_MEMBERS = "manage_members"
MANAGE_BILLING = "manage_billing"
MANAGE_MFA = "manage_mfa"
MANAGE_QBO_CONNECTION = "manage_qbo_connection"
EXPORT_TENANT_DATA = "export_tenant_data"
MANAGE_TENANT_SETTINGS = "manage_tenant_settings"
PLATFORM_CROSS_TENANT = "platform_cross_tenant"

# ── Weight ladder, keyed by UPPER-cased role string (canonical + legacy aliases).
# Guards and the agent tool policy look roles up here; every historical value a
# membership.role / Stytch role string can hold must resolve to a real weight.
ROLE_WEIGHTS: dict[str, int] = {
    # Canonical
    "VIEWER": VIEWER_WEIGHT,
    "ANALYST": ANALYST_WEIGHT,
    "ACCOUNTING": ACCOUNTING_WEIGHT,
    "TENANT_ADMIN": TENANT_ADMIN_WEIGHT,
    "PLATFORM_SUPER_ADMIN": PLATFORM_SUPER_ADMIN_WEIGHT,
    # Legacy → VIEWER (10). Stytch's built-in default "member" role and synced
    # "stytch_member" memberships both collapse to least-privilege read access.
    "READ_ONLY": VIEWER_WEIGHT,
    "USER": VIEWER_WEIGHT,
    "MEMBER": VIEWER_WEIGHT,
    "STYTCH_MEMBER": VIEWER_WEIGHT,
    # Legacy → ACCOUNTING (30)
    "EDITOR": ACCOUNTING_WEIGHT,
    "MANAGER": ACCOUNTING_WEIGHT,
    "CFO": ACCOUNTING_WEIGHT,
    # Legacy → TENANT_ADMIN (40)
    "OWNER": TENANT_ADMIN_WEIGHT,
    "ORG_ADMIN": TENANT_ADMIN_WEIGHT,
    "STYTCH_ADMIN": TENANT_ADMIN_WEIGHT,
    "ADMIN": TENANT_ADMIN_WEIGHT,
    "LEGACY_ADMIN": TENANT_ADMIN_WEIGHT,
    # Legacy → PLATFORM_SUPER_ADMIN (100)
    "INTERNAL_SUPER_ADMIN": PLATFORM_SUPER_ADMIN_WEIGHT,
}

# ── Legacy/raw role string → canonical role name (lower-cased keys). ──
# Used by the normalization migration and by any code that wants to collapse a
# historical or Stytch-supplied role string to one of the five canonical names.
LEGACY_ROLE_TO_CANONICAL: dict[str, str] = {
    # → VIEWER
    "viewer": VIEWER,
    "read_only": VIEWER,
    "user": VIEWER,
    "member": VIEWER,
    "stytch_member": VIEWER,
    # → ANALYST
    "analyst": ANALYST,
    # → ACCOUNTING
    "accounting": ACCOUNTING,
    "editor": ACCOUNTING,
    "manager": ACCOUNTING,
    "cfo": ACCOUNTING,
    # → TENANT_ADMIN
    "tenant_admin": TENANT_ADMIN,
    "owner": TENANT_ADMIN,
    "org_admin": TENANT_ADMIN,
    "stytch_admin": TENANT_ADMIN,
    "admin": TENANT_ADMIN,
    "legacy_admin": TENANT_ADMIN,
    # → PLATFORM_SUPER_ADMIN
    "platform_super_admin": PLATFORM_SUPER_ADMIN,
    "internal_super_admin": PLATFORM_SUPER_ADMIN,
}

# Least-privilege default for a newly invited / synced member.
DEFAULT_INVITE_ROLE = VIEWER


def canonical_role(role: str | None) -> str:
    """Collapse any legacy / Stytch / canonical role string to a canonical name.

    Unknown or empty values fall back to least-privilege VIEWER.
    """
    return LEGACY_ROLE_TO_CANONICAL.get((role or "").strip().lower(), VIEWER)


def role_weight(role: str | None) -> int:
    """Return the weight for a role string (canonical or legacy alias).

    Unknown values return 0 so they never satisfy a minimum-role check.
    """
    return ROLE_WEIGHTS.get((role or "").strip().upper(), 0)


def has_at_least(role: str | None, minimum_role: str | None) -> bool:
    """True if ``role`` is at least as privileged as ``minimum_role`` by weight.

    Accepts canonical or legacy role strings on either side.
    """
    return role_weight(role) >= role_weight(minimum_role)


def is_tenant_admin(role: str | None) -> bool:
    """True if a role string grants tenant-admin authority (weight >= 40).

    Canonical ``tenant_admin`` and every legacy alias that historically meant
    org-admin (owner / org_admin / stytch_admin / admin / legacy_admin) resolve
    here; ``platform_super_admin`` (weight 100) does too. ACCOUNTING(30) and
    below do not.
    """
    return role_weight(role) >= TENANT_ADMIN_WEIGHT


def role_permissions(role: str | None) -> list[str]:
    """Cumulative capability set granted by a role string.

    Capabilities accumulate by weight, so each tier inherits everything below
    it. The returned list is sorted and de-duplicated.
    """
    weight = role_weight(role)
    permissions = [READ]
    if weight >= ANALYST_WEIGHT:
        permissions.append(ANALYZE)
    if weight >= ACCOUNTING_WEIGHT:
        permissions.extend([QBO_WRITEBACK, MANAGE_SCHEDULES, MANAGE_ENTITY])
    if weight >= TENANT_ADMIN_WEIGHT:
        permissions.extend(
            [
                MANAGE_MEMBERS,
                MANAGE_BILLING,
                MANAGE_MFA,
                MANAGE_QBO_CONNECTION,
                EXPORT_TENANT_DATA,
                MANAGE_TENANT_SETTINGS,
            ]
        )
    if weight >= PLATFORM_SUPER_ADMIN_WEIGHT:
        permissions.append(PLATFORM_CROSS_TENANT)
    return sorted(set(permissions))

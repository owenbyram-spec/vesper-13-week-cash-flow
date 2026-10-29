from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from fastapi import HTTPException


from ..config import is_dev_environment, settings
from ..tenant_rls import org_grain_read, tenant_scope_live
from ..models import (
    AuthAuditEvent,
    EntityBilling,
    Organization,
    OrganizationCompany,
    OrganizationMembership,
    QBOToken,
    User,
    UserCompanyAccess,
)

# PLAN-EXEMPT: pilot LLM-usage attribution (AC-4.1) — shared bind helper imports.
import contextlib
from collections.abc import Iterator

from .tenant_tag import TenantTagContext, bind_tenant_tag

# Billing states that should shut a single company off from its tenant's users.
COMPANY_SUSPENDED_BILLING_STATUSES = {"past_due", "suspended", "canceled", "unpaid"}


def company_ids_for_organization(db: Session, organization: Organization | int) -> list[str]:
    organization_id = organization if isinstance(organization, int) else organization.id
    primary_realm_id = None if isinstance(organization, int) else organization.primary_realm_id
    # E-2: this function IS the org->realm enumeration - it feeds the company switcher and
    # accessible_company_ids_for_user's authorization ladder - so filtering it to one realm
    # would make it return its own input. It yields realm IDENTIFIERS the caller is
    # entitled to enumerate, never another realm's rows.
    with org_grain_read(
        "org->realm enumeration: returns the org's realm identifiers, which is the "
        "input to the company switcher and the per-company authorization ladder"
    ):
        realm_ids = {
            row.realm_id
            for row in (
                db.query(OrganizationCompany.realm_id)
                .filter(OrganizationCompany.organization_id == organization_id)
                .order_by(OrganizationCompany.is_primary.desc(), OrganizationCompany.realm_id.asc())
                .all()
            )
            if row.realm_id
        }
    if primary_realm_id:
        realm_ids.add(primary_realm_id)
    return sorted(realm_ids)


def organization_for_company(db: Session, realm_id: str) -> Optional[Organization]:
    realm_id = str(realm_id)
    mapped = (
        db.query(Organization)
        .join(OrganizationCompany, OrganizationCompany.organization_id == Organization.id)
        .filter(OrganizationCompany.realm_id == realm_id)
        .first()
    )
    if mapped:
        return mapped
    return db.query(Organization).filter(Organization.primary_realm_id == realm_id).first()


def organization_company_for_realm(
    db: Session,
    *,
    organization_id: Optional[int] = None,
    realm_id: str,
) -> Optional[OrganizationCompany]:
    query = db.query(OrganizationCompany).filter(OrganizationCompany.realm_id == str(realm_id))
    if organization_id is not None:
        query = query.filter(OrganizationCompany.organization_id == organization_id)
    return query.first()


@contextlib.contextmanager
def bind_tenant_tag_for_realm(
    db: Session,
    realm_id: str,
    *,
    user_id: int | None = None,
) -> Iterator[TenantTagContext | None]:
    # PLAN-EXEMPT: pilot LLM-usage attribution (AC-4.1) — the company-grained bind seam.
    """Resolve a realm's owning org + OrganizationCompany and bind the LLM-usage tenant tag.

    The single attribution seam for background / lane LLM work (nightly intelligence,
    txn classification, the prepaid-sync AI path). A Celery task / system job naturally
    has only the realm (the QBO company id); this resolves the company-grained org +
    OrganizationCompany via the RLS-excluded organization tables and binds them on the
    tenant-tag ContextVar that ``LaneLLMService._record_usage`` reads — so a lane LLM call
    fired inside this block carries the correct org/company/realm (the PRIMARY,
    company-grained attribution the realm-only RLS GUC cannot supply).

    ``user_id`` is carried as a separate structured field (never folded into the tag
    hash); it is NULL on user-less system/background work and the live actor on a
    request-path call. When the realm has no org mapping this binds ``None`` (NULL
    attribution) — the ``_record_usage`` backstop then still backfills org+realm from
    the pinned RLS GUC.
    """
    organization = organization_for_company(db, realm_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=realm_id)
        if organization
        else None
    )
    with bind_tenant_tag(
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        qbo_realm_id=str(realm_id),
        user_id=user_id,
    ) as ctx:
        yield ctx


def bind_qbo_token_to_company_scope(
    db: Session,
    token: QBOToken,
    *,
    organization: Optional[Organization] = None,
    organization_company: Optional[OrganizationCompany] = None,
) -> tuple[Optional[Organization], Optional[OrganizationCompany]]:
    """Attach a QBO token row to its canonical OrganizationCompany when available."""
    if organization is None:
        organization = organization_for_company(db, token.realm_id)
    if organization and organization_company is None:
        organization_company = organization_company_for_realm(
            db,
            organization_id=organization.id,
            realm_id=token.realm_id,
        )
    if organization:
        token.organization_id = organization.id
    if organization_company:
        token.organization_company_id = organization_company.id
    return organization, organization_company


def assert_qbo_token_company_scope(
    db: Session,
    token: QBOToken,
    *,
    require_mapping: Optional[bool] = None,
) -> tuple[Optional[Organization], Optional[OrganizationCompany]]:
    """Validate a QBO token cannot point at a different org/entity than its realm mapping."""
    organization = organization_for_company(db, token.realm_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=token.realm_id)
        if organization
        else None
    )
    if token.organization_id is not None and organization and token.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="QBO token organization scope mismatch")
    if (
        token.organization_company_id is not None
        and organization_company
        and token.organization_company_id != organization_company.id
    ):
        raise HTTPException(status_code=403, detail="QBO token entity scope mismatch")

    if require_mapping is None:
        require_mapping = not is_dev_environment(settings.environment)
    if require_mapping and not organization:
        raise HTTPException(status_code=403, detail="QBO token is not mapped to an organization")

    bind_qbo_token_to_company_scope(
        db,
        token,
        organization=organization,
        organization_company=organization_company,
    )
    return organization, organization_company


def assert_company_belongs_to_organization(
    db: Session,
    *,
    organization: Organization | int,
    realm_id: str,
) -> OrganizationCompany:
    organization_id = organization if isinstance(organization, int) else organization.id
    company = organization_company_for_realm(db, organization_id=organization_id, realm_id=realm_id)
    if company:
        return company
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if org and org.primary_realm_id == str(realm_id):
        company = OrganizationCompany(organization_id=org.id, realm_id=str(realm_id), is_primary=True)
        db.add(company)
        db.flush()
        return company
    raise PermissionError("Company does not belong to organization")


def company_is_suspended(
    db: Session,
    *,
    organization_company: Optional[OrganizationCompany],
) -> bool:
    """True when a single company has been shut off (manual suspend or unpaid billing).

    Scoped to one OrganizationCompany so suspending one entity never affects the
    tenant's other companies. Platform admins are expected to bypass this check at
    the call site (they manage billing/shutoff and must retain visibility).
    """
    if organization_company is None:
        return False
    if (organization_company.status or "active").lower() == "suspended":
        return True
    entity_billing = (
        db.query(EntityBilling)
        .filter(EntityBilling.organization_company_id == organization_company.id)
        .first()
    )
    if entity_billing and (entity_billing.subscription_status or "").lower() in COMPANY_SUSPENDED_BILLING_STATUSES:
        return True
    return False


def granted_org_company_ids(
    db: Session,
    user: User,
    organization: Organization | int,
) -> set[int] | None:
    """Per-company access grants for ``user`` within ``organization``.

    Returns ``None`` when the member has ZERO grant rows in that org — the
    backward-compatible "unrestricted" default (access to ALL of the org's
    companies; see ``UserCompanyAccess`` docstring). Returns a ``set[int]`` of
    ``organization_company_id`` when the member is restricted to exactly those
    companies (>=1 grant row).

    SECURITY-CRITICAL fail-closed contract: on a DB/query error this RAISES — it
    NEVER returns ``None`` (all-access) for a member who may HAVE grants. A
    swallowed error here would silently widen a restricted member to every
    company in the org — the exact least-privilege bug this module exists to
    prevent. The caller (``accessible_company_ids_for_user`` /
    ``_check_company_access``) must let the raise propagate to a 5xx, not degrade
    to open access.
    """
    organization_id = organization if isinstance(organization, int) else organization.id
    # READ-ORDER FIX (AC-7 P0 fail-open): user_company_access is org-keyed FORCE-RLS, and
    # this grant read can run BEFORE the request pins app.current_org_id (the gate in
    # _check_company_access reads grants before its own pin). As the non-superuser app_rls
    # role, an unpinned org-keyed read fail-closes to ZERO rows → the helper would return
    # None (unrestricted) → a restricted member silently widens to all-access. Read the
    # user's OWN grants under a transient bypass: the user_id + organization_id WHERE clause
    # IS the authorization boundary; bypass only guarantees the rows are visible — mirroring
    # check_entity_tenant_access's transient-bypass safety lookup. Fail-closed contract holds
    # (a real DB error still raises out of the block; it never degrades to None/all-access).
    with tenant_scope_live(db, bypass=True):
        rows = (
            db.query(UserCompanyAccess.organization_company_id)
            .filter(
                UserCompanyAccess.user_id == user.id,
                UserCompanyAccess.organization_id == organization_id,
            )
            .all()
        )
    granted = {row.organization_company_id for row in rows if row.organization_company_id is not None}
    if not granted:
        return None
    return granted


def member_company_ids_for_organization(db: Session, user: User, organization: Organization) -> list[str]:
    """The realm ids of ``organization`` that ``user`` may open as a member.

    Every company of the org for an unrestricted member (zero grant rows), only the
    granted companies for a restricted one — the same answer _check_company_access
    gives per realm. Membership itself is the caller's responsibility. A grant read
    error propagates (fail-closed), exactly as in granted_org_company_ids.
    """
    realm_ids = company_ids_for_organization(db, organization)
    granted = granted_org_company_ids(db, user, organization)
    if granted is None:
        return realm_ids
    with org_grain_read(
        "per-member realm enumeration: maps the member's granted company ids in their "
        "own org to realm identifiers for the company switcher"
    ):
        granted_realms = {
            row.realm_id
            for row in (
                db.query(OrganizationCompany.realm_id)
                .filter(
                    OrganizationCompany.organization_id == organization.id,
                    OrganizationCompany.id.in_(sorted(granted)),
                )
                .all()
            )
            if row.realm_id
        }
    return [realm_id for realm_id in realm_ids if realm_id in granted_realms]


def accessible_company_ids_for_user(db: Session, user: User, *, include_all_if_unbound_admin: bool = False) -> set[str]:
    if include_all_if_unbound_admin:
        realm_ids = {row.realm_id for row in db.query(QBOToken.realm_id).all() if row.realm_id}
    else:
        realm_ids = set()

    # Only ACTIVE memberships enumerate: a removed ("deleted"), never-activated
    # ("invited") or merged-away ("transferred") member must not keep listing the
    # org's companies, realm ids and connection state after the data routes close.
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

    # Resolve this user's per-company grants across ALL their orgs in ONE query
    # (no per-org N+1). Maps organization_id -> {granted realm_id}; an org absent
    # from the map = the member has zero grants there = unrestricted (the
    # backward-compatible default). Grants are joined to OrganizationCompany so a
    # grant resolves to a realm string WITHIN its own org and can never reach
    # across orgs. SECURITY-CRITICAL: a DB error propagates (fail-closed) — it
    # never degrades a restricted member to all-access.
    granted_realms_by_org: dict[int, set[str]] = {}
    # READ-ORDER FIX (AC-7 P0 fail-open): same hazard as granted_org_company_ids — this
    # join reads the org-keyed FORCE-RLS user_company_access table, and this function is
    # called from paths that have not pinned app.current_org_id (e.g. /lbo/companies). An
    # unpinned org-keyed read fail-closes to ZERO grant rows as the app_rls role → every
    # restricted member reads as unrestricted. Read the user's OWN grants under a transient
    # bypass (the user_id filter is the boundary; the per-org mapping below only applies a
    # grant to an org the user is a member of, so bypass cannot widen across tenants).
    with tenant_scope_live(db, bypass=True):
        grant_rows = (
            db.query(UserCompanyAccess.organization_id, OrganizationCompany.realm_id)
            .outerjoin(
                OrganizationCompany,
                (OrganizationCompany.id == UserCompanyAccess.organization_company_id)
                & (OrganizationCompany.organization_id == UserCompanyAccess.organization_id),
            )
            .filter(UserCompanyAccess.user_id == user.id)
            .all()
        )
    for row in grant_rows:
        # Every own grant keeps its organization restricted, including a
        # neutral company or malformed cross-org legacy reference. The outer
        # join cannot turn an unresolved realm into unrestricted legacy access.
        granted_realms_by_org.setdefault(row.organization_id, set())
        if row.realm_id:
            granted_realms_by_org[row.organization_id].add(row.realm_id)

    for org in orgs:
        org_realm_ids = set(company_ids_for_organization(db, org))
        if org.id in granted_realms_by_org:
            # Restricted member (>=1 grant in this org): intersect this org's
            # realms down to exactly the granted companies.
            org_realm_ids &= granted_realms_by_org[org.id]
        realm_ids.update(org_realm_ids)

    return realm_ids


class ForeignCompanyError(ValueError):
    """Raised when a grant references an organization_company_id outside the org."""


def replace_member_company_access(
    db: Session,
    *,
    organization: Organization,
    actor: User,
    target_user_id: int,
    mode: str,
    organization_company_ids: Iterable[int],
) -> set[int]:
    """REPLACE a member's per-company access grants within one organization.

    ``mode`` = "all" → delete every grant row (unrestricted). ``mode`` =
    "selected" → replace the grant set with EXACTLY the given
    ``organization_company_ids``, each VALIDATED to belong to ``organization`` (a
    foreign-org id raises ``ForeignCompanyError``). Emits an AuthAuditEvent via
    record_auth_event in the SAME transaction (event types
    company_access_granted / company_access_revoked / company_access_set_all).

    Returns the resulting granted set. The caller commits (router owns the
    transaction boundary), so the audit row and the grant rows commit atomically.
    Membership of ``target_user_id`` in ``organization`` is validated by the
    caller before this runs.
    """
    from .auth_migration import record_auth_event  # local import avoids a cycle

    selected_ids: set[int] = set()
    if mode == "selected":
        requested = {int(cid) for cid in organization_company_ids}
        if requested:
            owned_rows = (
                db.query(OrganizationCompany.id)
                .filter(
                    OrganizationCompany.organization_id == organization.id,
                    OrganizationCompany.id.in_(requested),
                )
                .all()
            )
            owned = {row.id for row in owned_rows}
            foreign = requested - owned
            if foreign:
                raise ForeignCompanyError(
                    f"organization_company_id(s) not in organization {organization.id}: "
                    f"{sorted(foreign)}"
                )
            selected_ids = owned

    db.query(UserCompanyAccess).filter(
        UserCompanyAccess.organization_id == organization.id,
        UserCompanyAccess.user_id == target_user_id,
    ).delete(synchronize_session=False)
    db.add_all(
        [
            UserCompanyAccess(
                organization_id=organization.id,
                user_id=target_user_id,
                organization_company_id=org_company_id,
                granted_by=actor.id,
            )
            for org_company_id in sorted(selected_ids)
        ]
    )

    if mode == "all":
        event_type = "company_access_set_all"
    elif selected_ids:
        event_type = "company_access_granted"
    else:
        event_type = "company_access_revoked"
    record_auth_event(
        db,
        event_type=event_type,
        user=actor,
        organization=organization,
        details={
            "target_user_id": target_user_id,
            "mode": mode,
            "organization_company_ids": sorted(selected_ids),
        },
    )
    return selected_ids


# PLAN-EXEMPT: plan 20260623-p0-hotfix-residual is genuinely ratified on disk
# (RATIFICATION-2026-06-23.md, Steven keyboard turn "ratify all three"); only the
# active-plan.json ratified_at MIRROR field is blank and editing governance state
# self-ratifies, so the audited doorway is the correct relief for this scoped write.
def rescope_realm_org_keyed_rows(
    db: Session,
    *,
    realm_id: str,
    target_organization_id: int,
) -> None:
    """Re-scope EVERY org-keyed, realm-bound row for one connected company.

    Walks the shared ``MODEL_SCOPES`` registry — the SAME definition export and
    deletion use — and bulk-repoints each realm-keyed model's ``organization_id``
    (and ``EntityBilling`` by ``organization_company_id``) to ``target_organization_id``.

    Filtering by ``realm_id`` (globally unique to one connected company) — not by
    the old ``organization_id`` — both moves every row for this company AND heals
    any row whose ``organization_id`` was left stale/NULL by a prior column-only
    move. ``OrganizationCompany`` itself is repointed by the caller (its id is
    stable, so ``organization_company_id`` FKs stay valid).

    One bulk UPDATE per model (no per-row N+1). The caller commits.
    """
    from .tenant_data_scope import MODEL_SCOPES  # local import avoids a cycle

    realm_id = str(realm_id).strip()
    for spec in MODEL_SCOPES:
        if not spec.org_id_attr or not spec.realm_attrs:
            continue  # need both an org column to repoint AND a realm column to filter
        if spec.model is OrganizationCompany:
            continue  # the caller repoints the OrganizationCompany row itself
        model = spec.model
        org_col = getattr(model, spec.org_id_attr)
        realm_conditions = [getattr(model, attr) == realm_id for attr in spec.realm_attrs]
        # SA-OK: bounded-loop over the fixed MODEL_SCOPES registry (~90 models, NOT
        # a per-row result set); each iteration is ONE bulk UPDATE — same shape as
        # the HEAD reassign loop this extracts. Heterogeneous models/columns cannot
        # be batched into a single statement.
        db.query(model).filter(or_(*realm_conditions)).update(
            {org_col: target_organization_id},
            synchronize_session=False,
        )

    # EntityBilling is keyed on organization_company_id; repoint it explicitly by
    # the OrganizationCompany for this realm (belt-and-suspenders alongside the
    # realm-based pass above, which already covers its realm_id column).
    company = (
        db.query(OrganizationCompany)
        .filter(OrganizationCompany.realm_id == realm_id)
        .first()
    )
    if company is not None:
        db.query(EntityBilling).filter(
            EntityBilling.organization_company_id == company.id
        ).update({EntityBilling.organization_id: target_organization_id}, synchronize_session=False)
        # Per-company grants belong to the SOURCE org's members and mean nothing in
        # the target org. Left behind, they keep pointing at the moved company from
        # the source org (and block deleting it), so they are dropped, not repointed.
        db.query(UserCompanyAccess).filter(
            UserCompanyAccess.organization_company_id == company.id,
            UserCompanyAccess.organization_id != target_organization_id,
        ).delete(synchronize_session=False)


def set_organization_companies(
    db: Session,
    *,
    organization: Organization,
    realm_ids: Iterable[str],
    primary_realm_id: str | None = None,
) -> list[OrganizationCompany]:
    normalized = [str(realm_id).strip() for realm_id in realm_ids if str(realm_id).strip()]
    deduped = list(dict.fromkeys(normalized))
    if primary_realm_id:
        primary_realm_id = str(primary_realm_id).strip()
    elif organization.primary_realm_id in deduped:
        primary_realm_id = organization.primary_realm_id
    elif deduped:
        primary_realm_id = deduped[0]

    if primary_realm_id and primary_realm_id not in deduped:
        deduped.insert(0, primary_realm_id)

    existing_tokens = {
        row.realm_id
        for row in db.query(QBOToken.realm_id).filter(QBOToken.realm_id.in_(deduped or [""])).all()
    }
    missing = [realm_id for realm_id in deduped if realm_id not in existing_tokens]
    if missing:
        raise ValueError(f"Unknown QBO realm_id(s): {', '.join(missing)}")

    # Detect which realms are a MOVE (currently owned by a DIFFERENT org) BEFORE we
    # repoint OrganizationCompany below — afterwards the prior owner is unknowable.
    # An initial assignment (no prior OrganizationCompany, or already owned by the
    # target org) is NOT a move and re-scopes nothing extra.
    moved_realms = [
        row.realm_id
        for row in db.query(OrganizationCompany.realm_id)
        .filter(
            OrganizationCompany.realm_id.in_(deduped or [""]),
            OrganizationCompany.organization_id != organization.id,
        )
        .all()
        if row.realm_id
    ]

    db.query(OrganizationCompany).filter(
        OrganizationCompany.organization_id == organization.id,
        OrganizationCompany.realm_id.notin_(deduped or [""]),
    ).delete(synchronize_session=False)

    if deduped:
        db.query(OrganizationCompany).filter(OrganizationCompany.realm_id.in_(deduped)).update(
            {OrganizationCompany.organization_id: organization.id, OrganizationCompany.is_primary: False},
            synchronize_session=False,
        )
        db.query(Organization).filter(
            Organization.id != organization.id,
            Organization.primary_realm_id.in_(deduped),
        ).update({Organization.primary_realm_id: None}, synchronize_session=False)

    companies: list[OrganizationCompany] = []
    for realm_id in deduped:
        company = db.query(OrganizationCompany).filter(OrganizationCompany.realm_id == realm_id).first()
        if not company:
            company = OrganizationCompany(organization_id=organization.id, realm_id=realm_id)
            db.add(company)
        company.organization_id = organization.id
        company.is_primary = realm_id == primary_realm_id
        companies.append(company)

    organization.primary_realm_id = primary_realm_id

    # Move-path lock: for every realm that MOVED here from another org, re-scope all
    # of its org-keyed, realm-bound rows (Document, CompanyMemory, QBOReport, …) to
    # this org. Without this, set_organization_companies repointed ONLY the
    # OrganizationCompany row and orphaned every other tenant row at the old org —
    # a cross-tenant isolation break the live RLS backstop then hides as
    # zero-rows-for-the-new-owner. reassign_company_to_organization shares this helper.
    for realm_id in moved_realms:
        rescope_realm_org_keyed_rows(db, realm_id=realm_id, target_organization_id=organization.id)

    return companies


def reassign_company_to_organization(
    db: Session,
    *,
    realm_id: str,
    target_organization: Organization,
) -> tuple[OrganizationCompany, int]:
    """Move ONE connected company (realm) from its current tenant to another.

    Re-scopes EVERY org-keyed row for this realm to the target org, using the
    SAME ``MODEL_SCOPES`` registry that export/deletion use, so the tenant
    isolation invariant holds afterwards. Unlike ``set_organization_companies``
    (which only repoints ``OrganizationCompany.organization_id``), this also
    moves ``QBOToken``, ``EntityBilling``, and all realm-keyed financial / chat /
    agent / usage rows.

    Re-scoping is filtered by ``realm_id`` (globally unique to one connected
    company), not by the old ``organization_id`` — so it also HEALS any rows
    whose ``organization_id`` was left stale/NULL by a prior sloppy reassign.

    Returns ``(company, source_organization_id)``. The target/Stripe guardrails
    and audit live in ``reassign_company_guarded``, which every caller outside
    this module goes through. Caller commits.
    """
    # PLAN-EXEMPT: ratified on disk (RATIFICATION-2026-06-23.md, Steven "ratify all
    # three"); only active-plan.json ratified_at mirror is blank + editing governance
    # state self-ratifies, so the audited doorway is the correct relief here.
    realm_id = str(realm_id).strip()
    company = (
        db.query(OrganizationCompany)
        .filter(OrganizationCompany.realm_id == realm_id)
        .first()
    )
    if not company:
        raise ValueError(f"No connected company for realm_id {realm_id}")
    source_org_id = company.organization_id
    if source_org_id == target_organization.id:
        return company, source_org_id  # no-op

    # 1. Move the OrganizationCompany row itself (its id is stable, so every
    #    organization_company_id FK pointing at it stays valid).
    company.organization_id = target_organization.id
    company.is_primary = False

    # 2. Re-scope every org-keyed, realm-bound row via the shared helper (the SAME
    #    MODEL_SCOPES re-scope set_organization_companies' move-path now uses).
    #    Filtering by realm repoints every row for this company AND heals any
    #    stale/NULL organization_id, plus the EntityBilling org_company-keyed pass.
    rescope_realm_org_keyed_rows(db, realm_id=realm_id, target_organization_id=target_organization.id)

    # 4. Fix primary_realm_id on the SOURCE org if this realm was its primary.
    source_org = db.query(Organization).filter(Organization.id == source_org_id).first()
    if source_org and source_org.primary_realm_id == realm_id:
        remaining = (
            db.query(OrganizationCompany.realm_id)
            .filter(
                OrganizationCompany.organization_id == source_org_id,
                OrganizationCompany.realm_id != realm_id,
            )
            .order_by(OrganizationCompany.is_primary.desc(), OrganizationCompany.realm_id.asc())
            .first()
        )
        source_org.primary_realm_id = remaining.realm_id if remaining else None

    # 5. Set target primary if it has none.
    if not target_organization.primary_realm_id:
        target_organization.primary_realm_id = realm_id
        company.is_primary = True

    db.flush()
    return company, source_org_id


class CompanyReassignRefused(Exception):
    """A reassign guardrail refused the move; ``status_code`` is its HTTP mapping."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class CompanyReassignment:
    company: OrganizationCompany
    source_organization_id: int
    target_organization: Organization
    stripe_reparent_required: bool


def reassign_company_guarded(
    db: Session,
    *,
    realm_id: str,
    target_organization_id: int,
    force_billing: bool,
    actor_user_id: Optional[int],
    actor_email: Optional[str],
    reason: Optional[str] = None,
) -> CompanyReassignment:
    """The ONE guarded reassign path: POST /admin/companies/reassign and
    scripts/reassign_company.py both call it.

    Refuses (``CompanyReassignRefused``) an unknown/inactive target, an unknown
    realm, a no-op move, and a company with a live Stripe subscription unless
    ``force_billing`` acknowledges it; then re-scopes every row through
    ``reassign_company_to_organization`` and audits on BOTH orgs. The move spans
    two tenants, which no single org pin can cover under FORCE RLS, so every read
    and write here runs under the audited bypass — an unscoped session would read
    no billing row (skipping the Stripe guard) and repoint nothing but the
    RLS-exempt OrganizationCompany. Flushes; the caller commits.
    """
    with tenant_scope_live(db, bypass=True):
        target = db.query(Organization).filter(Organization.id == target_organization_id).first()
        if not target:
            raise CompanyReassignRefused(404, "Target tenant not found")
        if not target.is_active:
            raise CompanyReassignRefused(400, "Target tenant is not active")

        realm = str(realm_id).strip()
        company = db.query(OrganizationCompany).filter(OrganizationCompany.realm_id == realm).first()
        if not company:
            raise CompanyReassignRefused(404, "Company not found")
        if company.organization_id == target.id:
            raise CompanyReassignRefused(400, "Company already belongs to the target tenant")

        billing = (
            db.query(EntityBilling)
            .filter(EntityBilling.organization_company_id == company.id)
            .first()
        )
        stripe_reparent_required = bool(billing and billing.stripe_subscription_id)
        if stripe_reparent_required and not force_billing:
            raise CompanyReassignRefused(
                409,
                "This company has a live Stripe subscription. Re-parent or cancel it in "
                "Stripe first, then retry with force_billing=true to move the local billing record.",
            )

        company, source_org_id = reassign_company_to_organization(
            db, realm_id=realm, target_organization=target
        )
        for org_id in {source_org_id, target.id}:
            db.add(
                AuthAuditEvent(
                    user_id=actor_user_id,
                    organization_id=org_id,
                    event_type="company_reassigned",
                    event_status="success",
                    actor_email=actor_email,
                    details={
                        "realm_id": company.realm_id,
                        "source_organization_id": source_org_id,
                        "target_organization_id": target.id,
                        "reason": reason,
                        "stripe_reparent_required": stripe_reparent_required,
                    },
                )
            )
        db.flush()
    return CompanyReassignment(
        company=company,
        source_organization_id=source_org_id,
        target_organization=target,
        stripe_reparent_required=stripe_reparent_required,
    )

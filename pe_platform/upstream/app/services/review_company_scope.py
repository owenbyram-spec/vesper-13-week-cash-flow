"""Server-authorized numeric company/book scope for monthly review.

Selectors identify requested resources; they never establish authority. This
resolver deliberately has no legacy/default-org or platform-admin fallback.
It does not connect to an accounting provider or approve a financial action.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..auth_session import AuthContext
from ..models import (
    AccountingBook,
    AccountingSourceConnection,
    Organization,
    OrganizationCompany,
    OrganizationMembership,
    User,
    UserCompanyAccess,
)
from ..roles import role_permissions, role_weight
from ..tenant_rls import pin_tenant_scope_live, tenant_scope_live
from .legal_gate import assent_gate
from .tenant_companies import company_is_suspended


@dataclass(frozen=True)
class ReviewCompanyContext:
    """Resolved server context; a provider ID is never a company grant."""

    user: User
    organization: Organization
    membership: OrganizationMembership
    company: OrganizationCompany
    connection: AccountingSourceConnection
    book: AccountingBook
    organization_id: int
    company_id: int
    connection_id: int
    book_id: int
    role: str
    permissions: tuple[str, ...]
    qbo_realm_id: str | None

    @property
    def organization_company_id(self) -> int:
        return self.company_id


def _positive_selector(value: object, name: str) -> int:
    # bool is an int subclass; coercion would turn untrusted values into IDs.
    if type(value) is not int or value <= 0:
        raise HTTPException(status_code=422, detail=f"{name} must be a positive integer")
    return value


def _identity(value: object) -> bool:
    return type(value) is int and value > 0


def _not_found() -> HTTPException:
    # Do not distinguish foreign/ungranted/missing companies or books.
    return HTTPException(status_code=404, detail="Resource not found")


def resolve_review_company_scope(
    db: Session,
    *,
    auth: AuthContext,
    company_id: int,
    book_id: int,
    minimum_role: str = "VIEWER",
) -> ReviewCompanyContext:
    """Revalidate one authenticated membership, then pin one company and book.

    ``auth`` comes only from the server's authentication dependency. Fresh
    database rows decide org lifecycle, membership, role and grants; cached auth
    objects are identity locators only. Query failures propagate, never becoming
    zero grants or a successful assent. The caller must supply the current
    authenticated organization explicitly through that context.

    The pure resolver is independent of the rollout flag; HTTP entry points
    separately require the default-off monthly-review flag. It grants read/
    review scope only, with no ERP posting or close/approval authority.
    """
    # A failed resolution must not leave a previous worker/request company or
    # legacy realm/bypass active on an already-open transaction.
    pin_tenant_scope_live(db, org_id=None, realm_id=None, company_id=None, bypass=False)
    company_id = _positive_selector(company_id, "company_id")
    book_id = _positive_selector(book_id, "book_id")
    required_weight = role_weight(minimum_role)
    if required_weight == 0:
        raise ValueError("Unknown server-configured minimum review role")

    if auth.organization is None or auth.membership is None or auth.user is None:
        raise HTTPException(status_code=403, detail="An authenticated organization membership is required")
    org_id, user_id, membership_id = auth.organization.id, auth.user.id, auth.membership.id
    authenticated_stytch_org_id = auth.organization.stytch_organization_id
    if not all(_identity(value) for value in (org_id, user_id, membership_id)):
        raise HTTPException(status_code=403, detail="An authenticated organization membership is required")
    if auth.membership.organization_id != org_id or auth.membership.user_id != user_id:
        raise HTTPException(status_code=403, detail="Authenticated membership identity is inconsistent")

    user = db.query(User).filter(User.id == user_id).populate_existing().one_or_none()
    org = db.query(Organization).filter(Organization.id == org_id).populate_existing().one_or_none()
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.id == membership_id,
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
        .populate_existing()
        .one_or_none()
    )
    if user is None or org is None or membership is None:
        raise HTTPException(status_code=403, detail="Your organization membership is not active")
    if org.stytch_organization_id != authenticated_stytch_org_id:
        raise HTTPException(status_code=403, detail="Authenticated organization identity changed")
    if org.is_active is not True:
        raise HTTPException(status_code=403, detail="This tenant is suspended")
    if org.status != "active":
        raise HTTPException(status_code=403, detail="Accept the current Terms of Service to access this organization")
    if membership.status != "active":
        raise HTTPException(status_code=403, detail="Your organization membership is not active")
    if role_weight(membership.role) < required_weight:
        raise HTTPException(status_code=403, detail="Minimum review role required")

    with tenant_scope_live(db, org_id=org_id, realm_id=None, company_id=None, bypass=False):
        assent = assent_gate(db, org.stytch_organization_id)
        if not isinstance(assent, dict) or assent.get("ok") is not True:
            raise HTTPException(status_code=403, detail="Current organization assent is required")
        company = (
            db.query(OrganizationCompany)
            .filter(OrganizationCompany.id == company_id, OrganizationCompany.organization_id == org_id)
            .populate_existing()
            .one_or_none()
        )
        if company is None:
            raise _not_found()
        # Grant presence is independent of whether a company has a QBO realm.
        # The exact authenticated org pin makes these own-grant rows visible
        # without the legacy helper's transient cross-tenant bypass.
        grants = (
            db.query(UserCompanyAccess.organization_company_id)
            .filter(UserCompanyAccess.organization_id == org_id, UserCompanyAccess.user_id == user_id)
            .all()
        )
        if grants and company_id not in {row.organization_company_id for row in grants}:
            raise _not_found()
        if company.status != "active" or company_is_suspended(db, organization_company=company):
            raise HTTPException(status_code=403, detail="This company is suspended or unavailable")

    with tenant_scope_live(db, org_id=org_id, realm_id=None, company_id=company_id, bypass=False):
        book = (
            db.query(AccountingBook)
            .filter(
                AccountingBook.id == book_id,
                AccountingBook.organization_id == org_id,
                AccountingBook.organization_company_id == company_id,
            )
            .populate_existing()
            .one_or_none()
        )
        if book is None:
            raise _not_found()
        connection = (
            db.query(AccountingSourceConnection)
            .filter(
                AccountingSourceConnection.id == book.connection_id,
                AccountingSourceConnection.organization_id == org_id,
                AccountingSourceConnection.organization_company_id == company_id,
            )
            .populate_existing()
            .one_or_none()
        )
        if connection is None:
            raise _not_found()
        if book.status != "active" or connection.status != "active":
            raise HTTPException(status_code=409, detail="The selected review source or book is unavailable")
        # W1 binds identity only. These values are not a claim that provider
        # capability, source completeness or live access has been qualified.
        if connection.provider not in {"qbo", "xero", "netsuite", "sage_intacct"}:
            raise HTTPException(status_code=409, detail="The selected review source is unsupported")
        qbo_realm_id = None
        if connection.provider == "qbo":
            if not company.realm_id or connection.external_entity_id != company.realm_id:
                raise HTTPException(status_code=409, detail="The QBO review source identity does not match the company")
            qbo_realm_id = company.realm_id

    # Never copy a provider entity ID into app.current_realm_id. Legacy QBO
    # routes retain their independent existing realm authorization ladder.
    pin_tenant_scope_live(db, org_id=org_id, realm_id=None, company_id=company_id, bypass=False)
    return ReviewCompanyContext(
        user=user,
        organization=org,
        membership=membership,
        company=company,
        connection=connection,
        book=book,
        organization_id=org_id,
        company_id=company_id,
        connection_id=connection.id,
        book_id=book.id,
        role=membership.role,
        permissions=tuple(role_permissions(membership.role)),
        qbo_realm_id=qbo_realm_id,
    )


@dataclass(frozen=True)
class SourceManagementContext:
    """Current authenticated company management scope, including first setup."""

    user: User
    organization: Organization
    membership: OrganizationMembership
    company: OrganizationCompany
    organization_id: int
    company_id: int
    role: str
    permissions: tuple[str, ...]

    @property
    def organization_company_id(self) -> int:
        return self.company_id


def resolve_source_management_scope(
    db: Session, *, auth: AuthContext, company_id: int,
    minimum_role: str = "TENANT_ADMIN",
) -> SourceManagementContext:
    """Revalidate company authority without requiring an existing active book.

    No realm, default-organization or platform-staff fallback is permitted.
    Current membership, assent, company state and grants decide authority.
    """
    # A failed resolution must not leave a previous worker/request company or
    # legacy realm/bypass active on an already-open transaction.
    pin_tenant_scope_live(db, org_id=None, realm_id=None, company_id=None, bypass=False)
    company_id = _positive_selector(company_id, "company_id")
    required_weight = role_weight(minimum_role)
    if required_weight < role_weight("TENANT_ADMIN"):
        raise ValueError("Source management requires at least TENANT_ADMIN")

    if auth.organization is None or auth.membership is None or auth.user is None:
        raise HTTPException(status_code=403, detail="An authenticated organization membership is required")
    org_id, user_id, membership_id = auth.organization.id, auth.user.id, auth.membership.id
    authenticated_stytch_org_id = auth.organization.stytch_organization_id
    if not all(_identity(value) for value in (org_id, user_id, membership_id)):
        raise HTTPException(status_code=403, detail="An authenticated organization membership is required")
    if auth.membership.organization_id != org_id or auth.membership.user_id != user_id:
        raise HTTPException(status_code=403, detail="Authenticated membership identity is inconsistent")

    user = db.query(User).filter(User.id == user_id).populate_existing().one_or_none()
    org = db.query(Organization).filter(Organization.id == org_id).populate_existing().one_or_none()
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.id == membership_id,
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
        .populate_existing()
        .one_or_none()
    )
    if user is None or org is None or membership is None:
        raise HTTPException(status_code=403, detail="Your organization membership is not active")
    if org.stytch_organization_id != authenticated_stytch_org_id:
        raise HTTPException(status_code=403, detail="Authenticated organization identity changed")
    if org.is_active is not True:
        raise HTTPException(status_code=403, detail="This tenant is suspended")
    if org.status != "active":
        raise HTTPException(status_code=403, detail="Accept the current Terms of Service to access this organization")
    if membership.status != "active":
        raise HTTPException(status_code=403, detail="Your organization membership is not active")
    if role_weight(membership.role) < required_weight:
        raise HTTPException(status_code=403, detail="Minimum review role required")

    with tenant_scope_live(db, org_id=org_id, realm_id=None, company_id=None, bypass=False):
        assent = assent_gate(db, org.stytch_organization_id)
        if not isinstance(assent, dict) or assent.get("ok") is not True:
            raise HTTPException(status_code=403, detail="Current organization assent is required")
        company = (
            db.query(OrganizationCompany)
            .filter(OrganizationCompany.id == company_id, OrganizationCompany.organization_id == org_id)
            .populate_existing()
            .one_or_none()
        )
        if company is None:
            raise _not_found()
        # Grant presence is independent of whether a company has a QBO realm.
        # The exact authenticated org pin makes these own-grant rows visible
        # without the legacy helper's transient cross-tenant bypass.
        grants = (
            db.query(UserCompanyAccess.organization_company_id)
            .filter(UserCompanyAccess.organization_id == org_id, UserCompanyAccess.user_id == user_id)
            .all()
        )
        if grants and company_id not in {row.organization_company_id for row in grants}:
            raise _not_found()
        if company.status != "active" or company_is_suspended(db, organization_company=company):
            raise HTTPException(status_code=403, detail="This company is suspended or unavailable")

    pin_tenant_scope_live(db, org_id=org_id, realm_id=None, company_id=company_id, bypass=False)
    return SourceManagementContext(
        user=user, organization=org, membership=membership, company=company,
        organization_id=org_id, company_id=company_id, role=membership.role,
        permissions=tuple(role_permissions(membership.role)),
    )

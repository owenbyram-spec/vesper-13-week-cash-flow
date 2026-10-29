"""Server admission and connection lifecycle for the default-off source lane.

Broker selectors locate a retained record; only an opaque permit and current
server authorization let that record be used. No function exchanges OAuth codes
or accepts caller-provided credentials. Public schemas are not grant evidence.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..auth_session import AuthContext
from ..models import (
    AccountingBook, AccountingSourceConnection, AccountingSourceHead,
    AccountingSourceInvalidation, AccountingSourceOAuthState,
    AccountingSourceOutbox, AccountingSourceRun, Organization,
    OrganizationCompany, OrganizationMembership, User,
    AccountingSourcePage, AccountingSourceRecord, AccountingSourceSnapshot,
    AccountingSourceCoverageReceipt, AccountingSourceControlBinding, AccountingSourceEvent,
)
from ..services.crypto import decrypt_secret, encrypt_secret, is_encrypted
from ..services.review_company_scope import (
    resolve_review_company_scope, resolve_source_management_scope,
)
from ..tenant_rls import get_tenant_scope, tenant_scope_live
from .contracts import SourceScope, canonical_data, canonical_digest, canonical_json

ADMISSION_ISSUER = "source-admission:v1"
ALLOWED_DATASETS = frozenset({"gl", "accounts", "ap", "ar", "credits", "payments", "budget"})
PROVIDERS = frozenset({"qbo", "xero", "netsuite", "sage_intacct"})
RUN_SECONDS = 600
MAX_HISTORY_MONTHS = 12


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    # SQLite drops the timezone marker; every persisted application instant is UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def canonical(value: Any) -> str:
    return canonical_json(value)


def digest(value: Any) -> str:
    return canonical_digest(value)


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def positive_id(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise HTTPException(422, f"{name} must be a positive integer")
    return value


def _conflict(message: str) -> HTTPException:
    return HTTPException(409, message)


def scope_json(scope: SourceScope) -> dict[str, Any]:
    result = asdict(scope)
    result["period_start"] = scope.period_start.isoformat()
    result["period_end"] = scope.period_end.isoformat()
    return result


def source_scope_from_json(value: dict[str, Any]) -> SourceScope:
    required = {
        "organization_id", "company_id", "connection_id", "book_id", "provider",
        "environment", "external_account_id", "external_entity_id", "external_book_id",
        "basis", "currency", "period_start", "period_end", "review_period",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise _conflict("The retained source scope contract is invalid")
    for key in ("organization_id", "company_id", "connection_id", "book_id"):
        positive_id(value[key], key)
    values = dict(value)
    try:
        values["period_start"] = date.fromisoformat(value["period_start"])
        values["period_end"] = date.fromisoformat(value["period_end"])
        return SourceScope(**values)
    except (TypeError, ValueError) as exc:
        raise _conflict("The retained source scope contract is invalid") from exc


def _period_window(period: str, now: datetime) -> tuple[date, date]:
    if not isinstance(period, str) or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period) is None:
        raise HTTPException(422, "review_period must be YYYY-MM")
    year, month = (int(part) for part in period.split("-"))
    # An admitted single month can address the current month or the preceding
    # twelve calendar months. No approximate thirty-day month arithmetic.
    delta = (now.year - year) * 12 + now.month - month
    if not 0 <= delta <= MAX_HISTORY_MONTHS:
        raise HTTPException(422, "Requested period is outside the server review history")
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _policy(dataset: str) -> dict[str, Any]:
    # This is the server-owned source policy, never a request-supplied policy.
    # Provider-specific capability gaps still prevent a qualified publication.
    return {
        "version": "source-policy:v1", "dataset": dataset,
        "partition_seconds": RUN_SECONDS, "physical_requests": 1000,
        "pages": 1000, "rows": 100000, "response_bytes": 8 * 1024 * 1024,
        "retained_bytes": 64 * 1024 * 1024, "diagnostic_bytes": 512 * 1024,
        "partitions": 1, "maximum_partitions": 32, "attempts_per_operation": 3,
        "requires_independent_reports": True,
        "requires_verified_change_coverage": True,
        "requires_complete_window": True, "allow_financial_mutations": False,
        "optional_gaps": ["budget", "service_periods", "obligations"],
    }


def _run_query(db: Session, run_id: int):
    return source_query(db, AccountingSourceRun).filter(AccountingSourceRun.id == run_id)


SOURCE_QUERY_MODELS = frozenset({
    AccountingBook, AccountingSourceConnection, AccountingSourceRun, AccountingSourceHead,
    AccountingSourceOAuthState, AccountingSourceOutbox, AccountingSourceInvalidation,
    AccountingSourcePage, AccountingSourceRecord, AccountingSourceSnapshot,
    AccountingSourceCoverageReceipt, AccountingSourceControlBinding, AccountingSourceEvent,
})
MUTABLE_SOURCE_FIELDS = {
    AccountingSourceRun: frozenset({"status", "admission_json", "admission_digest"}),
    AccountingSourceHead: frozenset({"epoch", "revision", "current_snapshot_id", "lease_generation",
        "lease_run_id", "lease_expires_at", "checkpoint_json", "qualified_checkpoint_json"}),
    AccountingSourceOutbox: frozenset({"status", "available_at", "attempts", "delivered_at", "lease_expires_at"}),
    AccountingSourceOAuthState: frozenset({"consumed_at"}),
    AccountingSourceConnection: frozenset({"status", "credentials_encrypted", "configuration_encrypted"}),
}


def _current_source_owner() -> tuple[int, int]:
    scope = get_tenant_scope()
    if (scope is None or type(scope.org_id) is not int or scope.org_id <= 0
            or type(scope.company_id) is not int or scope.company_id <= 0
            or scope.realm_id is not None or scope.bypass):
        raise HTTPException(403, "An exact non-bypass source company scope is required")
    return scope.org_id, scope.company_id


def source_query(db: Session, model):
    """Explicit owner predicates satisfy the frozen neutral-company guard.

    This query ceiling is not admission: current request authorization or an
    opaque retained worker permit must still authenticate the operation.
    """
    if model not in SOURCE_QUERY_MODELS:
        raise ValueError("Unregistered source query model")
    org_id, company_id = _current_source_owner()
    return db.query(model).filter(model.organization_id == org_id,
                                  model.organization_company_id == company_id)


def update_source_row(db: Session, row, values: dict[str, Any]) -> None:
    """Update an approved mutable row with explicit owner predicates.

    ORM dirty writes emit only a primary-key predicate and are refused by the
    existing guard. Synchronize committed identity-map values without queuing a
    second dirty flush. Scope and identity columns can never be updated here.
    """
    model = type(row)
    allowed = MUTABLE_SOURCE_FIELDS.get(model)
    if allowed is None or type(values) is not dict or not values or set(values) - allowed:
        raise ValueError("Unsupported mutable source fields")
    org_id, company_id = _current_source_owner()
    if row.organization_id != org_id or row.organization_company_id != company_id:
        raise HTTPException(403, "Source row does not belong to the pinned company")
    positive_id(row.id, "source row id")
    changes = dict(values)
    if "updated_at" in model.__table__.c:
        changes["updated_at"] = utc_now()
    with db.no_autoflush:
        result = db.execute(update(model).where(
            model.id == row.id, model.organization_id == org_id,
            model.organization_company_id == company_id,
        ).values(**changes).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(409, "Source row update lost its exact owner")
    for field, value in changes.items():
        set_committed_value(row, field, value)


def mark_source_storage_initialized(db: Session, *, organization_id: int, company_id: int) -> None:
    """Retain the hold under FOR NO KEY UPDATE, compatible with child FK checks.

    This changes no company key. An exclusive FOR UPDATE here would deadlock
    with a publisher holding the connection and inserting a company-owned row.
    """
    if _current_source_owner() != (organization_id, company_id):
        raise HTTPException(403, "Source marker owner differs from the pinned company")
    # This legacy company table is intentionally unavailable to a neutral pin.
    # Use only the already authenticated organization, without realm or bypass,
    # then restore the exact numeric source pin on both success and failure.
    with tenant_scope_live(db, org_id=organization_id, company_id=None, realm_id=None, bypass=False):
        company = db.query(OrganizationCompany).filter(
            OrganizationCompany.id == company_id, OrganizationCompany.organization_id == organization_id,
        ).populate_existing().with_for_update(key_share=True).one_or_none()
        if company is None:
            raise HTTPException(404, "Resource not found")
        if company.review_storage_initialized_at is None:
            initialized_at = utc_now()
            result = db.execute(update(OrganizationCompany).where(
                OrganizationCompany.id == company_id, OrganizationCompany.organization_id == organization_id,
            ).values(review_storage_initialized_at=initialized_at).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise HTTPException(409, "Company marker update lost its exact owner")
            set_committed_value(company, "review_storage_initialized_at", initialized_at)


def configure_source_database_waits(db: Session, *, remaining_seconds: float) -> None:
    """Bound DB waits without raw SQL or widening the current company pin.

    Under a numeric pin, the two controls are evaluated only from an actual
    retained connection row. Its existence and exact owner predicates are useful
    checks; a scalar SELECT without FROM is intentionally refused by the guard.
    """
    import math
    if (isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, (int, float))
            or not math.isfinite(remaining_seconds) or not 0 < remaining_seconds <= RUN_SECONDS):
        raise HTTPException(403, "Source wait deadline exhausted or invalid")
    if db.get_bind().dialect.name != "postgresql":
        return
    bound = str(max(1, int(remaining_seconds * 1000))) + "ms"
    statement = select(func.set_config("lock_timeout", bound, True),
                       func.set_config("statement_timeout", bound, True))
    scope = get_tenant_scope()
    if scope is not None and scope.company_id is not None:
        if (type(scope.org_id) is not int or scope.org_id <= 0 or type(scope.company_id) is not int
                or scope.company_id <= 0 or scope.bypass or scope.realm_id is not None):
            raise HTTPException(403, "Exact company scope required for source waits")
        statement = statement.select_from(AccountingSourceConnection).where(
            AccountingSourceConnection.organization_company_id == scope.company_id,
            AccountingSourceConnection.organization_id == scope.org_id,
        ).limit(1)
    if db.execute(statement).one_or_none() is None:
        raise HTTPException(404, "Source company is unavailable")


def admit_source_waits(db: Session, expires_at: datetime) -> None:
    """Bound source DB waits by the retained original permit, never a new clock."""
    remaining = (aware(expires_at) - utc_now()).total_seconds()
    configure_source_database_waits(db, remaining_seconds=remaining)
    if aware(expires_at) <= utc_now():
        raise HTTPException(403, "Source dispatch deadline exhausted")


@dataclass(frozen=True)
class AdmittedRun:
    run_id: int
    outbox_id: int
    status: str
    expires_at: datetime


def admit_source_run(
    db: Session, *, auth: AuthContext, company_id: int, book_id: int,
    review_period: str, dataset: str, idempotency_key: str,
    _server_profile: str = "native-source/v1",
) -> AdmittedRun:
    """Persist admission and its sealed delivery together; caller commits once.

    The returned DTO contains no permit. A transaction rollback admits no work.
    A concurrent first admission is serialized by the existing connection lock.
    """
    if dataset not in ALLOWED_DATASETS:
        raise HTTPException(422, "Unsupported source dataset")
    if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 128:
        raise HTTPException(422, "An idempotency key of 16 to 128 characters is required")
    context = resolve_review_company_scope(
        db, auth=auth, company_id=company_id, book_id=book_id, minimum_role="TENANT_ADMIN",
    )
    mark_source_storage_initialized(db, organization_id=context.organization_id, company_id=context.company_id)
    now = utc_now()
    if _server_profile not in {"native-source/v1", "synthetic-source-replay/v1"}:
        raise _conflict("Unregistered server source profile")
    synthetic = _server_profile == "synthetic-source-replay/v1"
    if synthetic and context.connection.environment != "sandbox":
        raise _conflict("Synthetic source replay is restricted to sandbox identities")
    start, end = _period_window(review_period, now)
    scope = SourceScope(
        organization_id=context.organization_id, company_id=context.company_id,
        connection_id=context.connection_id, book_id=context.book_id,
        provider=context.connection.provider, environment=context.connection.environment,
        external_account_id=context.connection.external_account_id,
        external_entity_id=context.connection.external_entity_id,
        external_book_id=context.book.external_book_id or "",
        basis=context.book.accounting_basis, currency=context.book.currency,
        period_start=start, period_end=end, review_period=review_period,
    )
    from .snapshots import lock_current_source
    _, _, head = lock_current_source(db, scope, dataset, create_head=True,
                                     expires_at=now + timedelta(seconds=RUN_SECONDS))
    request = {"review_period": review_period, "dataset": dataset, "book_id": context.book_id}
    policy = _policy(dataset)
    operation = "synthetic_replay" if synthetic else "extract"
    if synthetic:
        from ..monthly_review.contracts import ReviewControls, canonical_value
        policy.update({
            "qualifier_contract": "synthetic-source-replay/v1", "required_datasets": [dataset],
            "qualification_max_age_seconds": 3600, "point_in_time_required": True,
            "review_controls": canonical_value(ReviewControls(
                policy_version="synthetic-source-policy/v1",
                account_mappings=(("cash", "other"), ("expense", "opex")),
                enabled_checks=("variance", "cfo"),
                provenance_version="2",
            )),
        })
    admission = {
        "issuer": ADMISSION_ISSUER, "version": 1,
        "user_id": context.user.id, "membership_id": context.membership.id,
        "organization_id": context.organization_id, "company_id": context.company_id,
        "stytch_organization_id": context.organization.stytch_organization_id,
        "operation": operation, "source_origin": "synthetic" if synthetic else "provider_extract",
        "scope_digest": digest(scope_json(scope)),
        "policy_digest": digest(policy), "request_digest": digest(request),
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(seconds=RUN_SECONDS)).isoformat(),
    }
    existing = source_query(db, AccountingSourceRun).filter(
        AccountingSourceRun.organization_id == scope.organization_id,
        AccountingSourceRun.organization_company_id == scope.company_id,
        AccountingSourceRun.book_id == scope.book_id,
        AccountingSourceRun.idempotency_key == idempotency_key,
    ).one_or_none()
    if existing is not None:
        if existing.scope_digest != digest(scope_json(scope)) or existing.request_json != request or existing.policy_digest != digest(policy):
            raise _conflict("Idempotency key was already used for another source request")
        if existing.admitted_user_id != context.user.id or existing.admitted_membership_id != context.membership.id:
            raise _conflict("Idempotency key belongs to another retained admission")
        outbox = source_query(db, AccountingSourceOutbox).filter(
            AccountingSourceOutbox.run_id == existing.id,
            AccountingSourceOutbox.operation == operation,
        ).one()
        return AdmittedRun(existing.id, outbox.id, existing.status, aware(existing.permit_expires_at))
    if head.lease_run_id is not None and head.lease_expires_at is not None and aware(head.lease_expires_at) > now:
        prior_run = _run_query(db, head.lease_run_id).populate_existing().with_for_update().one_or_none()
        if prior_run is None or prior_run.status not in {"qualified", "failed", "blocked"}:
            raise _conflict("Another source run holds the period lease")
    permit = secrets.token_urlsafe(48)
    generation = (head.lease_generation or 0) + 1
    expires = now + timedelta(seconds=RUN_SECONDS)
    run = AccountingSourceRun(
        organization_id=scope.organization_id, organization_company_id=scope.company_id,
        connection_id=scope.connection_id, book_id=scope.book_id, period=review_period,
        dataset=dataset, idempotency_key=idempotency_key, operation=operation, status="admitted",
        scope_json=scope_json(scope), scope_digest=digest(scope_json(scope)),
        policy_json=policy, policy_digest=digest(policy), request_json=request,
        admission_json=admission, admission_digest=digest(admission),
        admitted_user_id=context.user.id, admitted_membership_id=context.membership.id,
        evaluation_at=now, epoch=head.epoch, lease_generation=generation,
        lease_expires_at=expires, permit_expires_at=expires, dispatch_permit_hash=secret_hash(permit),
    )
    db.add(run)
    db.flush()
    admission = {**admission, "run_id": run.id, "epoch": run.epoch, "lease_generation": generation}
    update_source_row(db, run, {"admission_json": admission, "admission_digest": digest(admission)})
    update_source_row(db, head, {"lease_generation": generation, "lease_run_id": run.id, "lease_expires_at": expires})
    payload = {"run_id": run.id, "dispatch_permit": permit, "organization_id": scope.organization_id,
               "company_id": scope.company_id, "connection_id": scope.connection_id,
               "book_id": scope.book_id, "epoch": run.epoch, "operation": operation,
               "lease_generation": generation}
    outbox = AccountingSourceOutbox(
        organization_id=scope.organization_id, organization_company_id=scope.company_id,
        connection_id=scope.connection_id, book_id=scope.book_id,
        run_id=run.id, event_id=None, operation=operation, status="pending",
        task_id=secrets.token_hex(24), payload_encrypted=encrypt_secret(canonical(payload)),
        available_at=now, attempts=0,
    )
    db.add(outbox)
    db.flush()
    return AdmittedRun(run.id, outbox.id, run.status, expires)


def admit_synthetic_replay(
    db: Session, *, auth: AuthContext, company_id: int, book_id: int,
    review_period: str, dataset: str, idempotency_key: str,
) -> AdmittedRun:
    """Closed server-only synthetic writer for the future isolated replay lane.

    No HTTP DTO, broker argument, environment flag or document selects this
    profile. It has the same current authorization and permit checks as native
    admission, and can never label the resulting coverage as a provider extract.
    """
    return admit_source_run(
        db, auth=auth, company_id=company_id, book_id=book_id, review_period=review_period,
        dataset=dataset, idempotency_key=idempotency_key, _server_profile="synthetic-source-replay/v1",
    )


def verify_source_run(
    db: Session, *, run_id: int, dispatch_permit: str, for_write: bool = True,
) -> AccountingSourceRun:
    """Revalidate retained admission under connection → book → head → run locks.

    A server-owned company pin must already exist. The worker receives only
    location hints, pins them without bypass, and this check proves that those
    hints agree with the retained admission before any source reads occur.
    """
    positive_id(run_id, "run_id")
    if not isinstance(dispatch_permit, str) or not 32 <= len(dispatch_permit) <= 256:
        raise HTTPException(403, "Invalid source dispatch permit")
    run = _run_query(db, run_id).populate_existing().one_or_none()
    if run is None or not hmac.compare_digest(run.dispatch_permit_hash or "", secret_hash(dispatch_permit)):
        raise HTTPException(403, "Invalid source dispatch permit")
    admit_source_waits(db, run.permit_expires_at)
    scope = source_scope_from_json(run.scope_json)
    from .snapshots import lock_current_source
    _, _, head = lock_current_source(db, scope, run.dataset, expected_epoch=run.epoch,
                                     expires_at=run.permit_expires_at)
    admit_source_waits(db, run.permit_expires_at)
    run = _run_query(db, run_id).populate_existing().with_for_update().one()
    now = utc_now()
    if (not hmac.compare_digest(run.dispatch_permit_hash or "", secret_hash(dispatch_permit))
            or run.operation not in {"extract", "synthetic_replay"} or run.status not in {"admitted", "running", "qualified"}
            or aware(run.permit_expires_at) <= now or aware(run.lease_expires_at) <= now
            or aware(run.evaluation_at) > now or head is None
            or head.epoch != run.epoch or head.lease_run_id != run.id
            or head.lease_generation != run.lease_generation
            or head.lease_expires_at is None or aware(head.lease_expires_at) <= now):
        raise HTTPException(403, "Expired, revoked or fenced source admission")
    admission = run.admission_json
    if (not isinstance(admission, dict) or admission.get("issuer") != ADMISSION_ISSUER
            or admission.get("version") != 1 or run.admission_digest != digest(admission)
            or run.scope_digest != digest(run.scope_json) or run.policy_digest != digest(run.policy_json)
            or admission.get("scope_digest") != run.scope_digest
            or admission.get("policy_digest") != run.policy_digest
            or admission.get("request_digest") != digest(run.request_json)
            or admission.get("operation") != run.operation
            or admission.get("run_id") != run.id or admission.get("epoch") != run.epoch
            or admission.get("lease_generation") != run.lease_generation
            or admission.get("issued_at") != aware(run.evaluation_at).isoformat()
            or admission.get("expires_at") != aware(run.permit_expires_at).isoformat()
            or aware(run.lease_expires_at) > aware(run.permit_expires_at)
            or admission.get("user_id") != run.admitted_user_id
            or admission.get("membership_id") != run.admitted_membership_id
            or admission.get("organization_id") != run.organization_id
            or admission.get("company_id") != run.organization_company_id
            or scope.organization_id != run.organization_id or scope.company_id != run.organization_company_id
            or scope.connection_id != run.connection_id or scope.book_id != run.book_id
            or scope.review_period != run.period):
        raise HTTPException(403, "Retained source admission failed its binding checks")
    if (run.request_json.get("dataset") != run.dataset or run.policy_json.get("dataset") != run.dataset
            or run.request_json.get("book_id") != run.book_id
            or run.request_json.get("review_period") != run.period):
        raise HTTPException(403, "Retained source request differs from its admitted dataset and window")
    if run.operation == "synthetic_replay" and (
        scope.environment != "sandbox" or admission.get("source_origin") != "synthetic"
        or run.policy_json.get("qualifier_contract") != "synthetic-source-replay/v1"
    ):
        raise HTTPException(403, "Synthetic source admission cannot authorize provider access")
    if run.operation == "extract" and (
        admission.get("source_origin") != "provider_extract"
        or run.policy_json.get("qualifier_contract") == "synthetic-source-replay/v1"
    ):
        raise HTTPException(403, "Native source admission cannot claim synthetic authority")
    with tenant_scope_live(db, org_id=run.organization_id, company_id=None, realm_id=None, bypass=False):
        user = db.query(User).filter(User.id == run.admitted_user_id).one_or_none()
        org = db.query(Organization).filter(Organization.id == run.organization_id).one_or_none()
        member = db.query(OrganizationMembership).filter(
            OrganizationMembership.id == run.admitted_membership_id,
            OrganizationMembership.user_id == run.admitted_user_id,
            OrganizationMembership.organization_id == run.organization_id,
        ).one_or_none()
    if user is None or org is None or member is None or org.stytch_organization_id != admission.get("stytch_organization_id"):
        raise HTTPException(403, "Admitting membership is no longer available")
    context = resolve_review_company_scope(
        db, auth=AuthContext(user=user, organization=org, membership=member,
                            auth_source="retained-source-admission", token=""),
        company_id=scope.company_id, book_id=scope.book_id, minimum_role="TENANT_ADMIN",
    )
    current = (context.connection.provider, context.connection.environment,
               context.connection.external_account_id, context.connection.external_entity_id,
               context.book.external_book_id or "", context.book.accounting_basis, context.book.currency)
    expected = (scope.provider, scope.environment, scope.external_account_id, scope.external_entity_id,
                scope.external_book_id, scope.basis, scope.currency)
    if current != expected:
        raise HTTPException(403, "Source identity changed after admission")
    if for_write and run.status != "qualified":
        update_source_row(db, run, {"status": "running"})
    return run


def disconnect_source(
    db: Session, *, auth: AuthContext, company_id: int, connection_id: int, revoke: bool = False,
) -> int:
    """Invalidate every period under the connection lock, including first-head races."""
    context = resolve_source_management_scope(db, auth=auth, company_id=company_id, minimum_role="TENANT_ADMIN")
    mark_source_storage_initialized(db, organization_id=context.organization_id, company_id=context.company_id)
    positive_id(connection_id, "connection_id")
    connection = source_query(db, AccountingSourceConnection).filter(
        AccountingSourceConnection.id == connection_id,
        AccountingSourceConnection.organization_id == context.organization_id,
        AccountingSourceConnection.organization_company_id == context.company_id,
    ).populate_existing().with_for_update().one_or_none()
    if connection is None:
        raise HTTPException(404, "Resource not found")
    # Book rows precede all period heads in the common global lock order.
    source_query(db, AccountingBook).filter(AccountingBook.connection_id == connection.id).order_by(AccountingBook.id).with_for_update().all()
    heads = source_query(db, AccountingSourceHead).filter(
        AccountingSourceHead.connection_id == connection.id,
    ).order_by(AccountingSourceHead.period, AccountingSourceHead.id).with_for_update().all()
    previous = source_query(db, AccountingSourceInvalidation).with_entities(func.max(AccountingSourceInvalidation.epoch)).filter(
        AccountingSourceInvalidation.connection_id == connection.id,
    ).scalar() or 1
    epoch = max([previous, *(head.epoch for head in heads)]) + 1
    update_source_row(db, connection, {"status": "revoked" if revoke else "disconnected", "credentials_encrypted": None})
    now = utc_now()
    books = source_query(db, AccountingBook).filter(AccountingBook.connection_id == connection.id).order_by(AccountingBook.id).all()
    for book in books:
        payload = {"organization_id": context.organization_id, "company_id": context.company_id,
                   "connection_id": connection.id, "book_id": book.id, "period": "*", "dataset": "*",
                   "epoch": epoch, "reason": connection.status, "source_event_id": None}
        db.add(AccountingSourceInvalidation(
            organization_id=context.organization_id, organization_company_id=context.company_id,
            connection_id=connection.id, book_id=book.id, epoch=epoch,
            period="*", dataset="*", source_event_id=None,
            reason=connection.status, content_digest=digest(payload),
            producer_version="source-invalidation:v1",
        ))
    for head in heads:
        update_source_row(db, head, {"epoch": epoch, "lease_run_id": None, "lease_expires_at": None,
            "lease_generation": (head.lease_generation or 0) + 1, "current_snapshot_id": None,
            "qualified_checkpoint_json": {}, "revision": (head.revision or 0) + 1})
    return epoch


@dataclass(frozen=True)
class OAuthFlowProfile:
    provider: str
    environment: str
    issuer: str
    redirect_uri: str
    supports_pkce_s256: bool
    contract_id: str


# A public schema does not inspect an app's issuer, exchange, discovery or grant.
# Populate only through a separately reviewed source delta; never from requests.
OAUTH_FLOW_PROFILES: dict[tuple[str, str], OAuthFlowProfile] = {}


def create_oauth_state(
    db: Session, *, auth: AuthContext, company_id: int, connection_id: int,
    provider: str, environment: str, book_id: int | None = None,
) -> dict[str, Any]:
    context = resolve_source_management_scope(db, auth=auth, company_id=company_id, minimum_role="TENANT_ADMIN")
    profile = OAUTH_FLOW_PROFILES.get((provider, environment))
    if profile is None or not profile.supports_pkce_s256:
        raise _conflict("OAuth issuer, redirect, PKCE and discovery contract is not admitted")
    if not auth.token:
        raise HTTPException(403, "A current authenticated session is required")
    mark_source_storage_initialized(db, organization_id=context.organization_id, company_id=context.company_id)
    connection = source_query(db, AccountingSourceConnection).filter(
        AccountingSourceConnection.id == positive_id(connection_id, "connection_id"),
        AccountingSourceConnection.organization_id == context.organization_id,
        AccountingSourceConnection.organization_company_id == context.company_id,
    ).populate_existing().with_for_update().one_or_none()
    if connection is None or (connection.provider, connection.environment) != (provider, environment):
        raise HTTPException(404, "Resource not found")
    if book_id is not None:
        book = source_query(db, AccountingBook).filter(
            AccountingBook.id == positive_id(book_id, "book_id"),
            AccountingBook.connection_id == connection.id,
            AccountingBook.organization_company_id == context.company_id,
        ).one_or_none()
        if book is None:
            raise HTTPException(404, "Resource not found")
    else:
        book = None
    state, verifier, nonce = (secrets.token_urlsafe(48) for _ in range(3))
    expires = utc_now() + timedelta(minutes=10)
    binding = {"connection_id": connection.id, "book_id": book_id,
               "external_account_id": connection.external_account_id,
               "external_entity_id": connection.external_entity_id, "contract_id": profile.contract_id,
               "connection_epoch": source_query(db, AccountingSourceInvalidation).with_entities(func.max(AccountingSourceInvalidation.epoch)).filter(
                   AccountingSourceInvalidation.connection_id == connection.id).scalar() or 1,
               "book_identity": None if book is None else {
                   "external_book_id": book.external_book_id, "basis": book.accounting_basis, "currency": book.currency,
               }}
    db.add(AccountingSourceOAuthState(
        organization_id=context.organization_id, organization_company_id=context.company_id,
        connection_id=connection.id, book_id=book_id, state_hash=secret_hash(state),
        session_hash=secret_hash(auth.token), actor_user_id=context.user.id,
        membership_id=context.membership.id, provider=provider, environment=environment,
        issuer=profile.issuer, redirect_uri=profile.redirect_uri, nonce_hash=secret_hash(nonce),
        code_verifier_encrypted=encrypt_secret(verifier), binding_json=binding,
        expires_at=expires, consumed_at=None,
    ))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return {"state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256", "redirect_uri": profile.redirect_uri,
            "issuer": profile.issuer, "expires_at": expires.isoformat()}


def consume_oauth_state(
    db: Session, *, auth: AuthContext, company_id: int, state: str,
    provider: str, environment: str, issuer: str, redirect_uri: str, nonce: str,
) -> AccountingSourceOAuthState:
    """Consume a genuine state; this supplies no credentials or activation."""
    context = resolve_source_management_scope(db, auth=auth, company_id=company_id, minimum_role="TENANT_ADMIN")
    if (not auth.token or not isinstance(state, str) or not 32 <= len(state) <= 256
            or not isinstance(nonce, str) or not 32 <= len(nonce) <= 256):
        raise HTTPException(403, "Invalid OAuth state")
    row = source_query(db, AccountingSourceOAuthState).filter(
        AccountingSourceOAuthState.state_hash == secret_hash(state),
        AccountingSourceOAuthState.organization_id == context.organization_id,
        AccountingSourceOAuthState.organization_company_id == context.company_id,
    ).populate_existing().with_for_update().one_or_none()
    if (row is None or row.consumed_at is not None or aware(row.expires_at) <= utc_now()
            or row.actor_user_id != context.user.id or row.membership_id != context.membership.id
            or not hmac.compare_digest(row.session_hash, secret_hash(auth.token))
            or not hmac.compare_digest(row.nonce_hash, secret_hash(nonce))
            or (row.provider, row.environment, row.issuer, row.redirect_uri) != (provider, environment, issuer, redirect_uri)):
        raise HTTPException(403, "Invalid, expired or replayed OAuth state")
    profile = OAUTH_FLOW_PROFILES.get((provider, environment))
    if profile is None or profile.contract_id != row.binding_json.get("contract_id") or profile.issuer != issuer or profile.redirect_uri != redirect_uri:
        raise _conflict("OAuth contract changed after authorization began")
    connection = source_query(db, AccountingSourceConnection).filter(
        AccountingSourceConnection.id == row.connection_id,
        AccountingSourceConnection.organization_id == context.organization_id,
        AccountingSourceConnection.organization_company_id == context.company_id,
    ).one_or_none()
    if connection is None or (connection.provider, connection.environment) != (provider, environment) or (connection.external_account_id, connection.external_entity_id) != (
        row.binding_json.get("external_account_id"), row.binding_json.get("external_entity_id"),
    ):
        raise HTTPException(403, "OAuth source identity changed")
    epoch = source_query(db, AccountingSourceInvalidation).with_entities(func.max(AccountingSourceInvalidation.epoch)).filter(
        AccountingSourceInvalidation.connection_id == connection.id).scalar() or 1
    if epoch != row.binding_json.get("connection_epoch"):
        raise HTTPException(403, "OAuth connection epoch changed")
    if row.book_id is not None:
        book = source_query(db, AccountingBook).filter(
            AccountingBook.id == row.book_id, AccountingBook.connection_id == connection.id,
            AccountingBook.organization_id == context.organization_id,
            AccountingBook.organization_company_id == context.company_id,
        ).one_or_none()
        identity = None if book is None else {"external_book_id": book.external_book_id,
                                              "basis": book.accounting_basis, "currency": book.currency}
        if identity != row.binding_json.get("book_identity"):
            raise HTTPException(403, "OAuth book identity changed")
    if not is_encrypted(row.code_verifier_encrypted) or not decrypt_secret(row.code_verifier_encrypted):
        raise _conflict("OAuth verifier evidence is unavailable")
    update_source_row(db, row, {"consumed_at": utc_now()})
    return row

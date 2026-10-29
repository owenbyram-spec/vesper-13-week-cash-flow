from sqlalchemy import Column, Integer, BigInteger, SmallInteger, String, CHAR, DateTime, Numeric, Boolean, Date, Text, JSON, Uuid, UniqueConstraint, CheckConstraint, ForeignKey, ForeignKeyConstraint, Float, Index, text  # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
import uuid  # Python-side uuid4 default for the portable legal.* Uuid PKs (DEC-portable-types, plan 20260720-clickwrap-backend-spine)
from typing import Any
from sqlalchemy.orm import synonym  # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (ledger run_id contract alias)
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector
from .db import Base

class QBOToken(Base):
    """
    Stores QuickBooks Online OAuth tokens per QuickBooks company (realmId).
    """
    __tablename__ = "qbo_tokens"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, unique=True, index=True, nullable=False)

    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    access_expires_at = Column(DateTime, nullable=False)
    refresh_expires_at = Column(DateTime, nullable=False)

    # Intuit-side revoke bookkeeping. A row only survives a disconnect attempt
    # when the Intuit revoke call failed; the encrypted refresh token is kept
    # so the next disconnect can retry the revoke. revoke_failed_reason stores
    # ONLY a short error category (e.g. "http_401", "network_error") — never
    # response bodies or token material.
    revoke_status = Column(String, nullable=True, index=True)
    revoke_failed_reason = Column(String, nullable=True)
    revoke_attempted_at = Column(DateTime(timezone=True), nullable=True)
    # Number of failed Intuit revoke attempts so far. Bounds the background
    # retry task; an on-access disconnect retry is always allowed.
    revoke_attempts = Column(Integer, nullable=False, default=0, server_default="0")

    # PLAN-EXEMPT: QBO connection-health revocation-detection — per-connection health.
    # A grant can be revoked out-of-band on Intuit's side while access_expires_at is
    # still in the FUTURE ("clock-fresh but server-invalid"). reconnect_required is the
    # server-confirmed dead-grant signal: set when a forced refresh returns invalid_grant
    # or a post-refresh call still 401s, and by the proactive health check; cleared on a
    # successful reconnect/health-ping. health_checked_at / last_health_error carry the
    # last probe time + a SHORT error CATEGORY only (e.g. "invalid_grant", "retry_still_401")
    # — NEVER a response body or token material (the never-log-the-payload rule).
    reconnect_required = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    health_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_health_error = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )


class TenantLLMCredential(Base):
    """
    Per-tenant LLM provider API keys, encrypted at rest.

    One row per organization (organization_id is UNIQUE). The two key columns
    hold ONLY the enc:v1: AEAD envelope produced by app.services.crypto
    encrypt_secret — there is NO plaintext key column, and the plaintext is
    never returned in an API response or written to a log (see
    app/services/tenant_llm_credentials.py). Tenant-scoped exactly like its
    encrypted sibling QBOToken: keyed on organization_id, which is the live
    Postgres RLS key (app.current_org_id). When `enabled` is False the resolver
    treats the tenant as having no override and falls back to the global key.

    The CHECK assertion that each non-null key column holds the enc:v1: prefix
    is a storage-layer backstop (AP-TER-07) added in the migration, not here.
    """
    __tablename__ = "tenant_llm_credentials"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # enc:v1: AEAD envelopes only — NEVER plaintext. Nullable: a tenant may set
    # one provider's key without the other.
    anthropic_api_key_encrypted = Column(String, nullable=True)
    openai_api_key_encrypted = Column(String, nullable=True)

    # Optional provider-side routing metadata (NOT secret): an Anthropic
    # workspace id / an OpenAI project id the tenant's key should bill against.
    provider_anthropic_workspace_id = Column(String, nullable=True)
    provider_openai_project_id = Column(String, nullable=True)

    # When False, the resolver ignores this row and uses the global key.
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    # Optional per-tenant model overrides (e.g. {"task": "claude-...",
    # "analysis": "..."}). Not secret; shape is resolver-defined.
    model_overrides = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class QBOOAuthState(Base):
    """
    Durable, one-time OAuth state for QuickBooks connection callbacks.
    Only the state hash is stored so callback bearer values are not persisted.
    """
    __tablename__ = "qbo_oauth_states"

    id = Column(Integer, primary_key=True, index=True)
    state_hash = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    intended_realm_id = Column(String, nullable=True, index=True)
    purpose = Column(String, nullable=False, default="connect")
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """
    Application users for email + password login.
    Only you will create these (no public self-signup).
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="user")  # e.g. "admin" or "user"
    display_name = Column(String, nullable=True)
    auth_source = Column(String, nullable=False, default="legacy")  # legacy | hybrid | stytch
    legacy_login_enabled = Column(Boolean, nullable=False, default=True)
    is_priority_admin = Column(Boolean, nullable=False, default=False)
    default_organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    migrated_to_stytch_at = Column(DateTime(timezone=True), nullable=True)
    stytch_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    force_password_reset = Column(Boolean, nullable=False, default=False)
    password_last_changed_at = Column(DateTime(timezone=True), nullable=True)
    password_reset_required_reason = Column(String, nullable=True)
    pending_email = Column(String, nullable=True, index=True)
    pending_email_token_hash = Column(String, nullable=True, index=True)
    pending_email_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Stripe Monetization
    stripe_customer_id = Column(String, nullable=True, index=True)
    subscription_status = Column(String, nullable=False, default="trialing") # active, past_due, canceled
    subscription_tier = Column(String, nullable=False, default="Basic") # Basic, Pro

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )


class Organization(Base):
    """
    Local mirror of a Stytch B2B organization and its mapped application tenant.
    """
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    stytch_organization_id = Column(String, unique=True, index=True, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    external_id = Column(String, unique=True, index=True, nullable=True)
    primary_realm_id = Column(String, unique=True, index=True, nullable=True)
    auth_methods = Column(String, nullable=False, default="ALL_ALLOWED")
    allowed_auth_methods = Column(JSON, nullable=False, default=list)
    mfa_policy = Column(String, nullable=False, default="OPTIONAL")
    mfa_methods = Column(String, nullable=False, default="ALL_ALLOWED")
    allowed_mfa_methods = Column(JSON, nullable=False, default=list)
    roles_last_synced_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    billing_status = Column(String, nullable=False, default="trialing")  # trialing, active, past_due, suspended, canceled
    billing_tier = Column(String, nullable=False, default="Basic")
    billing_customer_id = Column(String, nullable=True, index=True)
    suspended_at = Column(DateTime(timezone=True), nullable=True)
    suspended_reason = Column(Text, nullable=True)
    # Clickwrap proof-of-assent lifecycle axis (plan 20260720-clickwrap-backend-spine, DEC-F5);
    # orthogonal to is_active/billing_status — do not overload those.
    status = Column(String, nullable=False, default="pending_acceptance", index=True)  # pending_acceptance | active | reaccept_required | assent_backfill_required
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OrganizationCompany(Base):
    """
    Canonical company identity; realm_id is an optional legacy QBO binding.
    """
    __tablename__ = "organization_companies"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    realm_id = Column(String, nullable=True, unique=True, index=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    display_name = Column(String, nullable=True)
    billing_name = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active", index=True)
    # First YYYY-MM period this platform is responsible for closing. NULL means
    # "the month this company was connected" (see close_run_start.close_start_period_for_realm);
    # set it explicitly to opt into an earlier period.
    close_start_period = Column(String, nullable=True)
    # Persist under this company's row lock BEFORE the first review object write.
    # A retained marker survives disconnect/feature disablement; deletion must
    # inspect retained objects even after all review rows have disappeared.
    review_storage_initialized_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_organization_company_owner"),
        UniqueConstraint("organization_id", "realm_id", name="uq_organization_company_realm"),
        Index(
            "ux_organization_company_identity",
            "id",
            "organization_id",
            "realm_id",
            unique=True,
        ),
    )


class AccountingSourceConnection(Base):
    """Company-owned source identity, never a financial qualification receipt."""
    __tablename__ = "accounting_source_connections"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False)
    organization_company_id = Column(Integer, nullable=False)
    provider = Column(String(32), nullable=False)
    environment = Column(String(32), nullable=False)
    external_account_id = Column(String(200), nullable=False)
    external_entity_id = Column(String(200), nullable=False)
    status = Column(String(32), nullable=False, default="disconnected", server_default="disconnected")
    # Server-only existing-crypto envelopes. Never exported or returned to a model.
    credentials_encrypted = Column(Text, nullable=True)
    configuration_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"],
                             ["organization_companies.id", "organization_companies.organization_id"],
                             name="fk_accounting_source_company", ondelete="RESTRICT"),
        UniqueConstraint("id", "organization_company_id", "organization_id", name="uq_accounting_source_owner"),
        UniqueConstraint("organization_id", "organization_company_id", "provider", "environment",
                         "external_account_id", "external_entity_id", name="uq_accounting_source_identity"),
        CheckConstraint("provider = 'qbo' OR provider = 'xero' OR provider = 'netsuite' OR provider = 'sage_intacct'", name="ck_accounting_source_provider"),
        CheckConstraint("environment = 'sandbox' OR environment = 'production'", name="ck_accounting_source_environment"),
        CheckConstraint("length(trim(external_account_id)) > 0 AND length(trim(external_entity_id)) > 0", name="ck_accounting_source_external_identity"),
        CheckConstraint("status = 'active' OR status = 'disconnected' OR status = 'revoked' OR status = 'error'", name="ck_accounting_source_status"),
        CheckConstraint("credentials_encrypted IS NULL OR substr(credentials_encrypted,1,7) = 'enc:v1:'", name="ck_accounting_source_credentials"),
        CheckConstraint("configuration_encrypted IS NULL OR substr(configuration_encrypted,1,7) = 'enc:v1:'", name="ck_accounting_source_configuration"),
        Index("ix_accounting_source_company", "organization_id", "organization_company_id"),
        {"info": {"review_company_scoped": True}},
    )


class AccountingBook(Base):
    """Explicit reporting book and selected source, with composite owner binding."""
    __tablename__ = "accounting_books"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False)
    organization_company_id = Column(Integer, nullable=False)
    connection_id = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False)
    external_book_id = Column(String(200), nullable=True)
    accounting_basis = Column(String(16), nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String(16), nullable=False, default="inactive", server_default="inactive")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"],
                             ["organization_companies.id", "organization_companies.organization_id"],
                             name="fk_accounting_book_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"],
                             ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"],
                             name="fk_accounting_book_source", ondelete="RESTRICT"),
        UniqueConstraint("id", "organization_company_id", "organization_id", name="uq_accounting_book_owner"),
        UniqueConstraint("id", "connection_id", "organization_company_id", "organization_id",
                         name="uq_accounting_book_connection_owner"),
        UniqueConstraint("connection_id", "external_book_id", name="uq_accounting_source_book"),
        CheckConstraint("length(trim(name)) > 0", name="ck_accounting_book_name"),
        CheckConstraint("external_book_id IS NULL OR length(trim(external_book_id)) > 0", name="ck_accounting_book_external_identity"),
        CheckConstraint("accounting_basis = 'accrual' OR accounting_basis = 'cash'", name="ck_accounting_book_basis"),
        CheckConstraint("length(currency) = 3 AND substr(currency,1,1) >= 'A' AND substr(currency,1,1) <= 'Z' AND substr(currency,2,1) >= 'A' AND substr(currency,2,1) <= 'Z' AND substr(currency,3,1) >= 'A' AND substr(currency,3,1) <= 'Z'", name="ck_accounting_book_currency"),
        CheckConstraint("status = 'active' OR status = 'inactive'", name="ck_accounting_book_status"),
        Index("ix_accounting_book_company", "organization_id", "organization_company_id"),
        {"info": {"review_company_scoped": True}},
    )


class AccountingSourceRun(Base):
    """W2 runs: company-scoped; mutable bounded service state."""
    __tablename__ = "accounting_source_runs"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    period = Column('period', String(7), nullable=False)
    dataset = Column('dataset', String(32), nullable=False)
    epoch = Column('epoch', Integer, nullable=False)
    lease_generation = Column('lease_generation', Integer, nullable=False)
    lease_expires_at = Column('lease_expires_at', DateTime(timezone=True), nullable=False)
    dispatch_permit_hash = Column('dispatch_permit_hash', String(64), nullable=False)
    permit_expires_at = Column('permit_expires_at', DateTime(timezone=True), nullable=False)
    admitted_user_id = Column('admitted_user_id', Integer, nullable=False)
    admitted_membership_id = Column('admitted_membership_id', Integer, nullable=False)
    evaluation_at = Column('evaluation_at', DateTime(timezone=True), nullable=False)
    status = Column('status', String(32), nullable=False)
    operation = Column('operation', String(64), nullable=False)
    scope_json = Column('scope_json', JSON, nullable=False)
    scope_digest = Column('scope_digest', String(64), nullable=False)
    policy_json = Column('policy_json', JSON, nullable=False)
    policy_digest = Column('policy_digest', String(64), nullable=False)
    request_json = Column('request_json', JSON, nullable=False)
    admission_json = Column('admission_json', JSON, nullable=False)
    admission_digest = Column('admission_digest', String(64), nullable=False)
    idempotency_key = Column('idempotency_key', String(128), nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asrun_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asrun_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asrun_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asrun_scope"),
        Index("ix_asrun_company", "organization_id", "organization_company_id"),
        UniqueConstraint('organization_id', 'organization_company_id', 'book_id', 'idempotency_key', name="uq_asrun_idempotency"),
        CheckConstraint('epoch >= 0 AND lease_generation > 0', name="ck_asrun_counters"),
        CheckConstraint('permit_expires_at > evaluation_at', name="ck_asrun_expiry"),
        {"info": {"review_company_scoped": True, "source_append_only": False}},
    )


class AccountingSourcePage(Base):
    """W2 pages: company-scoped; append-only evidence."""
    __tablename__ = "accounting_source_pages"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    run_id = Column('run_id', Integer, nullable=False)
    ordinal = Column('ordinal', Integer, nullable=False)
    dataset = Column('dataset', String(32), nullable=False)
    partition_key = Column('partition_key', String(128), nullable=False)
    cursor = Column('cursor', String(512), nullable=True)
    next_cursor = Column('next_cursor', String(512), nullable=True)
    terminal = Column('terminal', Boolean, nullable=False)
    raw_digest = Column('raw_digest', String(64), nullable=False)
    payload_encrypted = Column('payload_encrypted', Text, nullable=False)
    byte_count = Column('byte_count', Integer, nullable=False)
    contract_id = Column('contract_id', String(128), nullable=False)
    page_json = Column('page_json', JSON, nullable=False)
    content_digest = Column('content_digest', String(64), nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_aspage_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_aspage_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_aspage_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_aspage_scope"),
        Index("ix_aspage_company", "organization_id", "organization_company_id"),
        ForeignKeyConstraint(['run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_runs.id', 'accounting_source_runs.book_id', 'accounting_source_runs.connection_id', 'accounting_source_runs.organization_company_id', 'accounting_source_runs.organization_id'], name='fk_aspage_run_id', ondelete="RESTRICT"),
        UniqueConstraint('run_id', 'dataset', 'partition_key', 'ordinal', name="uq_aspage_ordinal"),
        UniqueConstraint('id', 'run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id', name="uq_aspage_run_scope"),
        CheckConstraint('ordinal >= 0 AND byte_count >= 0 AND byte_count <= 8388608', name="ck_aspage_bounds"),
        CheckConstraint("substr(payload_encrypted,1,7) = 'enc:v1:'", name="ck_aspage_encrypted"),
        {"info": {"review_company_scoped": True, "source_append_only": True}},
    )


class AccountingSourceRecord(Base):
    """W2 records: company-scoped; append-only evidence."""
    __tablename__ = "accounting_source_records"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    run_id = Column('run_id', Integer, nullable=False)
    page_id = Column('page_id', Integer, nullable=False)
    dataset = Column('dataset', String(32), nullable=False)
    occurrence = Column('occurrence', String(512), nullable=False)
    source_id = Column('source_id', String(512), nullable=False)
    source_version = Column('source_version', String(128), nullable=False)
    content_digest = Column('content_digest', String(64), nullable=False)
    record_json = Column('record_json', JSON, nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asrecord_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asrecord_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asrecord_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asrecord_scope"),
        Index("ix_asrecord_company", "organization_id", "organization_company_id"),
        ForeignKeyConstraint(['run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_runs.id', 'accounting_source_runs.book_id', 'accounting_source_runs.connection_id', 'accounting_source_runs.organization_company_id', 'accounting_source_runs.organization_id'], name='fk_asrecord_run_id', ondelete="RESTRICT"),
        ForeignKeyConstraint(['page_id', 'run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_pages.id', 'accounting_source_pages.run_id', 'accounting_source_pages.book_id', 'accounting_source_pages.connection_id', 'accounting_source_pages.organization_company_id', 'accounting_source_pages.organization_id'], name='fk_asrecord_page_id', ondelete="RESTRICT"),
        UniqueConstraint('run_id', 'dataset', 'occurrence', name="uq_asrecord_occurrence"),
        CheckConstraint('length(trim(occurrence)) > 0', name="ck_asrecord_occurrence"),
        {"info": {"review_company_scoped": True, "source_append_only": True}},
    )


class AccountingSourceSnapshot(Base):
    """W2 snapshots: company-scoped; append-only evidence."""
    __tablename__ = "accounting_source_snapshots"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    run_id = Column('run_id', Integer, nullable=False)
    dataset = Column('dataset', String(32), nullable=False)
    epoch = Column('epoch', Integer, nullable=False)
    period = Column('period', String(7), nullable=False)
    manifest_json = Column('manifest_json', JSON, nullable=False)
    content_digest = Column('content_digest', String(64), nullable=False)
    issued_at = Column('issued_at', DateTime(timezone=True), nullable=False)
    expires_at = Column('expires_at', DateTime(timezone=True), nullable=False)
    idempotency_key = Column('idempotency_key', String(128), nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_assnap_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_assnap_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_assnap_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_assnap_scope"),
        Index("ix_assnap_company", "organization_id", "organization_company_id"),
        UniqueConstraint('organization_id', 'organization_company_id', 'book_id', 'idempotency_key', name="uq_assnap_idempotency"),
        ForeignKeyConstraint(['run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_runs.id', 'accounting_source_runs.book_id', 'accounting_source_runs.connection_id', 'accounting_source_runs.organization_company_id', 'accounting_source_runs.organization_id'], name='fk_assnap_run_id', ondelete="RESTRICT"),
        UniqueConstraint('id', 'run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id', name="uq_assnap_run_scope"),
        CheckConstraint('expires_at > issued_at', name="ck_assnap_expiry"),
        {"info": {"review_company_scoped": True, "source_append_only": True}},
    )


class AccountingSourceCoverageReceipt(Base):
    """W2 coverage_receipts: company-scoped; append-only evidence."""
    __tablename__ = "accounting_source_coverage_receipts"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    snapshot_id = Column('snapshot_id', Integer, nullable=False)
    run_id = Column('run_id', Integer, nullable=False)
    epoch = Column('epoch', Integer, nullable=False)
    dataset = Column('dataset', String(32), nullable=False)
    source_ref = Column('source_ref', String(200), nullable=False)
    source_version = Column('source_version', String(128), nullable=False)
    source_digest = Column('source_digest', String(64), nullable=False)
    receipt_json = Column('receipt_json', JSON, nullable=False)
    receipt_digest = Column('receipt_digest', String(64), nullable=False)
    qualification_json = Column('qualification_json', JSON, nullable=False)
    qualification_digest = Column('qualification_digest', String(64), nullable=False)
    issuer_id = Column('issuer_id', String(128), nullable=False)
    issuer_version = Column('issuer_version', String(64), nullable=False)
    issued_at = Column('issued_at', DateTime(timezone=True), nullable=False)
    expires_at = Column('expires_at', DateTime(timezone=True), nullable=False)
    idempotency_key = Column('idempotency_key', String(128), nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asqual_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asqual_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asqual_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asqual_scope"),
        Index("ix_asqual_company", "organization_id", "organization_company_id"),
        UniqueConstraint('organization_id', 'organization_company_id', 'book_id', 'idempotency_key', name="uq_asqual_idempotency"),
        ForeignKeyConstraint(['run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_runs.id', 'accounting_source_runs.book_id', 'accounting_source_runs.connection_id', 'accounting_source_runs.organization_company_id', 'accounting_source_runs.organization_id'], name='fk_asqual_run_id', ondelete="RESTRICT"),
        ForeignKeyConstraint(['snapshot_id', 'run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_snapshots.id', 'accounting_source_snapshots.run_id', 'accounting_source_snapshots.book_id', 'accounting_source_snapshots.connection_id', 'accounting_source_snapshots.organization_company_id', 'accounting_source_snapshots.organization_id'], name='fk_asqual_snapshot_id', ondelete="RESTRICT"),
        UniqueConstraint('id', 'snapshot_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id', name="uq_asqual_snapshot_scope"),
        CheckConstraint('expires_at > issued_at', name="ck_asqual_expiry"),
        {"info": {"review_company_scoped": True, "source_append_only": True}},
    )


class AccountingSourceControlBinding(Base):
    """W2 control_bindings: company-scoped; append-only evidence."""
    __tablename__ = "accounting_source_control_bindings"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    snapshot_id = Column('snapshot_id', Integer, nullable=False)
    qualification_id = Column('qualification_id', Integer, nullable=False)
    epoch = Column('epoch', Integer, nullable=False)
    bundle_digest = Column('bundle_digest', String(64), nullable=False)
    policy_digest = Column('policy_digest', String(64), nullable=False)
    receipt_digest = Column('receipt_digest', String(64), nullable=False)
    source_digest = Column('source_digest', String(64), nullable=False)
    source_ref = Column('source_ref', String(200), nullable=False)
    source_version = Column('source_version', String(128), nullable=False)
    binding_json = Column('binding_json', JSON, nullable=False)
    binding_digest = Column('binding_digest', String(64), nullable=False)
    issuer_id = Column('issuer_id', String(128), nullable=False)
    issuer_version = Column('issuer_version', String(64), nullable=False)
    issued_at = Column('issued_at', DateTime(timezone=True), nullable=False)
    expires_at = Column('expires_at', DateTime(timezone=True), nullable=False)
    idempotency_key = Column('idempotency_key', String(128), nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asbind_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asbind_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asbind_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asbind_scope"),
        Index("ix_asbind_company", "organization_id", "organization_company_id"),
        UniqueConstraint('organization_id', 'organization_company_id', 'book_id', 'idempotency_key', name="uq_asbind_idempotency"),
        ForeignKeyConstraint(['qualification_id', 'snapshot_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_coverage_receipts.id', 'accounting_source_coverage_receipts.snapshot_id', 'accounting_source_coverage_receipts.book_id', 'accounting_source_coverage_receipts.connection_id', 'accounting_source_coverage_receipts.organization_company_id', 'accounting_source_coverage_receipts.organization_id'], name='fk_asbind_qualification_id', ondelete="RESTRICT"),
        ForeignKeyConstraint(['snapshot_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_snapshots.id', 'accounting_source_snapshots.book_id', 'accounting_source_snapshots.connection_id', 'accounting_source_snapshots.organization_company_id', 'accounting_source_snapshots.organization_id'], name='fk_asbind_snapshot_id', ondelete="RESTRICT"),
        CheckConstraint('expires_at > issued_at', name="ck_asbind_expiry"),
        {"info": {"review_company_scoped": True, "source_append_only": True}},
    )


class AccountingSourceHead(Base):
    """W2 heads: company-scoped; mutable bounded service state."""
    __tablename__ = "accounting_source_heads"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    period = Column('period', String(7), nullable=False)
    dataset = Column('dataset', String(32), nullable=False)
    epoch = Column('epoch', Integer, nullable=False)
    revision = Column('revision', Integer, nullable=False)
    current_snapshot_id = Column('current_snapshot_id', Integer, nullable=True)
    lease_generation = Column('lease_generation', Integer, nullable=False)
    lease_run_id = Column('lease_run_id', Integer, nullable=True)
    lease_expires_at = Column('lease_expires_at', DateTime(timezone=True), nullable=True)
    checkpoint_json = Column('checkpoint_json', JSON, nullable=False)
    qualified_checkpoint_json = Column('qualified_checkpoint_json', JSON, nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_ashead_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_ashead_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_ashead_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_ashead_scope"),
        Index("ix_ashead_company", "organization_id", "organization_company_id"),
        UniqueConstraint('book_id', 'period', 'dataset', name="uq_ashead_period"),
        ForeignKeyConstraint(['current_snapshot_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_snapshots.id', 'accounting_source_snapshots.book_id', 'accounting_source_snapshots.connection_id', 'accounting_source_snapshots.organization_company_id', 'accounting_source_snapshots.organization_id'], name='fk_ashead_current_snapshot_id', ondelete="RESTRICT"),
        ForeignKeyConstraint(['lease_run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_runs.id', 'accounting_source_runs.book_id', 'accounting_source_runs.connection_id', 'accounting_source_runs.organization_company_id', 'accounting_source_runs.organization_id'], name='fk_ashead_lease_run_id', ondelete="RESTRICT"),
        CheckConstraint('epoch >= 0 AND revision >= 0 AND lease_generation >= 0', name="ck_ashead_counters"),
        {"info": {"review_company_scoped": True, "source_append_only": False}},
    )


class AccountingSourceInvalidation(Base):
    """W2 invalidations: company-scoped; append-only evidence."""
    __tablename__ = "accounting_source_invalidations"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    period = Column('period', String(7), nullable=False)
    dataset = Column('dataset', String(32), nullable=False)
    epoch = Column('epoch', Integer, nullable=False)
    reason = Column('reason', String(128), nullable=False)
    source_event_id = Column('source_event_id', Integer, nullable=True)
    content_digest = Column('content_digest', String(64), nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asinval_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asinval_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asinval_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asinval_scope"),
        Index("ix_asinval_company", "organization_id", "organization_company_id"),
        ForeignKeyConstraint(['source_event_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_events.id', 'accounting_source_events.book_id', 'accounting_source_events.connection_id', 'accounting_source_events.organization_company_id', 'accounting_source_events.organization_id'], name='fk_asinval_source_event_id', ondelete="RESTRICT"),
        {"info": {"review_company_scoped": True, "source_append_only": True}},
    )


class AccountingSourceEvent(Base):
    """W2 events: company-scoped; append-only evidence."""
    __tablename__ = "accounting_source_events"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    provider_event_id = Column('provider_event_id', String(200), nullable=False)
    entity_type = Column('entity_type', String(80), nullable=False)
    entity_id = Column('entity_id', String(200), nullable=False)
    operation = Column('operation', String(64), nullable=False)
    event_time = Column('event_time', DateTime(timezone=True), nullable=False)
    payload_json = Column('payload_json', JSON, nullable=False)
    content_digest = Column('content_digest', String(64), nullable=False)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asevent_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asevent_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asevent_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asevent_scope"),
        Index("ix_asevent_company", "organization_id", "organization_company_id"),
        UniqueConstraint('book_id', 'provider_event_id', name="uq_asevent_delivery"),
        {"info": {"review_company_scoped": True, "source_append_only": True}},
    )


class AccountingSourceOutbox(Base):
    """W2 outbox: company-scoped; mutable bounded service state."""
    __tablename__ = "accounting_source_outbox"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=False)
    book_id = Column('book_id', Integer, nullable=False)
    run_id = Column('run_id', Integer, nullable=True)
    event_id = Column('event_id', Integer, nullable=True)
    operation = Column('operation', String(64), nullable=False)
    status = Column('status', String(32), nullable=False)
    task_id = Column('task_id', String(128), nullable=False)
    payload_encrypted = Column('payload_encrypted', Text, nullable=False)
    available_at = Column('available_at', DateTime(timezone=True), nullable=False)
    attempts = Column('attempts', Integer, nullable=False)
    delivered_at = Column('delivered_at', DateTime(timezone=True), nullable=True)
    lease_expires_at = Column('lease_expires_at', DateTime(timezone=True), nullable=True)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asout_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asout_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asout_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asout_scope"),
        Index("ix_asout_company", "organization_id", "organization_company_id"),
        ForeignKeyConstraint(['run_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_runs.id', 'accounting_source_runs.book_id', 'accounting_source_runs.connection_id', 'accounting_source_runs.organization_company_id', 'accounting_source_runs.organization_id'], name='fk_asout_run_id', ondelete="RESTRICT"),
        ForeignKeyConstraint(['event_id', 'book_id', 'connection_id', 'organization_company_id', 'organization_id'], ['accounting_source_events.id', 'accounting_source_events.book_id', 'accounting_source_events.connection_id', 'accounting_source_events.organization_company_id', 'accounting_source_events.organization_id'], name='fk_asout_event_id', ondelete="RESTRICT"),
        UniqueConstraint('task_id', name="uq_asout_task"),
        CheckConstraint('attempts >= 0', name="ck_asout_attempts"),
        CheckConstraint("substr(payload_encrypted,1,7) = 'enc:v1:'", name="ck_asout_encrypted"),
        CheckConstraint('(run_id IS NOT NULL OR event_id IS NOT NULL)', name="ck_asout_target"),
        {"info": {"review_company_scoped": True, "source_append_only": False}},
    )


class AccountingSourceOAuthState(Base):
    """W2 oauth_states: company-scoped; mutable bounded service state."""
    __tablename__ = "accounting_source_oauth_states"

    id = Column('id', Integer, nullable=False, primary_key=True)
    organization_id = Column('organization_id', Integer, nullable=False)
    organization_company_id = Column('organization_company_id', Integer, nullable=False)
    connection_id = Column('connection_id', Integer, nullable=True)
    book_id = Column('book_id', Integer, nullable=True)
    state_hash = Column('state_hash', String(64), nullable=False)
    session_hash = Column('session_hash', String(64), nullable=False)
    actor_user_id = Column('actor_user_id', Integer, nullable=False)
    membership_id = Column('membership_id', Integer, nullable=False)
    provider = Column('provider', String(32), nullable=False)
    environment = Column('environment', String(32), nullable=False)
    issuer = Column('issuer', String(200), nullable=False)
    redirect_uri = Column('redirect_uri', String(512), nullable=False)
    code_verifier_encrypted = Column('code_verifier_encrypted', Text, nullable=False)
    nonce_hash = Column('nonce_hash', String(64), nullable=False)
    binding_json = Column('binding_json', JSON, nullable=False)
    expires_at = Column('expires_at', DateTime(timezone=True), nullable=False)
    consumed_at = Column('consumed_at', DateTime(timezone=True), nullable=True)
    schema_version = Column('schema_version', String(32), nullable=False, default="1", server_default="1")
    producer_version = Column('producer_version', String(64), nullable=False, default="accounting-source-1", server_default="accounting-source-1")
    created_at = Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id"], ["organization_companies.id", "organization_companies.organization_id"], name="fk_asoauth_company", ondelete="RESTRICT"),
        ForeignKeyConstraint(["connection_id", "organization_company_id", "organization_id"], ["accounting_source_connections.id", "accounting_source_connections.organization_company_id", "accounting_source_connections.organization_id"], name="fk_asoauth_connection", ondelete="RESTRICT"),
        ForeignKeyConstraint(['book_id', 'connection_id', 'organization_company_id', 'organization_id'], ["accounting_books.id", "accounting_books.connection_id", "accounting_books.organization_company_id", "accounting_books.organization_id"], name="fk_asoauth_book", ondelete="RESTRICT"),
        UniqueConstraint("id", "book_id", "connection_id", "organization_company_id", "organization_id", name="uq_asoauth_scope"),
        Index("ix_asoauth_company", "organization_id", "organization_company_id"),
        UniqueConstraint('state_hash', name="uq_asoauth_state"),
        CheckConstraint('book_id IS NULL OR connection_id IS NOT NULL', name="ck_asoauth_book_pair"),
        CheckConstraint("substr(code_verifier_encrypted,1,7) = 'enc:v1:'", name="ck_asoauth_encrypted"),
        CheckConstraint("provider IN ('qbo','xero','netsuite','sage_intacct') AND environment IN ('sandbox','production')", name="ck_asoauth_provider"),
        {"info": {"review_company_scoped": True, "source_append_only": False}},
    )


class FirmOperationsSnapshot(Base):
    """Immutable reviewed metadata, owned by an organization independently of QBO.

    The actor integer is historical attribution, not an access grant or user FK.
    Only the existing tenant lifecycle may delete these snapshots.
    """
    __tablename__ = "firm_operations_snapshots"

    id = Column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    imported_by_actor_id = Column(Integer, nullable=False)
    imported_at = Column(DateTime(timezone=True), nullable=False)
    review_attestation_version = Column(String(40), nullable=False)
    reviewed_extension_keys = Column(JSON, nullable=False)
    input_digest = Column(String(64), nullable=False)
    schema_version = Column(String(40), nullable=False)
    evaluator_version = Column(String(40), nullable=False)
    evaluated_at = Column(DateTime(timezone=True), nullable=False)
    input_json = Column(JSON, nullable=False)
    result_json = Column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "input_digest", name="uq_firm_ops_org_digest"),
        Index("ix_firm_ops_org_imported", "organization_id", "imported_at", "id"),
        CheckConstraint("length(input_digest) = 64 AND input_digest = lower(input_digest)", name="ck_firm_ops_digest"),
        CheckConstraint("imported_by_actor_id > 0", name="ck_firm_ops_actor"),
        CheckConstraint("schema_version = 'firm-operations/v1'", name="ck_firm_ops_schema"),
        CheckConstraint("length(evaluator_version) > 0", name="ck_firm_ops_evaluator"),
        CheckConstraint("review_attestation_version = 'firm-operations-review/v1'", name="ck_firm_ops_review"),
        CheckConstraint("evaluated_at <= imported_at", name="ck_firm_ops_time"),
    )


class UserCompanyAccess(Base):
    """
    Per-company access grant WITHIN a tenant (intra-org least-privilege scoping).

    Fresh shape (NOT the dropped realm-keyed `(user_id, realm_id, firm_role)` table
    removed in migration e5f6a7b8c9d0): org-scoped and keyed on
    `organization_company_id`, so it composes with the Organization model + the
    org-keyed RLS backstop instead of fighting it.

    Semantics (v1 = binary access scoping): a member with ZERO grant rows keeps
    access to ALL companies in their org (backward-compatible default); a member
    with >=1 grant row is restricted to exactly those companies. The grant
    controls WHICH companies a member may touch; the member's ROLE is unchanged
    (still derived from org membership).
    """
    __tablename__ = "user_company_access"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(
        Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Reserved for a future per-company role override; NOT enforced in v1 (binary
    # access scoping only) — a future reader must not assume this column gates.
    role_override = Column(String, nullable=True)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "organization_company_id", name="uq_user_company_access_user_company"),
    )


class TenantProvisioningRequest(Base):
    """
    Governed request to create or repair a tenant provisioning workflow.
    The payload is sanitized before storage; external invite links/secrets are
    never persisted here.
    """
    __tablename__ = "tenant_provisioning_requests"

    id = Column(Integer, primary_key=True, index=True)
    mode = Column(String, nullable=False, default="shared", index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    idempotency_key = Column(String, unique=True, nullable=False, index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    request_payload_json = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TenantProvisioningRun(Base):
    """One execution attempt for a provisioning request."""
    __tablename__ = "tenant_provisioning_runs"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("tenant_provisioning_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, nullable=False, default="running", index=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TenantProvisioningStep(Base):
    """Auditable step-level progress for provisioning runs."""
    __tablename__ = "tenant_provisioning_steps"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("tenant_provisioning_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    attempt = Column(Integer, nullable=False, default=1)
    details_json = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TenantProvisioningExternalResource(Base):
    """External resource touched by provisioning, such as Stytch org/member or Render service."""
    __tablename__ = "tenant_provisioning_external_resources"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("tenant_provisioning_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("tenant_provisioning_runs.id", ondelete="CASCADE"), nullable=True, index=True)
    provider = Column(String, nullable=False, index=True)
    resource_type = Column(String, nullable=False, index=True)
    resource_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="ready", index=True)
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider", "resource_type", "resource_id", name="uq_tenant_provisioning_resource"),
    )


class TenantProvisioningNotification(Base):
    """Notification emitted by provisioning, e.g. admin invite queued/sent."""
    __tablename__ = "tenant_provisioning_notifications"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("tenant_provisioning_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("tenant_provisioning_runs.id", ondelete="CASCADE"), nullable=True, index=True)
    channel = Column(String, nullable=False, index=True)
    recipient = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="queued", index=True)
    payload_json = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TenantBilling(Base):
    """
    Billing state for an application tenant.
    Stripe subscriptions belong to the organization rather than an individual user.
    """
    __tablename__ = "tenant_billing"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    stripe_customer_id = Column(String, nullable=True, index=True)
    stripe_subscription_id = Column(String, nullable=True, index=True)
    subscription_status = Column(String, nullable=False, default="trialing", index=True)
    subscription_tier = Column(String, nullable=False, default="Basic", index=True)
    billing_email = Column(String, nullable=True)
    token_limit_monthly = Column(Integer, nullable=True)
    monthly_budget_cents = Column(Integer, nullable=True)
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class StripeWebhookEvent(Base):
    """
    Idempotency ledger for Stripe webhook deliveries.
    Stripe retries webhooks and can deliver events out of order, so every event
    id is claimed before applying billing state changes.
    """
    __tablename__ = "stripe_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, unique=True, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="processing", index=True)
    error = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ApiKey(Base):
    """Per-tenant, read-only Pull-API credential.

    Bound to exactly one organization. Only a SHA-256 hash of the full key is
    stored (mirrors the approval-token pattern in app/services/email_service.py);
    the raw secret is shown once at creation and never persisted or logged.
    """
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_org_status", "organization_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    key_id = Column(String, unique=True, index=True, nullable=False)       # public lookup id segment
    token_hash = Column(String, unique=True, index=True, nullable=False)   # sha256 of the full key
    name = Column(String, nullable=True)
    scopes = Column(JSON, nullable=False, default=list)                    # e.g. ["pull:entities"]
    status = Column(String, nullable=False, default="active", index=True)  # active | revoked
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class QBOWebhookEvent(Base):
    """
    Durable ledger for QBO webhook entity notifications.
    Intuit webhook payloads do not include a single delivery id, so event_key is
    derived from trusted notification fields and used for idempotent processing.
    """
    __tablename__ = "qbo_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_key = Column(String, unique=True, nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    entity_name = Column(String, nullable=False, index=True)
    entity_id = Column(String, nullable=False, index=True)
    operation = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0, server_default="0")
    payload_json = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    processing_started_at = Column(DateTime(timezone=True), nullable=True, index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SentryWebhookEvent(Base):
    """Durable, sanitized ledger for Sentry internal-integration webhooks."""
    __tablename__ = "sentry_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, unique=True, nullable=False, index=True)
    resource = Column(String, nullable=True, index=True)
    action = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False, default="processing", index=True)
    payload_json = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DebugIncident(Base):
    """
    Sanitized debug incident created from Sentry webhooks.
    This is intentionally read-only triage state; no raw Sentry payload is kept.
    """
    __tablename__ = "debug_incidents"

    id = Column(Integer, primary_key=True, index=True)
    sentry_event_id = Column(String, unique=True, nullable=False, index=True)
    sentry_issue_id = Column(String, nullable=True, index=True)
    project_slug = Column(String, nullable=True, index=True)
    environment = Column(String, nullable=True, index=True)
    level = Column(String, nullable=True, index=True)
    title = Column(String, nullable=False)
    culprit = Column(String, nullable=True)
    status = Column(String, nullable=False, default="open", index=True)
    sanitized_payload_json = Column(JSON, nullable=False, default=dict)
    event_count = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DebugTriageRun(Base):
    """Read-only triage result derived from a sanitized debug incident."""
    __tablename__ = "debug_triage_runs"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("debug_incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, nullable=False, default="completed", index=True)
    severity = Column(String, nullable=True, index=True)
    summary = Column(Text, nullable=True)
    recommendations_json = Column(JSON, nullable=True)
    sanitized_input_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DebugNotificationSubscription(Base):
    """Authorized recipient preferences for sanitized debug incident alerts."""
    __tablename__ = "debug_notification_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    recipient_email = Column(String, nullable=False, index=True)
    channel = Column(String, nullable=False, default="email")
    min_level = Column(String, nullable=False, default="error", index=True)
    environments_json = Column(JSON, nullable=True)
    project_slugs_json = Column(JSON, nullable=True)
    cooldown_seconds = Column(Integer, nullable=False, default=3600)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DebugNotificationDelivery(Base):
    """Durable record of each sanitized debug notification dispatch attempt."""
    __tablename__ = "debug_notification_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("debug_notification_subscriptions.id", ondelete="SET NULL"), nullable=True, index=True)
    incident_id = Column(Integer, ForeignKey("debug_incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    triage_run_id = Column(Integer, ForeignKey("debug_triage_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    channel = Column(String, nullable=False, default="email")
    recipient = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    reason = Column(String, nullable=True)
    payload_json = Column(JSON, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EntityBilling(Base):
    """
    Billing state for one QBO company/entity under an organization.
    TenantBilling remains the organization-level default for legacy customers.
    """
    __tablename__ = "entity_billing"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    realm_id = Column(String, nullable=False, index=True)
    stripe_customer_id = Column(String, nullable=True, index=True)
    stripe_subscription_id = Column(String, nullable=True, unique=True, index=True)
    # Custom per-company pricing. Prefer a Dashboard-managed Stripe Price
    # (stripe_price_id); custom_monthly_cents is an ad-hoc override for bespoke
    # contracts. When both are null, checkout falls back to the tier price table.
    stripe_price_id = Column(String, nullable=True, index=True)
    custom_monthly_cents = Column(Integer, nullable=True)
    # Payment methods Checkout should offer for this company (e.g.
    # ["card", "us_bank_account"]). NULL falls back to the global default.
    payment_method_kinds = Column(JSON, nullable=True)
    subscription_status = Column(String, nullable=False, default="trialing", index=True)
    subscription_tier = Column(String, nullable=False, default="Basic", index=True)
    billing_email = Column(String, nullable=True)
    token_limit_monthly = Column(Integer, nullable=True)
    monthly_budget_cents = Column(Integer, nullable=True)
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OrganizationMembership(Base):
    """
    Local mirror of a Stytch B2B member within an organization.
    """
    __tablename__ = "organization_memberships"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    stytch_member_id = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False, default="read_only")
    status = Column(String, nullable=False, default="active")
    is_default = Column(Boolean, nullable=False, default=False)
    mfa_enrolled = Column(Boolean, nullable=False, default=False)
    last_authenticated_at = Column(DateTime(timezone=True), nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_membership_user"),
    )


class AuthSession(Base):
    """
    Local audit mirror of a Stytch-backed or legacy-backed application session.
    """
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    membership_id = Column(Integer, ForeignKey("organization_memberships.id", ondelete="SET NULL"), nullable=True, index=True)
    auth_source = Column(String, nullable=False)  # legacy | stytch | migration
    cookie_token_hint = Column(String, nullable=True, index=True)
    stytch_session_id = Column(String, nullable=True, index=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class AuthAuditEvent(Base):
    """
    Auth-specific audit trail for logins, migrations, MFA, invites, and role changes.
    """
    __tablename__ = "auth_audit_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    membership_id = Column(Integer, ForeignKey("organization_memberships.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    event_status = Column(String, nullable=False, default="success")
    actor_email = Column(String, nullable=True)
    target_email = Column(String, nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TenantUsageEvent(Base):
    """
    Normalized usage ledger for tenant billing, limits, and admin reporting.
    """
    __tablename__ = "tenant_usage_events"
    __table_args__ = (
        Index("ix_tenant_usage_events_org_occurred_at", "organization_id", "occurred_at"),
        Index("ix_tenant_usage_events_org_company_occurred_at", "organization_id", "company_id", "occurred_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    source = Column(String, nullable=False, default="llm", index=True)
    provider = Column(String, nullable=True, index=True)
    model = Column(String, nullable=True, index=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    cost_estimate = Column(Numeric(precision=12, scale=6), nullable=True)
    event_metadata = Column(JSON, nullable=True)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class QBOCompanyInfo(Base):
    """
    Stores high-level company metadata from QBO.
    """
    __tablename__ = "qbo_company_info"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, unique=True, index=True, nullable=False)
    company_name = Column(String)
    legal_name = Column(String)
    country = Column(String)
    currency = Column(String)
    fiscal_year_start_month = Column(String)
    
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOAccount(Base):
    """
    Chart of Accounts from QBO.

    ``UNIQUE(realm_id, qbo_id)`` is the account IDENTITY and is enforced by the
    DATABASE, not by a caller's in-process view. Before it existed, ``sync_accounts``
    deduplicated against a Python dict built from one SELECT, so two concurrent syncs
    of the same realm both missed and both inserted — and every downstream identity
    that keys on an account (mapping, leaf classification, balance snapshots) became
    nondeterministic across runs. The pair is the key, not ``qbo_id`` alone: the same
    QBO account id legitimately occurs in different realms.
    Plan 20260910-consolidator-prerequisites AC-2.
    """
    __tablename__ = "qbo_accounts"
    __table_args__ = (
        UniqueConstraint("realm_id", "qbo_id", name="uq_qbo_account_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_id = Column(String, nullable=False, index=True)
    
    name = Column(String, nullable=False)
    fully_qualified_name = Column(String)
    acct_num = Column(String, nullable=True)   # QBO AcctNum, e.g. "1005"
    active = Column(Boolean, default=True)
    classification = Column(String)
    account_type = Column(String)
    account_sub_type = Column(String)
    parent_ref = Column(String, nullable=True)
    
    currency = Column(String)
    current_balance = Column(Numeric(precision=18, scale=2))

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOAccountBalanceSnapshot(Base):
    """Period-stamped snapshot of a QBO account's balance (0D).

    QBOAccount.current_balance is a single scalar; this table records one balance
    per account per month so period-over-period deltas (e.g. a debt account's
    principal reduction, the trailing-DSCR term) become derivable. Forward-only:
    rows accrue from when 0D ships — no historical backfill is possible from a
    single live scalar.
    """
    __tablename__ = "qbo_account_balance_snapshots"
    __table_args__ = (
        # One snapshot per account per month; the unique index also serves the
        # (realm_id, qbo_account_id, period) series lookup.
        UniqueConstraint("realm_id", "qbo_account_id", "period", name="uq_qbo_balance_snapshot"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_account_id = Column(String, nullable=False)
    period = Column(String, nullable=False)   # 'YYYY-MM'
    balance = Column(Numeric(precision=18, scale=2))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# PLAN-EXEMPT: item-4 MetricSnapshot custom reporting, ratified 2026-06-25 ("proceed with 1 and 2");
# design work/batch-6-reporting-covenants/design/20260625-addbacks-and-metric-snapshots.md.
class MetricSnapshot(Base):
    """Point-in-time snapshot of a tenant metric (AP/AR/cash/custom) for intra-month history.

    CustomKPI is definition-only and InsightKPI/InsightTrend are month-keyed; this table
    accumulates one row per (realm, metric, as_of_date) so AP/AR/blended-cash history is
    chartable across a month. ``captured_at`` (when the run happened) is kept DISTINCT from
    ``as_of_date`` (the QBO period the value represents). org-keyed under FORCE RLS; the daily
    capture writes under ``tenant_scope_for_realm`` so the org-keyed WITH CHECK passes.
    """
    __tablename__ = "metric_snapshots"
    __table_args__ = (
        UniqueConstraint("company_id", "metric", "as_of_date", name="uq_metric_snapshot"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)   # realm
    metric = Column(String, nullable=False)                   # ap_total | ar_total | cash_blended | cash:<qbo_id> | custom:<id>
    value = Column(Numeric(precision=18, scale=2))
    as_of_date = Column(Date, nullable=False)                 # the QBO period the value represents
    captured_at = Column(DateTime(timezone=True), server_default=func.now())  # when the run happened
    source = Column(String, nullable=False, default="scheduled")  # scheduled | on_demand
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class QBOVendor(Base):
    """Vendor list cache. ``UNIQUE(realm_id, qbo_id)`` is the identity, resolved by
    the database (``sync_vendors`` upserts ON CONFLICT), so overlapping syncs of one
    realm cannot insert the same vendor twice."""
    __tablename__ = "qbo_vendors"
    __table_args__ = (
        UniqueConstraint("realm_id", "qbo_id", name="uq_qbo_vendor_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_id = Column(String, nullable=False, index=True)
    
    display_name = Column(String)
    company_name = Column(String)
    email = Column(String)
    balance = Column(Numeric(precision=18, scale=2))
    active = Column(Boolean, default=True)
    
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOCustomer(Base):
    """Customer list cache; identity ``UNIQUE(realm_id, qbo_id)`` (see QBOVendor)."""
    __tablename__ = "qbo_customers"
    __table_args__ = (
        UniqueConstraint("realm_id", "qbo_id", name="uq_qbo_customer_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_id = Column(String, nullable=False, index=True)
    
    display_name = Column(String)
    company_name = Column(String)
    email = Column(String)
    balance = Column(Numeric(precision=18, scale=2))
    active = Column(Boolean, default=True)
    
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOItem(Base):
    """
    Products and Services. Identity ``UNIQUE(realm_id, qbo_id)`` (see QBOVendor).
    """
    __tablename__ = "qbo_items"
    __table_args__ = (
        UniqueConstraint("realm_id", "qbo_id", name="uq_qbo_item_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_id = Column(String, nullable=False, index=True)
    
    name = Column(String, nullable=False)
    type = Column(String)  # e.g., Service, Inventory, NonInventory
    unit_price = Column(Numeric(precision=18, scale=2))
    expense_account_ref = Column(String)
    income_account_ref = Column(String)
    active = Column(Boolean, default=True)
    
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOJournalEntry(Base):
    """
    Journal Entries. Identity ``UNIQUE(realm_id, qbo_id)`` (see QBOVendor).
    """
    __tablename__ = "qbo_journal_entries"
    __table_args__ = (
        UniqueConstraint("realm_id", "qbo_id", name="uq_qbo_journal_entry_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_id = Column(String, nullable=False, index=True)
    
    doc_number = Column(String)
    txn_date = Column(Date, nullable=False)
    total_amt = Column(Numeric(precision=18, scale=2))
    private_note = Column(Text)
    adjustment = Column(Boolean, default=False)
    
    # Store lines as JSON for full fidelity (deits/credits, descriptions, entity refs)
    lines = Column(JSON)
    
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOTransaction(Base):
    """
    Flattened transaction table for analysis (Invoices, Bills, Payments, Expenses, etc.).

    ``UNIQUE(realm_id, entity_type, qbo_id, bank_account_ref)`` is the book-row
    identity (LOCK-1), enforced by the database: ``sync_transactions`` upserts ON
    CONFLICT, so two overlapping syncs cannot write the same posting twice. Rows
    whose ``entity_type``/``bank_account_ref`` are NULL (non-QBO sources) are not
    constrained.
    """
    __tablename__ = "qbo_transactions"
    __table_args__ = (
        UniqueConstraint(
            "realm_id", "entity_type", "qbo_id", "bank_account_ref",
            name="uq_qbo_transaction_identity",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_id = Column(String, nullable=False, index=True)
    
    transaction_type = Column(String)
    date = Column(Date, nullable=False, index=True)
    amount = Column(Numeric(precision=18, scale=2))
    description = Column(Text)
    
    account_ref = Column(String, index=True)     # Classification/category account (NULL = pending)
    entity_ref = Column(String, index=True)     # Customer or Vendor ID
    # The bank/CC account this txn posted to — bank-reconciliation's filter key.
    # Distinct from account_ref (the P&L/BS classification column).
    bank_account_ref = Column(String, index=True, nullable=True)
    # QBO entity type ("Purchase"/"Deposit"/...) — LOCK-1: QBO ids are unique
    # per entity type only, so (realm_id, entity_type, qbo_id, bank_account_ref)
    # is the upsert identity, not qbo_id alone.
    entity_type = Column(String, index=True, nullable=True)

    currency_ref = Column(String)
    exchange_rate = Column(Numeric(precision=18, scale=6), default=1.0)

    # Source of transaction ingestion: "qbo", "bill_com", "manual"
    source = Column(String, default="qbo", nullable=False)
    # Resolved vendor display name (from QBOVendor or AI suggestion)
    vendor_name = Column(String, nullable=True)
    # Reconciliation: has this transaction been marked as cleared by the accountant?
    cleared = Column(Boolean, default=False)
    cleared_session_id = Column(Integer, nullable=True)  # FK to reconciliation_sessions.id

    # First-class Skip (plan 20260716-batch5-remediation AC-C3): a human dismissed
    # this transaction from the classification Needs-Review queue. Stamped by
    # POST /classify/reject unconditionally — with or without a prediction row
    # (the pre-fix Skip 200'd having written nothing the list reads). NOT stamped
    # by correct-and-teach (a corrected row is categorized, not skipped). The four
    # classification list/count/generation/sweep queries filter IS NULL on it.
    classification_dismissed_at = Column(DateTime(timezone=True), nullable=True)

    # Set when QBO stopped returning this posting (deleted, voided out of the bank
    # account, or re-pointed to another account) but the row could not be deleted
    # because a reconciliation already cleared it. Uncleared rows are deleted
    # outright, so the bank-recon candidate/outstanding readers never see them.
    qbo_removed_at = Column(DateTime(timezone=True), nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOSearchRecord(Base):
    """
    Normalized, deterministic search index for QuickBooks entities.

    The assistant can ask this table questions, but all finance records returned
    from search must originate here rather than from generated text.
    """
    __tablename__ = "qbo_search_records"
    __table_args__ = (
        UniqueConstraint(
            "realm_id",
            "entity_type",
            "qbo_id",
            name="uq_qbo_search_record_identity",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    entity_type = Column(String, index=True, nullable=False)
    qbo_id = Column(String, nullable=False, index=True)

    doc_number = Column(String, nullable=True, index=True)
    display_name = Column(String, nullable=True, index=True)
    txn_date = Column(Date, nullable=True, index=True)
    due_date = Column(Date, nullable=True, index=True)
    amount = Column(Numeric(precision=18, scale=2), nullable=True, index=True)
    open_balance = Column(Numeric(precision=18, scale=2), nullable=True)
    status = Column(String, nullable=True, index=True)

    customer_id = Column(String, nullable=True, index=True)
    customer_name = Column(String, nullable=True, index=True)
    vendor_id = Column(String, nullable=True, index=True)
    vendor_name = Column(String, nullable=True, index=True)
    account_id = Column(String, nullable=True, index=True)
    account_name = Column(String, nullable=True, index=True)
    class_id = Column(String, nullable=True, index=True)
    class_name = Column(String, nullable=True, index=True)
    location_id = Column(String, nullable=True, index=True)
    location_name = Column(String, nullable=True, index=True)

    memo = Column(Text, nullable=True)
    search_text = Column(Text, nullable=False, default="")
    raw_data = Column(JSON, nullable=True)

    source_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QBOReport(Base):
    """
    Stores JSON snapshots of QBO reports (P&L, Balance Sheet, etc.)
    """
    __tablename__ = "qbo_reports"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    
    report_name = Column(String, nullable=False, index=True) # ProfitAndLoss, BalanceSheet
    
    start_date = Column(Date)
    end_date = Column(Date)
    as_of_date = Column(Date) # For BS
    
    data = Column(Text, nullable=False) # JSON string
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class QBOGLLine(Base):
    """
    Normalized General Ledger transaction lines parsed from QBO reports.

    This gives assistant and drilldown workflows a fast local path instead of
    repeatedly fetching and parsing large GeneralLedger report JSON payloads.

    Identity is one row per POSTING. The report legitimately carries several
    postings that agree on every content column (two same-day $500 deposits with
    no doc/name/memo), so ``posting_ordinal`` numbers the repeats within one
    report (0, 1, ...). The ordinal depends only on content, never on report
    position, so the same posting fetched by two overlapping reports gets the
    same identity and a concurrent double insert still collides.
    """
    __tablename__ = "qbo_gl_lines"
    __table_args__ = (
        UniqueConstraint(
            "realm_id",
            "account_id",
            "txn_date",
            "transaction_type",
            "doc_number",
            "name",
            "memo",
            "amount",
            "posting_ordinal",
            name="uq_qbo_gl_line_posting",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    account_id = Column(String, nullable=True, index=True)
    account_name = Column(String, nullable=True, index=True)

    txn_date = Column(Date, nullable=False, index=True)
    transaction_type = Column(String, nullable=True, index=True)
    doc_number = Column(String, nullable=True, index=True)
    name = Column(String, nullable=True, index=True)
    memo = Column(Text, nullable=True)
    split = Column(String, nullable=True)

    amount = Column(Numeric(precision=18, scale=2), nullable=False, default=0)
    balance = Column(Numeric(precision=18, scale=2), nullable=True)

    # Position of this line within the source QBO GeneralLedger report (0-based
    # enumeration order of the report's data rows). QBO's register order for
    # same-day transactions is only recoverable from this position, so the
    # drill-down orders by (txn_date, report_row_index) to reproduce the register.
    # Nullable: rows cached before this column existed carry NULL and fall back to
    # deterministic id order (NULLS LAST). Plan 20260701-pilot-fixes-batch-3 AC-1.
    report_row_index = Column(Integer, nullable=True, index=True)
    # Nth repeat (0-based) of an identical content tuple within one report; see the
    # class docstring. Rows cached before the column existed are ordinal 0.
    posting_ordinal = Column(Integer, nullable=False, default=0, server_default="0")
    # The QBO transaction id the GL report carries on the transaction-type cell.
    qbo_txn_id = Column(String, nullable=True)

    raw_data = Column(JSON, nullable=True)
    source_start_date = Column(Date, nullable=True, index=True)
    source_end_date = Column(Date, nullable=True, index=True)
    source_synced_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)


class ChatConversation(Base):
    """
    Groups chat messages for a specific company (realm_id).
    """
    __tablename__ = "chat_conversations"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    user_id = Column(Integer, index=True, nullable=True)
    title = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChatMessage(Base):
    """
    Individual messages within a conversation.
    """
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True, nullable=False) # ForeignKey('chat_conversations.id')
    role = Column(String, nullable=False) # "user" or "assistant"
    content = Column(Text, nullable=False)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """
    Tracks all write operations (Create, Update, Delete) for compliance.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    action = Column(String, nullable=False) # CREATE, UPDATE, DELETE
    
    entity_type = Column(String, nullable=False) # JournalEntry, Transaction
    entity_id = Column(String, nullable=True) # QBO Id
    
    user_id = Column(String, nullable=True) # If we haveauthUser
    details = Column(JSON, nullable=True) # Snapshot of change


class ClassificationRule(Base):
    """
    User-defined rules for deterministic categorization.
    e.g. If description contains "UBER", assign to "Travel" (confidence 1.0)
    """
    __tablename__ = "classification_rules"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    
    keyword = Column(String, nullable=False) # e.g. "starbucks"
    target_account_id = Column(String, nullable=False) # QBO Id of the expense account
    target_account_name = Column(String) # For readability
    
    priority = Column(Integer, default=0) # Higher wins
    active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())



class ReconciliationMatch(Base):
    """
    Stores potential matches between a transaction and an existing QBO ledger entry.
    Used to prevent duplicates and link bank feed items to manual entries.
    """
    __tablename__ = "reconciliation_matches"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    # AC-0.2 Decision: relax bank_txn_id to nullable + overload it as a
    # bank_feed_items.id reference for statement-source matches.
    # Reasoning: a statement-upload line has no QBO BankTransaction.Id, so a
    # NOT-NULL QBO-id column cannot represent a statement match; SYNTHESIS §2B
    # reuses ReconciliationMatch for the local-plane 3-way match, and
    # bank_feed_items.id is the natural local handle for a statement row already
    # in that table (extend-not-clone). Kept a plain nullable String (NOT an FK)
    # per the no-cross-FORCE-RLS-FK guidance (DEC-BREC / §2B). Adding a distinct
    # second nullable column would fork one semantic role across two half-empty
    # id columns. Confidence: 88%.
    # For a QBO-feed match this is the QBO ID of a synced/imported transaction;
    # for a statement_upload match it is the str(bank_feed_items.id) of the line.
    bank_txn_id = Column(String, nullable=True, index=True)
    # 'ledger_txn_id' is the QBO ID of an existing manual transaction we are matching against.
    ledger_txn_id = Column(String, nullable=False, index=True)

    # AC-M2 identity columns (plan 20260716-batch5-remediation, migration
    # c9f4b2e7a1d6): the keyed identity of one confirmed pairing.
    # - `session_id` — the ReconciliationSession.id that confirmed this pair.
    # - `book_txn_local_id` — the LOCAL qbo_transactions.id of the book leg.
    # Together they make confirm/unconfirm/release lookups KEYED (killing the
    # `.first()` ambiguity when duplicate qbo_ids share one ledger_txn_id
    # string) and let the group-aware matcher PIN already-confirmed pairs
    # instead of re-deriving them. Plain nullable Integers, deliberately NOT
    # ForeignKeys (the AC-0.2 no-cross-FORCE-RLS-FK guidance above); NULL =
    # a legacy row confirmed before these columns existed (backfilled on the
    # next confirm touch).
    session_id = Column(Integer, nullable=True, index=True)
    book_txn_local_id = Column(Integer, nullable=True, index=True)

    match_score = Column(Integer) # 0-100
    match_type = Column(String) # EXACT, EXACT_GROUP, FUZZY_DATE
    status = Column(String, default="PENDING") # PENDING, APPROVED, REJECTED
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ReconciliationSession(Base):
    """
    Tracks a bank reconciliation session for a specific account and period.
    The accountant enters the ending balance from their bank statement, then
    ticks off transactions as 'cleared' until the difference reaches $0.
    """
    __tablename__ = "reconciliation_sessions"
    # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
    # One LIVE reconciliation per (realm, account, period): the create path is
    # idempotent (session_service.create_session returns the existing row on a
    # duplicate submit), and this partial unique index is the DB backstop that
    # makes a concurrent double-submit fail closed instead of forking two
    # sessions for the same statement period (the duplicate-session defect).
    # ABANDONED rows are excluded so an abandoned reconciliation (kept for
    # audit — B5-2) never blocks a fresh start on the same statement period.
    __table_args__ = (
        Index(
            "uq_reconciliation_session_period",
            "realm_id",
            "account_id",
            "period_start",
            "period_end",
            unique=True,
            postgresql_where=text("coalesce(status, 'OPEN') != 'ABANDONED'"),
            sqlite_where=text("coalesce(status, 'OPEN') != 'ABANDONED'"),
        ),
        Index(
            "ux_reconciliation_session_id_realm",
            "id",
            "realm_id",
            unique=True,
        ),
        UniqueConstraint(
            "id",
            "realm_id",
            "statement_import_id",
            name="uq_recon_session_import_identity",
        ),
        ForeignKeyConstraint(
            ["statement_import_id", "realm_id"],
            ["bank_statement_imports.id", "bank_statement_imports.realm_id"],
            name="fk_recon_session_statement_import_realm",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "lineage_status IN ('controlled', 'legacy_snapshot')",
            name="ck_recon_session_lineage_status",
        ),
        CheckConstraint(
            "(lineage_status = 'legacy_snapshot' AND statement_import_id IS NULL) OR "
            "(lineage_status = 'controlled' AND statement_import_id IS NOT NULL)",
            name="ck_recon_session_controlled_import",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    # NULL is historical-only. New sessions bind to one passed statement import;
    # the migration freezes open legacy sessions as ``legacy_snapshot`` instead
    # of fabricating an import identity.
    statement_import_id = Column(Integer, nullable=True, index=True)
    lineage_status = Column(
        String,
        nullable=False,
        server_default="legacy_snapshot",
    )

    account_id = Column(String, nullable=False, index=True)   # QBO Account ID
    account_name = Column(String, nullable=False)

    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)

    # The ending balance from the bank statement (entered by the accountant)
    ending_balance = Column(Numeric(precision=18, scale=2), nullable=False)
    # Running total of cleared transactions — updated on each clear/unclear action
    cleared_balance = Column(Numeric(precision=18, scale=2), default=0)
    # Opening balance SNAPSHOT stamped by try_complete at completion (B5-6): the
    # clean-start anchor rolls FORWARD to this rec's ending on complete, so a
    # completed session recomputing its tie-out against the LIVE anchor would show
    # opening == its own ending (out-of-balance nonsense). NULL on legacy/OPEN rows
    # (OPEN reads the live anchor; legacy COMPLETE derives opening from the stamped
    # cleared_balance − signed cleared sum).
    opening_balance = Column(Numeric(precision=18, scale=2), nullable=True)

    status = Column(
        String,
        nullable=False,
        default="OPEN",
        server_default="OPEN",
    )  # OPEN | COMPLETE | ABANDONED

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # PDF certificate generated after successful reconciliation
    certificate_path = Column(String, nullable=True)

class MonthlyFinancials(Base):
    """
    aggregated financial data for a specific month and account.
    Used for quick reporting and trend analysis.
    """
    __tablename__ = "monthly_financials"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    
    month_start = Column(Date, nullable=False, index=True) # e.g. 2025-01-01
    account_id = Column(String, nullable=False, index=True)
    
    total_debit = Column(Numeric(precision=18, scale=2), default=0)
    total_credit = Column(Numeric(precision=18, scale=2), default=0)
    net_change = Column(Numeric(precision=18, scale=2), default=0)
    ending_balance = Column(Numeric(precision=18, scale=2), default=0)
    
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class VendorProfile(Base):
    """
    Statistical profile of a vendor's behavior.
    Used for anomaly detection, classification confidence, and learning.
    """
    __tablename__ = "vendor_profiles"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    vendor_name = Column(String, nullable=False, index=True)  # Normalized name
    qbo_vendor_id = Column(String, nullable=True)  # Link to QBO Vendor if known

    avg_monthly_spend = Column(Numeric(precision=18, scale=2))
    std_dev_amount = Column(Numeric(precision=18, scale=2))  # Spend variability
    frequency = Column(String)  # WEEKLY, MONTHLY, SPORADIC
    last_interaction_date = Column(Date)

    typical_account_id = Column(String)  # The account most often used
    interaction_count = Column(Integer, default=0)

    # Learning engine extensions
    common_categories = Column(JSON, nullable=True)   # [{"account_id": "...", "pct": 0.8}]
    seasonality = Column(JSON, nullable=True)          # {"peak_months": [11, 12], "trough": [6, 7]}
    risk_flags = Column(JSON, nullable=True)            # ["new_vendor", "spend_spike", "category_shift"]
    expected_frequency = Column(String, nullable=True)  # DAILY/WEEKLY/MONTHLY/QUARTERLY/ANNUAL

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SystemSettings(Base):
    """
    Configuration for the Autonomy/Autopilot engine per company.
    """
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, unique=True, index=True, nullable=False)

    # NOTE: auto_classify_enabled was dropped (migration s0t1u2v3w4x5) — it keyed
    # the legacy run_autonomy_cycle auto-classify path that bypassed the gated
    # autonomy framework. Auto-execution now lives entirely in TenantTaskAutonomy
    # + the calibration/gate/circuit-breaker stack.
    auto_match_enabled = Column(Boolean, default=False)

    min_confidence_score = Column(Numeric(precision=3, scale=2), default=0.90) # 0.90
    
    # AI Model Configuration
    default_model = Column(String, default="gpt-4o")
    fast_model = Column(String, default="gpt-4o-mini")

    # Email / notification config
    email_approver = Column(String, nullable=True)       # Recipient for approval emails
    email_smtp_host = Column(String, nullable=True)
    email_smtp_port = Column(Integer, nullable=True, default=587)
    email_smtp_user = Column(String, nullable=True)
    email_smtp_pass = Column(String, nullable=True)
    slack_webhook_url = Column(String, nullable=True)    # Optional Slack webhook for digests
    
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FPAWorkbenchSession(Base):
    """Durable interview state owned by an authenticated company and organization."""
    __tablename__ = "fpa_workbench_sessions"

    id = Column(String(36), primary_key=True)
    company_id = Column(String, nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id"), nullable=False, index=True)
    created_by_user_id = Column(Integer, nullable=False)
    state_schema_version = Column(Integer, nullable=False, default=1, server_default="1")
    state_json = Column(JSON, nullable=False)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProposedAction(Base):
    """
    Stores actions proposed by the agent for human review before execution.
    Serves as the unified approval inbox for AI-led workflows.
    """
    __tablename__ = "proposed_actions"
    __table_args__ = (
        # Phase-B close-orchestration dedupe: a given close task + the same input
        # fingerprint yields at most one proposal. NULL close_task_id rows are NOT
        # deduped (Postgres treats NULLs as distinct), so only close-orchestration
        # proposals collide — chat/proactive proposals (close_task_id IS NULL) are
        # untouched. This is the intended scope (DEC-CAR-B-5, AC-CR-2b).
        UniqueConstraint(
            "close_task_id",
            "inputs_fingerprint",
            name="uq_proposed_action_closetask_fingerprint",
        ),
        # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave3-1-agent-plans (marker
        # RATIFICATION-2026-07-14.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent).
        # Wave-3.1 agent-plan dedupe (plan 20260714-wave3-1-agent-plans, decisions
        # 1/4): a given plan step + the same input fingerprint yields at most one
        # proposal — the redelivery double-draft class closed for plan steps exactly
        # as the close lane closed it above. NULL plan_step_id rows are NOT deduped
        # (NULLs are distinct in a UNIQUE), so chat/proactive/close proposals are
        # untouched; every walker-drafted proposal MUST therefore carry its
        # plan_step_id or it silently loses this dedup.
        UniqueConstraint(
            "plan_step_id",
            "inputs_fingerprint",
            name="uq_proposed_action_planstep_fingerprint",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False) # realm_id
    action_type = Column(String, nullable=False) # JOURNAL_ENTRY, RECLASSIFICATION, VENDOR_UPDATE
    payload = Column(JSON, nullable=False)
    # Retained audit identities deliberately have no deletion-to-NULL foreign key.
    # New trusted ORM producers stamp version 1; old/raw writers remain unknown.
    human_preparer_user_id = Column(Integer, nullable=True)
    last_human_editor_user_id = Column(Integer, nullable=True)
    provenance_version = Column(Integer, nullable=False, default=1, server_default="0")
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    reasoning = Column(Text)
    confidence_score = Column(Numeric(precision=3, scale=2)) # e.g. 0.95
    status = Column(String, default="PENDING", index=True) # PENDING, APPROVED, REJECTED, EXECUTED, FAILED
    execution_result = Column(JSON)

    # AI-led platform fields
    source = Column(String, default="ai_assistant")  # ai_assistant, ai_proactive, system
    category = Column(String, nullable=True)  # budget_update, journal_entry, reforecast, alert, report_ready
    priority = Column(String, default="medium")  # low, medium, high, critical
    expires_at = Column(DateTime(timezone=True), nullable=True)  # Auto-dismiss stale proposals

    # Phase-B close-orchestration step-link fields (DEC-CAR-B-5). All nullable:
    # a chat/proactive proposal has none of these set; a close-orchestration
    # proposal links back to the close task + agent run that produced it, carries
    # the step index within the close run, and a fingerprint of its inputs for dedupe.
    close_task_id = Column(Integer, ForeignKey("close_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    step_index = Column(Integer, nullable=True)
    inputs_fingerprint = Column(String, nullable=True, index=True)

    # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md on disk). Wave-3.1 agent-plan step linkage
    # (decisions 1/4): a walker-drafted proposal links back to the AgentPlanStep
    # that produced it; paired with uq(plan_step_id, inputs_fingerprint) above so
    # a resumed walk adopts (find-or-create) instead of double-drafting. Nullable:
    # chat/proactive/close proposals never set it.
    plan_step_id = Column(Integer, ForeignKey("agent_plan_steps.id", ondelete="SET NULL"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    reviewed_by = Column(String, nullable=True) # User ID or email
    reviewed_at = Column(DateTime(timezone=True), nullable=True)


class QBOActionExecution(Base):
    """
    Durable execution ledger for QBO write-backs created from approved actions.
    The idempotency key prevents duplicate provider side effects for the same
    approved action payload.
    """
    __tablename__ = "qbo_action_executions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_qbo_action_executions_idempotency_key"),
        Index("ix_qbo_action_executions_action_company", "action_id", "company_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    action_id = Column(Integer, ForeignKey("proposed_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    company_id = Column(String, nullable=False, index=True)
    qbo_realm_id = Column(String, nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    action_type = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    provider_request_id = Column(String, nullable=True, index=True)
    before_state_json = Column(JSON, nullable=True)
    after_state_json = Column(JSON, nullable=True)
    request_summary_json = Column(JSON, nullable=True)
    response_summary_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R5 (round-2, PR #381 remediation row):
# the internal-DB approval-execution lane previously had NO idempotency ledger (QBO lane only) —
# under Celery acks_late at-least-once redelivery / a double-click, protection was only the F-07
# claim + per-handler guards. This mirrors QBOActionExecution for the 13-type internal-DB write
# class: SHA-256 key over {action_id, company_id, action_type, canonical tool_input}, unique-
# constrained, race-safe claim, replay-from-ledger on retry. RLS: org-keyed policy added in
# migration c4d7e2f9a1b3 (ENABLE+FORCE, two-GUC pattern), matching qbo_action_executions.
class InternalActionExecution(Base):
    """Durable execution ledger for internal-DB writes executed from approved actions."""
    __tablename__ = "internal_action_executions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_internal_action_executions_idempotency_key"),
        Index("ix_internal_action_executions_action_company", "action_id", "company_id"),
        UniqueConstraint("company_id", "active_intent_key", name="uq_internal_action_active_intent"),
        UniqueConstraint("id", "company_id", name="uq_internal_execution_id_company"),
    )

    id = Column(Integer, primary_key=True, index=True)
    action_id = Column(Integer, ForeignKey("proposed_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    company_id = Column(String, nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    action_type = Column(String, nullable=False, index=True)
    tool_name = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending", index=True)  # pending|running|succeeded|not_applied|recovery_required; legacy states retained
    protocol_version = Column(Integer, nullable=False, default=2, server_default="1")
    intent_fingerprint = Column(String(64), nullable=True)
    active_intent_key = Column(String(64), nullable=True)
    owner_claim_token = Column(String(36), nullable=True)
    proposal_revision = Column(Integer, nullable=True)
    approval_fingerprint = Column(String(64), nullable=True)
    effect_contract = Column(String(40), nullable=True)
    source_snapshot_json = Column(JSON, nullable=True)
    source_snapshot_digest = Column(String(64), nullable=True)
    effect_revoked_at = Column(DateTime(timezone=True), nullable=True)
    effect_revoked_by_user_id = Column(Integer, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    result_message = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class InternalActionEffectReceipt(Base):
    """Immutable creation evidence; a deleted soft target never erases this record."""
    __tablename__ = "internal_action_effect_receipts"
    __table_args__ = (
        UniqueConstraint("execution_id", "company_id", name="uq_internal_effect_execution"),
        ForeignKeyConstraint(["execution_id", "company_id"],
                             ["internal_action_executions.id", "internal_action_executions.company_id"],
                             name="fk_internal_effect_execution_scope"),
        CheckConstraint("effect_contract = 'schedule-create/v1'", name="ck_internal_effect_contract"),
    )
    id = Column(Integer, primary_key=True)
    execution_id = Column(Integer, nullable=False)
    action_id = Column(Integer, nullable=False)
    company_id = Column(String, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False, index=True)
    organization_company_id = Column(Integer, nullable=False)
    effect_contract = Column(String(40), nullable=False)
    source_snapshot_digest = Column(String(64), nullable=False)
    intent_fingerprint = Column(String(64), nullable=False)
    approval_fingerprint = Column(String(64), nullable=False)
    proposal_revision = Column(Integer, nullable=False)
    target_schedule_id = Column(Integer, nullable=False)  # retained soft identity
    effect_snapshot_json = Column(JSON, nullable=False)
    effect_snapshot_digest = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InternalActionRecoveryCase(Base):
    """Append-once candidate and conditional append-once two-person decision."""
    __tablename__ = "internal_action_recovery_cases"
    __table_args__ = (
        UniqueConstraint("company_id", "active_execution_id", name="uq_internal_recovery_active"),
        UniqueConstraint("candidate_nonce", name="uq_internal_recovery_nonce"),
        ForeignKeyConstraint(["execution_id", "company_id"],
                             ["internal_action_executions.id", "internal_action_executions.company_id"],
                             name="fk_internal_recovery_execution_scope"),
        CheckConstraint("effect_contract = 'schedule-create/v1'", name="ck_internal_recovery_contract"),
        CheckConstraint("finding IN ('applied', 'not_applied')", name="ck_internal_recovery_finding"),
        CheckConstraint("decision IS NULL OR decision IN ('confirmed_applied', 'confirmed_not_applied', 'rejected')", name="ck_internal_recovery_decision"),
        CheckConstraint("(decision IS NULL AND decision_user_id IS NULL AND decided_at IS NULL AND decision_digest IS NULL AND active_execution_id IS NOT NULL AND active_execution_id = execution_id) OR (decision IS NOT NULL AND decision_user_id IS NOT NULL AND decided_at IS NOT NULL AND decision_digest IS NOT NULL AND active_execution_id IS NULL)", name="ck_internal_recovery_terminal"),
    )
    id = Column(Integer, primary_key=True)
    execution_id = Column(Integer, nullable=False)
    action_id = Column(Integer, nullable=False)
    company_id = Column(String, nullable=False, index=True)
    organization_id = Column(Integer, nullable=False, index=True)
    organization_company_id = Column(Integer, nullable=False)
    active_execution_id = Column(Integer, nullable=True)
    candidate_nonce = Column(String(36), nullable=False)
    preparer_user_id = Column(Integer, nullable=False)
    prepared_at = Column(DateTime(timezone=True), nullable=False)
    effect_contract = Column(String(40), nullable=False)
    source_snapshot_digest = Column(String(64), nullable=False)
    approval_fingerprint = Column(String(64), nullable=False)
    proposal_revision = Column(Integer, nullable=False)
    finding = Column(String(20), nullable=False)
    receipt_id = Column(Integer, nullable=True)
    receipt_digest = Column(String(64), nullable=True)
    target_observation_digest = Column(String(64), nullable=True)
    owner_token_digest = Column(String(64), nullable=False)
    fenced_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(500), nullable=False)
    evidence_locator = Column(String(200), nullable=False)
    candidate_digest = Column(String(64), nullable=False)
    decision = Column(String(32), nullable=True)
    decision_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_digest = Column(String(64), nullable=True)


class ApprovalToken(Base):
    """
    Single-use approval/rejection link issued for a ProposedAction recipient.
    Only a hash of the bearer secret is stored.
    """
    __tablename__ = "approval_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String, unique=True, index=True, nullable=False)
    action_id = Column(Integer, ForeignKey("proposed_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    proposal_revision = Column(Integer, nullable=True)
    company_id = Column(String, index=True, nullable=False)
    decision = Column(String, nullable=False)
    recipient_email = Column(String, nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    used_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentConfirmationToken(Base):
    """
    Short-lived, single-use token that lets an authenticated user confirm one
    risky assistant tool for one scoped agent run.
    """
    __tablename__ = "agent_confirmation_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id = Column(String, index=True, nullable=False)
    tenant_id = Column(String, nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"), nullable=True, index=True)
    tool_name = Column(String, nullable=False, index=True)
    action_fingerprint = Column(String, nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    used_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentObjective(Base):
    """
    User or system-defined recurring objective for the agent runtime.
    Examples: morning briefing, cash watch, budget variance watch.
    """
    __tablename__ = "agent_objectives"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    name = Column(String, nullable=False)
    objective_type = Column(String, nullable=False, index=True)
    instructions = Column(Text, nullable=True)
    schedule = Column(JSON, nullable=True)
    enabled = Column(Boolean, default=True, index=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    next_run_at = Column(DateTime(timezone=True), nullable=True, index=True)
    permission_scope = Column(JSON, nullable=True)
    approval_policy = Column(JSON, nullable=True)
    notification_policy = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AgentRun(Base):
    """
    Durable trace of a single agent execution, whether triggered by chat,
    a schedule, a webhook, or an internal system job.
    """
    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    objective_id = Column(Integer, ForeignKey("agent_objectives.id", ondelete="SET NULL"), nullable=True, index=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id", ondelete="SET NULL"), nullable=True, index=True)

    trigger_type = Column(String, nullable=False, default="chat", index=True)
    status = Column(String, nullable=False, default="queued", index=True)
    model_provider = Column(String, nullable=True)
    model_name = Column(String, nullable=True)
    prompt_version = Column(String, nullable=True)

    input_summary = Column(Text, nullable=True)
    output_summary = Column(Text, nullable=True)
    token_usage = Column(JSON, nullable=True)
    cost_estimate = Column(Numeric(precision=12, scale=6), nullable=True)
    error = Column(Text, nullable=True)
    permission_scope = Column(JSON, nullable=True)
    request_id = Column(String, nullable=True, index=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AgentObjectiveOccurrence(Base):
    """Durable generic-run identity; unresolved claims are never automatically replayed."""
    __tablename__ = "agent_objective_occurrences"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, nullable=False, index=True)
    organization_company_id = Column(Integer, nullable=False, index=True)
    company_id = Column(String, nullable=False, index=True)
    objective_id = Column(Integer, ForeignKey("agent_objectives.id", ondelete="SET NULL"), nullable=True, index=True)
    source_objective_id = Column(Integer, nullable=False)
    identity_key = Column(String(160), nullable=False)
    trigger_type = Column(String(16), nullable=False)
    due_at = Column(DateTime(timezone=True), nullable=True)
    schedule_snapshot = Column(JSON, nullable=False)
    schedule_fingerprint = Column(String(64), nullable=False)
    enabled_snapshot = Column(Boolean, nullable=False)
    objective_snapshot = Column(JSON, nullable=False)
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="pending", server_default="pending")
    dispatch_status = Column(String(24), nullable=False, default="pending", server_default="pending")
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    claim_token = Column(String(36), nullable=True)
    publish_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    started_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    outcome = Column(JSON, nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id", "company_id"],
            ["organization_companies.id", "organization_companies.organization_id", "organization_companies.realm_id"],
            name="fk_objective_occurrence_scope"),
        UniqueConstraint("source_objective_id", "identity_key", name="uq_objective_occurrence_identity"),
        CheckConstraint("status IN ('pending','running','recovery_required','succeeded','partial','failed')", name="ck_objective_occurrence_status"),
        CheckConstraint("dispatch_status IN ('pending','published','manual')", name="ck_objective_occurrence_dispatch"),
        CheckConstraint("trigger_type IN ('schedule','manual')", name="ck_objective_occurrence_trigger"),
        CheckConstraint("revision >= 1 AND attempts >= 0 AND publish_attempts >= 0", name="ck_objective_occurrence_counters"),
        Index("uq_objective_occurrence_active", "source_objective_id", unique=True,
            postgresql_where=text("status IN ('pending','running','recovery_required')"),
            sqlite_where=text("status IN ('pending','running','recovery_required')")),
    )


class BriefingDelivery(Base):
    """Daily notification evidence. Sending/uncertain records prevent automatic resend."""
    __tablename__ = "briefing_deliveries"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, nullable=False, index=True)
    organization_company_id = Column(Integer, nullable=False, index=True)
    company_id = Column(String, nullable=False, index=True)
    briefing_id = Column(Integer, ForeignKey("executive_briefings.id", ondelete="SET NULL"), nullable=True)
    delivery_date = Column(Date, nullable=False)
    kind = Column(String(32), nullable=False, default="morning_email", server_default="morning_email")
    content_digest = Column(String(64), nullable=False)
    recipient_digest = Column(String(64), nullable=False)
    message_id = Column(String(128), nullable=False)
    status = Column(String(24), nullable=False, default="ready", server_default="ready")
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    claim_token = Column(String(36), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reason = Column(String(80), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(["organization_company_id", "organization_id", "company_id"],
            ["organization_companies.id", "organization_companies.organization_id", "organization_companies.realm_id"],
            name="fk_briefing_delivery_scope"),
        UniqueConstraint("organization_id", "organization_company_id", "company_id", "delivery_date", "kind", name="uq_briefing_delivery_day"),
        CheckConstraint("status IN ('ready','sending','sent','partial','retryable','failed','uncertain')", name="ck_briefing_delivery_status"),
        CheckConstraint("revision >= 1 AND attempts >= 0 AND attempts <= 3", name="ck_briefing_delivery_counters"),
    )


class AgentStep(Base):
    """
    Fine-grained trace step within an AgentRun: model call, tool call,
    proposal creation, approval wait, notification, or eval result.
    """
    __tablename__ = "agent_steps"

    id = Column(Integer, primary_key=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)

    step_type = Column(String, nullable=False, index=True)
    tool_name = Column(String, nullable=True, index=True)
    input = Column(JSON, nullable=True)
    output = Column(JSON, nullable=True)
    status = Column(String, nullable=False, default="running", index=True)
    latency_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AgentEpisode(Base):
    """Distilled episodic memory of a completed agent run (LG-4, Wave 3.3).

    A post-run, template-assembled summary of a terminal-SUCCESS ``AgentRun`` —
    investigated (``input_summary``) / concluded (``output_summary``) / evidence
    (succeeded tool-step names) — embedded for similarity recall at a FUTURE run's
    start. It is MODEL-generated content, so it re-enters context ONLY through the
    ``wrap_untrusted`` fence and is NEVER authoritative: it informs the agent's
    reasoning, it never unlocks a tool, expands scope, confirms a proposal, or
    drives a write (every side effect stays proposal-gated — AWB-06).

    A SEPARATE table (NOT a ``CompanyKnowledge`` scope), mirroring the shipping
    ``AICorrectionEvent`` precedent: the Memory-v2 scope-validation would RAISE on
    an episode and the consolidation beat would MERGE distinct episodes. Org-keyed
    FORCE-RLS (migration ``b2f9d4a7c1e8``); ``UNIQUE(agent_run_id)`` is the
    idempotent one-episode-per-run dedup, ``ON DELETE SET NULL`` so the episode (the
    durable memory) survives run-trace pruning.
    """
    __tablename__ = "agent_episodes"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    trigger_type = Column(String, nullable=True)  # copied from AgentRun (chat|schedule|webhook) for relevance

    content = Column(Text, nullable=False)  # the template-assembled honest paragraph (embedded + retrievable)
    # pgvector store — same pinned space as CompanyKnowledge (text-embedding-3-small,
    # 1536). PG gets a real Vector column (HNSW vector_cosine_ops index in migration
    # b2f9d4a7c1e8 + the squashed baseline); the SQLite test dialect keeps JSON
    # (none_as_null=True so a None embedding is SQL NULL, matching PG, for the
    # retrieval's `.isnot(None)` filter).
    embedding_vector = Column(Vector(1536).with_variant(JSON(none_as_null=True), "sqlite"), nullable=True)  # VECTOR-OK: sqlite-test dialect json fallback; PG is Vector(1536)

    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)  # decay/review horizon

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_agent_episodes_agent_run_id"),
    )


class ToolFailureMemory(Base):
    """Per-(org, realm, tool, error_class) tool-failure health aggregate (LG-5,
    Wave 3.3).

    A mutable UPSERT aggregate — NOT an append-only per-call log. On a tool RUNTIME
    failure the ``ToolExecutor.execute`` catch upserts this row (increment
    ``failure_count``, slide the ``expires_at`` TTL, refresh the redacted error);
    on that tool's next SUCCESS the realm's open row is soft-resolved
    (``resolved_at``). Retrieval surfaces only ``resolved_at IS NULL AND
    expires_at > now`` — so a transient failure decays (TTL) and a fixed tool
    clears (success), never becoming a permanent "broken" caveat.

    It feeds ONE always-on, allowed-tools-filtered CAVEAT the model reads — code-
    controlled fields ONLY. ``last_error_redacted`` is stored for the dashboard/
    audit lane and is NEVER projected into the prompt caveat (``redact_to_string``
    strips PII but does not neutralize ``<system>``/injection tags, and the caveat
    sits in the TRUSTED, UNFENCED request block — E1/E-D). The caveat NEVER
    deselects a tool: it informs, it never gates selection (AWB-06 — every side
    effect stays proposal-gated).

    Org-keyed FORCE-RLS (migration ``c9e2b7f4a1d6``), keyed on the ``AICorrectionEvent``
    org-triple scope shape (``organization_id`` NOT NULL so a NULL-org orphan is
    never stored — the record helper skips when the org can't be resolved).
    ``error_class`` reuses the ``classify_error`` closed vocab (llm_usage.py).
    """
    __tablename__ = "tool_failure_memory"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    tool_name = Column(String, nullable=False, index=True)
    error_class = Column(String, nullable=False)  # classify_error closed vocab (llm_usage.py)

    failure_count = Column(Integer, nullable=False, default=1)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True, index=True)  # soft-resolve on next success
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)   # sliding-window TTL

    # Stored for the dashboard/audit lane ONLY — NEVER projected into the prompt
    # caveat (E1/E-D): redact_to_string strips PII but not injection role tags, and
    # the caveat is the TRUSTED, UNFENCED block.
    last_error_redacted = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "company_id", "tool_name", "error_class",
            name="uq_tool_failure_memory_scope",
        ),
    )


class InsightArtifact(Base):
    """
    Structured, source-backed insight generated by the agent runtime.
    This is the durable counterpart to narrative-only AI summaries.
    """
    __tablename__ = "insight_artifacts"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)

    title = Column(String, nullable=False)
    summary = Column(Text, nullable=True)
    severity = Column(String, default="info", index=True)
    metric = Column(String, nullable=True, index=True)
    period = Column(String, nullable=True, index=True)

    source_refs = Column(JSON, nullable=True)
    evidence = Column(JSON, nullable=True)
    recommendations = Column(JSON, nullable=True)
    confidence = Column(Numeric(precision=3, scale=2), nullable=True)
    data_freshness_at = Column(DateTime(timezone=True), nullable=True)

    status = Column(String, default="open", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ExecutiveBriefing(Base):
    """
    Pre-computed executive summary for a company, updated nightly.
    Provides rich context to the AI assistant without live computation.
    """
    __tablename__ = "executive_briefings"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    briefing_date = Column(Date, nullable=False, index=True)
    
    # Structured financial data
    financial_position = Column(JSON, nullable=True)  # KPIs, ratios, cash position
    key_risks = Column(JSON, nullable=True)  # Concentration, runway, covenant breaches
    pending_actions_count = Column(Integer, default=0)
    open_anomalies_count = Column(Integer, default=0)
    
    # LLM-generated narrative summary
    narrative = Column(Text, nullable=True)  # 500-word AI-generated briefing
    
    data_freshness_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WatchdogAlert(Base):
    """
    Proactive watchdog alert — created by the nightly intelligence loop
    when critical financial conditions are detected.

    Alert types: COVENANT_BREACH, NEGATIVE_CASH, LOW_RUNWAY,
                 LARGE_TRANSACTION, AR_OVERDUE_SPIKE, REVENUE_DROP
    """
    __tablename__ = "watchdog_alerts"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    alert_type = Column(String, nullable=False, index=True)
    severity = Column(String, default="warning")         # warning | critical
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    # What triggered the alert
    metric_value = Column(Numeric(precision=18, scale=4), nullable=True)
    threshold_value = Column(Numeric(precision=18, scale=4), nullable=True)
    # How metric_value/threshold_value should be RENDERED: "percent" (a % variance/ratio),
    # "score" (a bare number — runway months, DSCR/leverage ratio), or "currency" (a $ amount).
    # Nullable: a legacy alert with no unit defaults to currency on the client (today's behavior).
    metric_unit = Column(String, nullable=True)

    # Acknowledgment
    acknowledged = Column(Boolean, default=False, index=True)
    acknowledged_by = Column(String, nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)

    # Proactive-alert FEEDBACK (plan 20260712-agentic-finance-os W7, GAP-9, directive §13):
    # whether the user opened / accepted / dismissed / resolved / escalated this alert.
    # Captured for OFFLINE ranking + eval — a production agent NEVER self-rewrites ranking
    # from it. See app/services/alert_feedback.py.
    feedback_action = Column(String, nullable=True, index=True)  # opened | accepted | dismissed | resolved | escalated
    feedback_at = Column(DateTime(timezone=True), nullable=True)
    feedback_by = Column(String, nullable=True)

    # Related entity (e.g. covenant_id, anomaly_id)
    related_entity_type = Column(String, nullable=True)  # "covenant", "anomaly", etc.
    related_entity_id = Column(Integer, nullable=True)

    # Context data
    context = Column(JSON, nullable=True)                # Extra structured data
    period = Column(String, nullable=True, index=True)   # YYYY-MM

    # Stable identity of the underlying condition (detector + entity + period).
    # Used so nightly regeneration does not resurrect acknowledged alerts.
    # See app/services/alert_fingerprints.py.
    fingerprint = Column(String, nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (WF-3c, decision 2) — marker
# RATIFICATION-2026-07-13.md verified on disk; active-plan.json absent in this dispatched
# sub-agent session (the ratifying keyboard turn lives in the orchestrator transcript).
class BillPayment(Base):
    """
    Tracks bill payment batches created through the AP workflow.
    Status: DRAFT → APPROVED → SUBMITTED → COMPLETED | PARTIALLY_FAILED | FAILED
    (PARTIALLY_FAILED added by plan 20260713-wave0-ai-integration-p0 WF-3c: some bills
    paid, some failed — the batch is resubmittable, and the BillPaymentExecution ledger
    below guarantees already-paid bills are never re-created in QBO on resubmit.)
    """
    __tablename__ = "bill_payments"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    batch_name = Column(String, nullable=True)

    # Bills included in this payment batch
    bills = Column(JSON, nullable=False)  # [{bill_id, vendor, amount, due_date, priority}]
    total_amount = Column(Numeric(precision=18, scale=2), default=0)
    bill_count = Column(Integer, default=0)

    # Payment details
    payment_method = Column(String, default="check")  # check | ach | wire
    payment_account_id = Column(String, nullable=True)  # QBO bank account
    payment_date = Column(Date, nullable=True)

    # Workflow
    status = Column(String, default="DRAFT", index=True)  # DRAFT, APPROVED, SUBMITTED, COMPLETED, FAILED
    created_by = Column(String, nullable=True)
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)

    # Prioritization context
    strategy = Column(String, default="due_date")  # due_date | discount | vendor_critical | cash_constrained
    available_cash = Column(Numeric(precision=18, scale=2), nullable=True)

    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk A (marker
    # RATIFICATION-2026-07-13.md) — decision 5 (E1, blocking): the proposal-linkage
    # column. The BILL_PAY executor arm materializes find-or-create on this FK so a
    # resume re-enters the SAME batch row (same batch_id → same per-bill idempotency
    # keys). Without it a retry would mint a new batch + all-new keys — the exact
    # double-pay this plan prevents. UNIQUE where NOT NULL: a plain unique index on a
    # nullable column already permits many NULLs (PG default NULLS DISTINCT; sqlite
    # matches), which IS the partial-unique semantics decision 5 pins.
    proposed_action_id = Column(
        Integer,
        ForeignKey("proposed_actions.id"),
        nullable=True,
        unique=True,
        index=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C3 (WF-3c, decision 2 / refuter
# change E2): the HUMAN AP batch-submit lane previously had NO idempotency ledger — weaker than
# the agent lane — so a mid-loop crash or concurrent double-submit could double-pay real money.
# QBOActionExecution/InternalActionExecution CANNOT be reused: both key action_id as a
# nullable=False FK to proposed_actions, and this router lane has no ProposedAction row. This
# ledger mirrors the executor's SHA-256 claim pattern on its own table: key over {batch id,
# bill id, amount, payment account}, UNIQUE-constrained (the race gate), claimed BEFORE
# qbo.create("BillPayment", ...). RLS: company_id(realm)-keyed FORCE-RLS policy in migration
# f7c3a9d1b4e8 (ENABLE+FORCE, two-GUC pattern, RLS block outside the squashed-baseline guard).
# All ORM writes to this table live in app/qbo_actions/bill_payment_ledger.py (E5), never the
# router body.
class BillPaymentExecution(Base):
    """Race-safe idempotency ledger for AP payment-batch BillPayment submits (both lanes).

    Plan 20260713-wave2-2-ap-batches chunk A (decisions 3/6, refuter E2/E4):
    - ``status`` ('claimed' | 'succeeded' | 'uncertain'): 'uncertain' keeps the claim
      (the payment MAY exist in QBO — probe-on-encounter reconciles it); legacy rows
      (status NULL, pre-migration) derive: qbo_txn_id present = succeeded, else claimed.
    - UNIQUE(company_id, bill_id) is the CROSS-BATCH money gate: a succeeded /
      uncertain / claimed row structurally blocks any second batch from claiming the
      same bill (concurrent approvals included — the constraint, not a check-then-act,
      is the gate). A confirmed-reject release deletes the row and frees the bill.
      Partial-payments-across-batches stay OUT of v1 — this constraint IS that pin.
    """
    __tablename__ = "bill_payment_executions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_bill_payment_executions_idempotency_key"),
        # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk A (marker
        # RATIFICATION-2026-07-13.md) — decision 6 (E4, blocking) cross-batch UNIQUE gate.
        UniqueConstraint("company_id", "bill_id", name="uq_bill_payment_executions_company_bill"),
        Index("ix_bill_payment_executions_batch_company", "batch_id", "company_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # REALM-STRING-ONLY-OK: mirrors its parent bill_payments (realm-string-keyed batch table);
    # execution rows are QBO-realm-scoped and the realm-GUC FORCE-RLS policy keys on this column.
    company_id = Column(String, nullable=False, index=True)
    batch_id = Column(Integer, ForeignKey("bill_payments.id", ondelete="CASCADE"), nullable=False, index=True)
    bill_id = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    qbo_txn_id = Column(String, nullable=True)
    # 'claimed' | 'succeeded' | 'uncertain'; NULL = legacy row (see class docstring).
    status = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuditEntry(Base):
    """
    Fine-grained audit trail for compliance tracking.
    Captures every significant action: JE approvals, payment submissions, etc.
    """
    __tablename__ = "audit_entries"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    user_id = Column(String, nullable=True)
    user_email = Column(String, nullable=True)

    action = Column(String, nullable=False, index=True)  # e.g. APPROVE_JE, SUBMIT_PAYMENT, DISMISS_ALERT
    entity_type = Column(String, nullable=True)  # e.g. journal_entry, bill_payment, watchdog_alert
    entity_id = Column(String, nullable=True)

    details = Column(JSON, nullable=True)  # Before/after snapshot, amounts, etc.
    ip_address = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FPAModel(Base):
    __tablename__ = "fpa_models"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False) # realm_id
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    currency = Column(String, default="USD")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FPAScenario(Base):
    __tablename__ = "fpa_scenarios"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(Integer, ForeignKey("fpa_models.id", ondelete="CASCADE"), index=True, nullable=False) # ForeignKey
    name = Column(String, nullable=False) # Base, Upside, Downside
    is_default = Column(Boolean, default=False)
    start_year = Column(Integer, nullable=False)
    end_year = Column(Integer, nullable=False)


class FPAAssumption(Base):
    __tablename__ = "fpa_assumptions"

    id = Column(Integer, primary_key=True, index=True)
    scenario_id = Column(Integer, ForeignKey("fpa_scenarios.id", ondelete="CASCADE"), index=True, nullable=False)
    
    # We store the assumptions as JSON for flexibility 
    # { "revenue_growth": 0.10, "gross_margin": 0.65, "opex_pct": 0.35, "ar_days": 30, "ap_days": 45, "inv_days": 15, "capex_pct": 0.05, "tax_rate": 0.21, "interest_rate": 0.07 }
    driver_data = Column(JSON, nullable=False)


class FPADebtTranche(Base):
    __tablename__ = "fpa_debt_tranches"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(Integer, ForeignKey("fpa_models.id", ondelete="CASCADE"), index=True, nullable=False)
    scenario_id = Column(Integer, ForeignKey("fpa_scenarios.id", ondelete="CASCADE"), index=True, nullable=True) # Null if applies to all scenarios
    
    name = Column(String, nullable=False) # Term Loan, Revolver, Mezz
    starting_balance = Column(Numeric(precision=18, scale=2), default=0)
    rate = Column(Numeric(precision=6, scale=4), default=0) # e.g. 0.0800
    amortization_profile = Column(JSON, nullable=True) # E.g. {"type": "straight_line", "years": 5}
    cash_sweep_flag = Column(Boolean, default=False)


# ---------------------------------------------------------
# LBO Module Models
# ---------------------------------------------------------

class LboCompany(Base):
    """Master configuration for a portfolio company LBO model."""
    __tablename__ = "lbo_companies"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # QBO realm_id
    company_name = Column(String, nullable=False)
    close_date = Column(Date, nullable=True)                 # LBO closing date
    currency = Column(String, default="USD")
    fiscal_year_end = Column(Integer, default=12)            # Month 1-12
    model_start_year = Column(Integer, nullable=False)
    model_end_year = Column(Integer, nullable=False)
    exit_year = Column(Integer, nullable=True)               # Relative year index
    default_tax_rate = Column(Numeric(precision=6, scale=4), default=0.21)
    granularity = Column(String, default="annual")           # annual | monthly
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LboScenario(Base):
    """Scenario selection for an LBO model (Base, Upside, Downside, etc.)."""
    __tablename__ = "lbo_scenarios"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    lbo_company_id = Column(Integer, ForeignKey("lbo_companies.id", ondelete="CASCADE"), index=True, nullable=False)  # FK → lbo_companies.id
    name = Column(String, nullable=False)                         # "Base", "Upside", "Downside"
    is_active = Column(Boolean, default=False)
    cash_up_front = Column(Numeric(precision=18, scale=2), default=0)
    deferred_payment = Column(Numeric(precision=18, scale=2), default=0)
    senior_debt_amount = Column(Numeric(precision=18, scale=2), default=0)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LboDriver(Base):
    """Central assumption panel per company per scenario. Stored as a JSON blob."""
    __tablename__ = "lbo_drivers"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    scenario_id = Column(Integer, ForeignKey("lbo_scenarios.id", ondelete="CASCADE"), index=True, nullable=False)  # FK → lbo_scenarios.id

    # Full structured driver blob — see implementation_plan.md for schema
    # Keys: revenue_growth_by_stream, margin_by_stream, ar_dso, ap_dpo,
    #        other_ca_pct, accrued_pct, other_liab_pct, capex_pct,
    #        goodwill_amount, goodwill_amort_years, tax_rate,
    #        entry_ebitda_multiple, exit_ebitda_multiple, exit_year,
    #        transaction_fees_pct, equity_carry_pct, ebitda_adjustments
    driver_data = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LboEmployee(Base):
    """Individual employee census for headcount planning."""
    __tablename__ = "lbo_employees"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    scenario_id = Column(Integer, ForeignKey("lbo_scenarios.id", ondelete="CASCADE"), index=True, nullable=False)

    # Employee details
    employee_name = Column(String, nullable=False)
    job_title = Column(String, nullable=True)
    department = Column(String, nullable=True)
    start_date = Column(String, nullable=True)     # ISO date
    annual_salary = Column(Numeric(precision=18, scale=2), default=0)

    # COA mapping for salary
    salary_gl_account_id = Column(String, nullable=True)
    salary_gl_account_name = Column(String, nullable=True)

    sort_order = Column(Integer, default=0)


class LboPayrollDriver(Base):
    """Department-level payroll drivers with COA mapping & FTE projections."""
    __tablename__ = "lbo_payroll_drivers"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    scenario_id = Column(Integer, ForeignKey("lbo_scenarios.id", ondelete="CASCADE"), index=True, nullable=False)
    department = Column(String, nullable=False)
    year = Column(Integer, nullable=False)

    # Headcount
    ftes = Column(Numeric(precision=10, scale=2), default=0)
    avg_salary = Column(Numeric(precision=18, scale=2), default=0)

    # Tax & benefit rates
    payroll_tax_pct = Column(Numeric(precision=6, scale=4), default=0)
    benefits_pct = Column(Numeric(precision=6, scale=4), default=0)
    merit_increase_pct = Column(Numeric(precision=6, scale=4), default=0)

    # COA mapping — salary
    salary_gl_account_id = Column(String, nullable=True)
    salary_gl_account_name = Column(String, nullable=True)

    # COA mapping — employer taxes
    tax_gl_account_id = Column(String, nullable=True)
    tax_gl_account_name = Column(String, nullable=True)

    # COA mapping — benefits
    benefits_gl_account_id = Column(String, nullable=True)
    benefits_gl_account_name = Column(String, nullable=True)

    # FTE projection driver: yoy_growth | rev_capacity | fixed | custom
    fte_driver_type = Column(String, default="yoy_growth")
    fte_driver_value = Column(Numeric(precision=8, scale=4), default=0)  # e.g. 0.05 = 5% growth
    fte_driver_rev_per_fte = Column(Numeric(precision=18, scale=2), default=0)  # $ rev per FTE for capacity

    sort_order = Column(Integer, default=0)


class LboCapexDriver(Base):
    """Fixed asset and capex drivers by COA-level category."""
    __tablename__ = "lbo_capex_drivers"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    scenario_id = Column(Integer, ForeignKey("lbo_scenarios.id", ondelete="CASCADE"), index=True, nullable=False)
    category = Column(String, nullable=False)   # Equipment, Software, Capital Improvements, Goodwill

    # COA linkage
    gl_account_id = Column(String, nullable=True)    # QBO account ID
    gl_account_name = Column(String, nullable=True)  # e.g., "1500 - Fixed Assets"

    # Asset type: tangible → depreciation, goodwill → amortization
    asset_type = Column(String, default="tangible")  # tangible | goodwill

    # Beginning balances (as of analysis date)
    beginning_gross = Column(Numeric(precision=18, scale=2), default=0)
    beginning_accum_depr = Column(Numeric(precision=18, scale=2), default=0)

    # Spend / depreciation params
    useful_life_years = Column(Integer, default=5)
    capex_pct_revenue = Column(Numeric(precision=6, scale=4), default=0)  # Annual spend as % of rev
    salvage_value_pct = Column(Numeric(precision=6, scale=4), default=0)

    # Historical spend trend data
    hist_avg_spend = Column(Numeric(precision=18, scale=2), default=0)    # Avg annual spend ($)
    hist_avg_pct_rev = Column(Numeric(precision=8, scale=6), default=0)   # Avg annual spend as % rev

    sort_order = Column(Integer, default=0)


class LboDebtTranche(Base):
    """Individual debt tranche for an LBO scenario."""
    __tablename__ = "lbo_debt_tranches"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    scenario_id = Column(Integer, index=True, nullable=False)
    tranche_name = Column(String, nullable=False)   # Senior A, Senior B, Subordinated, Seller Note

    # COA linkage
    gl_account_id = Column(String, nullable=True)    # QBO account ID (e.g., "123")
    gl_account_name = Column(String, nullable=True)  # e.g., "2710 - Term Loan"

    # Loan terms
    start_date = Column(String, nullable=True)       # ISO date string
    maturity_date = Column(String, nullable=True)     # ISO date string
    current_balance = Column(Numeric(precision=18, scale=2), default=0)  # Outstanding balance
    target_balance = Column(Numeric(precision=18, scale=2), default=0)   # Balance at exit (usually 0)

    # Rate — always expressed as SOFR + spread (bps)
    sofr_rate = Column(Numeric(precision=8, scale=6), default=0.043)     # Current SOFR rate
    spread_bps = Column(Integer, default=200)                             # Spread in basis points

    # Legacy fields kept for backward compat
    funded_amount = Column(Numeric(precision=18, scale=2), default=0)
    interest_rate = Column(Numeric(precision=8, scale=6), default=0)
    rate_type = Column(String, default="fixed")     # fixed | floating
    base_rate = Column(Numeric(precision=8, scale=6), default=0)    # SOFR etc for floating
    spread = Column(Numeric(precision=8, scale=6), default=0)       # Spread above base
    amort_years = Column(Integer, nullable=True)
    bullet_pct = Column(Numeric(precision=6, scale=4), default=0)
    pik_flag = Column(Boolean, default=False)
    fees_pct = Column(Numeric(precision=6, scale=4), default=0)     # % of funded
    sort_order = Column(Integer, default=0)

    # Sweep / revolver mechanics (chunk C1, plan 20260716-five-domain-remediation,
    # migration b8e4d1c7f2a9). The per-tranche fields compute_debt_schedule already
    # reads from the engine payload — real columns make them reachable:
    #   cash_sweep_flag — the tranche participates in the excess-FCF cash sweep.
    #   revolver_flag   — the tranche is the revolver / liquidity facility (draws on
    #                     a cash shortfall up to `commitment`, repaid first).
    #   commitment      — the revolver's total commitment (NULL for term tranches).
    #   seniority_rank  — explicit sweep order (lower = more senior; NULL = list order).
    cash_sweep_flag = Column(Boolean, nullable=False, default=False, server_default="false")
    revolver_flag = Column(Boolean, nullable=False, default=False, server_default="false")
    commitment = Column(Numeric(precision=18, scale=2), nullable=True)
    seniority_rank = Column(Integer, nullable=True)


class LboRevenueStream(Base):
    """Individual revenue stream for granular revenue detail."""
    __tablename__ = "lbo_revenue_streams"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    scenario_id = Column(Integer, index=True, nullable=False)
    stream_name = Column(String, nullable=False)         # Product, service, customer segment
    stream_type = Column(String, default="volume_price") # volume_price | contract | recurring
    base_units = Column(Numeric(precision=18, scale=2), default=0)     # Starting volume/contracts
    unit_price = Column(Numeric(precision=18, scale=2), default=0)     # Price per unit
    growth_rate = Column(Numeric(precision=8, scale=6), default=0)     # YoY unit growth
    price_escalation = Column(Numeric(precision=8, scale=6), default=0) # YoY price increase
    churn_rate = Column(Numeric(precision=8, scale=6), default=0)      # Annual attrition (recurring)
    sort_order = Column(Integer, default=0)


class LboCoaMapping(Base):
    """Maps QBO GL accounts to standardized LBO model categories."""
    __tablename__ = "lbo_coa_mapping"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    gl_account_number = Column(String, nullable=False)
    gl_account_name = Column(String, nullable=True)
    mapping_category = Column(String, nullable=False)  # Revenue_Service, COGS_Direct, Payroll_Surgical, etc.
    driver_tag = Column(String, nullable=True)          # Optional tie to specific driver


class LboCell(Base):
    """Single cell in the Excel-like LBO grid."""
    __tablename__ = "lbo_cells"

    id = Column(Integer, primary_key=True, index=True)
    scenario_id = Column(Integer, index=True, nullable=False)
    row_index = Column(Integer, nullable=False)
    col_index = Column(Integer, nullable=False)       # 0=label-only, 1=Y0 … 6=Y5
    line_code = Column(String, index=True, nullable=True) # standardized PE template slot (e.g. 'cogs', 'cash', 'retained_earnings')
    label = Column(String, default="")
    value = Column(Numeric(precision=18, scale=4), nullable=True)
    formula = Column(String, nullable=True)
    display_fmt = Column(String, default="number")     # number|pct|currency|multiple|none
    row_type = Column(String, default="input")          # header|input|formula|subtotal|pct|blank
    section = Column(String, default="")
    indent = Column(Integer, default=0)
    qbo_account_id = Column(String, nullable=True)     # links to QBO account
    driver_type = Column(String, default="yoy")         # yoy|volume|pct_revenue|fixed|dso|dpo|dio|depr|amort|debt_amort|auto
    driver_params = Column(JSON, default={})            # e.g. {"growth_rate": 0.05}
    # Period-axis key (plan 20260717-three-statement-spine C1-a, DEC-LBO3S-1):
    # references lbo_periods.id by convention — a bare Integer matching this
    # table's FK-less scenario_id shape. NULLABLE until the C1-b backfill stamps
    # it; col_index remains the authoritative read path until the Wave-3 cutover.
    period_id = Column(Integer, index=True, nullable=True)


class LboPeriod(Base):
    """One column of the LBO statement spine — the period axis (DEC-LBO3S-1).

    Replaces positional ``LboCell.col_index`` keying (SYNTHESIS
    20260717-three-statement-spine §3): a scenario's spine is FY-3, FY-2, FY-1,
    LTM, PF-open, Y1..Y5 — each one row here, ordered by ``seq``. ``kind`` is
    the closed vocabulary {historical_fy, ltm, pf_open, forecast}; ``label``
    carries the rendered column header ("FY-3" … "Y5"). ``period_start`` /
    ``period_end`` are the calendar bounds where applicable (``pf_open`` is the
    transaction instant — both NULL). ``provenance`` uses the LBO family's
    stamp vocabulary (qbo_actual | derived | assumption) recording where the
    period's dating came from. Realm-keyed tenant table (company_id = QBO
    realm_id, matching the whole lbo_* family) — the FORCE-RLS realm policy
    ships in migration d5b9e2c8a4f7.
    """
    # REALM-STRING-ONLY-OK: child row of the realm-keyed LBO family — its parent
    # (lbo_scenarios via the scenario_id FK) is company_id-realm-keyed with a
    # realm RLS policy (e7f1a9c3d2b5), and every access path re-asserts tenancy
    # through check_entity_tenant_access on LboScenario. An organization_id FK
    # here would key the child to a DIFFERENT tenant axis than the parent rows
    # it orders; the realm RLS policy (same two-GUC shape as the family) ships
    # in migration d5b9e2c8a4f7.
    __tablename__ = "lbo_periods"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # QBO realm_id (RLS realm key)
    scenario_id = Column(
        Integer,
        ForeignKey("lbo_scenarios.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    seq = Column(Integer, nullable=False)       # spine order: FY-3 < FY-2 < FY-1 < LTM < PF-open < Y1 < … < Y5
    kind = Column(String, nullable=False)       # historical_fy | ltm | pf_open | forecast
    label = Column(String, nullable=False)      # "FY-3" | "FY-2" | "FY-1" | "LTM" | "PF Open" | "Y1".."Y5"
    period_start = Column(Date, nullable=True)  # calendar bounds where applicable
    period_end = Column(Date, nullable=True)    # (pf_open carries none — a close-date instant)
    provenance = Column(String, default="derived")  # qbo_actual | derived | assumption

    __table_args__ = (
        UniqueConstraint("scenario_id", "seq", name="uq_lbo_periods_scenario_seq"),
    )


# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-6A (AC-1 S&U override table)
class LboSourcesUsesOverride(Base):
    """User-confirmed Sources & Uses override snapshot for an LBO scenario (W-6A).

    ONE row per scenario. ``overrides_json`` holds ONLY the fields the user
    explicitly overrode in the editable S&U grid:
      {"enterprise_value": float, "transaction_fees_pct": float,
       "tranches": {"<tranche_id>": funded_amount, ...}}
    The math itself always runs through the existing compute_sources_uses engine
    (app/lbo/sources_uses.py) with these values merged OVER the driver/tranche
    state — the snapshot is an input overlay + audit record, never a fork of the
    computed result. Realm-keyed tenant table (company_id = QBO realm_id) —
    FORCE-RLS realm policy ships in migration f8c3a1d6e9b2.
    """
    # REALM-STRING-ONLY-OK: child row of the realm-keyed LBO family — its ONLY parents
    # (lbo_scenarios via FK, lbo_debt_tranches via overrides_json ids) are themselves
    # company_id-realm-keyed with realm RLS policies (e7f1a9c3d2b5), and every access
    # path re-asserts tenancy through check_entity_tenant_access on LboScenario. An
    # organization_id FK here would key the child to a DIFFERENT tenant axis than the
    # parent rows it overlays; the realm RLS policy (same two-GUC shape as the family)
    # ships in migration f8c3a1d6e9b2.
    __tablename__ = "lbo_sources_uses_overrides"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # QBO realm_id (RLS realm key)
    scenario_id = Column(
        Integer,
        ForeignKey("lbo_scenarios.id", ondelete="CASCADE"),
        unique=True, index=True, nullable=False,
    )
    overrides_json = Column(JSON, nullable=False, default=dict)
    confirmed_by = Column(String, nullable=True)   # confirming user's email (audit)
    confirmed_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Report(Base):
    """Tracks generated PDF financial reports."""
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False) # realm_id
    period = Column(String, nullable=False, index=True) # e.g., '2025-01'
    file_path = Column(String, nullable=False)
    report_type = Column(String, default="month_end") # month_end, ad_hoc
    generated_by = Column(String, default="system")
    generated_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------
# AI-First Pipeline Models
# ---------------------------------------------------------

class SyncEvent(Base):
    """Tracks data sync lifecycle and triggers AI analysis pipeline."""
    __tablename__ = "sync_events"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)       # 'YYYY-MM'
    event_type = Column(String, nullable=False, default="DATA_SYNCED")
    status = Column(String, nullable=False, default="PENDING") # PENDING, PROCESSING, DONE, FAILED, SUPERSEDED
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class InsightKPI(Base):
    """Precomputed KPI values produced by the AI orchestration pipeline."""
    __tablename__ = "insights_kpis"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)                     # e.g. 'ttm_revenue', 'ebitda', 'cash'
    value = Column(Numeric(precision=18, scale=2), default=0)
    delta = Column(Numeric(precision=10, scale=4), nullable=True)  # MoM or YoY change %
    unit = Column(String, default="USD")                       # USD, %, ratio
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InsightTrend(Base):
    """Time-series data points for trend charts, precomputed by the pipeline."""
    __tablename__ = "insights_trends"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    metric = Column(String, nullable=False, index=True)       # 'revenue', 'ebitda', 'cash', 'expenses'
    period = Column(String, nullable=False, index=True)       # Individual month 'YYYY-MM'
    value = Column(Numeric(precision=18, scale=2), default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InsightAnomaly(Base):
    """Detected anomalies and flags from the AI analysis pipeline."""
    __tablename__ = "insights_anomalies"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)
    anomaly_type = Column(String, nullable=False)             # 'expense_spike', 'negative_balance', 'unusual_je', etc.
    description = Column(Text, nullable=False)
    severity = Column(String, default="info")                 # info, warning, critical
    data_ref = Column(JSON, nullable=True)                    # Reference to source row/txn
    dismissed = Column(Boolean, default=False)                # User dismissed this alert
    dismissed_at = Column(DateTime(timezone=True), nullable=True)
    # Stable identity of the underlying condition (detector + entity + period).
    # Used so pipeline re-runs do not resurrect dismissed anomalies.
    # See app/services/alert_fingerprints.py.
    fingerprint = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InsightCashflow(Base):
    """Precomputed cash flow breakdown by category."""
    __tablename__ = "insights_cashflow"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False)                 # 'cfo', 'cfi', 'cff', 'net'
    value = Column(Numeric(precision=18, scale=2), default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ReportSection(Base):
    """Pre-baked report sections generated by the AI pipeline for PDF assembly."""
    __tablename__ = "report_sections"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)
    section_type = Column(String, nullable=False)             # 'FRONT_PAGE', 'CURRENT_PL', 'TTM_CASHFLOW', 'AI_NOTES', etc.
    content = Column(JSON, nullable=True)                     # Structured data for rendering
    order_index = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------
# Learning Engine Models
# ---------------------------------------------------------

class TransactionFeature(Base):
    """Extracted transaction-level features for the learning engine."""
    __tablename__ = "transaction_features"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    transaction_id = Column(String, index=True, nullable=False)  # QBO txn ID

    raw_text = Column(Text)               # Original description/memo
    cleaned_text = Column(Text)           # Normalized text
    vendor_id = Column(String, index=True)
    merchant_code = Column(String)
    amount = Column(Numeric(precision=18, scale=2))
    date = Column(Date)
    day_of_month = Column(Integer)
    day_of_week = Column(Integer)         # 0=Monday, 6=Sunday
    month = Column(Integer)

    recurrence_signal = Column(Numeric(precision=5, scale=4))   # 0.0 - 1.0
    seasonality_signal = Column(Numeric(precision=5, scale=4))  # 0.0 - 1.0
    account_ref = Column(String, nullable=True)  # GL account if categorized

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RecurrencePattern(Base):
    """Detected recurring transaction patterns per vendor."""
    __tablename__ = "recurrence_patterns"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    vendor_id = Column(String, index=True, nullable=False)

    frequency = Column(String, nullable=False)  # DAILY, WEEKLY, BIWEEKLY, MONTHLY, QUARTERLY, ANNUAL, NON_RECURRING
    expected_amount = Column(Numeric(precision=18, scale=2))
    std_dev_amount = Column(Numeric(precision=18, scale=2))
    expected_dates = Column(JSON, nullable=True)   # [{"day_of_month": 15}, ...]
    confidence = Column(Numeric(precision=5, scale=4))  # 0.0 - 1.0
    sample_count = Column(Integer, default=0)       # Number of transactions analyzed

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PaymentMethodBehavior(Base):
    """Behavioral profile for bank accounts and credit cards."""
    __tablename__ = "payment_method_behavior"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    payment_method_id = Column(String, index=True, nullable=False)  # QBO Account ID

    account_type = Column(String)              # Bank, CreditCard
    historical_cycle = Column(JSON, nullable=True)  # {"avg_days_between_txns": 3.2, ...}
    historical_spend_ranges = Column(JSON, nullable=True)  # {"min": 10, "max": 5000, "avg": 200}
    common_merchants = Column(JSON, nullable=True)  # [{"vendor": "...", "count": 15}]
    risk_score = Column(Numeric(precision=5, scale=4), default=0)  # 0.0 - 1.0

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ClassificationPrediction(Base):
    """ML-predicted GL account for uncategorized transactions."""
    __tablename__ = "classification_predictions"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    transaction_id = Column(String, index=True, nullable=False)
    # QBO ids are unique PER ENTITY TYPE only (LOCK-1, see QBOTransaction) — a
    # Purchase and a Deposit can share a qbo_id in-realm, so prediction identity
    # is (company, transaction, entity_type). Nullable: legacy rows stay NULL and
    # resolve via the NULL-tolerant lookup predicate until naturally replaced
    # (AC-1, plan 20260716-classification-hardening; never backfilled by guess).
    entity_type = Column(String, nullable=True)

    predicted_account_id = Column(String, nullable=False)
    predicted_account_name = Column(String, nullable=True)
    confidence_score = Column(Numeric(precision=5, scale=4))  # 0.0 - 1.0
    basis = Column(String, nullable=False)  # embedding, vendor, pattern, history, rule
    reasoning = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CompanyMemory(Base):
    """Persistent knowledge graph per company — aggregated learning outputs."""
    __tablename__ = "company_memory"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, unique=True, index=True, nullable=False)

    # ── Company Identity (injected into every system prompt) ──────────────
    company_profile = Column(Text, nullable=True)      # LLM-generated: industry, business model, revenue model, key relationships (~200 words)
    user_context = Column(Text, nullable=True)          # User-provided: pitch deck summary, manual notes, overrides

    # ── Financial Intelligence ────────────────────────────────────────────
    learned_rules = Column(JSON, nullable=True)       # Auto-discovered classification rules
    trend_summaries = Column(JSON, nullable=True)     # Revenue/expense trajectory
    seasonality = Column(JSON, nullable=True)          # Company-wide seasonal patterns
    cash_behavior = Column(JSON, nullable=True)        # Cash flow rhythm, burn rate
    customer_behavior = Column(JSON, nullable=True)    # Payment timing, concentration
    vendor_behavior = Column(JSON, nullable=True)      # Aggregated vendor intelligence
    risk_map = Column(JSON, nullable=True)              # Outstanding risk indicators

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AssistantContextCache(Base):
    """
    Precomputed executive summary per company for the AI assistant.
    Refreshed nightly so the assistant has instant awareness without
    burning tokens on live analysis packs every request.
    """
    __tablename__ = "assistant_context_cache"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    realm_id = Column(String, unique=True, index=True, nullable=False)

    # ~500 token executive briefing — precomputed by nightly job
    executive_summary = Column(Text, nullable=True)

    # Raw module snapshots for fallback/debugging
    module_snapshot = Column(JSON, nullable=True)

    # Token count of the summary (for budget tracking)
    summary_tokens = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def _bank_feed_lineage_default(context: Any) -> str | None:
    """Keep the live v1 statement writer usable until the v2 writer lands."""

    parameters = context.get_current_parameters()
    if parameters.get("source") == "statement_upload":
        return "legacy_unbound"
    return None


class BankFeedItem(Base):
    """
    Caches QBO's unreviewed bank feed transactions (the 'For Review' tab in QBO).
    Enriched with AI-suggested account classifications, vendor/customer links,
    match candidates, and QBO cleared-status tracking before user approval.
    """
    __tablename__ = "bank_feed_items"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    # Exact parser provenance. These remain NULL only for pre-cutover rows and
    # non-statement sources. A v2 statement row is controlled only when all
    # three values identify its import, logical chunk, and stable source order.
    statement_import_id = Column(Integer, nullable=True, index=True)
    parse_chunk_id = Column(Uuid, nullable=True, index=True)
    source_ordinal = Column(Integer, nullable=True)
    lineage_status = Column(String, nullable=True, default=_bank_feed_lineage_default)
    # Provenance discriminator: which feed produced this row. Default preserves
    # every existing row + every QBO-webhook write as "qbo_feed", so the
    # month-end close gate (_verify_bank_feed) only counts genuine QBO-feed
    # items — a "statement_upload" row from bank-reconciliation import must NOT
    # flip the close gate (see app/routers/close_tasks.py). One of
    # "qbo_feed" | "statement_upload" | "stripe_fc" (DEC-BREC per SYNTHESIS §2B/§3).
    source = Column(String, nullable=False, server_default="qbo_feed")
    # Nullable because a statement-upload / non-QBO line has no QBO BankTransaction.Id.
    # The ix_bank_feed_items_qbo_txn_id index is unique=False (migration
    # b2f783e8dcad:52), so relaxing to nullable is safe — no unique-null conflict.
    qbo_txn_id = Column(String, nullable=True, index=True)   # QBO BankTransaction.Id (None for non-QBO sources)

    # Raw transaction data from QBO
    txn_date = Column(Date, nullable=False)
    amount = Column(Numeric(18, 2))
    description = Column(Text)
    txn_type = Column(String)          # DEBIT | CREDIT
    bank_account_ref = Column(String)  # QBO account ID
    bank_account_name = Column(String)

    # AI classification
    suggested_account_id = Column(String)
    suggested_account_name = Column(String)
    confidence_score = Column(Numeric(5, 4))
    ai_reasoning = Column(Text)

    # Vendor / Customer (From/To) — pre-filled by AI or user selection
    vendor_id = Column(String, nullable=True)        # QBO Vendor ID
    vendor_name = Column(String, nullable=True)      # Display name
    customer_id = Column(String, nullable=True)      # QBO Customer ID
    customer_name = Column(String, nullable=True)    # Display name
    entity_type = Column(String, nullable=True)      # "vendor" | "customer"

    # Match to existing open QBO transaction (Invoice / Bill)
    matched_txn_id = Column(String, nullable=True)   # QBO ID of matched open txn
    matched_txn_type = Column(String, nullable=True) # "Invoice" | "Bill" | "Payment" ...
    matched_txn_ref = Column(String, nullable=True)  # Doc number / memo for display

    # User override (only set if user changed from AI suggestion)
    override_account_id = Column(String)
    override_account_name = Column(String)

    # Workflow: pending | approved | ignored
    status = Column(String, default="pending", index=True)
    qbo_result_txn_id = Column(String)   # ID of the created Purchase/Deposit in QBO

    # QBO cleared / reconciliation tracking
    cleared_in_qbo = Column(Boolean, default=False)  # DEC-BREC-1 (2026-07-01): QBO exposes NO reconcile / ClearedStatus write API — reconciliation state is Peregrine-local and is NEVER pushed to QBO. Legacy For-Review-feed field; not written by the bank-reconciliation feature.

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ux_bank_feed_item_id_realm", "id", "realm_id", unique=True),
        ForeignKeyConstraint(
            ["statement_import_id", "realm_id"],
            ["bank_statement_imports.id", "bank_statement_imports.realm_id"],
            name="fk_bank_feed_statement_import_realm",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parse_chunk_id", "statement_import_id", "realm_id"],
            [
                "bank_statement_parse_chunks.id",
                "bank_statement_parse_chunks.bank_statement_import_id",
                "bank_statement_parse_chunks.realm_id",
            ],
            name="fk_bank_feed_parse_chunk_import_realm",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_ordinal IS NULL OR source_ordinal >= 0",
            name="ck_bank_feed_source_ordinal_nonnegative",
        ),
        CheckConstraint(
            "lineage_status IS NULL OR "
            "lineage_status IN ('controlled', 'legacy_unbound')",
            name="ck_bank_feed_lineage_status",
        ),
        CheckConstraint(
            "(source != 'statement_upload' AND lineage_status IS NULL "
            "AND statement_import_id IS NULL AND parse_chunk_id IS NULL "
            "AND source_ordinal IS NULL) OR "
            "(source = 'statement_upload' AND lineage_status IS NOT NULL AND "
            "((lineage_status = 'legacy_unbound' "
            "AND statement_import_id IS NULL AND parse_chunk_id IS NULL "
            "AND source_ordinal IS NULL) OR (lineage_status = 'controlled' "
            "AND statement_import_id IS NOT NULL AND parse_chunk_id IS NOT NULL "
            "AND source_ordinal IS NOT NULL)))",
            name="ck_bank_feed_controlled_provenance",
        ),
        UniqueConstraint(
            "statement_import_id",
            "source_ordinal",
            name="uq_bank_feed_import_source_ordinal",
        ),
        UniqueConstraint(
            "id",
            "realm_id",
            "statement_import_id",
            "parse_chunk_id",
            "source_ordinal",
            name="uq_bank_feed_parser_identity",
        ),
    )


class BankStatementImport(Base):
    """
    Batch header for one imported bank statement (the parent of the
    statement-source bank_feed_items rows an import produces).

    Realm-keyed (never org-keyed) so it enrolls in the live RLS backstop on the
    `realm` GUC axis, matching every recon/bank sibling (bank_feed_items,
    reconciliation_sessions, reconciliation_matches, bank_description_rules —
    RLS migration e7f1a9c3d2b5). See SYNTHESIS §2B / DEC-BREC-2/3.
    """
    __tablename__ = "bank_statement_imports"

    # REALM-STRING-ONLY-OK: bank/recon-family table; SYNTHESIS §2B + DEC-BREC-2/3
    # ruled organization_id is the WRONG RLS axis here — all four recon/bank siblings
    # (bank_feed_items, reconciliation_sessions, reconciliation_matches,
    # bank_description_rules) enroll on the `realm` GUC in migration e7f1a9c3d2b5,
    # and this table's rows key to a QBO account under a realm. The C0-a migration
    # enrolls it in RLS on realm_id (`realm`), matching those siblings.
    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    account_id = Column(String, nullable=False, index=True)   # QBO Account ID the statement covers
    account_name = Column(String, nullable=True)

    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)

    source = Column(String, nullable=False, server_default="statement_upload")  # csv | pdf | statement_upload

    # Tenant-prefixed object-storage key of the uploaded (scanned) statement file.
    storage_key = Column(String, nullable=True)

    # Frozen v2 identity. Nullable only for honest pre-cutover history; every
    # row entering the authoritative lifecycle carries all four values.
    source_sha256 = Column(String(64), nullable=True)
    parser_version = Column(String, nullable=True)
    extractor_version = Column(String, nullable=True)
    prompt_schema_version = Column(String, nullable=True)
    lifecycle_status = Column(
        String,
        nullable=False,
        server_default="review_required",
        index=True,
    )
    lineage_status = Column(
        String,
        nullable=False,
        server_default="legacy_unbound",
    )
    review_reason_code = Column(String, nullable=True)
    review_action = Column(String, nullable=True)

    opening_balance = Column(Numeric(precision=18, scale=2), nullable=True)
    closing_balance = Column(Numeric(precision=18, scale=2), nullable=True)

    status = Column(String, nullable=False, server_default="pending")  # pending | parsing | parsed | matched | complete | error

    # AC-5 (plan 20260716-pdf-import-async): the ASYNC worker's result. A PDF import
    # returns `parsing` immediately (the LLM parse runs 40-183s — prod-measured — so
    # it cannot be held on the request) and the Celery task fills these in:
    # `line_count` = parsed row count on success; `error_detail` = the honest failure
    # message when status='error'. The task ALWAYS finalizes to a terminal status —
    # never a stuck `parsing`.
    line_count = Column(Integer, nullable=True)
    error_detail = Column(Text, nullable=True)

    # The user's deliberate force-reimport intent, CARRIED to the async worker. The
    # router only enqueues `.delay(import_id)`, so without this column the flag is LOST
    # on the PDF lane: a legitimate re-import then hits the batch-level duplicate guard
    # and errors "retry with force_reimport" — which the user already did (a dead-end
    # loop). The sync CSV lane never had this problem. NULL = not forced.
    force_reimport = Column(Boolean, nullable=True)

    # Wave 4A.1 instrumentation mirrors the latest *executed parse* outcome here so
    # callers can distinguish a feature result from the evidence quality around it.
    # These fields never gate or alter the import itself: metering is fail-open, while
    # a missing/partial provider trail is marked telemetry_incomplete and excluded
    # from the parser-baseline evidence set.
    parse_telemetry_status = Column(String, nullable=True)
    parse_evidence_eligible = Column(Boolean, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ux_bank_statement_import_id_realm", "id", "realm_id", unique=True),
        CheckConstraint(
            "source_sha256 IS NULL OR length(source_sha256) = 64",
            name="ck_bank_import_source_sha256",
        ),
        CheckConstraint(
            "lifecycle_status IN "
            "('queued', 'processing', 'review_required', 'parsed', 'error')",
            name="ck_bank_import_lifecycle_status",
        ),
        CheckConstraint(
            "lineage_status IN "
            "('controlled', 'legacy_unbound', 'legacy_unverified')",
            name="ck_bank_import_lineage_status",
        ),
        CheckConstraint(
            "(lineage_status IN ('legacy_unbound', 'legacy_unverified') "
            "AND source_sha256 IS NULL AND parser_version IS NULL "
            "AND extractor_version IS NULL AND prompt_schema_version IS NULL) OR "
            "(lineage_status = 'controlled' AND source_sha256 IS NOT NULL "
            "AND parser_version IS NOT NULL AND extractor_version IS NOT NULL "
            "AND prompt_schema_version IS NOT NULL)",
            name="ck_bank_import_v2_identity",
        ),
        CheckConstraint(
            "review_action IS NULL OR review_action IN "
            "('reupload', 'correct', 'reject', 'retry')",
            name="ck_bank_import_review_action",
        ),
    )


class BankStatementParseDeliveryAttempt(Base):
    """One durable row for every known-import Celery delivery/redelivery.

    ``celery_task_id`` is diagnostic metadata, not the identity: broker redelivery
    can reuse it. ``id`` is generated afresh on every worker entry so historical
    attempts are never overwritten.
    """

    __tablename__ = "bank_statement_parse_delivery_attempts"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    bank_statement_import_id = Column(Integer, nullable=False)
    # V2 authority runs chunk -> delivery. NULL denotes a retained one-shot
    # telemetry row created before the resumable cutover.
    chunk_id = Column(Uuid, nullable=True, index=True)
    delivery_ordinal = Column(Integer, nullable=True)
    broker_task_id = Column(String, nullable=True, unique=True, index=True)
    lifecycle_status = Column(String, nullable=True, index=True)
    realm_id = Column(String, nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    organization_company_id = Column(
        Integer,
        ForeignKey("organization_companies.id", ondelete="SET NULL"),
        nullable=True,
    )
    celery_task_id = Column(String, nullable=True, index=True)
    redelivered = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # Frozen one-shot control envelope. These values observe the current parser;
    # they do not change its feature behavior.
    parser_mode = Column(String, nullable=False, default="one_shot", server_default="one_shot")
    max_input_chars = Column(Integer, nullable=False)
    timeout_seconds = Column(Integer, nullable=False)
    max_retries = Column(Integer, nullable=False)
    allow_fallback = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    status = Column(String, nullable=False, default="started", server_default="started")
    telemetry_status = Column(
        String,
        nullable=False,
        default="pending",
        server_default="pending",
    )
    evidence_eligible = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    provider_calls_started = Column(Integer, nullable=False, default=0, server_default="0")
    provider_envelope_failures = Column(Integer, nullable=False, default=0, server_default="0")
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["bank_statement_import_id", "realm_id"],
            ["bank_statement_imports.id", "bank_statement_imports.realm_id"],
            name="fk_bank_parse_delivery_import_realm",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "chunk_id",
                "bank_statement_import_id",
                "realm_id",
                "organization_id",
                "organization_company_id",
            ],
            [
                "bank_statement_parse_chunks.id",
                "bank_statement_parse_chunks.bank_statement_import_id",
                "bank_statement_parse_chunks.realm_id",
                "bank_statement_parse_chunks.organization_id",
                "bank_statement_parse_chunks.organization_company_id",
            ],
            name="fk_bank_parse_delivery_chunk_identity",
            ondelete="CASCADE",
            onupdate="CASCADE",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["organization_company_id", "organization_id", "realm_id"],
            [
                "organization_companies.id",
                "organization_companies.organization_id",
                "organization_companies.realm_id",
            ],
            name="fk_bank_parse_delivery_tenant_identity",
            onupdate="CASCADE",
        ),
        Index(
            "ux_bank_parse_delivery_identity",
            "id",
            "bank_statement_import_id",
            "realm_id",
            "organization_id",
            "organization_company_id",
            unique=True,
        ),
        Index(
            "ix_bank_parse_delivery_import",
            "bank_statement_import_id",
        ),
        Index(
            "ix_bank_parse_delivery_org_company",
            "organization_company_id",
        ),
        CheckConstraint("max_input_chars > 0", name="ck_bank_parse_delivery_chars_positive"),
        CheckConstraint("timeout_seconds > 0", name="ck_bank_parse_delivery_timeout_positive"),
        CheckConstraint("max_retries >= 0", name="ck_bank_parse_delivery_retries_nonnegative"),
        CheckConstraint("provider_calls_started >= 0", name="ck_bank_parse_delivery_calls_nonnegative"),
        CheckConstraint(
            "provider_envelope_failures >= 0",
            name="ck_bank_parse_delivery_failures_nonnegative",
        ),
        CheckConstraint(
            "telemetry_status IN ('pending', 'complete', 'telemetry_incomplete')",
            name="ck_bank_parse_delivery_telemetry_status",
        ),
        CheckConstraint(
            "NOT evidence_eligible OR telemetry_status = 'complete'",
            name="ck_bank_parse_delivery_evidence_complete",
        ),
        CheckConstraint(
            "delivery_ordinal IS NULL OR delivery_ordinal >= 0",
            name="ck_bank_parse_delivery_ordinal_nonnegative",
        ),
        CheckConstraint(
            "lifecycle_status IS NULL OR lifecycle_status IN "
            "('created', 'dispatched', 'started', 'succeeded', 'failed', 'expired')",
            name="ck_bank_parse_delivery_lifecycle_status",
        ),
        CheckConstraint(
            "(lifecycle_status IS NULL AND chunk_id IS NULL "
            "AND delivery_ordinal IS NULL AND broker_task_id IS NULL) OR "
            "(lifecycle_status IS NOT NULL AND chunk_id IS NOT NULL "
            "AND delivery_ordinal IS NOT NULL AND broker_task_id IS NOT NULL "
            "AND organization_id IS NOT NULL "
            "AND organization_company_id IS NOT NULL)",
            name="ck_bank_parse_delivery_v2_identity",
        ),
        UniqueConstraint(
            "chunk_id",
            "delivery_ordinal",
            name="uq_bank_parse_delivery_chunk_ordinal",
        ),
        Index(
            "ix_bank_parse_delivery_import_started",
            "bank_statement_import_id",
            "started_at",
        ),
    )


class BankStatementParseChunk(Base):
    """A durable parser chunk identity (one whole-document chunk in 4A.1)."""

    __tablename__ = "bank_statement_parse_chunks"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    bank_statement_import_id = Column(Integer, nullable=False, index=True)
    # Legacy one-shot evidence only. V2 deliveries point to a stable chunk;
    # there is deliberately no chunk -> delivery FK and therefore no cycle.
    delivery_attempt_id = Column(Uuid, nullable=True, index=True)
    realm_id = Column(String, nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    organization_company_id = Column(
        Integer,
        ForeignKey("organization_companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False, default=0, server_default="0")
    source_sha256 = Column(String(64), nullable=True)
    source_size_bytes = Column(Integer, nullable=True)
    parser_version = Column(String, nullable=True)
    extractor_version = Column(String, nullable=True)
    prompt_schema_version = Column(String, nullable=True)
    page_start = Column(Integer, nullable=True)
    page_end = Column(Integer, nullable=True)
    span_start = Column(Integer, nullable=True)
    span_end = Column(Integer, nullable=True)
    chunk_revision = Column(Integer, nullable=True)
    supersedes_chunk_id = Column(Uuid, nullable=True)
    resolution_payload = Column(JSON, nullable=True)
    lifecycle_status = Column(String, nullable=True, index=True)
    lease_token = Column(String, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    application_retry_count = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="started", server_default="started")
    telemetry_status = Column(
        String,
        nullable=False,
        default="pending",
        server_default="pending",
    )
    evidence_eligible = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["bank_statement_import_id", "realm_id"],
            ["bank_statement_imports.id", "bank_statement_imports.realm_id"],
            name="fk_bank_parse_chunk_import_realm",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_company_id", "organization_id", "realm_id"],
            [
                "organization_companies.id",
                "organization_companies.organization_id",
                "organization_companies.realm_id",
            ],
            name="fk_bank_parse_chunk_tenant_identity",
            onupdate="CASCADE",
        ),
        Index(
            "ux_bank_parse_chunk_identity",
            "id",
            "delivery_attempt_id",
            "bank_statement_import_id",
            "realm_id",
            "organization_id",
            "organization_company_id",
            unique=True,
        ),
        UniqueConstraint(
            "id",
            "bank_statement_import_id",
            "realm_id",
            "organization_id",
            "organization_company_id",
            name="ux_bank_parse_chunk_v2_identity",
        ),
        Index(
            "ux_bank_parse_chunk_import_realm",
            "id",
            "bank_statement_import_id",
            "realm_id",
            unique=True,
        ),
        CheckConstraint("chunk_index >= 0", name="ck_bank_parse_chunk_index_nonnegative"),
        CheckConstraint(
            "source_sha256 IS NULL OR length(source_sha256) = 64",
            name="ck_bank_parse_chunk_source_sha256",
        ),
        CheckConstraint(
            "source_size_bytes IS NULL OR source_size_bytes >= 0",
            name="ck_bank_parse_chunk_source_size",
        ),
        CheckConstraint(
            "status IN ('started', 'parsed', 'error')",
            name="ck_bank_parse_chunk_status",
        ),
        CheckConstraint(
            "telemetry_status IN ('pending', 'complete', 'telemetry_incomplete')",
            name="ck_bank_parse_chunk_telemetry_status",
        ),
        CheckConstraint(
            "NOT evidence_eligible OR telemetry_status = 'complete'",
            name="ck_bank_parse_chunk_evidence_complete",
        ),
        CheckConstraint(
            "lifecycle_status IS NULL OR lifecycle_status IN "
            "('pending', 'leased', 'succeeded', 'review_required', 'failed')",
            name="ck_bank_parse_chunk_lifecycle_status",
        ),
        CheckConstraint(
            "page_start IS NULL OR (page_start >= 1 AND page_end >= page_start)",
            name="ck_bank_parse_chunk_page_range",
        ),
        CheckConstraint(
            "span_start IS NULL OR (span_start >= 0 AND span_end >= span_start)",
            name="ck_bank_parse_chunk_span_range",
        ),
        CheckConstraint(
            "chunk_revision IS NULL OR chunk_revision >= 0",
            name="ck_bank_parse_chunk_revision_nonnegative",
        ),
        CheckConstraint(
            "application_retry_count IS NULL OR "
            "application_retry_count BETWEEN 0 AND 1",
            name="ck_bank_parse_chunk_retry_bounded",
        ),
        CheckConstraint(
            "(lifecycle_status IS NULL AND parser_version IS NULL "
            "AND extractor_version IS NULL AND prompt_schema_version IS NULL "
            "AND page_start IS NULL AND page_end IS NULL "
            "AND span_start IS NULL AND span_end IS NULL "
            "AND chunk_revision IS NULL AND supersedes_chunk_id IS NULL "
            "AND resolution_payload IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL "
            "AND application_retry_count IS NULL) OR "
            "(lifecycle_status IS NOT NULL AND source_sha256 IS NOT NULL "
            "AND parser_version IS NOT NULL "
            "AND extractor_version IS NOT NULL AND prompt_schema_version IS NOT NULL "
            "AND page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND span_start IS NOT NULL AND span_end IS NOT NULL "
            "AND chunk_revision IS NOT NULL AND application_retry_count IS NOT NULL "
            "AND organization_id IS NOT NULL "
            "AND organization_company_id IS NOT NULL)",
            name="ck_bank_parse_chunk_v2_identity",
        ),
        CheckConstraint(
            "supersedes_chunk_id IS NULL OR lifecycle_status IS NOT NULL",
            name="ck_bank_parse_chunk_successor_v2",
        ),
        CheckConstraint(
            "lifecycle_status != 'leased' OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL)",
            name="ck_bank_parse_chunk_active_lease",
        ),
        UniqueConstraint(
            "bank_statement_import_id",
            "parser_version",
            "source_sha256",
            "page_start",
            "page_end",
            "span_start",
            "span_end",
            "chunk_revision",
            name="uq_bank_parse_chunk_logical_identity",
        ),
        ForeignKeyConstraint(
            [
                "supersedes_chunk_id",
                "bank_statement_import_id",
                "realm_id",
                "organization_id",
                "organization_company_id",
            ],
            [
                "bank_statement_parse_chunks.id",
                "bank_statement_parse_chunks.bank_statement_import_id",
                "bank_statement_parse_chunks.realm_id",
                "bank_statement_parse_chunks.organization_id",
                "bank_statement_parse_chunks.organization_company_id",
            ],
            name="fk_bank_parse_chunk_supersedes",
            ondelete="RESTRICT",
            onupdate="CASCADE",
        ),
        UniqueConstraint(
            "supersedes_chunk_id",
            name="uq_bank_parse_chunk_supersedes",
        ),
        UniqueConstraint(
            "delivery_attempt_id",
            "chunk_index",
            name="uq_bank_parse_chunk_delivery_index",
        ),
        Index(
            "ix_bank_parse_chunk_import_delivery",
            "bank_statement_import_id",
            "delivery_attempt_id",
        ),
    )


class BankStatementParseProviderAttempt(Base):
    """One physical provider invocation and its raw evidence envelope.

    The existing ``llm_usage_events`` table remains the sole paid-call ledger.
    ``usage_event_id`` is a one-to-one correlation into that ledger; the copied
    provider-returned metrics make recorder loss observable without inventing a
    replacement ledger row or blocking the bank-statement feature.
    """

    __tablename__ = "bank_statement_parse_provider_attempts"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    bank_statement_import_id = Column(Integer, nullable=False)
    delivery_attempt_id = Column(Uuid, nullable=False, index=True)
    chunk_id = Column(Uuid, nullable=False, index=True)
    attempt_ordinal = Column(Integer, nullable=True)
    provider_call_id = Column(String, nullable=True, unique=True, index=True)
    lifecycle_status = Column(String, nullable=True, index=True)
    realm_id = Column(String, nullable=False, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_company_id = Column(
        Integer,
        ForeignKey("organization_companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    provider = Column(String, nullable=False)
    model = Column(String, nullable=False)
    lane = Column(String, nullable=False)
    status = Column(String, nullable=False, default="started", server_default="started")
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cached_input_tokens = Column(Integer, nullable=True)
    cache_creation_input_tokens = Column(Integer, nullable=True)
    cache_read_input_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Numeric(precision=12, scale=6), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    error_type = Column(String, nullable=True)
    token_usage_reported = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    cache_tokens_reported = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    output_truncated = Column(Boolean, nullable=True)
    tenant_tag_succeeded = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    recorder_succeeded = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    usage_event_id = Column(
        Integer,
        ForeignKey("llm_usage_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            [
                "delivery_attempt_id",
                "bank_statement_import_id",
                "realm_id",
                "organization_id",
                "organization_company_id",
            ],
            [
                "bank_statement_parse_delivery_attempts.id",
                "bank_statement_parse_delivery_attempts.bank_statement_import_id",
                "bank_statement_parse_delivery_attempts.realm_id",
                "bank_statement_parse_delivery_attempts.organization_id",
                "bank_statement_parse_delivery_attempts.organization_company_id",
            ],
            name="fk_bank_parse_provider_delivery_identity",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        UniqueConstraint(
            "usage_event_id",
            name="uq_bank_parse_provider_usage_event",
        ),
        Index(
            "ix_bank_parse_provider_import",
            "bank_statement_import_id",
        ),
        Index(
            "ix_bank_parse_provider_org_company",
            "organization_company_id",
        ),
        ForeignKeyConstraint(
            [
                "chunk_id",
                "bank_statement_import_id",
                "realm_id",
                "organization_id",
                "organization_company_id",
            ],
            [
                "bank_statement_parse_chunks.id",
                "bank_statement_parse_chunks.bank_statement_import_id",
                "bank_statement_parse_chunks.realm_id",
                "bank_statement_parse_chunks.organization_id",
                "bank_statement_parse_chunks.organization_company_id",
            ],
            name="fk_bank_parse_provider_chunk_identity",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_company_id", "organization_id", "realm_id"],
            [
                "organization_companies.id",
                "organization_companies.organization_id",
                "organization_companies.realm_id",
            ],
            name="fk_bank_parse_provider_tenant_identity",
            onupdate="CASCADE",
        ),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_bank_parse_input_tokens"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_bank_parse_output_tokens"),
        CheckConstraint(
            "cached_input_tokens IS NULL OR cached_input_tokens >= 0",
            name="ck_bank_parse_cached_input_tokens",
        ),
        CheckConstraint(
            "cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0",
            name="ck_bank_parse_cache_creation_tokens",
        ),
        CheckConstraint(
            "cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0",
            name="ck_bank_parse_cache_read_tokens",
        ),
        CheckConstraint("total_tokens IS NULL OR total_tokens >= 0", name="ck_bank_parse_total_tokens"),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_bank_parse_estimated_cost",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_bank_parse_latency"),
        CheckConstraint(
            "status IN ('started', 'ok', 'error')",
            name="ck_bank_parse_provider_status",
        ),
        CheckConstraint(
            "NOT recorder_succeeded OR usage_event_id IS NOT NULL",
            name="ck_bank_parse_provider_recorder_link",
        ),
        CheckConstraint(
            "NOT cache_tokens_reported OR "
            "cached_input_tokens IS NOT NULL OR "
            "cache_creation_input_tokens IS NOT NULL OR "
            "cache_read_input_tokens IS NOT NULL",
            name="ck_bank_parse_provider_cache_proof",
        ),
        CheckConstraint(
            "attempt_ordinal IS NULL OR attempt_ordinal >= 0",
            name="ck_bank_parse_provider_ordinal_nonnegative",
        ),
        CheckConstraint(
            "lifecycle_status IS NULL OR lifecycle_status IN "
            "('started', 'succeeded', 'failed', 'truncated', 'timed_out')",
            name="ck_bank_parse_provider_lifecycle_status",
        ),
        CheckConstraint(
            "(lifecycle_status IS NULL AND attempt_ordinal IS NULL "
            "AND provider_call_id IS NULL) OR "
            "(lifecycle_status IS NOT NULL AND attempt_ordinal IS NOT NULL "
            "AND provider_call_id IS NOT NULL "
            "AND organization_id IS NOT NULL "
            "AND organization_company_id IS NOT NULL)",
            name="ck_bank_parse_provider_v2_identity",
        ),
        CheckConstraint(
            "lifecycle_status IS NULL OR usage_event_id IS NOT NULL",
            name="ck_bank_parse_provider_terminal_usage",
        ),
        UniqueConstraint(
            "chunk_id",
            "attempt_ordinal",
            name="uq_bank_parse_provider_chunk_ordinal",
        ),
        Index(
            "ix_bank_parse_provider_delivery_started",
            "delivery_attempt_id",
            "started_at",
        ),
    )


class BankStatementParseCandidate(Base):
    """One staged statement line before controlled feed-row persistence."""

    __tablename__ = "bank_statement_parse_candidates"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    statement_import_id = Column(Integer, nullable=False, index=True)
    chunk_id = Column(Uuid, nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    # Projection link for this exact import occurrence. Multiple imports may
    # reuse one canonical feed row, while an import may bind that row to only
    # one source ordinal. Staged/rejected rows are never projection-authorized.
    bank_feed_item_id = Column(Integer, nullable=True)
    source_ordinal = Column(Integer, nullable=False)
    source_page = Column(Integer, nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    parser_version = Column(String, nullable=False)
    extractor_version = Column(String, nullable=False)
    prompt_schema_version = Column(String, nullable=False)
    txn_date = Column(Date, nullable=True)
    signed_amount = Column(Numeric(precision=18, scale=2), nullable=True)
    description = Column(Text, nullable=True)
    txn_type = Column(String, nullable=True)
    source_text = Column(Text, nullable=False)
    resolution_status = Column(
        String,
        nullable=False,
        default="staged",
        server_default="staged",
    )
    resolution_payload = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["statement_import_id", "realm_id"],
            ["bank_statement_imports.id", "bank_statement_imports.realm_id"],
            name="fk_bank_parse_candidate_import_realm",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "statement_import_id", "realm_id"],
            [
                "bank_statement_parse_chunks.id",
                "bank_statement_parse_chunks.bank_statement_import_id",
                "bank_statement_parse_chunks.realm_id",
            ],
            name="fk_bank_parse_candidate_chunk_import_realm",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["bank_feed_item_id", "realm_id"],
            ["bank_feed_items.id", "bank_feed_items.realm_id"],
            name="fk_bank_parse_candidate_feed_realm",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "statement_import_id",
            "source_ordinal",
            name="uq_bank_parse_candidate_import_ordinal",
        ),
        UniqueConstraint(
            "statement_import_id",
            "bank_feed_item_id",
            name="uq_bank_parse_candidate_import_feed_item",
        ),
        UniqueConstraint(
            "statement_import_id",
            "realm_id",
            "chunk_id",
            "source_ordinal",
            "bank_feed_item_id",
            name="uq_bank_parse_candidate_projection_identity",
        ),
        CheckConstraint(
            "source_ordinal >= 0",
            name="ck_bank_parse_candidate_ordinal_nonnegative",
        ),
        CheckConstraint(
            "source_page >= 1",
            name="ck_bank_parse_candidate_page_positive",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_bank_parse_candidate_source_sha256",
        ),
        CheckConstraint(
            "resolution_status IN ('staged', 'accepted', 'corrected', 'rejected')",
            name="ck_bank_parse_candidate_resolution_status",
        ),
        CheckConstraint(
            "bank_feed_item_id IS NULL OR "
            "resolution_status IN ('accepted', 'corrected')",
            name="ck_bank_parse_candidate_projection_status",
        ),
    )


class BankStatementControlCertificate(Base):
    """Versioned, code-calculated statement completeness and balance proof."""

    __tablename__ = "bank_statement_control_certificates"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    statement_import_id = Column(Integer, nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    certificate_version = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="pending", server_default="pending")
    source_sha256 = Column(String(64), nullable=False)
    parser_version = Column(String, nullable=False)
    extractor_version = Column(String, nullable=False)
    prompt_schema_version = Column(String, nullable=False)
    expected_count = Column(Integer, nullable=False)
    observed_count = Column(Integer, nullable=False)
    accepted_count = Column(Integer, nullable=False)
    rejected_count = Column(Integer, nullable=False)
    candidate_set_sha256 = Column(String(64), nullable=False)
    opening_balance = Column(Numeric(precision=18, scale=2), nullable=True)
    signed_activity = Column(Numeric(precision=18, scale=2), nullable=True)
    closing_balance = Column(Numeric(precision=18, scale=2), nullable=True)
    residual = Column(Numeric(precision=18, scale=2), nullable=True)
    calculated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["statement_import_id", "realm_id"],
            ["bank_statement_imports.id", "bank_statement_imports.realm_id"],
            name="fk_bank_control_certificate_import_realm",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "statement_import_id",
            "certificate_version",
            name="uq_bank_control_certificate_import_version",
        ),
        CheckConstraint(
            "certificate_version >= 1",
            name="ck_bank_control_certificate_version_positive",
        ),
        CheckConstraint(
            "status IN ('pending', 'failed', 'passed')",
            name="ck_bank_control_certificate_status",
        ),
        CheckConstraint(
            "expected_count >= 0 AND observed_count >= 0 "
            "AND accepted_count >= 0 AND rejected_count >= 0",
            name="ck_bank_control_certificate_counts_nonnegative",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_bank_control_certificate_source_sha256",
        ),
        CheckConstraint(
            "length(candidate_set_sha256) = 64",
            name="ck_bank_control_certificate_candidate_sha256",
        ),
        CheckConstraint(
            "status != 'passed' OR (opening_balance IS NOT NULL "
            "AND signed_activity IS NOT NULL AND closing_balance IS NOT NULL "
            "AND residual IS NOT NULL AND abs(residual) <= 0.01 "
            "AND residual = opening_balance + signed_activity - closing_balance "
            "AND observed_count = expected_count "
            "AND accepted_count + rejected_count = observed_count)",
            name="ck_bank_control_certificate_passed",
        ),
    )


class ReconciliationSessionStatementLine(Base):
    """Frozen statement-side membership for one reconciliation session."""

    __tablename__ = "reconciliation_session_statement_lines"

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    reconciliation_session_id = Column(Integer, nullable=False, index=True)
    bank_feed_item_id = Column(Integer, nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    statement_import_id = Column(Integer, nullable=True, index=True)
    parse_chunk_id = Column(Uuid, nullable=True)
    source_ordinal = Column(Integer, nullable=True)
    lineage_status = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["reconciliation_session_id", "realm_id"],
            ["reconciliation_sessions.id", "reconciliation_sessions.realm_id"],
            name="fk_recon_session_line_session_realm",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["reconciliation_session_id", "realm_id", "statement_import_id"],
            [
                "reconciliation_sessions.id",
                "reconciliation_sessions.realm_id",
                "reconciliation_sessions.statement_import_id",
            ],
            name="fk_recon_session_line_session_import_identity",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["bank_feed_item_id", "realm_id"],
            ["bank_feed_items.id", "bank_feed_items.realm_id"],
            name="fk_recon_session_line_feed_realm",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "statement_import_id",
                "realm_id",
                "parse_chunk_id",
                "source_ordinal",
                "bank_feed_item_id",
            ],
            [
                "bank_statement_parse_candidates.statement_import_id",
                "bank_statement_parse_candidates.realm_id",
                "bank_statement_parse_candidates.chunk_id",
                "bank_statement_parse_candidates.source_ordinal",
                "bank_statement_parse_candidates.bank_feed_item_id",
            ],
            name="fk_recon_session_line_candidate_projection_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["statement_import_id", "realm_id"],
            ["bank_statement_imports.id", "bank_statement_imports.realm_id"],
            name="fk_recon_session_line_import_realm",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parse_chunk_id", "statement_import_id", "realm_id"],
            [
                "bank_statement_parse_chunks.id",
                "bank_statement_parse_chunks.bank_statement_import_id",
                "bank_statement_parse_chunks.realm_id",
            ],
            name="fk_recon_session_line_chunk_import_realm",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "reconciliation_session_id",
            "bank_feed_item_id",
            name="uq_recon_session_statement_line",
        ),
        CheckConstraint(
            "lineage_status IN ('controlled', 'legacy_snapshot')",
            name="ck_recon_session_line_lineage_status",
        ),
        CheckConstraint(
            "(lineage_status = 'legacy_snapshot' AND statement_import_id IS NULL "
            "AND parse_chunk_id IS NULL AND source_ordinal IS NULL) OR "
            "(lineage_status = 'controlled' AND statement_import_id IS NOT NULL "
            "AND parse_chunk_id IS NOT NULL AND source_ordinal IS NOT NULL)",
            name="ck_recon_session_line_controlled_provenance",
        ),
        CheckConstraint(
            "source_ordinal IS NULL OR source_ordinal >= 0",
            name="ck_recon_session_line_ordinal_nonnegative",
        ),
    )


class BankReconAccountConfig(Base):
    """
    Per-account clean-start anchor for bank reconciliation: the last
    reconciled balance + date the user confirmed (or that was suggested from a
    QBO `cleared=Reconciled` enumeration and then user-confirmed — an
    approximation, never authoritative; SYNTHESIS 2A / DEC-BREC-5). Matching
    never touches transactions on/before opening_reconciled_date.

    Realm-keyed (never org-keyed) for the same RLS-axis reason as
    BankStatementImport above (DEC-BREC-2/3).
    """
    __tablename__ = "bank_recon_account_configs"

    # REALM-STRING-ONLY-OK: bank/recon-family table; SYNTHESIS §2B + DEC-BREC-2/3
    # ruled organization_id is the WRONG RLS axis here — enrolls on the `realm` GUC
    # like its recon/bank siblings (migration e7f1a9c3d2b5); rows key to a QBO
    # account under a realm. The C0-a migration enrolls it in RLS on realm_id.
    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    account_id = Column(String, nullable=False, index=True)   # QBO Account ID

    opening_reconciled_balance = Column(Numeric(precision=18, scale=2), nullable=True)
    opening_reconciled_date = Column(Date, nullable=True)

    # How the anchor was established: "manual" (user-set) | "qbo_inferred" (suggested, then confirmed).
    seed_source = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BankDescriptionRule(Base):
    """
    Learned mapping: bank description keyword → vendor/customer + chart-of-account.

    Created automatically each time a user Posts (approves) a bank feed item.
    Applied during fetch_and_cache_bank_feed to pre-fill incoming items before
    the AI classifier runs, so repeat transactions are categorized instantly.
    """
    __tablename__ = "bank_description_rules"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    # Normalized keyword extracted from the bank description (lowercased, stripped)
    keyword = Column(String, nullable=False, index=True)

    # Vendor or Customer this description maps to
    entity_type = Column(String, nullable=True)      # "vendor" | "customer"
    entity_id = Column(String, nullable=True)        # QBO ID
    entity_name = Column(String, nullable=True)      # Display name

    # Chart of account this description maps to
    target_account_id = Column(String, nullable=False)
    target_account_name = Column(String, nullable=True)

    # Usage stats — higher hit_count rules are preferred on collisions
    hit_count = Column(Integer, default=1)
    active = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Accounting Module Models
# ---------------------------------------------------------

class AccountingSchedule(Base):
    """
    A detected or manually created sub-ledger schedule for a prepaid, accrual,
    fixed asset, or intangible asset.

    Lifecycle: DETECTED → PENDING_APPROVAL → ACTIVE → FULLY_AMORTIZED | DISPOSED
    """
    __tablename__ = "accounting_schedules"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    # What kind of schedule is this? A code from the schedule-type descriptor
    # registry (app/services/schedule_types.py): PREPAID | ACCRUAL |
    # FIXED_ASSET | INTANGIBLE | GENERIC | DEFERRED_REVENUE | DEPOSIT |
    # DEBT_PRINCIPAL (presets store their own code; plain String, no DB enum).
    schedule_type = Column(String, nullable=False, index=True)

    name = Column(String, nullable=False)           # e.g. "Prepaid Insurance – Travelers 2026"
    description = Column(Text, nullable=True)
    vendor_name = Column(String, nullable=True)     # QBO vendor name
    note = Column(String, nullable=True)            # e.g. "1yr software subscription", "Prepaid Insurance"

    # Source transaction from QBO (may be null for manually-created schedules)
    qbo_txn_id = Column(String, nullable=True, index=True)
    qbo_account_id = Column(String, nullable=True)  # Balance-sheet account (Prepaid, FA, etc.)
    qbo_account_name = Column(String, nullable=True)
    expense_account_id = Column(String, nullable=True)   # P&L account for amortization/depreciation expense
    expense_account_name = Column(String, nullable=True)

    # Financials
    original_amount = Column(Numeric(precision=18, scale=2), nullable=False)
    remaining_amount = Column(Numeric(precision=18, scale=2), nullable=False)
    accumulated_amount = Column(Numeric(precision=18, scale=2), default=0)  # Depr/amort taken BEFORE Peregrine

    # Onboarding
    implementation_date = Column(Date, nullable=True)  # Date the company was brought onto Peregrine

    # Schedule dates
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)           # Null for open-ended accruals
    useful_life_months = Column(Integer, nullable=True)

    # Fixed asset / intangible specific
    asset_class = Column(String, nullable=True)       # COMPUTER | VEHICLE | FURNITURE | SOFTWARE | TRADEMARK | etc.
    depreciation_method = Column(String, default="STRAIGHT_LINE")  # STRAIGHT_LINE | DECLINING_BALANCE

    # Phase-B FA lifecycle (plan 20260702-phase-b-fa-lifecycle, AC-1/AC-2).
    #
    # salvage_value: the residual the asset never depreciates below. The
    # depreciable base everywhere in the engine is original − salvage; the
    # server_default 0 keeps every pre-Phase-B row byte-identical. Only
    # salvage-applicable registry types (FIXED_ASSET, INTANGIBLE) may carry a
    # non-zero value (DTO-enforced).
    salvage_value = Column(
        Numeric(precision=18, scale=2), nullable=False, default=0, server_default="0"
    )
    # asset_cost_account_id: the QBO asset COST account the disposal JE credits
    # to remove the asset. DELIBERATELY a separate column from qbo_account_id:
    # qbo_account_id is "the schedule's own BS account" — the FA sync
    # (sync-fixed-assets) writes the QBO AccountType='Fixed Asset' account
    # there (usually the cost account), but grid/import-created FA rows leave
    # it NULL and legacy realms may have pointed it at the accum-dep contra
    # (je_builder's per-entry fallback CREDITS it with default name
    # "Accumulated Depreciation"). A disposal JE that guessed wrong would
    # remove the wrong side of the asset, so the cost account is explicit and
    # disposal is blocked (409) until it is set. The contra (accum-dep) side
    # resolves through ScheduleCreditMapping exactly like monthly generation.
    asset_cost_account_id = Column(String, nullable=True)
    asset_cost_account_name = Column(String, nullable=True)

    # Disposal facts, written ONLY when the proposal-gated disposal JE
    # EXECUTES (never at draft time — a rejected proposal leaves the schedule
    # ACTIVE and these NULL). disposal_proposed_action_id is set at draft time
    # and doubles as the double-dispose guard (a PENDING/APPROVED linked
    # proposal blocks a second dispose).
    disposal_date = Column(Date, nullable=True)
    disposal_proceeds = Column(Numeric(precision=18, scale=2), nullable=True)
    disposal_gain_loss = Column(Numeric(precision=18, scale=2), nullable=True)  # + gain / − loss
    disposal_proposed_action_id = Column(Integer, nullable=True)  # FK → proposed_actions.id (soft, like ScheduleEntry.proposed_action_id)

    # Wave-2 AC-5 (plan 20260701-phase-a-generic-schedules): how the monthly
    # beat task treats this schedule.
    #   manual                  → the beat task skips it entirely (human runs
    #                             generation from the grid when ready)
    #   auto_propose (default)  → beat drafts the entries + JE ProposedActions
    #                             (PENDING, human approves — today's behavior,
    #                             now scheduled)
    #   auto_post_deterministic → the JE proposal is auto-approved ONLY when
    #                             every entry in the batch is a plain unmodified
    #                             straight-line period (never correctives,
    #                             true-ups, final plugs, manual overrides,
    #                             negatives, or non-SL methods) — otherwise the
    #                             whole batch falls back to propose. The QBO
    #                             write still flows through the existing
    #                             executor path (audit ledger, closed-period
    #                             gate); the policy changes WHEN approval
    #                             happens, never the write path.
    automation_policy = Column(
        String, nullable=False, default="auto_propose", server_default="auto_propose"
    )

    # Workflow state
    status = Column(String, default="DETECTED", index=True)
    # DETECTED → AI flagged, awaiting human confirmation
    # PENDING_APPROVAL → human was asked, submission in progress
    # ACTIVE → confirmed, entries generating
    # FULLY_AMORTIZED → remaining_amount == 0
    # DISPOSED → asset sold/retired early

    # AI detection metadata
    detection_confidence = Column(Numeric(precision=5, scale=4), nullable=True)
    ai_reasoning = Column(Text, nullable=True)

    # Human review
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ScheduleEntry(Base):
    """
    An individual amortization/depreciation/accrual line for a specific period.
    Each entry becomes a ProposedAction (journal entry) pending human approval.
    """
    __tablename__ = "schedule_entries"
    __table_args__ = (
        UniqueConstraint("schedule_id", "period", name="uq_schedule_entries_schedule_period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(
        Integer,
        ForeignKey("accounting_schedules.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    realm_id = Column(String, index=True, nullable=False)

    period = Column(String, nullable=False, index=True)         # YYYY-MM
    amount = Column(Numeric(precision=18, scale=2), nullable=False)

    # Link to the journal entry proposal created for this entry
    proposed_action_id = Column(Integer, nullable=True)         # FK → proposed_actions.id

    # Wave-2 AC-6 (plan 20260701-phase-a-generic-schedules): auto-reversing
    # accruals. When a reversal-flavored entry POSTS, the engine auto-DRAFTs
    # the period+1 reversal (opposite sign) linked back to the posted entry
    # via this FK. A non-NULL value marks the row as a reversal — the guard
    # against reversal-of-reversal chains (a reversal never spawns another).
    reversal_of_entry_id = Column(
        Integer,
        ForeignKey("schedule_entries.id", ondelete="SET NULL"),
        nullable=True,
    )

    status = Column(String, default="DRAFT", index=True)
    # DRAFT → generated, not yet proposed
    # PENDING_APPROVAL → ProposedAction created
    # POSTED → JE executed in QBO
    # SKIPPED → human chose to skip this period

    posted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ScheduleCreditMapping(Base):
    """
    Maps a schedule type — optionally narrowed to one schedule BS account —
    to the BS account that should be credited when generating consolidated
    amortization JEs.

    AC-3 (plan 20260701-phase-a-generic-schedules): uniqueness is relaxed
    from one-row-per-(realm, type) to per-(realm, type, source account).
      * source_account_id NULL  → the TYPE-LEVEL default row (every pre-AC-3
        row is this shape; they keep resolving unchanged).
      * source_account_id set   → an ACCOUNT-LEVEL override that applies only
        to schedules whose AccountingSchedule.qbo_account_id matches.
    Resolution order: account-level → type-level → the schedule's own BS
    account (engine fallback).

    qbo_account_id / qbo_account_name remain the CREDIT-TARGET account
    (unchanged meaning — proactive detection and the tie-out read them as
    the mapped/watched credit accounts).

    NULL-uniqueness note: Postgres treats NULLs as distinct inside a unique
    constraint, so the constraint alone cannot forbid two default rows for
    one (realm, type). The PUT /schedule-credit-mapping upsert is the single
    writer and updates-in-place; readers resolve deterministically
    (order_by id, first match wins).
    """
    __tablename__ = "schedule_credit_mappings"
    __table_args__ = (
        UniqueConstraint(
            "realm_id", "schedule_type", "source_account_id",
            name="uq_credit_mapping_account",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    schedule_type = Column(String, nullable=False)   # a schedule_types registry code

    # The schedule BS account this mapping is scoped to (NULL = type default)
    source_account_id = Column(String, nullable=True)
    source_account_name = Column(String, nullable=True)

    # The credit-target account
    qbo_account_id = Column(String, nullable=False)
    qbo_account_name = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ScheduleAccountCoverage(Base):
    """
    Per-(realm, QBO account) manual reconciliation flag for the coverage view
    (Wave-2 AC-7, plan 20260701-phase-a-generic-schedules).

    The coverage endpoint derives ``scheduled`` status live (an ACTIVE
    AccountingSchedule maps the account, or a ScheduleCreditMapping targets it
    as the credit/contra account of a type with active schedules). This table
    persists ONLY the human judgment the data cannot derive: "this
    balance-sheet account is reconciled outside Peregrine" (bank rec, external
    subledger, equity rollforward in the GL, …). Absence of a row = not
    flagged.
    """
    __tablename__ = "schedule_account_coverage"
    __table_args__ = (
        UniqueConstraint("realm_id", "qbo_account_id", name="uq_schedule_account_coverage"),
    )

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    qbo_account_id = Column(String, nullable=False)
    qbo_account_name = Column(String, nullable=True)

    reconciled_elsewhere = Column(Boolean, nullable=False, default=False)
    note = Column(String, nullable=True)
    updated_by = Column(String, nullable=True)   # user email

    # PLAN-EXEMPT: chunk 1 of ratified plan 20260714-wave3-2-asc606-engine (marker
    # RATIFICATION-2026-07-14.md on disk; active-plan.json not synced into this worktree).
    # E3/E7 double-recognition seam (plan 20260714-wave3-2-asc606-engine, decision 5).
    # The RevenueContract that EXCLUSIVELY owns recognition of this deferred-revenue
    # BS account. NULL = unowned / legitimately shared (many AccountingSchedules and/or
    # a contract co-exist on one Deferred Revenue account — co-existence is NOT a
    # conflict). Set by the contract-activation service (Chunk 3) so the ASC-606 engine
    # and the AccountingSchedule DEFERRED_REVENUE engine cannot both recognize the SAME
    # account for the SAME source. reconciled_elsewhere stays the human "reconciled
    # outside Peregrine" judgment the tie-out (Chunk 4) reads; this ref is the machine
    # exclusive-ownership partition. Nullable FK, SET NULL on contract delete.
    owning_contract_id = Column(
        Integer,
        ForeignKey("revenue_contracts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @classmethod
    def deferred_rev_recognition_conflict(
        cls, db, realm_id, qbo_account_id, requesting_contract_id=None
    ) -> bool:
        """
        E3/E7 same-source double-recognition guard (plan 20260714-wave3-2-asc606-engine,
        decision 5; AC-3). The service-callable check the contract-create/activate path
        (Chunk 3) consults before recognizing revenue against a QBO deferred-revenue
        account.

        Returns ``True`` (REFUSE) ONLY when the account is ALREADY exclusively owned for
        recognition by a DIFFERENT RevenueContract on this realm — a genuine same-source
        double-recognition. Returns ``False`` (ALLOW) for:
          * an account with no coverage row, or ``owning_contract_id IS NULL`` — a
            LEGITIMATE shared Deferred Revenue account (many schedules / an unclaimed
            account) is NOT falsely refused (the E7 anti-over-block requirement);
          * the SAME contract re-checking its own account (idempotent activate/modify).

        The predicate is realm-scoped (tenant-safe) and account-level (the pinned E7
        partition granularity).
        """
        row = (
            db.query(cls)
            .filter(cls.realm_id == realm_id, cls.qbo_account_id == qbo_account_id)
            .first()
        )
        if row is None or row.owning_contract_id is None:
            return False
        return row.owning_contract_id != requesting_contract_id


class LeaseSchedule(Base):
    """
    An ASC 842 lease (plan 20260702-phase-c-asc842, AC-1). Leases get their
    OWN engine + entities — effective-interest math must not contort the
    straight-line AccountingSchedule substrate — but ride the SAME rails:
    JOURNAL_ENTRY ProposedAction multi-line payloads, the Wave-2 beat,
    automation_policy semantics, period locks, tie-out, coverage.

    Lifecycle: DRAFT → ACTIVE → TERMINATED | EXPIRED.
      * DRAFT: terms editable, no rows, nothing recognized.
      * ACTIVE (POST /leases/{id}/activate): liability = PV(fixed payments at
        the LOCKED discount rate), ROU = liability + IDC + prepaid −
        incentives, rows persisted, commencement JE drafted (ALWAYS
        proposal-gated).
      * TERMINATED: set ONLY when the proposal-gated termination JE EXECUTES
        (a rejected proposal changes nothing) — mirrors Phase-B disposal.
      * EXPIRED: the natural end of term (final row posted).

    Measurement conventions (see app/services/lease_engine.py):
      * monthly rate = discount_rate_annual / 12 (documented convention).
      * discount_rate_annual + rate_basis are LOCKED at inception — a
        remeasurement reuses the ORIGINAL rate unless a rate change is
        explicit in the remeasure request (ASC 842 modification semantics).
      * short_term_exempt (term ≤ 12 months, ASC 842-20-25-2 election —
        binding US GAAP): NO rows, NO commencement JE; rent is expensed as
        incurred outside Peregrine (documented expense-as-incurred).
    """
    __tablename__ = "lease_schedules"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)

    name = Column(String, nullable=False)            # e.g. "HQ office — 500 Main St"
    description = Column(Text, nullable=True)

    # Classification: operating | finance, plus the stored answers to the five
    # ASC 842-10-25-2 criteria (transfers_ownership,
    # purchase_option_reasonably_certain, term_major_part_of_life,
    # pv_substantially_all_fair_value, specialized_asset). Any TRUE criterion
    # forces finance (router-enforced); the answers are kept for audit.
    classification = Column(String, nullable=False, index=True)
    classification_criteria = Column(JSON, nullable=True)

    commencement_date = Column(Date, nullable=False)
    term_months = Column(Integer, nullable=False)

    # Payments: fixed monthly amount; fixed escalations stored as a
    # stepped-payment JSON [{"start_month": 13, "payment": "1100.00"}, ...]
    # (1-based month index, strictly increasing; amounts as strings so the
    # engine reads exact Decimals, never floats).
    payment_amount = Column(Numeric(precision=18, scale=2), nullable=False)
    payment_frequency = Column(String, nullable=False, default="monthly", server_default="monthly")
    payment_timing = Column(String, nullable=False, default="arrears", server_default="arrears")  # advance | arrears
    escalation_steps = Column(JSON, nullable=True)

    # The discount rate — LOCKED at inception.
    discount_rate_annual = Column(Numeric(precision=9, scale=6), nullable=False)
    rate_basis = Column(String, nullable=False, default="ibr", server_default="ibr")  # ibr | risk_free (ASC 842-20-30-3 private-co election)

    initial_direct_costs = Column(Numeric(precision=18, scale=2), nullable=False, default=0, server_default="0")
    incentives = Column(Numeric(precision=18, scale=2), nullable=False, default=0, server_default="0")
    prepaid_amount = Column(Numeric(precision=18, scale=2), nullable=False, default=0, server_default="0")
    short_term_exempt = Column(Boolean, nullable=False, default=False, server_default="false")

    # Measured at activation (NULL while DRAFT / for exempt leases):
    # initial_liability = PV; initial_rou = liability + IDC + prepaid − incentives.
    initial_liability = Column(Numeric(precision=18, scale=2), nullable=True)
    initial_rou = Column(Numeric(precision=18, scale=2), nullable=True)

    # QBO accounts. Liability is ONE account in v1 — the current/long-term
    # split is REPORTED by the disclosures endpoint, not posted as a monthly
    # QBO reclass (documented convention, plan header).
    rou_asset_account_id = Column(String, nullable=True)
    rou_asset_account_name = Column(String, nullable=True)
    accum_rou_amort_account_id = Column(String, nullable=True)
    accum_rou_amort_account_name = Column(String, nullable=True)
    lease_liability_account_id = Column(String, nullable=True)
    lease_liability_account_name = Column(String, nullable=True)
    interest_expense_account_id = Column(String, nullable=True)
    interest_expense_account_name = Column(String, nullable=True)
    lease_expense_account_id = Column(String, nullable=True)       # operating single lease cost
    lease_expense_account_name = Column(String, nullable=True)
    rou_amort_expense_account_id = Column(String, nullable=True)   # finance only
    rou_amort_expense_account_name = Column(String, nullable=True)

    # Reuses the Phase-A vocabulary (schedule_types.AUTOMATION_POLICIES):
    # manual | auto_propose | auto_post_deterministic. auto_post applies ONLY
    # to plain unremeasured monthly rows — commencement / remeasurement /
    # termination JEs are ALWAYS proposal-gated regardless of policy.
    automation_policy = Column(String, nullable=False, default="auto_propose", server_default="auto_propose")

    status = Column(String, nullable=False, default="DRAFT", server_default="DRAFT", index=True)

    # Lifecycle proposal linkage (set at draft time — the double-draft guards)
    # + termination facts (written ONLY when the termination JE EXECUTES).
    commencement_proposed_action_id = Column(Integer, nullable=True)
    remeasure_proposed_action_id = Column(Integer, nullable=True)
    termination_proposed_action_id = Column(Integer, nullable=True)
    termination_date = Column(Date, nullable=True)
    termination_settlement = Column(Numeric(precision=18, scale=2), nullable=True)
    termination_gain_loss = Column(Numeric(precision=18, scale=2), nullable=True)  # + gain / − loss

    created_by = Column(String, nullable=True)   # user email
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LeaseScheduleRow(Base):
    """
    One persisted, IMMUTABLE amortization month of a LeaseSchedule (plan
    20260702-phase-c-asc842, AC-1/AC-2). Rows are written once at activation
    and regenerated ONLY via the gated remeasurement path (rows ≥ the
    effective period, rebuilt when the remeasurement proposal EXECUTES, with
    remeasured=True). Posted/locked periods are never restated.

    Column semantics (all Decimal, quantized to cents by the engine):
      payment             contractual payment for the month
      interest            effective interest (beginning liability × monthly
                          rate; engine-owned final-row plug zeroes liability)
      liability_reduction payment − interest
      ending_liability    running liability balance (0.00 on the final row)
      rou_amortization    operating: expense − interest (the ROU plug);
                          finance: straight-line ROU ÷ term (final-row plug)
      ending_rou          running ROU net balance (0.00 on the final row)
      expense             operating: the single straight-line lease cost;
                          finance: interest + rou_amortization (P&L total)
    """
    __tablename__ = "lease_schedule_rows"
    __table_args__ = (
        UniqueConstraint("lease_id", "period", name="uq_lease_schedule_rows_lease_period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    lease_id = Column(
        Integer,
        ForeignKey("lease_schedules.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    realm_id = Column(String, index=True, nullable=False)

    period = Column(String, nullable=False, index=True)  # YYYY-MM

    payment = Column(Numeric(precision=18, scale=2), nullable=False)
    interest = Column(Numeric(precision=18, scale=2), nullable=False)
    liability_reduction = Column(Numeric(precision=18, scale=2), nullable=False)
    ending_liability = Column(Numeric(precision=18, scale=2), nullable=False)
    rou_amortization = Column(Numeric(precision=18, scale=2), nullable=False)
    ending_rou = Column(Numeric(precision=18, scale=2), nullable=False)
    expense = Column(Numeric(precision=18, scale=2), nullable=False)

    # True on rows rebuilt by an executed remeasurement — such rows NEVER
    # auto-post (auto_post_deterministic covers plain unremeasured rows only).
    remeasured = Column(Boolean, nullable=False, default=False, server_default="false")

    proposed_action_id = Column(Integer, nullable=True)  # FK → proposed_actions.id (soft, like ScheduleEntry)
    status = Column(String, nullable=False, default="DRAFT", server_default="DRAFT", index=True)
    # DRAFT → generated, not yet proposed
    # PENDING_APPROVAL → JE ProposedAction drafted
    # POSTED → JE executed in QBO (flipped by the post-execution lease hook)
    # SKIPPED → human skipped / superseded by termination

    posted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FinancialCalendar(Base):
    """
    Tracks the accounting close state for each period per company.
    Drives automated month-end workflows.
    """
    __tablename__ = "financial_calendar"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)         # YYYY-MM

    status = Column(String, default="OPEN", index=True)
    # OPEN → current period, accepting new transactions
    # MONTH_END_IN_PROGRESS → workflows running
    # CLOSED → all adjusting entries posted, period locked

    # Month-end workflow tracking
    month_end_triggered_at = Column(DateTime(timezone=True), nullable=True)
    month_end_completed_at = Column(DateTime(timezone=True), nullable=True)

    # Summary counts populated during workflow
    missing_accruals_count = Column(Integer, default=0)
    adjusting_entries_count = Column(Integer, default=0)
    schedules_processed = Column(Integer, default=0)

    notes = Column(JSON, nullable=True)                         # Workflow run log

    # Hard period lock — prevents any further writes to QBO for this period
    locked = Column(Boolean, default=False)
    locked_by = Column(String, nullable=True)   # User email or ID
    locked_at = Column(DateTime(timezone=True), nullable=True)
    lock_notes = Column(Text, nullable=True)    # Reason if break-glass unlock used

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdjustingEntry(Base):
    """
    Links a ScheduleEntry (or an ad-hoc period-end detection) to the
    ProposedAction it generated, providing full traceability.
    """
    __tablename__ = "adjusting_entries"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)

    schedule_id = Column(Integer, nullable=True, index=True)    # FK → accounting_schedules.id (null for ad-hoc)
    schedule_entry_id = Column(Integer, nullable=True, index=True)  # FK → schedule_entries.id

    entry_type = Column(String, nullable=False)
    # PREPAID_AMORTIZATION | DEPRECIATION | AMORTIZATION | ACCRUAL | MANUAL

    proposed_action_id = Column(Integer, nullable=True, index=True)  # FK → proposed_actions.id

    status = Column(String, default="DRAFT", index=True)
    # DRAFT | PENDING_APPROVAL | APPROVED | POSTED | REJECTED

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Cash Flow Forecast Models
# ---------------------------------------------------------

class CashForecastOverride(Base):
    """
    Stores user-entered overrides for individual cells in the 13-week
    rolling cash flow forecast (customer collections or vendor outflows).
    When present, overrides the QBO-derived projected amount for that week.
    """
    __tablename__ = "cash_forecast_overrides"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "entity_name",
            "entity_type",
            "week_ending",
            name="uq_cash_forecast_override_cell",
        ),
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-3 #16) — composite hot-path
        # index: every forecast read filters (company_id, week_ending ∈ window ∪ sentinels).
        Index("ix_cash_forecast_overrides_company_week", "company_id", "week_ending"),
    )

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)

    entity_name = Column(String, nullable=False)         # Customer or Vendor display name
    entity_type = Column(String, nullable=False)          # "customer" | "vendor"
    week_ending = Column(Date, nullable=False, index=True) # Friday of the forecast week

    override_amount = Column(Numeric(precision=18, scale=2), nullable=False)
    note = Column(Text, nullable=True)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CashForecastActual(Base):
    """
    Categorized bank transactions that feed the actuals (lookback) columns
    of the 13-week cash flow forecast.

    Workflow:
      1. Accountant categorizes bank feeds in QBO
      2. Peregrine pulls bank transactions for the prior week
      3. User confirms/adjusts entity mapping in Peregrine
      4. Categorized actuals populate the lookback column(s)

    For Bill.com payments: a parent row holds the lump-sum amount and child
    rows hold the per-vendor decomposition (linked via parent_id).
    """
    __tablename__ = "cash_forecast_actuals"
    __table_args__ = (
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-2 (F-3 #16) — composite hot-path
        # index: the per-week counts, actuals reconciliation, and uncategorized-banner reads all
        # filter (company_id, week_ending [+ category/parent_id]).
        Index("ix_cash_forecast_actuals_company_week", "company_id", "week_ending"),
    )

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)

    # Original QBO transaction reference
    qbo_txn_id = Column(String, nullable=False)           # QBO transaction ID
    qbo_txn_type = Column(String, nullable=False)         # Deposit, Purchase, Transfer, JournalEntry, BillPayment
    txn_date = Column(Date, nullable=False)
    amount = Column(Numeric(precision=18, scale=2), nullable=False)  # always positive
    direction = Column(String, nullable=False)             # "in" | "out"
    description = Column(String, nullable=True)            # bank memo / payee name
    qbo_account_id = Column(String, nullable=True, index=True)  # QBO cash account ID hit by this transaction
    account_name = Column(String, nullable=True)           # bank account name

    # User categorization
    entity_name = Column(String, nullable=True)            # Customer or Vendor display name
    entity_type = Column(String, nullable=True)            # "customer" | "vendor"
    category = Column(String, default="uncategorized")     # uncategorized | categorized | excluded | decomposed

    # Bill.com decomposition
    is_billcom = Column(Boolean, default=False)
    parent_id = Column(Integer, ForeignKey("cash_forecast_actuals.id"), nullable=True)

    # Week mapping
    week_ending = Column(Date, nullable=False, index=True)  # Friday of the week this belongs to

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CashForecastWeekStatus(Base):
    """
    Tracks the review lifecycle for each weekly actuals column.

    This lets the forecast distinguish "not pulled yet" from "pulled and
    genuinely no transactions", which is critical for safe missed-week
    handling in the rolling cash forecast.
    """
    __tablename__ = "cash_forecast_week_statuses"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "week_ending",
            name="uq_cash_forecast_week_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    week_ending = Column(Date, nullable=False, index=True)

    status = Column(String, nullable=False, default="not_pulled")
    transaction_count = Column(Integer, nullable=False, default=0)
    uncategorized_count = Column(Integer, nullable=False, default=0)
    reconciliation_delta = Column(Numeric(precision=18, scale=2), nullable=True)
    note = Column(Text, nullable=True)

    # PLAN-EXEMPT: AC-1 of ratified plan 20260619-phase2-2a-partial-data names a
    # nullable pull_partial column on CashForecastWeekStatus; app/models.py was
    # omitted from declared_scopes (clerical) — the migration is in-scope.
    # True when the QBO pull that produced this week's actuals had >=1 swallowed
    # fetch failure (a partial pull) — lets the forecast + /actuals/close tell a
    # genuinely-complete week apart from one built on incomplete data (2A).
    pull_partial = Column(Boolean, nullable=True)

    pulled_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CashForecastSnapshot(Base):
    """
    Cached snapshot of the fully-computed cash flow forecast grid.

    Stores the QBO-derived projection data (open invoices, bills, DSO/DPO,
    historical patterns, hybrid fill) so that normal page loads don't need
    to hit QBO at all. One row per company — updated when the user explicitly
    clicks "Refresh from QBO".

    User overrides, actuals, and balance adjustments are applied ON TOP of
    the snapshot at read time, so they're always fresh.
    """
    __tablename__ = "cash_forecast_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False, unique=True)

    # Complete forecast data — the raw projection grid before overrides
    snapshot_data = Column(JSON, nullable=False)

    # Bank balance at time of snapshot (for beginning balance default)
    qbo_bank_balance = Column(Numeric(precision=18, scale=2), nullable=True)

    computed_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CashForecastSnapshotVersion(Base):
    """Frozen weekly capture of the 13-week forecast AS IT WAS SERVED — the
    provenance record forecast-accuracy tracking scores against (plan
    20260706-cfo-grade-financial-workflows, AC-2B.1).

    ``CashForecastSnapshot`` is deliberately latest-only (unique company_id):
    it is the serve cache and its contract is UNCHANGED. This table is the
    versioned sibling: one row per (company_id, as_of_week_ending), where
    ``as_of_week_ending`` is the first FORECAST Friday at capture time. Within
    its own week a row is upserted (last build of the week wins — the nightly
    beat keeps it current); once the calendar rolls past that Friday the row is
    frozen and is never overwritten, so "the forecast that was made N weeks
    ago" survives re-anchoring. Accuracy math (MAPE + bias) compares actuals to
    THESE frozen grids, never to the continuously re-anchored serve payload —
    scoring against the revised grid is tautological (cashflow-modeling §6).

    ``grid`` stores the compact served view: week_headers, lookback, per-entity
    customer/vendor amounts, total_cash_in/out, beginning/ending balances, and
    the bank anchor. Realm-keyed under live RLS (company_id →
    app.current_realm_id, like cash_forecast_snapshots), with the org FK for
    org-keyed joins/audits.
    """
    __tablename__ = "cash_forecast_snapshot_versions"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "as_of_week_ending",
            name="uq_cff_snapshot_version_week",
        ),
        Index(
            "ix_cff_snapshot_versions_company_asof",
            "company_id",
            "as_of_week_ending",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)

    # The first forecast Friday at capture time — the week this version was "made".
    as_of_week_ending = Column(Date, nullable=False, index=True)
    lookback = Column(Integer, nullable=False, default=1)

    # Compact frozen grid (see docstring) — the forecast-as-made.
    grid = Column(JSON, nullable=False)
    qbo_bank_balance = Column(Numeric(precision=18, scale=2), nullable=True)

    captured_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CashForecastExportSnapshot(Base):
    """Immutable, content-addressed capture of one fully served CFF grid.

    The latest-only serve cache and the weekly accuracy version keep their existing
    contracts. This sibling is the complete reproducible export boundary: direct
    XLSX/PDF downloads load its strict envelope by UUID and never rebuild the
    forecast, reread live overrides, or call QBO.

    ``organization_id`` is lifecycle ownership and may change only through the
    audited company-reassignment flow. The company, schema, content hash, payload,
    and capture time have no application UPDATE path.
    """
    __tablename__ = "cash_forecast_export_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "schema_version",
            "content_hash",
            name="uq_cff_export_snapshot_content",
        ),
        CheckConstraint(
            "schema_version > 0",
            name="ck_cff_export_snapshot_schema_positive",
        ),
        CheckConstraint(
            "length(content_hash) = 64 AND content_hash = lower(content_hash)",
            name="ck_cff_export_snapshot_hash_canonical",
        ),
        Index(
            "ix_cff_export_snapshots_company_captured",
            "company_id",
            "captured_at",
        ),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id = Column(String, nullable=False, index=True)
    schema_version = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False)
    captured_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# PLAN-EXEMPT: market-ready ar-ap latency fix (Owen-ratified 2026-07-06)
class ARAPSummarySnapshot(Base):
    """Stored GET /ar-ap/summary payload — the snapshot-first read path.

    One row per (company, period 'YYYY-MM'): the full summary response the live
    three-report QBO compute produced, served on subsequent GETs with zero QBO
    calls (mirrors CashForecastSnapshot). Warmed by the Celery task
    ``warm_ar_ap_summary`` on a snapshot miss and rewritten by the explicit
    POST /ar-ap/refresh. Realm-keyed under live RLS (company_id →
    app.current_realm_id, like cash_forecast_snapshots), with the org FK for
    the org-keyed joins/audits.
    """
    __tablename__ = "ar_ap_summary_snapshots"
    __table_args__ = (
        UniqueConstraint("company_id", "period", name="uq_ar_ap_summary_company_period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False)   # 'YYYY-MM'
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Intelligence Hub Models
# ---------------------------------------------------------

class CompanyKnowledge(Base):
    """
    Persistent Intelligence Hub knowledge base.
    Stores user-taught context or agent-inferred rules that compound over time.
    """
    __tablename__ = "company_knowledge"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    
    category = Column(String, nullable=False, index=True) # e.g. "Vendor Intelligence", "Deal Context", "Business Operations", "Accounting Policies"
    content = Column(Text, nullable=False)                # The extracted factual statement or JSON
    confidence = Column(String, default="high")           # "high" (user-taught/verified) or "medium" (inferred)
    source = Column(String, default="manual")             # "manual", "document", "inferred"
    # pgvector store (1536-d, pinned text-embedding-3-small). PG gets a real Vector
    # column (HNSW vector_cosine_ops index in migration company_knowledge_pgvector);
    # the SQLite test dialect keeps JSON so create_all + the suite still build.
    # none_as_null=True so the SQLite test dialect stores a None embedding as SQL NULL
    # (matching the PG vector column's native None→NULL) — keeps `.is_(None)` filters
    # dialect-consistent for the re-embed backfill.
    embedding_vector = Column(Vector(1536).with_variant(JSON(none_as_null=True), "sqlite"), nullable=True)  # VECTOR-OK: sqlite-test dialect json fallback; PG is Vector(1536)

    # ── Memory v2 (plan 20260712-agentic-finance-os W5, GAP-5a, directive §10) ──
    # Additive: existing rows default to a company-scoped factual v1 memory. The
    # agent_memory service enforces the memory rules (non-authoritative-by-default,
    # provenance, supersession, expiry, explicit acceptance). NOTE: model-sourced
    # memory is NEVER authoritative until a user accepts it — see is_authoritative().
    scope = Column(String, nullable=False, server_default="company", index=True)  # company | team | personal | workflow
    owner_ref = Column(String, nullable=True, index=True)   # user id/email for personal/team scope; NULL for company
    memory_type = Column(String, nullable=False, server_default="factual")  # factual | preferential | procedural
    version = Column(Integer, nullable=False, server_default="1")
    superseded_by_id = Column(Integer, ForeignKey("company_knowledge.id", ondelete="SET NULL"), nullable=True, index=True)
    effective_from = Column(Date, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)  # review/expiry horizon
    accepted_by = Column(String, nullable=True)   # user who explicitly accepted an inferred memory (NULL = tentative)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    provenance = Column(JSON, nullable=True)      # {source_type, evidence: [...], correction_of: <id>}
    sensitivity = Column(String, nullable=True)   # normal | sensitive

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FinancialHypothesis(Base):
    """
    A typed hypothesis for an investigative workflow (plan 20260712-agentic-finance-os
    W7, GAP-10, directive §18): a candidate explanation with the evidence supporting
    and contradicting it, the tests done and remaining, and a status — so the agent
    can investigate (margin decline, cash variance, forecast miss, aging
    deterioration, reconciliation difference) WITHOUT presenting every plausible
    explanation as a fact. ``presentation_safe`` gates whether a hypothesis may be
    surfaced externally at all.

    Tenant table — org FK + realm; org-keyed FORCE-RLS (migration below).
    """
    __tablename__ = "financial_hypotheses"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    statement = Column(Text, nullable=False)
    metric = Column(String, nullable=True, index=True)   # the affected metric (semantic id / label)
    period = Column(String, nullable=True, index=True)   # affected period YYYY-MM
    status = Column(String, nullable=False, default="proposed", index=True)
    # proposed | under_investigation | supported | partially_supported | contradicted | inconclusive | discarded

    evidence_for = Column(JSON, nullable=True)       # list of evidence refs supporting it
    evidence_against = Column(JSON, nullable=True)   # list of evidence refs contradicting it
    tests_done = Column(JSON, nullable=True)         # list of investigation steps performed
    tests_remaining = Column(JSON, nullable=True)    # list of investigation steps still open

    confidence = Column(String, nullable=False, default="low")  # low | medium | high
    presentation_safe = Column(Boolean, nullable=False, default=False)  # safe to surface externally?
    recommended_next_step = Column(Text, nullable=True)

    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Cap Table Models
# ---------------------------------------------------------

class CapTableEntry(Base):
    """
    Tracks equity ownership per investor/partner for a company.
    Supports multiple share classes (Common, Preferred, SAFE, Note).
    """
    __tablename__ = "cap_table_entries"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)

    investor_name = Column(String, nullable=False)
    investor_type = Column(String, default="individual")   # individual | entity | fund

    share_class = Column(String, nullable=False, default="Common")  # Common | Preferred A | SAFE | Note
    shares_owned = Column(Numeric(precision=18, scale=4), default=0)
    total_shares_outstanding = Column(Numeric(precision=18, scale=4), default=0)
    ownership_pct = Column(Numeric(precision=8, scale=6), nullable=True)  # Computed or manual

    investment_amount = Column(Numeric(precision=18, scale=2), default=0)  # Cost basis / amount invested
    valuation_at_investment = Column(Numeric(precision=18, scale=2), nullable=True)
    current_valuation_override = Column(Numeric(precision=18, scale=2), nullable=True)  # Latest 409A / manual

    entry_date = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Budget Models
# ---------------------------------------------------------

class BudgetLine(Base):
    """
    A single line in the 12-month forward-looking budget.
    One row per account (or custom line), with monthly amounts Jan–Dec.
    """
    __tablename__ = "budget_lines"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    budget_year = Column(Integer, nullable=False, index=True)

    account_name = Column(String, nullable=False)
    account_type = Column(String, nullable=False)   # revenue | cogs | opex
    qbo_account_id = Column(String, nullable=True)  # Optional link to QBO COA

    # Monthly amounts stored as JSON: {"jan": 10000, "feb": 10000, ...}
    monthly_amounts = Column(JSON, nullable=False, default=dict)

    sort_order = Column(Integer, default=0)
    notes = Column(Text, nullable=True)
    department = Column(String, nullable=True)              # Optional department/cost center
    status = Column(String, nullable=False, default="draft") # draft | submitted | approved | locked
    scenario = Column(String, nullable=False, default="base") # base | upside | downside | custom
    driver_formula = Column(String, nullable=True)           # e.g. "headcount * avg_salary"
    parent_line_id = Column(Integer, nullable=True)          # FK to budget_lines.id (for vendor sub-lines)
    vendor_name = Column(String, nullable=True)              # Vendor name for sub-lines
    customer_name = Column(String, nullable=True)             # Customer name for revenue sub-lines
    original_monthly_amounts = Column(JSON, nullable=True)   # Snapshot of QBO actuals before drivers overwrite

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BudgetMonthLock(Base):
    """Track which months are locked (closed) for a budget year."""
    __tablename__ = "budget_month_locks"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    budget_year = Column(Integer, nullable=False)
    month = Column(String, nullable=False)       # "jan", "feb", etc.
    locked_at = Column(DateTime(timezone=True), server_default=func.now())
    locked_by = Column(String, nullable=True)     # user email or ID


class BudgetAuditLog(Base):
    """Audit trail for budget cell changes."""
    __tablename__ = "budget_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    budget_line_id = Column(Integer, nullable=False)
    field = Column(String, nullable=False)        # e.g. "jan", "feb", "notes", "department"
    old_value = Column(String, nullable=True)
    new_value = Column(String, nullable=True)
    changed_by = Column(String, nullable=True)
    changed_at = Column(DateTime(timezone=True), server_default=func.now())


class BudgetDriver(Base):
    """Budget driver linked to a COA account with a computation method."""
    __tablename__ = "budget_drivers"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    budget_year = Column(Integer, nullable=False)
    scenario = Column(String, nullable=False, default="base")
    budget_line_id = Column(Integer, nullable=True)          # FK to budget_lines.id
    account_name = Column(String, nullable=False)            # COA account name
    qbo_account_id = Column(String, nullable=True)           # QBO account ID
    driver_method = Column(String, nullable=False)            # ttm_avg | pct_revenue | fixed | yoy_growth | volume
    driver_category = Column(String, nullable=False, default="expense")  # 'revenue' or 'expense'
    vendor_name = Column(String, nullable=True)               # vendor name for expense drivers
    customer_name = Column(String, nullable=True)              # customer name for revenue drivers
    driver_params = Column(JSON, nullable=False, default=dict)  # e.g. {"pct": 10} or {"growth_pct": 5} or {"amount": 5000}
    computed_values = Column(JSON, nullable=True, default=dict) # Computed monthly values {jan: ..., feb: ...}
    overrides = Column(JSON, nullable=True, default=dict)       # Manual cell overrides {jan: 5000, ...}
    is_dirty = Column(Boolean, nullable=False, default=True)    # True = pending unapplied change
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Company Agreement Models
# ---------------------------------------------------------

class CompanyAgreement(Base):
    """
    Uploaded company agreement document (Operating Agreement, LP Agreement, etc.).
    Stores the raw extracted text and AI-parsed economic terms + cap table.
    """
    __tablename__ = "company_agreements"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)

    filename = Column(String, nullable=False)
    file_type = Column(String, nullable=False)            # "pdf" | "docx" | "txt"
    raw_text = Column(Text, nullable=True)                # Extracted full text

    # AI-extracted structured data
    agreement_type = Column(String, nullable=True)        # LLC Operating Agreement | LP Agreement | SHA | etc.
    extracted_cap_table = Column(JSON, nullable=True)     # [{investor, class, shares, pct, invested}]
    extracted_economics = Column(JSON, nullable=True)     # {mgmt_fee, carry, hurdle, waterfall, ...}
    ai_summary = Column(Text, nullable=True)              # Plain-English summary of the agreement

    processing_status = Column(String, default="pending") # pending | processing | done | failed
    error_message = Column(Text, nullable=True)
    upload_scan_status = Column(String, nullable=False, default="skipped", index=True)
    upload_scan_provider = Column(String, nullable=True)
    upload_scan_result = Column(JSON, nullable=True)
    quarantine_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Document(Base):
    """
    Generalized document & contract manager.
    Any important document (lease, insurance, vendor contract, loan agreement, etc.)
    can be uploaded. The LLM extracts key terms, dates, and a summary.
    Documents can be tagged and linked to Peregrine modules for cross-module context.
    """
    __tablename__ = "documents"

    id              = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id      = Column(String, index=True, nullable=False)

    # File info
    filename        = Column(String, nullable=False)
    file_type       = Column(String, nullable=False)          # pdf | docx | txt | xlsx | csv
    file_size_bytes = Column(Integer, nullable=True)

    # Stored original (S3-compatible object storage; see app/services/object_storage.py).
    # storage_key NULL = legacy doc with no stored original (back-compat marker).
    storage_key          = Column(String, nullable=True)      # object-store key for the original upload
    storage_content_type = Column(String, nullable=True)      # MIME type recorded at store time

    # Classification
    document_type   = Column(String, nullable=False, default="other")
    # contract | lease | loan_agreement | insurance_policy | engagement_letter
    # operating_agreement | employment_agreement | nda | vendor_agreement
    # tax_return | financial_statement | board_resolution | other

    # AI Extraction
    raw_text        = Column(Text, nullable=True)             # Full extracted text (capped at 200k chars)
    ai_summary      = Column(Text, nullable=True)             # Plain-english summary
    key_terms       = Column(JSON, nullable=True)             # Structured key terms extracted by LLM
    # {parties: [], effective_date, expiration_date, renewal_terms,
    #  payment_terms, termination_clauses, key_obligations: [],
    #  financial_terms: {}, governing_law, ...}

    tags            = Column(JSON, nullable=True, default=[])  # User-defined tags ["insurance", "2026"]

    # Cross-module context — which Peregrine modules can use this knowledge
    context_modules = Column(JSON, nullable=True, default=[])
    # ["budget", "cashflow", "covenants", "lbo", "assistant"]

    # Processing
    processing_status = Column(String, default="pending")     # pending | processing | done | failed
    error_message   = Column(Text, nullable=True)
    upload_scan_status = Column(String, nullable=False, default="skipped", index=True)
    upload_scan_provider = Column(String, nullable=True)
    upload_scan_result = Column(JSON, nullable=True)
    quarantine_reason = Column(Text, nullable=True)

    # Dates extracted from the document
    effective_date  = Column(Date, nullable=True)
    expiration_date = Column(Date, nullable=True)

    # Optional link to CompanyAgreement for backward compat
    agreement_id    = Column(Integer, nullable=True)

    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Month End Close Task Models
# ---------------------------------------------------------

class CloseTask(Base):
    """
    A checklist task for a specific month-end close period.
    Auto-generated by the AI from prior period patterns, or created manually.
    """
    __tablename__ = "close_tasks"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False, index=True)   # YYYY-MM

    task_type = Column(String, nullable=False, index=True)
    # BANK_RECONCILIATION | REVIEW_ACCRUALS | APPROVE_JES | REVIEW_BANK_FEED
    # REVIEW_ANOMALIES | LOCK_PERIOD | GENERATE_REPORT | CUSTOM

    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, default=0)

    # Workflow
    status = Column(String, default="OPEN", index=True)  # OPEN | COMPLETE | SKIPPED
    completed_by = Column(String, nullable=True)          # User email
    completed_at = Column(DateTime(timezone=True), nullable=True)
    is_auto_generated = Column(Boolean, default=True)

    # Assignee & Deadline tracking
    assigned_to = Column(String, nullable=True)           # User email or name
    due_date = Column(Date, nullable=True)                # Task deadline
    priority = Column(String, default="normal")           # low | normal | high | critical

    # Auto-verification results
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verification_message = Column(Text, nullable=True)    # Last verification result message

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



class CloseTemplate(Base):
    """
    A reusable close checklist template for a company.
    Stores task definitions as JSON so companies can customize and reuse
    their month-end close workflow each period.
    """
    __tablename__ = "close_templates"

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    name = Column(String, nullable=False)               # "Standard Monthly Close"
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, default=False)          # Default template for auto-generate

    # Task definitions — list of dicts:
    # [{"task_type": "REVIEW_BANK_FEED", "title": "Clear Bank Feed",
    #   "description": "...", "sort_order": 10, "priority": "high",
    #   "default_assignee": "jane@co.com", "due_day_offset": 5}]
    tasks = Column(JSON, nullable=False, default=list)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CloseRunLock(Base):
    """
    PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-1 lock TTL).

    The per-realm close single-flight LEASE — one row per realm, claimed by the
    close-orchestration walk before it starts and heartbeat-bumped on every
    step commit (``_mark_progress``). Replaces the session-level pg advisory
    lock, whose one failure mode was a stuck-but-alive session holding a realm
    indefinitely: a lease whose ``heartbeat_at`` is older than the TTL
    (``settings.close_lock_ttl_seconds``, default 30 min) is STEALABLE by the
    next run and RELEASABLE by the close watchdog — a dead or wedged session
    can never hold a realm past the TTL.

    Acquire/steal/release semantics live in
    ``app/agent/runtime/close_orchestration.py`` (portable check-then-write
    with an IntegrityError-on-PK race arm + an optimistic heartbeat CAS on the
    steal path). Realm-keyed tenant table → FORCE-RLS realm policy (migration
    precedent b3f7d2a9c8e4 / a7d3e9c1f5b8).
    """
    __tablename__ = "close_run_locks"

    realm_id = Column(String, primary_key=True)
    agent_run_id = Column(Integer, nullable=True, index=True)
    locked_at = Column(DateTime(timezone=True), nullable=False)
    heartbeat_at = Column(DateTime(timezone=True), nullable=False, index=True)


# ---------------------------------------------------------
# PBC checklist + evidence binder (Wave 3.4, WF-9)
# ---------------------------------------------------------
# Plan 20260714-wave3-4-tax-pbc-binder AC-2. The PBC (Prepared-By-Client) checklist
# + evidence binder for an audit/tax/review engagement. EXTEND, NOT reuse CloseTask:
# CloseTask is period(YYYY-MM)-keyed with a closed close-specific task_type vocab and
# NO evidence-link column (models.py:3645), welded to the close dashboard/verify-all
# machinery — the PBC line is engagement(fiscal-span)-keyed with its own status vocab
# and an explicit evidence-link table (R-Dim D2 §2). All four are tenant tables:
# indexed org FK (nullable=False, AP-005) + realm company_id; org-keyed FORCE-RLS in
# the paired migration (mirrors handoffs/agent_plans e9c5a1f8b3d7 + tool_failure
# c9e2b7f4a1d6).


class PBCRequest(Base):
    """A PBC engagement header — the binder for one audit/tax/review of a fiscal
    span (NOT a calendar month). Mirrors the ``Handoff`` header shape (D2 §2.3)."""
    __tablename__ = "pbc_requests"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    engagement_name = Column(String, nullable=False)
    engagement_type = Column(String, nullable=False, default="AUDIT")  # AUDIT | TAX | REVIEW
    period_start = Column(Date, nullable=True)   # fiscal-year span start (NOT YYYY-MM)
    period_end = Column(Date, nullable=True)
    status = Column(String, nullable=False, default="OPEN", index=True)  # OPEN | IN_PROGRESS | DELIVERED
    created_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PBCItem(Base):
    """A single PBC checklist line under a ``PBCRequest``. Copies the proven
    CloseTask column set (owner/status/due/priority/sort) + a ``category`` and a
    ``requested_by`` (preparer/auditor) DISTINCT from ``assigned_to`` (the client
    who provides) — the requester/preparer split CloseTask lacks (D2 §2.2)."""
    __tablename__ = "pbc_items"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id
    pbc_request_id = Column(Integer, ForeignKey("pbc_requests.id", ondelete="CASCADE"), nullable=False, index=True)

    category = Column(String, nullable=False)  # ∈ pbc_binder.PBC_CATEGORIES
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    assigned_to = Column(String, nullable=True)   # owner — the client who provides the item
    requested_by = Column(String, nullable=True)  # requester — the auditor/preparer
    due_date = Column(Date, nullable=True)
    priority = Column(String, default="normal")   # low | normal | high | critical
    sort_order = Column(Integer, default=0)
    # PBC-suited status vocab (NOT the close OPEN/COMPLETE/SKIPPED). The binder NEVER
    # flips this to a satisfied state — a line is provided only by a human action.
    status = Column(String, default="OPEN", index=True)  # OPEN | PROVIDED | ACCEPTED | NA

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PBCEvidenceLink(Base):
    """A human-asserted link from a PBC line to ONE real evidence source row
    (proposal / QBO or internal execution ledger / document / audit entry). The
    binder RE-RESOLVES each ref under the tenant scope at render (D2 §3.3) — a
    forged/foreign/absent ref resolves to NO_DATA, never a fabricated link."""
    __tablename__ = "pbc_evidence_links"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id
    pbc_item_id = Column(Integer, ForeignKey("pbc_items.id", ondelete="CASCADE"), nullable=False, index=True)

    # ∈ pbc_binder.EVIDENCE_RESOLVERS keys:
    # proposal | qbo_execution | internal_execution | document | audit_entry
    evidence_type = Column(String, nullable=False)
    evidence_ref = Column(String, nullable=False)  # the source row's id/entity_id (stringified)
    label = Column(String, nullable=True)
    # The human-assertion marker. The binder computes the RESOLVED state
    # (LINKED / NO_DATA) fresh at render — this column is NOT trusted as the answer.
    link_state = Column(String, nullable=False, default="ASSERTED")
    linked_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PBCTemplate(Base):
    """A reusable PBC checklist definition (mirror of ``CloseTemplate``,
    models.py:3684). ``items`` is a JSON list of line definitions seeded into a new
    ``PBCRequest``."""
    __tablename__ = "pbc_templates"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    name = Column(String, nullable=False)
    engagement_type = Column(String, nullable=False, default="AUDIT")  # AUDIT | TAX | REVIEW
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, default=False)
    # [{"category": "...", "title": "...", "description": "...", "priority": "high",
    #   "sort_order": 10, "due_day_offset": 14}]
    items = Column(JSON, nullable=False, default=list)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Covenant Tracking Models
# ---------------------------------------------------------

class DebtCovenant(Base):
    """
    A financial covenant tied to a loan or credit facility.
    Peregrine computes the metric each period and flags breaches.
    """
    __tablename__ = "debt_covenants"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)

    loan_name = Column(String, nullable=False)           # e.g. "Term Loan A — First Bank"
    name = Column(String, nullable=False)                # e.g. "Minimum DSCR"
    metric_type = Column(String, nullable=False)         # DSCR | LEVERAGE | MIN_CASH | CUSTOM
    threshold = Column(Numeric(precision=18, scale=4), nullable=False)
    breach_direction = Column(String, default="BELOW")   # BELOW → breach if value < threshold
    custom_formula = Column(Text, nullable=True)         # For CUSTOM metric_type
    # PLAN-EXEMPT: item-3 EBITDA add-backs, ratified 2026-06-25 ("commit and ratify 3+4");
    # design work/batch-6-reporting-covenants/design/20260625-addbacks-and-metric-snapshots.md.
    # JSON list of user-defined bank-EBITDA adjustment lines, each:
    #   {"label": str, "kind": "fixed"|"account", "amount": float|null,
    #    "account_ref": str|null, "sign": "add"|"subtract"}
    # Mirrors the custom_formula JSON-in-Text precedent above; absent → book EBITDA only.
    ebitda_addbacks = Column(Text, nullable=True)
    # Covenant-tracking Wave 1 (plan 20260630-covenant-tracking AC-3): JSON list of the QBO
    # liability account refs this covenant services. DSCR principal-paid = Σ over these accounts
    # of max(0, balance_as_of(TTM_start) − balance_as_of(TTM_end)) from QBO Balance-Sheet-as-of-date.
    # Mirrors the custom_formula / ebitda_addbacks JSON-in-Text precedent above; absent/[] → DSCR
    # returns an explicit "map the serviced debt account" no-data reason (never a fabricated 0).
    debt_service_accounts = Column(Text, nullable=True)
    # AC-4: per-covenant WARN cushion (fraction, default 0.10). The band between PASS and BREACH
    # is threshold ± cushion·threshold; NULL → the 0.10 default is applied at compute time.
    warn_cushion = Column(Numeric(precision=6, scale=4), nullable=True)
    active = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CompanyEbitdaConfig(Base):
    """ONE shared company-level EBITDA definition every EBITDA covenant uses (R3).

    EBITDA add-backs were per-covenant on ``DebtCovenant.ebitda_addbacks`` — a user had to
    re-map interest/tax/D&A on each DSCR and Debt/EBITDA covenant. Steven's decision: define
    EBITDA ONCE per company. This carries that single definition; the covenant status/compute
    path loads it (by ``company_id``) and passes its add-backs to ``compute_builtin`` for BOTH
    DSCR and Debt/EBITDA. ``addbacks`` is the SAME categorized JSON-in-Text line schema the
    per-covenant ``ebitda_addbacks`` used (each line: label / kind / amount / account_ref / sign /
    category), so the compute is unchanged — only the SOURCE of the add-backs moves here.

    Tenant-scoped by ``company_id`` (the QBO realm id — the covenant tenant key), UNIQUE + indexed
    so there is exactly ONE config per company and the load is a keyed lookup.
    """
    # REALM-STRING-ONLY-OK: mirrors the sibling DebtCovenant tenant model exactly — the covenant
    # subsystem is keyed on company_id (the QBO realm id) so the compute path loads this config by
    # the SAME key it filters covenants by; an org FK here would diverge from that keying.
    __tablename__ = "company_ebitda_config"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, unique=True, index=True, nullable=False)
    # JSON list of the same categorized add-back lines as DebtCovenant.ebitda_addbacks:
    #   {"label": str, "kind": "fixed"|"account", "amount": float|null,
    #    "account_ref": str|null, "sign": "add"|"subtract", "category": str}
    # Absent/[] → book EBITDA only (Net Income, no add-backs).
    addbacks = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk A decision 1 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json not synced into this worktree).
class CompanyCeclConfig(Base):
    """The EXPLICIT CECL account MAPPING — one row per company (D1, never inference).

    The CECL allowance tool (``app/analysis/cecl_allowance.py``) drafts its
    adjustment JE against exactly these two accounts: ``allowance_account_id`` (the
    Allowance-for-Credit-Losses contra-asset — QBO Classification must be Asset)
    and ``bad_debt_account_id`` (the Bad Debt / credit-loss expense — Classification
    Expense). BOTH are required-when-set (a half-mapping is refused at PUT time);
    unset = both NULL (the tool then refuses to draft, with setup instructions —
    it never keyword-resolves a contra-asset). Classification is validated against
    the live QBO Account entity at config-SET time (``app/services/cecl_config.py``).

    Tenant-scoped by ``company_id`` (the QBO realm id), UNIQUE + indexed so there is
    exactly ONE mapping per company and the engine's load is a keyed lookup —
    realm-keyed FORCE-RLS policy per the house pattern (the wave's ONE migration).
    """
    # REALM-STRING-ONLY-OK: mirrors the sibling CompanyEbitdaConfig tenant model exactly —
    # the CECL engine + the covenant subsystem are keyed on company_id (the QBO realm id),
    # so this config is loaded by the SAME key the aging/haircut reads are scoped by; an
    # org FK here would diverge from that keying. Realm-GUC FORCE-RLS backstops it.
    __tablename__ = "company_cecl_config"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, unique=True, index=True, nullable=False)
    # QBO account id of the Allowance for Credit Losses / Doubtful Accounts
    # contra-asset (Classification Asset — validated at set time). NULL = unmapped.
    allowance_account_id = Column(String, nullable=True)
    # QBO account id of the Bad Debt / credit-loss expense (Classification Expense —
    # validated at set time). NULL = unmapped.
    bad_debt_account_id = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── Company finance profile (plan 20260711-peregrine-finance-os, FOS-1 Wave A) ──
# `validates` is imported HERE rather than at the top of the file because models.py
# is append-only under this plan (the existing import lines stay byte-unchanged);
# a mid-module import executes once at import time exactly like a top-of-file one.
from sqlalchemy.orm import validates  # noqa: E402

# The only reporting bases the profile accepts — enforced in code by the
# @validates hook below (CHECK-style, dialect-portable: the SQLite test lane
# has no CHECK-constraint migration path).
REPORTING_BASES = ("accrual", "cash")


class CompanyFinanceProfile(Base):
    """Versioned, append-only company finance policy profile (FOS-1 Wave A).

    One row per (``qbo_realm_id``, ``version``): the company-level financial
    reporting policy — fiscal-year start, reporting basis, materiality floors,
    sign/KPI/close-cadence preferences — that downstream analysis reads through
    ``app/services/company_profile.py`` (which merges the latest EFFECTIVE
    version over the fallback chain QBOCompanyInfo → review_checks defaults and
    carries per-field provenance).

    VERSIONING IS APPEND-ONLY: a policy change is a NEW row with
    ``version = max(version) + 1``. Financial policy fields on an existing row
    are NEVER updated in place — the history of what policy applied when is the
    audit trail. The ONLY permitted post-insert write is
    ``superseded_by_version``: the pointer the appender stamps onto the previous
    latest row (version lineage, distinct from the ``effective_date`` window).

    Tenancy: the InternalActionExecution triple conventions — ``organization_id``
    / ``organization_company_id`` FKs (ondelete SET NULL, nullable, indexed) plus
    the required realm string, here named ``qbo_realm_id`` (the loop-metrics /
    realm-context naming). RLS: org-keyed FORCE-RLS policy in migration
    f3b8d1c6a2e9 (the c4d7e2f9a1b3 / e7f1a9c3d2b5 two-GUC fail-closed shape).
    """
    __tablename__ = "company_finance_profiles"
    __table_args__ = (
        UniqueConstraint("qbo_realm_id", "version", name="uq_company_finance_profiles_realm_version"),
        Index("ix_company_finance_profiles_realm_effective", "qbo_realm_id", "effective_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    qbo_realm_id = Column(String, nullable=False, index=True)

    version = Column(Integer, nullable=False)
    effective_date = Column(Date, nullable=False)
    # None → defer to QBOCompanyInfo.fiscal_year_start_month (the QBO-synced value).
    fiscal_year_start_month = Column(Integer, nullable=True)
    reporting_basis = Column(String, nullable=False, default="accrual")  # accrual|cash — @validates below
    materiality_abs = Column(Numeric(precision=18, scale=2), nullable=True)
    materiality_pct = Column(Numeric(precision=9, scale=4), nullable=True)
    sign_conventions = Column(JSON, nullable=True)
    kpi_preferences = Column(JSON, nullable=True)
    close_cadence = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    superseded_by_version = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @validates("reporting_basis")
    def _validate_reporting_basis(self, _key, value):
        """CHECK-style code validation: refuse any basis outside accrual|cash."""
        if value not in REPORTING_BASES:
            raise ValueError(
                f"reporting_basis must be one of {REPORTING_BASES}, got {value!r}"
            )
        return value


# ---------------------------------------------------------
# Custom Strategic KPI Models
# ---------------------------------------------------------

class CustomKPI(Base):
    """
    User-defined KPI with a formula referencing QBO account IDs or known metrics.
    Computed on-demand against current QBO data.
    """
    __tablename__ = "custom_kpis"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)

    name = Column(String, nullable=False)         # e.g. "Revenue per Employee"
    description = Column(Text, nullable=True)
    formula_json = Column(JSON, nullable=False)
    # {"type": "ratio", "numerator_account_id": "abc", "denominator_constant": 12}
    # {"type": "sum", "account_ids": ["abc", "def"]}
    unit = Column(String, default="USD")          # USD | % | ratio | custom
    sort_order = Column(Integer, default=0)
    active = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Report Scheduling
# ---------------------------------------------------------

class ReportSchedule(Base):
    """
    Configures automated recurring report generation and email delivery.
    Supports WEEKLY (KPI digest), MONTHLY (board pack), and QUARTERLY frequencies.
    """
    __tablename__ = "report_schedules"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    name = Column(String, nullable=False)             # e.g. "Monthly Board Pack"
    frequency = Column(String, nullable=False)         # WEEKLY | MONTHLY | QUARTERLY
    report_format = Column(String, default="pdf")      # pdf | excel | both
    report_type = Column(String, default="month_end", server_default="month_end")  # month_end | standard_financials | ar_ap_aging | covenant | cashflow_forecast
    recipients = Column(JSON, nullable=False)           # ["user@example.com", ...]
    enabled = Column(Boolean, default=True)

    # Scheduling parameters
    day_of_week = Column(Integer, nullable=True)       # 0=Mon ... 6=Sun (WEEKLY only)
    day_of_month = Column(Integer, nullable=True)      # 1-28 (MONTHLY/QUARTERLY)
    hour_utc = Column(Integer, default=8)              # 0-23, hour to send (UTC)

    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# ------------------------------------------------------------
# COVENANTS — PHASE 3
# ------------------------------------------------------------

class CovenantDefinition(Base):
    """
    Defines a financial covenant (e.g., Min Fixed Charge Coverage, Max Leverage).
    Links the definition to specific QBO GL accounts or high-level calculation formulas.
    """
    __tablename__ = "covenant_definitions"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    
    name = Column(String, nullable=False)                # e.g., "Max Leverage Ratio"
    covenant_type = Column(String, nullable=False)       # LEVERAGE, COVERAGE, LIQUIDITY, NET_WORTH
    
    threshold_value = Column(Float, nullable=False)      # e.g., 3.5 (Max Leverage)
    threshold_type = Column(String, nullable=False)      # MAX, MIN
    
    # Storage for formula elements: list of account IDs, or custom formula string
    formula_config = Column(JSON, nullable=True)         # {"numerator": ["Acc1", "Acc2"], "denominator": ["Acc3"]}
    description = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CovenantRecord(Base):
    """
    Periodic (monthly) compliance snapshot of a DebtCovenant against actuals.

    Covenant-tracking Wave 3 (plan 20260630-covenant-tracking AC-1): the beat task
    ``snapshot_realm_covenants`` writes ONE row per active ``DebtCovenant`` per CLOSED
    month, evaluating the SAME ``/covenants/status`` compute (no re-derivation). The
    monthly compliance grid (AC-2) and the breach/near-breach alert (AC-3) both read
    these stored rows. ``covenant_id`` references ``debt_covenants.id`` — the live
    covenant entity ``/status`` evaluates — NOT the orphaned ``covenant_definitions``
    model (which has no migration and is not the entity the platform computes).

    Idempotent per (covenant_id, period): the unique constraint + the task's upsert
    mean a re-run of the same closed month UPDATES the same row, never duplicates it
    (safe under acks_late at-least-once delivery).
    """
    __tablename__ = "covenant_records"
    # REALM-STRING-ONLY-OK: mirrors its parent DebtCovenant (debt_covenants.company_id is the
    # realm-string covenant key with no org FK); a snapshot row is keyed to that covenant by FK and
    # carries the same realm-string company_id, sharing the covenant family's realm-string isolation
    # rather than inventing an org FK the parent covenant table itself does not have.

    id = Column(Integer, primary_key=True, index=True)
    covenant_id = Column(Integer, ForeignKey("debt_covenants.id"), index=True, nullable=False)
    company_id = Column(String, index=True, nullable=False)
    period = Column(String, nullable=False)              # YYYY-MM (the closed month)

    actual_value = Column(Float, nullable=True)          # The computed metric value (NULL → no-data period)
    # The tri-state lender compliance verdict for the period (PASS | WARN | BREACH | UNKNOWN).
    # ``is_compliant`` is the boolean projection (True only for PASS) kept for legacy/simple reads.
    status = Column(String, nullable=False)
    is_compliant = Column(Boolean, nullable=False)

    # The exact metric breakdown snapshot the compute produced for this period (display-only,
    # ties the rendered grid back to the stored figure).
    calculation_context = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # Idempotency key: one snapshot per covenant per closed month.
        UniqueConstraint("covenant_id", "period", name="uq_covenant_record_covenant_period"),
    )


class SystemAuditLog(Base):
    """
    Immutable audit log for all manual overrides, config changes, and key inferences.
    """
    __tablename__ = "system_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(String, index=True, nullable=False)
    user_id = Column(Integer, nullable=True, index=True) # Nullable for AI agent actions
    
    action = Column(String, nullable=False, index=True)  # e.g. "TEACH_KNOWLEDGE", "OVERRIDE_DRIVER"
    entity_type = Column(String, nullable=False)         # e.g. "CompanyKnowledge"
    entity_id = Column(String, nullable=False)           # PK of modified row (string cast)
    
    previous_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=True)

    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class LLMUsageEvent(Base):
    """
    One row per LLM API call — the observability backbone for the
    optimization layer (audit item S4).

    Scoped by the full realm triple (organization_id /
    organization_company_id / qbo_realm_id) when the call carries realm
    context. Realm fields stay NULL only for genuinely non-realm calls
    (e.g. embeddings of non-company text). Never stores raw prompts, raw
    financial data, secrets, or tokens — only counts, costs, latency, and
    a redacted error string.
    """
    __tablename__ = "llm_usage_events"

    id = Column(Integer, primary_key=True, index=True)

    # Realm-scope triple (nullable for genuinely non-realm calls).
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    qbo_realm_id = Column(String, nullable=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_step_id = Column(Integer, ForeignKey("agent_steps.id", ondelete="SET NULL"), nullable=True, index=True)

    module = Column(String, nullable=True)
    route = Column(String, nullable=True)
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1
    # Which platform work unit spent these tokens (the F-7 ledger dimension):
    # chat | close | briefing | watchdog | schedule | board_report | other.
    # Backfilled from the bound LLM task context (llm_task_context.py) when the
    # caller does not pass it explicitly; "chat" for the interactive agent loop.
    task_type = Column(String, nullable=True, index=True)
    # Human-readable step label inside a run (e.g. a close step
    # "REVIEW_ANOMALIES:123") so per-step spend is attributable within a run.
    step = Column(String, nullable=True)
    # W-1 ledger-contract alias: the plan names the run link "run_id"; the
    # existing column is agent_run_id — one fact, two names, no second column.
    run_id = synonym("agent_run_id")
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    prompt_version = Column(String, nullable=True)
    context_packet_version = Column(String, nullable=True)
    tool_bundle_name = Column(String, nullable=True)

    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cached_input_tokens = Column(Integer, nullable=True)
    cache_creation_input_tokens = Column(Integer, nullable=True)
    cache_read_input_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)

    estimated_cost_usd = Column(Numeric(precision=12, scale=6), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    first_token_latency_ms = Column(Integer, nullable=True)

    tool_call_count = Column(Integer, nullable=False, default=0)
    tool_error_count = Column(Integer, nullable=False, default=0)

    status = Column(String, nullable=False, default="ok", index=True)
    error_type = Column(String, nullable=True)
    redacted_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_llm_usage_events_org_created", "organization_id", "created_at"),
        Index("ix_llm_usage_events_company_created", "organization_company_id", "created_at"),
        Index("ix_llm_usage_events_realm_created", "qbo_realm_id", "created_at"),
        Index("ix_llm_usage_events_provider_model_created", "provider", "model", "created_at"),
        Index("ix_llm_usage_events_module_created", "module", "created_at"),
        Index("ix_llm_usage_events_agent_run", "agent_run_id"),
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 — the admin
        # token-usage rollup groups (org, task_type) over a created_at window.
        Index("ix_llm_usage_events_org_task_created", "organization_id", "task_type", "created_at"),
    )


class BankReconciliationSuggestionAttempt(Base):
    """Durable spend authorization and result for one suggestion page attempt.

    This table is deliberately separate from ``llm_usage_events``: it owns the
    reconciliation-specific claim/start/terminal state machine, while the usage
    event remains the single source of truth for a physical provider call.  A
    physical-call attempt links to exactly one usage event through the unique
    ``usage_event_id`` foreign key before provider spend.  The full
    tenant/company/realm triple is required on every row and reinforced by the
    composite company identity FK.
    """

    __tablename__ = "bank_reconciliation_suggestion_attempts"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_company_id = Column(
        Integer,
        ForeignKey("organization_companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    qbo_realm_id = Column(String, nullable=False)
    reconciliation_session_id = Column(Integer, nullable=False)
    statement_import_id = Column(Integer, nullable=True)
    lineage_status = Column(String, nullable=False)

    traversal_id = Column(String(32), nullable=False)
    page_index = Column(Integer, nullable=False)
    page_count = Column(Integer, nullable=False)
    page_fingerprint = Column(String(64), nullable=False)
    attempt_ordinal = Column(Integer, nullable=False)
    attempt_fingerprint = Column(String(64), nullable=False)
    task_type = Column(String, nullable=False)
    step = Column(String(200), nullable=False)

    status = Column(String, nullable=False, default="claimed", server_default="claimed")
    failure_code = Column(String, nullable=True)
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    usage_event_id = Column(
        Integer,
        ForeignKey("llm_usage_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Only the bounded, id-validated suggestion DTO is retained.  Prompts and
    # raw provider responses are never stored here.
    result_json = Column(JSON, nullable=True)

    claimed_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    terminal_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_company_id", "organization_id", "qbo_realm_id"],
            [
                "organization_companies.id",
                "organization_companies.organization_id",
                "organization_companies.realm_id",
            ],
            name="fk_bank_recon_suggest_attempt_tenant",
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        ForeignKeyConstraint(
            ["reconciliation_session_id", "qbo_realm_id"],
            ["reconciliation_sessions.id", "reconciliation_sessions.realm_id"],
            name="fk_bank_recon_suggest_attempt_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "reconciliation_session_id",
                "qbo_realm_id",
                "statement_import_id",
            ],
            [
                "reconciliation_sessions.id",
                "reconciliation_sessions.realm_id",
                "reconciliation_sessions.statement_import_id",
            ],
            name="fk_bank_recon_suggest_attempt_import",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "organization_company_id",
            "qbo_realm_id",
            "attempt_fingerprint",
            name="uq_bank_recon_suggest_attempt_identity",
        ),
        UniqueConstraint(
            "usage_event_id",
            name="uq_bank_recon_suggest_attempt_usage",
        ),
        Index(
            "ix_bank_recon_suggest_attempt_scope_created",
            "organization_id",
            "organization_company_id",
            "qbo_realm_id",
            "created_at",
        ),
        Index("ix_bank_recon_suggest_attempt_org", "organization_id"),
        Index(
            "ix_bank_recon_suggest_attempt_org_company",
            "organization_company_id",
        ),
        Index("ix_bank_recon_suggest_attempt_realm", "qbo_realm_id"),
        Index(
            "ix_bank_recon_suggest_attempt_session",
            "reconciliation_session_id",
        ),
        Index("ix_bank_recon_suggest_attempt_import", "statement_import_id"),
        Index(
            "ix_bank_recon_suggest_attempt_session_page",
            "reconciliation_session_id",
            "page_index",
            "attempt_ordinal",
        ),
        Index(
            "ix_bank_recon_suggest_attempt_status_started",
            "status",
            "started_at",
        ),
        CheckConstraint(
            "lineage_status IN ('controlled', 'legacy_snapshot')",
            name="ck_bank_recon_suggest_attempt_lineage",
        ),
        CheckConstraint(
            "(lineage_status = 'legacy_snapshot' AND statement_import_id IS NULL) OR "
            "(lineage_status = 'controlled' AND statement_import_id IS NOT NULL)",
            name="ck_bank_recon_suggest_attempt_import_shape",
        ),
        CheckConstraint(
            "length(traversal_id) = 32 AND length(page_fingerprint) = 64 "
            "AND length(attempt_fingerprint) = 64",
            name="ck_bank_recon_suggest_attempt_hashes",
        ),
        CheckConstraint(
            "page_count > 0 AND page_index >= 0 AND page_index < page_count",
            name="ck_bank_recon_suggest_attempt_page",
        ),
        CheckConstraint(
            "attempt_ordinal BETWEEN 0 AND 1",
            name="ck_bank_recon_suggest_attempt_ordinal",
        ),
        CheckConstraint(
            "status IN ('claimed', 'started', 'succeeded', 'provider_error', "
            "'provider_timeout', 'usage_unmetered', 'usage_ambiguous', "
            "'usage_mismatch', 'invalid_response', 'response_truncated', "
            "'result_unavailable')",
            name="ck_bank_recon_suggest_attempt_status",
        ),
        CheckConstraint(
            "(status = 'claimed' AND started_at IS NULL AND terminal_at IS NULL "
            "AND provider IS NULL AND model IS NULL AND usage_event_id IS NULL "
            "AND result_json IS NULL AND failure_code IS NULL) OR "
            "(status = 'started' AND started_at IS NOT NULL "
            "AND terminal_at IS NULL AND provider IS NOT NULL AND model IS NOT NULL "
            "AND usage_event_id IS NOT NULL AND result_json IS NULL "
            "AND failure_code IS NULL) OR "
            "(status = 'succeeded' AND started_at IS NOT NULL "
            "AND terminal_at IS NOT NULL AND provider IS NOT NULL "
            "AND model IS NOT NULL AND usage_event_id IS NOT NULL "
            "AND result_json IS NOT NULL AND failure_code IS NULL) OR "
            "(status IN ('provider_error', 'provider_timeout', 'usage_unmetered', "
            "'usage_ambiguous', 'usage_mismatch', 'invalid_response', "
            "'response_truncated', 'result_unavailable') "
            "AND started_at IS NOT NULL AND terminal_at IS NOT NULL "
            "AND provider IS NOT NULL AND model IS NOT NULL "
            "AND usage_event_id IS NOT NULL AND result_json IS NOT NULL "
            "AND failure_code = status)",
            name="ck_bank_recon_suggest_attempt_lifecycle",
        ),
    )


# W-1 ledger-contract alias (plan 20260702-qa-remediation): the plan names the
# metering table "llm_usage_ledger"; the platform's existing per-call ledger IS
# llm_usage_events (one table, not two — root-cause over duplication). Callers
# written against the plan name resolve to the same model.
LLMUsageLedger = LLMUsageEvent


class RealmContextProfile(Base):
    """
    Per-realm precomputed context substrate (audit item S6) — the single
    isolated context profile for one QBO realm.

    This is the CORE of the realm-isolation invariant: every row is scoped by
    the full triple (organization_id / organization_company_id / qbo_realm_id)
    and a UNIQUE constraint on that triple guarantees one profile per realm.
    Nothing realm-specific is ever stored at tenant level, and a profile for
    one realm can never surface for another. Stores only safe summaries /
    digests (e.g. chart-of-accounts digest), never raw financial dumps,
    tokens, or secrets.
    """
    __tablename__ = "realm_context_profiles"

    id = Column(Integer, primary_key=True, index=True)

    # Realm-scope triple — all three required; this row is meaningless without
    # a fully-resolved realm.
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"), nullable=False, index=True)
    qbo_realm_id = Column(String, nullable=False, index=True)

    profile_version = Column(String, nullable=False, default="v1")
    status = Column(String, nullable=False, default="pending", index=True)

    fiscal_context_json = Column(JSON, nullable=True)
    chart_of_accounts_digest_json = Column(JSON, nullable=True)
    module_context_json = Column(JSON, nullable=True)
    known_data_gaps_json = Column(JSON, nullable=True)
    safe_memory_summary_json = Column(JSON, nullable=True)
    source_refs_json = Column(JSON, nullable=True)
    confidence_json = Column(JSON, nullable=True)

    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "organization_company_id",
            "qbo_realm_id",
            name="uq_realm_context_profile_scope",
        ),
        Index("ix_realm_context_profiles_company_realm", "organization_company_id", "qbo_realm_id"),
    )


class RealmContextArtifact(Base):
    """
    A single precomputed, realm-scoped context artifact (audit item S6) — e.g.
    a precomputed onboarding KPI/covenant recommendation set or a chart-of-
    accounts digest produced by RealmContextBuilder.

    Every artifact carries the full realm triple (organization_id /
    organization_company_id / qbo_realm_id); artifacts for one realm can never
    be read or written by another. Stores summarized content only — never raw
    financial dumps, tokens, or secrets — plus a content_hash, freshness, and
    source references for grounding.
    """
    __tablename__ = "realm_context_artifacts"

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"), nullable=False, index=True)
    qbo_realm_id = Column(String, nullable=False, index=True)

    artifact_type = Column(String, nullable=False, index=True)
    source_type = Column(String, nullable=False)
    source_id = Column(String, nullable=True)
    source_period = Column(String, nullable=True)
    freshness_at = Column(DateTime(timezone=True), nullable=True)

    content_json = Column(JSON, nullable=True)
    content_hash = Column(String, nullable=True, index=True)
    confidence = Column(Float, nullable=True)

    created_by_agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_realm_context_artifacts_scope_type",
            "organization_company_id",
            "qbo_realm_id",
            "artifact_type",
        ),
        Index("ix_realm_context_artifacts_realm_created", "qbo_realm_id", "created_at"),
    )


# ---------------------------------------------------------
# Tenant Data Lifecycle (export + deletion)
# ---------------------------------------------------------

class TenantDataExport(Base):
    """
    Durable record of a tenant data-export bundle.

    The serialized bundle (JSON + CSVs, zipped) is written to local object
    storage; only a SHA-256 hash of the single-use download secret is stored
    here, mirroring the ApprovalToken / AgentConfirmationToken pattern, so the
    bearer download link is never persisted. The bundle NEVER contains encrypted
    QBO tokens, password hashes, or Stytch secrets.
    """
    __tablename__ = "tenant_data_exports"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    status = Column(String, nullable=False, default="pending", index=True)  # pending | processing | ready | failed | expired
    storage_path = Column(String, nullable=True)
    download_token_hash = Column(String, unique=True, index=True, nullable=True)
    file_size_bytes = Column(Integer, nullable=True)
    row_counts_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TenantDeletionRequest(Base):
    """
    Two-step, platform-admin-only tenant deletion request.

    Step 1 (open) soft-deletes the org (suspended/pending-deletion) and records
    a grace window. Step 2 (confirm) only proceeds after the grace period has
    elapsed, unless a platform admin sets an explicit force flag, and enqueues
    the hard-delete task. Both steps are audited via AuthAuditEvent.
    """
    __tablename__ = "tenant_deletion_requests"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    confirmed_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    status = Column(String, nullable=False, default="pending_deletion", index=True)
    # pending_deletion | confirmed | completed | cancelled | failed
    reason = Column(Text, nullable=True)
    forced = Column(Boolean, nullable=False, default=False)

    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    grace_until = Column(DateTime(timezone=True), nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    deleted_row_counts_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AICorrectionEvent(Base):
    """One row per human accept/edit/reject/dismiss/confirm of an AI-produced
    proposal — the structured training signal behind confidence calibration and
    the tenant-knowledge flywheel (Autonomy roadmap, Phase 1).

    Realm-scoped: ``organization_id`` and ``qbo_realm_id`` are NOT NULL because
    every captured task is tenant work; ``organization_company_id`` is nullable
    because some realms have no mapped OrganizationCompany. The capture helper
    (``app/services/correction_capture.py``) refuses to write a row whose
    organization can't be resolved, so a tenant-task correction is never stored
    with a NULL org. Stores only structured values / refs — never raw documents.
    """
    __tablename__ = "ai_correction_events"

    id = Column(Integer, primary_key=True, index=True)

    # categorization | reclassification | journal_entry | budget_value |
    # schedule_entry | covenant_flag | cashflow_flag
    task_type = Column(String, nullable=False, index=True)

    # Realm-scope: org + realm required; company nullable (mapping may be absent).
    # CASCADE, not SET NULL: the column is NOT NULL, so SET NULL could never succeed
    # and an organization delete would fail on its correction history instead.
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    qbo_realm_id = Column(String, nullable=False, index=True)

    # The proposal/action/row id this correction relates to (e.g. ProposedAction
    # id, transaction id). String for cross-source uniformity.
    source_object_ref = Column(String, nullable=True)

    ai_output = Column(JSON, nullable=True)
    ai_confidence = Column(Float, nullable=True)

    # pgvector embedding of the correction (task + corrected value) for U5 exemplar
    # retrieval — same pinned space as CompanyKnowledge (text-embedding-3-small, 1536).
    # PG gets a real Vector column (HNSW vector_cosine_ops index in the migration +
    # the squashed baseline); the SQLite test dialect keeps JSON (none_as_null=True so
    # a None embedding is SQL NULL, matching PG, for the backfill's .is_(None) filter).
    correction_embedding = Column(Vector(1536).with_variant(JSON(none_as_null=True), "sqlite"), nullable=True)  # VECTOR-OK: sqlite-test dialect json fallback; PG is Vector(1536)

    # accepted | edited | rejected | dismissed | confirmed
    human_decision = Column(String, nullable=False)
    human_output = Column(JSON, nullable=True)

    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Shadow-mode evaluation (Autonomy roadmap, Phase 2 PR1). Computed at capture
    # time from the calibrated confidence vs the tenant's threshold; NULL when not
    # evaluated (no confidence, or a task type that isn't shadow-scored).
    # would_autoexecute: would this have auto-executed under L3? matched_human:
    # did applying the AI output match the human's decision (i.e. accepted as-is)?
    would_autoexecute = Column(Boolean, nullable=True)
    matched_human = Column(Boolean, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_ai_correction_events_org_task_created", "organization_id", "task_type", "created_at"),
        Index("ix_ai_correction_events_realm_created", "qbo_realm_id", "created_at"),
    )


class TenantTaskAutonomy(Base):
    """Per-tenant, per-task autonomy level (Autonomy roadmap, Phase 2 PR2).

    Controls whether a task runs proposal-first (human approves everything),
    shadow (measure would-be-auto but still route to human), or supervised_l3
    (gated auto-execution for reversible actions within caps). **The absence of a
    row means the DEFAULT, ``shadow``** — so every tenant/task starts in shadow
    and merging PR2 changes no behavior until a row is deliberately set to
    supervised_l3. The circuit breaker only ever writes ``proposal_first`` here
    (fail-closed demotion); trust is never auto-granted.
    """
    __tablename__ = "tenant_task_autonomy"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type = Column(String, nullable=False)

    # proposal_first | shadow | supervised_l3
    level = Column(String, nullable=False, default="shadow")
    # Per-tenant confidence threshold override; NULL → config default.
    confidence_threshold = Column(Float, nullable=True)

    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "task_type", name="uq_tenant_task_autonomy_org_task"),
    )


# ---------------------------------------------------------
# Close & Review — per-realm review policy (Phase E, DEC-E-2 / DEC-E-7)
# ---------------------------------------------------------

class ReviewPolicy(Base):
    """
    Per-realm review-policy substrate (Phase E, DEC-E-2 / DEC-E-7).

    Reuses the realm-isolation shape of RealmContextProfile (models.py:2946):
    every row is scoped by the full triple (organization_id /
    organization_company_id / qbo_realm_id) and a UNIQUE constraint on that
    triple guarantees ONE policy per realm. The policy supplies the per-company
    review configuration the review engine consults — aggressiveness, the
    focus-account / risk-area emphasis, and a soft pointer to a default review
    period. Per-check materiality OVERRIDES live in the typed
    ReviewPolicyCheckOverride child (mirroring the Materiality value object),
    never as a JSON map on this row.
    """
    __tablename__ = "review_policies"

    id = Column(Integer, primary_key=True, index=True)

    # Realm-scope triple — all three required; this policy is meaningless
    # without a fully-resolved realm (mirrors RealmContextProfile).
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"), nullable=False, index=True)
    qbo_realm_id = Column(String, nullable=False, index=True)

    # balanced | conservative | aggressive — how hard the review pushes.
    review_aggressiveness = Column(String, nullable=False, default="balanced")

    # Emphasis hints (nullable). Accounts/areas the reviewer should weight up.
    focus_accounts_json = Column(JSON, nullable=True)
    risk_areas_json = Column(JSON, nullable=True)

    # Soft pointer to a default review period (e.g. "2026-Q2" / "last-month").
    # Deliberately a string, NOT a hard FK — the period it names may not exist
    # as a row, and the policy must not break when it is stale.
    default_review_period = Column(String, nullable=True)

    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-1 close budget).
    # Per-realm override of the per-close-run LLM token budget. NULL/≤0 → the
    # platform default (settings.close_run_token_budget). Enforced fail-loud in
    # the LLM wrapper (llm_task_context.enforce_close_run_budget).
    close_run_token_budget = Column(Integer, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "organization_company_id",
            "qbo_realm_id",
            name="uq_review_policy_scope",
        ),
        Index("ix_review_policies_company_realm", "organization_company_id", "qbo_realm_id"),
    )


class ReviewPolicyCheckOverride(Base):
    """
    A single per-check materiality OVERRIDE under a ReviewPolicy (Phase E).

    Mirrors the Materiality value object (app/services/review_checks/materiality.py)
    as a TYPED ROW — shape (the MaterialityShape string: absolute/percent/derived/
    dual), abs_floor + pct_floor (Numeric/Decimal), and label — so a company can
    override a registered check's materiality floor with ZERO check-code change
    (DEC-CAR-C-3). A typed row, NOT a JSON map: each override is independently
    queryable and the (policy, check) pair is uniquely constrained so one check
    carries at most one override per policy.
    """
    __tablename__ = "review_policy_check_overrides"

    # REALM-STRING-ONLY-OK: child-of-realm-scoped-parent — tenant ownership flows
    # transitively through the review_policy_id FK (ondelete=CASCADE) to the
    # realm-triple-scoped review_policies parent; this child carries no realm
    # triple of its own by design (it is meaningless outside its policy, like a
    # per-parent detail row, not a top-level tenant table).

    id = Column(Integer, primary_key=True, index=True)

    review_policy_id = Column(
        Integer,
        ForeignKey("review_policies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The registered check this override applies to (the check's registry id).
    check_id = Column(String, nullable=False)

    # The Materiality shape — absolute | percent | derived | dual.
    shape = Column(String, nullable=False)

    # The floors (mirror Materiality.abs_floor / pct_floor; either may be NULL
    # depending on the shape — ABSOLUTE/DERIVED use abs_floor, PERCENT uses
    # pct_floor, DUAL uses both).
    abs_floor = Column(Numeric(precision=18, scale=2), nullable=True)
    pct_floor = Column(Numeric(precision=18, scale=4), nullable=True)

    label = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "review_policy_id",
            "check_id",
            name="uq_review_policy_check_override",
        ),
    )


# NOTE (merge reconciliation 2026-07-12): this file's canonical CompanyFinanceProfile is
# the one defined ABOVE by plan 20260711-peregrine-finance-os FOS-1 Wave A (migration
# f3b8d1c6a2e9, service app/services/company_profile.py, wired into app/analysis/ratios.py).
# The 20260712-agentic-finance-os W1 defined a SECOND, incompatible CompanyFinanceProfile here
# (migration e8b3f6a1d9c4) — a duplicate `__tablename__` that crashed `import app.models` and
# broke every deploy. It is removed; the FOS profile is authoritative. The W1 metric CATALOG
# (app/services/semantics/) + W2 evidence/claim-verification + W3 mission-state below are
# independent of the profile table and are retained. See work/agentic-finance-os/.


class AgentRunCheckpoint(Base):
    """
    Durable run-progress event for an AgentRun (plan 20260712-agentic-finance-os
    W3, GAP-3): each row marks a validated STATE TRANSITION (from_state → to_state
    with actor + reason) and/or a resumable CHECKPOINT (a named savepoint carrying
    the accumulated state snapshot). A worker restart mid-mission resumes from the
    latest checkpoint's state instead of re-running completed deterministic work.

    Rows are append-only and monotonically sequenced per run (uq on
    agent_run_id + sequence). This EXTENDS the existing tracking.py state machine
    (which already validates transitions + immutable terminals) with an audit +
    resume ledger — it does not replace it.

    Tenant scoping is TRANSITIVE through ``agent_run_id`` (CASCADE FK to the
    tenant-owned, RLS'd agent_runs), mirroring the sibling ``agent_steps`` table —
    a checkpoint has no existence independent of its run, and a caller can only
    obtain a run_id for a run it already owns. No independent tenant column / RLS
    policy, exactly like agent_steps.
    """
    __tablename__ = "agent_run_checkpoints"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "sequence", name="uq_agent_run_checkpoint_sequence"),
    )

    id = Column(Integer, primary_key=True, index=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)

    sequence = Column(Integer, nullable=False)  # monotonic per run
    from_state = Column(String, nullable=True)
    to_state = Column(String, nullable=True, index=True)
    actor = Column(String, nullable=False, default="system")  # system | user:<id> | worker:<name> | schedule
    reason = Column(Text, nullable=True)

    checkpoint_key = Column(String, nullable=True, index=True)  # named savepoint, e.g. "facts_gathered"
    # none_as_null: a null state is stored as SQL NULL (not JSON 'null'), so a
    # `state_json IS NOT NULL` resume query correctly skips state-less transition rows.
    state_json = Column(JSON(none_as_null=True), nullable=True)  # the resumable accumulated state

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class WorkItem(Base):
    """
    A node in the shared finance WORK GRAPH (plan 20260712-agentic-finance-os W4,
    GAP-4, directive §4): an objective / task / question / risk / open-loop / decision
    that is NOT already a first-class row (proposals, close tasks, agent objectives,
    insights, and alerts are FEDERATED by the work_graph service, never copied here).
    A WorkItem can cross-link to one of those rows when it wraps or tracks it.

    Tenant table — org FK + realm; org-keyed FORCE-RLS (migration below).
    """
    __tablename__ = "work_items"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    kind = Column(String, nullable=False, index=True)  # objective | task | question | risk | open_loop | decision
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="open", index=True)  # open | in_progress | blocked | waiting | done | cancelled
    owner_kind = Column(String, nullable=False, default="unassigned")  # human | agent | unassigned
    owner_ref = Column(String, nullable=True)  # user id/email or agent name — the "who owns the next step"

    parent_id = Column(Integer, ForeignKey("work_items.id", ondelete="SET NULL"), nullable=True, index=True)
    blocked_by = Column(JSON, nullable=True)      # list of blocker refs (work_item ids / external)
    materiality = Column(String, nullable=True)   # low | medium | high | critical
    due_date = Column(Date, nullable=True)
    evidence_refs = Column(JSON, nullable=True)   # list of {kind, ref} — claim_id / fact_id / artifact_id / source
    completion_criteria = Column(Text, nullable=True)

    # Cross-links to the federated first-class rows (a WorkItem tracks at most one).
    linked_proposal_id = Column(Integer, ForeignKey("proposed_actions.id", ondelete="SET NULL"), nullable=True, index=True)
    linked_close_task_id = Column(Integer, ForeignKey("close_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    linked_objective_id = Column(Integer, ForeignKey("agent_objectives.id", ondelete="SET NULL"), nullable=True, index=True)
    linked_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    linked_artifact_id = Column(Integer, ForeignKey("insight_artifacts.id", ondelete="SET NULL"), nullable=True, index=True)

    created_by = Column(String, nullable=True)  # human:id | agent:name | system
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)


class Handoff(Base):
    """
    A structured HANDOFF PACKAGE (plan 20260712-agentic-finance-os W4, GAP-4a,
    directive §11): the durable, continue-without-the-transcript artifact passed
    human→agent, agent→human, or agent→agent. The ``package`` JSON carries the
    directive's fields (work completed / remaining, findings, evidence, assumptions,
    open questions, risks, decisions, recommended next step, owner, due date,
    freshness, reconciliation status) — assembled from the work graph, never a raw
    conversation transcript.

    Tenant table — org FK + realm; org-keyed FORCE-RLS (migration below).
    """
    __tablename__ = "handoffs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    organization_company_id = Column(Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String, index=True, nullable=False)  # realm_id

    objective = Column(String, nullable=False)
    from_actor = Column(String, nullable=False)  # human:<id> | agent:<name> | system
    to_actor = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open", index=True)  # open | accepted | completed
    package = Column(JSON, nullable=False)  # the structured handoff payload (assembled from the graph)

    work_item_id = Column(Integer, ForeignKey("work_items.id", ondelete="SET NULL"), nullable=True, index=True)
    recommended_next_step = Column(Text, nullable=True)
    accepted_by = Column(String, nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------
# Agent-authored checkpointed plans (Wave 3.1, WF-12)
# ---------------------------------------------------------
# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).


class AgentPlan(Base):
    """
    A model-composed, human-approved multi-step plan (plan 20260714-wave3-1-agent-plans,
    decision 1). The chat agent COMPOSES the DAG; ``plan_validation.validate_plan``
    gates it at persist; a human approves the AGENT_PLAN proposal before step 1 runs;
    the sibling walker (chunk B) drives it through the existing proposal funnel.

    IMMUTABILITY CONTRACT (E4): the hash-covered DEFINITION lives in the step rows
    (tool name, input payload, dep edges, order) and is sealed by ``steps_hash``
    (``plan_validation.steps_content_hash``) — no service updates definition fields
    post-persist; approval re-hashes the rows and REFUSES on mismatch. Execution-state
    (``status`` here; step status/attempts/proposal linkage) is mutable BY THE WALKER
    ONLY, via claim-style conditional UPDATEs (E9).

    Tenant table — indexed org FK (AP-005) + realm; realm-keyed FORCE-RLS
    (migration e9c5a1f8b3d7).
    """
    __tablename__ = "agent_plans"

    # Closed status vocabulary (decision 8). Terminal states never re-fire the sweep.
    STATUSES = (
        "DRAFT",
        "PENDING_APPROVAL",
        "ACTIVE",
        "PAUSED",
        "CANCELLED",
        "COMPLETE",
        "FAILED",
        "EXPIRED",
    )
    TERMINAL_STATUSES = ("CANCELLED", "COMPLETE", "FAILED", "EXPIRED")

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)

    title = Column(String, nullable=False)
    goal = Column(Text, nullable=False)

    status = Column(String, nullable=False, default="DRAFT", index=True)  # ∈ STATUSES
    # SHA-256 over the canonical step serialization (plan_validation.steps_content_hash).
    # The tamper seal: AGENT_PLAN approval re-hashes the step rows and refuses on mismatch.
    steps_hash = Column(String, nullable=False)

    created_by = Column(String, nullable=True)  # user id/email that asked for the plan
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AgentPlanStep(Base):
    """
    One node of an AgentPlan DAG. DEFINITION fields (``step_index``, ``tool_name``,
    ``tool_input``, ``depends_on``) are hash-covered and immutable post-persist (E4);
    execution-state fields (``status``, ``attempts``, ``proposed_action_id``,
    ``evidence``) are walker-mutable only.

    ``is_write`` is DERIVED at persist from the registry flags
    (``mutates_state``/``creates_proposed_action``) — never model-declared
    (decision 5). A write step drafts ONE standard PENDING ProposedAction carrying
    ``plan_step_id`` (the uq dedup key); the walker NEVER executes it (decision 6).

    Tenant table — realm denormalized from the parent plan so the realm-keyed
    FORCE-RLS policy applies row-locally (migration e9c5a1f8b3d7); org isolation is
    carried by the parent agent_plans org FK.  # REALM-STRING-ONLY-OK: plan-scoped child rows; org FK lives on parent agent_plans
    """
    __tablename__ = "agent_plan_steps"
    __table_args__ = (
        # A plan's step indexes are unique — the DAG's dep edges reference them.
        UniqueConstraint("plan_id", "step_index", name="uq_agent_plan_steps_plan_step_index"),
    )

    # Closed status vocabulary (decision 6 park taxonomy).
    STATUSES = (
        "pending",
        "ready",
        "running",
        "complete",
        "awaiting_approval",
        "blocked_retryable",
        "bounded_daily",
        "failed",
        "skipped",
        "expired",
    )

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("agent_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    realm_id = Column(String, index=True, nullable=False)  # denormalized from the plan (RLS key)

    # ── DEFINITION (hash-covered, immutable post-persist — E4) ──
    step_index = Column(Integer, nullable=False)
    tool_name = Column(String, nullable=False)
    tool_input = Column(JSON, nullable=False, default=dict)   # scope-stripped at validation
    depends_on = Column(JSON, nullable=False, default=list)   # list of step_indexes this step waits on
    is_write = Column(Boolean, nullable=False, default=False)  # DERIVED from registry flags at persist

    # ── EXECUTION STATE (walker-mutable only) ──
    status = Column(String, nullable=False, default="pending", index=True)  # ∈ STATUSES
    attempts = Column(Integer, nullable=False, default=0)
    # Plain Integer, NOT a ForeignKey: proposed_actions.plan_step_id already FKs this
    # table, and a second FK back would make the pair circular — create_all's
    # topological sort (and the squashed baseline, which builds from sorted_tables)
    # cannot order a cycle. The uq-bearing FK direction (proposal -> step) is the
    # load-bearing one; this column is the walker's convenience pointer.
    proposed_action_id = Column(Integer, nullable=True, index=True)
    evidence = Column(Text, nullable=True)  # honest walker findings (park reasons, results)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AgentPlanRunLock(Base):
    """
    The per-realm agent-plan walk single-flight LEASE — the CloseRunLock mirror
    (decisions 2/9: per-realm single-ACTIVE-plan). One row per realm, claimed by the
    plan walker before it starts and heartbeat-bumped on every step commit; a lease
    whose ``heartbeat_at`` is older than the TTL is STEALABLE by the next run — a
    dead or wedged session can never hold a realm past the TTL. ``plan_id`` records
    which plan holds the lease (plan-scoped, per AC-A1).

    Acquire/steal/release semantics land in ``app/agent/runtime/plan_walker.py``
    (chunk B), copying the close_orchestration lease PATTERN (not imports).
    Realm-keyed tenant table → FORCE-RLS realm policy (migration e9c5a1f8b3d7).
    # REALM-STRING-ONLY-OK: per-realm lease row, CloseRunLock mirror — realm IS the key
    """
    __tablename__ = "agent_plan_run_locks"

    realm_id = Column(String, primary_key=True)
    plan_id = Column(Integer, nullable=True, index=True)
    agent_run_id = Column(Integer, nullable=True, index=True)
    locked_at = Column(DateTime(timezone=True), nullable=False)
    heartbeat_at = Column(DateTime(timezone=True), nullable=False, index=True)


# PLAN-EXEMPT: chunk 1 of ratified plan 20260714-wave3-2-asc606-engine (marker
# RATIFICATION-2026-07-14.md on disk; active-plan.json not synced into this worktree).
# ── ASC 606 revenue engine v2 (plan 20260714-wave3-2-asc606-engine, decision 1/2/4) ──
# The RevenueContract engine gets its OWN entities — the same architecture ASC 842 got
# its own LeaseSchedule/LeaseScheduleRow (SSP relative-allocation + over-time cost-to-cost
# recognition must not contort the single straight-line AccountingSchedule substrate) —
# but rides the SAME rails: JOURNAL_ENTRY ProposedAction, period locks, tie-out. The
# ENGINE (Chunk 2) owns every recognized number; the human confirms SSP + progress% + the
# ASC-606 criteria (never engine-invented — the LOCKED-lease-rate precedent).
class RevenueContract(Base):
    """
    An ASC 606 revenue contract (plan 20260714-wave3-2-asc606-engine, AC-4/E8). Header
    for one-to-many PerformanceObligations. The transaction_price is a CONFIRMED FIXED
    human input this wave (variable-consideration constraint 606-10-32-11+ is DEFERRED to
    a follow-on wave — plan Out-of-scope); the engine allocates it across obligations by
    relative SSP at activation (measure-at-activate, the 842 pattern).

    Lifecycle: DRAFT → ACTIVE → MODIFIED → (COMPLETE | CANCELLED).
      * DRAFT: terms editable, no obligations allocated, nothing recognized.
      * ACTIVE (activate): SSP relative allocation runs, obligation allocated_price
        persisted, recognition rows generated per method, recognition JEs proposal-gated.
      * MODIFIED: a contract-modification re-allocated remaining transaction price across
        obligations; posted periods UNCHANGED, the cumulative catch-up delta surfaced.

    Tenant table — indexed org FK (AP-005) + realm; realm-keyed FORCE-RLS (migration
    f1a7c3e9d2b6), the AgentPlan precedent.
    """
    __tablename__ = "revenue_contracts"

    # Closed status vocabulary (decision 1). Terminal states recognize nothing new.
    STATUSES = ("DRAFT", "ACTIVE", "MODIFIED", "COMPLETE", "CANCELLED")
    TERMINAL_STATUSES = ("COMPLETE", "CANCELLED")

    id = Column(Integer, primary_key=True, index=True)
    realm_id = Column(String, index=True, nullable=False)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name = Column(String, nullable=False)             # e.g. "Acme SaaS + implementation 2026"
    description = Column(Text, nullable=True)
    customer_name = Column(String, nullable=True)     # QBO customer

    contract_date = Column(Date, nullable=False)      # inception / agreement date

    # The confirmed FIXED transaction price the engine allocates (Σ allocated ≡ this).
    transaction_price = Column(Numeric(precision=18, scale=2), nullable=False)

    status = Column(String, nullable=False, default="DRAFT", server_default="DRAFT", index=True)

    # Set only when activation succeeds (allocation measured); NULL while DRAFT.
    activated_at = Column(DateTime(timezone=True), nullable=True)

    created_by = Column(String, nullable=True)        # user email
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PerformanceObligation(Base):
    """
    One distinct performance obligation of a RevenueContract (plan
    20260714-wave3-2-asc606-engine, AC-4). The unit of allocation AND recognition.

    ``ssp`` (standalone selling price) and ``total_estimated_cost`` (for over-time
    cost-to-cost) are REQUIRED HUMAN INPUTS validated server-side — the engine NEVER
    fabricates them (a missing/invalid SSP is rejected, never defaulted; the
    lease_service:115 precedent). ``allocated_price`` is ENGINE-computed at activation
    (relative-SSP allocation, Σ ≡ contract transaction_price, last-obligation plug).

    ``recognition_method`` ∈ {over_time, point_in_time}; ``over_time_measure`` ∈
    {cost_to_cost (input), output} for over-time obligations. ``recognition_criteria``
    stores the human-answered ASC 606-10-25-27 over-time criteria booleans (the
    Chunk-0 criteria-key set) — the engine reads them, never self-evaluates from
    contract text (the 842 classification_criteria precedent, LeaseSchedule).

    Tenant child — realm denormalized from the parent contract as the RLS key; org
    isolation carried by the parent contract's org FK.
    # REALM-STRING-ONLY-OK: contract-scoped child rows; org FK lives on parent revenue_contracts
    """
    __tablename__ = "performance_obligations"
    __table_args__ = (
        UniqueConstraint(
            "contract_id", "obligation_index", name="uq_performance_obligations_contract_index"
        ),
    )

    RECOGNITION_METHODS = ("over_time", "point_in_time")
    OVER_TIME_MEASURES = ("cost_to_cost", "output")

    id = Column(Integer, primary_key=True, index=True)
    contract_id = Column(
        Integer, ForeignKey("revenue_contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    realm_id = Column(String, index=True, nullable=False)  # denormalized from the contract (RLS key)

    # Deterministic ordering — the relative-SSP last-obligation plug lands on the row
    # with the highest obligation_index (Σ rounding residual). Strictly ordered per contract.
    obligation_index = Column(Integer, nullable=False)

    name = Column(String, nullable=False)             # e.g. "SaaS subscription (12mo)"
    description = Column(Text, nullable=True)

    # REQUIRED human input — the standalone selling price (relative-allocation basis).
    # Nullable while DRAFT; server-validated non-null + > 0 at activation (never defaulted).
    ssp = Column(Numeric(precision=18, scale=2), nullable=True)

    # ENGINE-computed at activation (relative-SSP allocation). NULL while DRAFT.
    allocated_price = Column(Numeric(precision=18, scale=2), nullable=True)

    recognition_method = Column(String, nullable=False)          # ∈ RECOGNITION_METHODS
    over_time_measure = Column(String, nullable=True)            # ∈ OVER_TIME_MEASURES (over_time only)

    # REQUIRED human input for cost_to_cost over-time (PoC = cumulative_cost / this).
    # Nullable otherwise; server-validated non-null + > 0 when method+measure demand it.
    total_estimated_cost = Column(Numeric(precision=18, scale=2), nullable=True)

    # Human-answered ASC 606-10-25-27 over-time criteria booleans (Chunk-0 key set).
    # The engine READS these to gate over-time recognition — never self-evaluates.
    recognition_criteria = Column(JSON, nullable=True)

    # The contract-liability (deferred revenue) BS account and the contract-asset BS
    # account this obligation recognizes against, plus the P&L revenue credit account.
    # deferred_revenue_account_id is the E3/E7 double-recognition partition target
    # (ScheduleAccountCoverage.owning_contract_id keys on it at activation).
    deferred_revenue_account_id = Column(String, nullable=True)
    deferred_revenue_account_name = Column(String, nullable=True)
    contract_asset_account_id = Column(String, nullable=True)
    contract_asset_account_name = Column(String, nullable=True)
    revenue_account_id = Column(String, nullable=True)
    revenue_account_name = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RevenueRecognitionRow(Base):
    """
    One persisted, IMMUTABLE recognition period of a PerformanceObligation (plan
    20260714-wave3-2-asc606-engine, decision 4 / D3.3 columns). Mirrors LeaseScheduleRow:
    rows are written once by the engine and regenerated ONLY via the gated modification
    path (rows ≥ the effective period, rebuilt when the modification proposal EXECUTES,
    with ``modified=True``). Posted/locked periods are NEVER restated — the cumulative
    catch-up surfaces as a delta on the current period, never a silent rewrite.

    Every financial column is ``nullable=False`` — a recognition row is fully-formed and
    immutable at write (the immutability contract; there is no partial-then-patch path).

    Column semantics (all Decimal, quantized to cents by the engine unless noted):
      allocated_price                the obligation's engine-allocated price (basis)
      measure_of_progress_pct        PoC / output measure for the period (the E3 golden:
                                     32000/64000 = 0.500000; Numeric(9,6), 1.0 = complete)
      revenue_recognized_this_period the DELTA of cumulative recognized (period revenue)
      cumulative_recognized_to_date  allocated_price × measure (the E3 golden: 80000×0.50
                                     = 40000.00 ≠ the 32000 cost incurred)
      remaining_to_recognize         allocated_price − cumulative_recognized_to_date
      contract_liability_bal         deferred-revenue (contract-liability) running balance
      contract_asset_bal             contract-asset running balance (billings < recognition)
    """
    __tablename__ = "revenue_recognition_rows"
    __table_args__ = (
        UniqueConstraint(
            "obligation_id", "period", name="uq_revenue_recognition_rows_obligation_period"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    obligation_id = Column(
        Integer,
        ForeignKey("performance_obligations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    realm_id = Column(String, index=True, nullable=False)  # denormalized from the obligation (RLS key)

    period = Column(String, nullable=False, index=True)   # YYYY-MM

    allocated_price = Column(Numeric(precision=18, scale=2), nullable=False)
    measure_of_progress_pct = Column(Numeric(precision=9, scale=6), nullable=False)
    revenue_recognized_this_period = Column(Numeric(precision=18, scale=2), nullable=False)
    cumulative_recognized_to_date = Column(Numeric(precision=18, scale=2), nullable=False)
    remaining_to_recognize = Column(Numeric(precision=18, scale=2), nullable=False)
    contract_liability_bal = Column(Numeric(precision=18, scale=2), nullable=False)
    contract_asset_bal = Column(Numeric(precision=18, scale=2), nullable=False)

    # True on rows rebuilt by an executed contract modification — such rows carry the
    # re-allocated basis; posted periods are excluded from the rebuild (never restated).
    modified = Column(Boolean, nullable=False, default=False, server_default="false")

    proposed_action_id = Column(Integer, nullable=True)   # FK → proposed_actions.id (soft, like LeaseScheduleRow)
    status = Column(String, nullable=False, default="DRAFT", server_default="DRAFT", index=True)
    # DRAFT → generated · PENDING_APPROVAL → JE drafted · POSTED → JE executed · SKIPPED

    posted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# Clickwrap & Proof-of-Assent — legal schema ORM models
# ---------------------------------------------------------------------------
# Plan 20260720-clickwrap-backend-spine (Wave 1, sub-chunk W1-b). These nine
# classes are the ORM mirror of the append-only legal-assent datastore whose DDL
# is the W1-a Alembic migration (migrations/versions/c7f2b9e4a1d3_*). They live
# in the DEFAULT schema, namespaced by their legal_/assent_/connection_/
# org_legal_ table-name prefix (DEC-default-schema 2026-07-20; no __table_args__
# schema dict), and match that migration column-for-column (type, nullability,
# server_default),
# with the ratified DEC deltas applied:
#   DEC-enum      every SPEC ENUM is a Text column (allowed values inline); the
#                 IN (...) CHECK lives in the migration, not the model.
#   DEC-trigger   the assent hash chain is computed in the Python service; the
#                 model only carries the row_hash / prev_row_hash columns.
#   DEC-F1        the assent document snapshot columns are NULLABLE (a version-
#                 less entity_attestation / aup_ack row carries none).
#   DEC-F4        legal_document_versions.is_current_required is dropped;
#                 legal_documents.current_required_version_id is kept as a plain
#                 uuid one-writer cache pointer with NO FK (the effective_date +
#                 version_ordinal query is the sole required-version resolver).
#   DEC-F2        legal.* is an RLS-EXEMPT evidence store keyed on the Stytch
#                 string organization_id (Text), never an FK to organizations.id.
#   DEC-ipcheck   ip_address is NULLABLE, paired with an ip_unavailable flag.
#   DEC-presented_at  presented_at is NULLABLE (advisory server render-time).
# DEC-portable-types: types are GENERIC so they render on BOTH Postgres (prod +
# the PG-RLS lane) AND SQLite (the test substrate's Base.metadata.create_all).
# uuid -> Uuid (PG native uuid / SQLite CHAR32; Python-side default=uuid.uuid4,
# not a DB-function server_default), text -> Text, char(64) -> CHAR(64),
# inet -> String, jsonb -> JSON, text[] -> JSON (the list stored as JSON), bigint
# hash-chain ordering -> plain BigInteger (the W2 service assigns seq), smallint
# -> SmallInteger. The legal tables live in the DEFAULT schema (no __table_args__
# schema dict), so Base.metadata.create_all resolves them directly on SQLite with
# no conftest schema-translate map. The migration is Postgres-guarded (returns
# early on non-Postgres) and is kept in exact TYPE parity with the columns below.


class LegalDocument(Base):
    """Per-document-type header row of the manifest mirror (SPEC §7.3)."""
    __tablename__ = "legal_documents"

    document_type = Column(Text, primary_key=True)  # tos | privacy | dpa | entity_attestation | aup_ack
    display_name = Column(Text, nullable=False)
    # DEC-F4: kept as a plain uuid one-writer cache pointer — NO FK (the
    # effective_date + version_ordinal query is the required-version resolver).
    current_required_version_id = Column(Uuid, nullable=True)
    retention_years_post_termination = Column(SmallInteger, nullable=False, server_default=text("7"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LegalDocumentVersion(Base):
    """One immutable, re-hashed manifest version per (document_type, version) (SPEC §7.3)."""
    __tablename__ = "legal_document_versions"
    __table_args__ = (
        UniqueConstraint("document_type", "version", name="uq_doc_version"),
        UniqueConstraint("document_type", "version_ordinal", name="uq_doc_ordinal"),
        UniqueConstraint("document_type", "sha256", name="uq_doc_sha"),
        Index("ix_ldv_type_version", "document_type", "version_ordinal"),
    )

    id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    document_type = Column(Text, nullable=False)  # tos | privacy | dpa | entity_attestation | aup_ack
    version = Column(Text, nullable=False)  # canonical 'v1.0' label
    version_ordinal = Column(Integer, nullable=False)  # drives all staleness comparisons
    effective_date = Column(Date, nullable=False)
    sha256 = Column(CHAR(64), nullable=False)  # regex CHECK lives in the migration
    snapshot_uri = Column(Text, nullable=False)
    content_media_type = Column(Text, nullable=False, server_default=text("'text/html'"))
    is_material = Column(Boolean, nullable=False, server_default=text("false"))
    # DEC-F4: no is_current_required flag / partial-unique index.
    stable_url = Column(Text, nullable=False)
    redline_url = Column(Text, nullable=True)
    changelog_summary = Column(Text, nullable=True)
    byte_length = Column(Integer, nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=False)  # ingest re-hash OK time
    manifest_generated_at = Column(DateTime(timezone=True), nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AssentRecord(Base):
    """Append-only, hash-chained proof-of-assent row (SPEC §7.4). Corrections are new rows."""
    __tablename__ = "assent_records"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", "document_type", name="uq_assent_idem"),
        Index("ix_assent_latest", "organization_id", "document_type", text("accepted_at DESC")),
        Index("ix_assent_member", "member_id"),
        Index("ix_assent_intent", "connection_intent_id", postgresql_where=text("connection_intent_id IS NOT NULL")),
        Index("ix_assent_realm", "realm_id", postgresql_where=text("realm_id IS NOT NULL")),
        Index("ix_assent_doc_version", "document_type", "document_version"),
        Index("ix_assent_org_seq", "organization_id", "seq"),
    )

    assent_id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    seq = Column(BigInteger, nullable=False)  # per-org hash-chain ordering (W2 service assigns it)
    assent_batch_id = Column(Uuid, nullable=False)  # groups a multi-doc POST
    idempotency_key = Column(Text, nullable=False)

    # WHO
    member_id = Column(Text, nullable=False)  # Stytch Member ID
    member_email = Column(Text, nullable=False)  # snapshot at accept time
    # REALM-STRING-ONLY-OK: legal.* is an RLS-exempt append-only evidence store keyed on the Stytch string org id (DEC-F2 / AC-10)
    organization_id = Column(Text, nullable=False)  # Stytch Organization ID (tenant)
    entity_legal_name = Column(Text, nullable=False)
    entity_legal_name_source = Column(Text, nullable=True)  # reconciled Intuit legalName
    connection_intent_id = Column(Uuid, nullable=True)  # non-null for entity_attestation
    realm_id = Column(Text, nullable=True)  # reconciled post-OAuth
    connected_entity_id = Column(Uuid, nullable=True)
    authority_attested = Column(Boolean, nullable=False)
    business_status_evidence = Column(JSON, nullable=False, server_default=text("'{}'"))

    # WHAT (denormalized snapshot). DEC-F1: all document snapshot columns NULLABLE.
    document_version_id = Column(Uuid, ForeignKey("legal_document_versions.id", name="fk_assent_document_version"), nullable=True)
    document_type = Column(Text, nullable=False)  # tos | privacy | dpa | entity_attestation | aup_ack
    document_version = Column(Text, nullable=True)  # 'v1.0'
    document_effective_date = Column(Date, nullable=True)
    document_sha256 = Column(CHAR(64), nullable=True)
    document_snapshot_uri = Column(Text, nullable=True)
    acceptance_statement_text = Column(Text, nullable=False)  # exact clickwrap sentence rendered

    # OUTCOME (a decline is representable in an append-only store)
    outcome = Column(Text, nullable=False, server_default=text("'accepted'"))  # accepted | declined | acknowledged | warranted

    # HOW / WHEN
    assent_mechanism = Column(Text, nullable=False, server_default=text("'checkbox+button'"))  # checkbox+button | re_acceptance_modal | admin_backfill | signed_order_form
    onboarding_context = Column(Text, nullable=False, server_default=text("'org_creation'"))  # org_creation | reacceptance | backfill | connect_entity | first_login
    supporting_instrument_uri = Column(Text, nullable=True)  # executed PDF for signed_order_form / admin_backfill
    actor_note = Column(Text, nullable=True)  # named human + reason for non-clickwrap rows
    auth_method = Column(Text, nullable=True)  # e.g. 'stytch_magic_link', 'sso'
    presented_at_client = Column(DateTime(timezone=True), nullable=True)  # advisory
    presented_at = Column(DateTime(timezone=True), nullable=True)  # DEC-presented_at: NULLABLE (server render time)
    accepted_at = Column(DateTime(timezone=True), nullable=False)  # server: affirmative act (UTC)
    ip_address = Column(String, nullable=True)  # DEC-ipcheck: NULLABLE (hop-counted XFF)
    ip_unavailable = Column(Boolean, nullable=False, server_default=text("false"))  # DEC-ipcheck flag
    user_agent = Column(Text, nullable=True)
    session_id = Column(Text, nullable=True)  # Stytch member_session_id

    # TAMPER EVIDENCE (DEC-trigger: chain computed in the Python service, not a DB trigger)
    prev_row_hash = Column(CHAR(64), nullable=True)
    row_hash = Column(CHAR(64), nullable=False)

    # CORRECTION LINK (no updates, ever)
    supersedes_assent_id = Column(Uuid, ForeignKey("assent_records.assent_id", name="fk_assent_supersedes"), nullable=True)
    correction_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ConnectionEvent(Base):
    """Append-only connected-entity lifecycle events (SPEC §7.5)."""
    __tablename__ = "connection_events"
    __table_args__ = (
        Index("ix_conn_evt_org", "organization_id", "connection_intent_id", "seq"),
    )

    event_id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    seq = Column(BigInteger, nullable=False)  # per-org ordering (W2 service assigns it)
    # REALM-STRING-ONLY-OK: legal.* is an RLS-exempt append-only evidence store keyed on the Stytch string org id (DEC-F2 / AC-10)
    organization_id = Column(Text, nullable=False)
    connection_intent_id = Column(Uuid, nullable=False)
    realm_id = Column(Text, nullable=True)  # null until reconciled at callback
    member_id = Column(Text, nullable=False)
    # intent_created | oauth_initiated | oauth_completed | realm_reconciled | legalname_mismatch_flagged | disconnected | reconnect_requested (CHECK in migration)
    event = Column(Text, nullable=False)
    detail = Column(JSON, nullable=False, server_default=text("'{}'"))
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LegalNoticeLog(Base):
    """Append-only change-notice log (SPEC §7.6)."""
    __tablename__ = "legal_notice_log"
    __table_args__ = (
        Index("ix_notice_org_doc", "organization_id", "document_type", "target_ordinal"),
    )

    notice_id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    seq = Column(BigInteger, nullable=False)  # per-org ordering (W2 service assigns it)
    # REALM-STRING-ONLY-OK: legal.* is an RLS-exempt append-only evidence store keyed on the Stytch string org id (DEC-F2 / AC-10)
    organization_id = Column(Text, nullable=False)
    member_id = Column(Text, nullable=True)  # null for org-wide banner rows
    member_email = Column(Text, nullable=True)
    document_type = Column(Text, nullable=False)  # tos | privacy | dpa | entity_attestation | aup_ack
    target_version = Column(Text, nullable=False)
    target_ordinal = Column(Integer, nullable=False)
    document_version_id = Column(Uuid, ForeignKey("legal_document_versions.id", name="fk_notice_document_version"), nullable=False)
    is_material = Column(Boolean, nullable=False)
    channel = Column(Text, nullable=False)  # email_admin | in_app_banner | in_app_modal
    notice_kind = Column(Text, nullable=False)  # advance_material | passive_nonmaterial | backfill
    effective_date = Column(Date, nullable=False)
    notified_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    provider_message_id = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LegalNoticeDeliveryEvent(Base):
    """Append-only per-notice delivery events (SPEC §7.6)."""
    __tablename__ = "legal_notice_delivery_event"
    __table_args__ = (
        Index("ix_notice_event", "notice_id", "occurred_at"),
    )

    event_id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    notice_id = Column(Uuid, ForeignKey("legal_notice_log.notice_id", name="fk_delivery_notice"), nullable=False)
    status = Column(Text, nullable=False)  # queued | sent | delivered | opened | bounced | failed | shown | dismissed | acknowledged
    detail = Column(Text, nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OrgLegalState(Base):
    """Rebuildable cache of the latest accepted ordinal per (org, doc_type) (SPEC §7.7)."""
    __tablename__ = "org_legal_state"

    # REALM-STRING-ONLY-OK: legal.* is an RLS-exempt evidence-derived cache keyed on the Stytch string org id (DEC-F2 / AC-10)
    organization_id = Column(Text, primary_key=True)
    document_type = Column(Text, primary_key=True)  # tos | privacy | dpa | entity_attestation | aup_ack
    accepted_version = Column(Text, nullable=False)
    accepted_ordinal = Column(Integer, nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False)
    accepted_by_member = Column(Text, nullable=False)
    assent_id = Column(Uuid, ForeignKey("assent_records.assent_id", name="fk_org_state_assent"), nullable=False)
    pending_ordinal = Column(Integer, nullable=True)  # required ordinal the org is behind on
    grace_started_at = Column(DateTime(timezone=True), nullable=True)


class LegalIdempotency(Base):
    """Idempotency record written in the same transaction as the assent rows (SPEC §7.7).

    Composite (organization_id, idempotency_key) primary key, matching the W1-a
    migration (the SPEC §7.7 shape); one row serializes concurrent same-key POSTs.
    """
    __tablename__ = "legal_idempotency"

    # REALM-STRING-ONLY-OK: legal.* is an RLS-exempt append-only evidence store keyed on the Stytch string org id (DEC-F2 / AC-10)
    organization_id = Column(Text, primary_key=True)
    idempotency_key = Column(Text, primary_key=True)
    request_body_sha256 = Column(CHAR(64), nullable=False)
    response_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LegalAssentReadAudit(Base):
    """Append-only audit of reads/exports of proof records (SPEC §7.7)."""
    __tablename__ = "legal_assent_read_audit"

    read_audit_id = Column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_member_id = Column(Text, nullable=False)
    actor_email = Column(Text, nullable=False)
    actor_roles = Column(JSON, nullable=False)  # DEC-portable-types: text[] stored as a JSON list
    # REALM-STRING-ONLY-OK: legal.* is an RLS-exempt append-only evidence store keyed on the Stytch string org id (DEC-F2 / AC-10)
    target_organization_id = Column(Text, nullable=False)
    target_assent_id = Column(Uuid, nullable=True)
    export_range_since = Column(DateTime(timezone=True), nullable=True)
    export_range_until = Column(DateTime(timezone=True), nullable=True)
    action = Column(Text, nullable=False)  # view | export (CHECK in migration)
    ip_address = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ─────────────────────── Multi-entity consolidation hierarchy ───────────────────────
# Plan 20260911-hierarchy-and-fanin (Wave 2), AC-1. MECHANISM ONLY: these tables can
# RECORD any hierarchy, and select none. EG-4 is open, so no topology, ownership
# threshold, or consolidation default is encoded here. A hierarchy with no rows does
# nothing, which is the correct behaviour while that gate is open.
#
# This is NOT a restoration of the retired Consolidation feature (dropped in
# f8b1d3e6a2c9). That surface was a flat list of realms under a `company_group`; this is
# a dated, ownership-bearing, role-classified node/edge model. The non-restoration guard
# at docs/product/company-onboarding.md:272 is respected in name and in shape.


class ConsolidationNode(Base):
    """One entity in a reporting hierarchy.

    GRAIN — deliberately org-keyed with NO realm column. A table carrying
    ``organization_id`` AND a realm column is automatically a guard table
    (``app.tenant_rls._guard_tables``), which would require every read to bind a single
    realm — and subtree resolution reads MANY nodes across MANY realms in one org-scoped
    query. So the link to a QBO company is ``organization_company_id``, an Integer FK, and
    the realm string is resolved by joining ``organization_companies`` inside an
    ``org_grain_read`` block (the same org->realm enumeration justified as exemption E-2).
    Adding a realm column here would silently break subtree resolution.

    ``structure_role`` is stored INDEPENDENTLY of the edges, and that separation is
    load-bearing: "an OPERATING node with zero inbound edges" is an orphan error, while
    "a FUND node with zero inbound edges" is correct. A pure edge list cannot distinguish
    those, and orphan detection is the check that protects statement completeness.

    ``reporting_entity_type`` is HUMAN-SET and never inferred from ``structure_role``. An
    investment company under ASC 946 does not line-by-line consolidate its operating
    investees, so the determination is an accounting-policy call per client, not a schema
    default. Under D2 (Parent-only v1) an ``INVESTMENT_COMPANY`` node may not root a
    consolidating subtree.
    """

    __tablename__ = "consolidation_nodes"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "organization_company_id",
            name="uq_consolidation_node_org_company",
        ),
        Index("ix_consolidation_nodes_org_role", "organization_id", "structure_role"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Nullable: a node need not be a QBO company. D3 (disclose the boundary) means v1
    # MEMBERS must have one, but the schema does not force it - a structural holdco can be
    # recorded and visibly excluded rather than being unrepresentable.
    organization_company_id = Column(
        # SET NULL, not RESTRICT and not CASCADE. Offboarding a member company must not be
        # BLOCKED - RESTRICT aborted run_delete_company with a ForeignKeyViolation after the
        # Intuit grant was already revoked, because the per-company sweep walks only
        # realm-keyed tables and this one is org-keyed - and it must not AMPUTATE the subtree,
        # which CASCADE would do, silently changing what every past consolidation covered.
        # Nulling the link leaves a structural node with no connected company: exactly the D3
        # state the design already handles, where a run refuses and NAMES the node rather than
        # quietly consolidating a hole. Found by an adversarial deploy review.
        Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"),
        nullable=True, index=True, unique=True,
    )
    structure_role = Column(String, nullable=False)          # FUND | HOLDING | OPERATING
    reporting_entity_type = Column(String, nullable=True)    # INVESTMENT_COMPANY | OPERATING_GROUP
    reporting_entity_basis = Column(Text, nullable=True)     # why, recorded by the human who set it
    display_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ConsolidationEdge(Base):
    """A dated, ownership-bearing parent->child relationship.

    OWNERSHIP IS AN EXACT RATIONAL, not a float. It compounds multiplicatively down a chain
    (fund -> platform -> add-on), and float error accumulates into the number that decides
    non-controlling interest. ``Numeric`` would also work; an explicit numerator/denominator
    pair additionally preserves what was actually agreed (7/8, not 0.875).

    ``method`` records the accounting treatment the client's policy owner chose; the schema
    offers the vocabulary and picks nothing. ``control_basis`` is required when the method
    is FULL so a consolidation decision always carries its stated reason.

    Effective dating is what makes a run reproducible: a mid-period acquisition or disposal
    closes one edge and opens another, and a historical run resolves the hierarchy as it
    stood, not as it stands now.
    """

    __tablename__ = "consolidation_edges"
    __table_args__ = (
        Index("ix_consolidation_edges_org_parent", "organization_id", "parent_node_id"),
        Index("ix_consolidation_edges_org_child", "organization_id", "child_node_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    parent_node_id = Column(
        Integer, ForeignKey("consolidation_nodes.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    child_node_id = Column(
        Integer, ForeignKey("consolidation_nodes.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    ownership_numerator = Column(Integer, nullable=False)
    ownership_denominator = Column(Integer, nullable=False)
    method = Column(String, nullable=False)                  # FULL | EQUITY | EXCLUDED
    control_basis = Column(Text, nullable=True)              # required when method == FULL
    effective_from = Column(Date, nullable=False, index=True)
    effective_to = Column(Date, nullable=True, index=True)
    supersedes_edge_id = Column(
        Integer, ForeignKey("consolidation_edges.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ─────────────────── Consolidation client-configuration registry ───────────────────
# Plan 20260911-client-config-spine, AC-1. ONE registry for every client-supplied policy
# the consolidator needs: the group taxonomy, intercompany designations, tolerances, the
# unmapped-account policy, the ASC 810-10-45-18 attribution election.
#
# WHY ONE AND NOT THREE. The decomposition proposed three parallel required-config
# mechanisms with three refusal paths. Three refusal paths are three places for a
# permissive fallback to reappear, and a fallback here is exactly the EG-4 breach the
# wave exists to avoid: a value the platform picked, standing in for one a client owns.
#
# EG-4 POSTURE (D5). This table is a CONTAINER. It ships with zero rows, has no default
# for any key, and a run refuses when a required key has no approved version. The payload
# is opaque JSON: the registry never interprets a client's values, so it selects none.


class ConsolidationConfigVersion(Base):
    """One approved version of one client-supplied configuration key.

    Append-only. A version is never updated in place; a change is a NEW version, and the
    prior one is marked superseded. A run pins EXACT INTEGER VERSIONS, never "current", so
    a later approval cannot retroactively alter what a completed run was computed against.

    SEGREGATION OF DUTIES (D7) — and the bootstrap problem it hides. Preparer must differ
    from approver. But if the escape from that rule were itself a configuration key, the
    FIRST version of that key could never be approved by a second person at a
    one-to-three-person firm: the rule would gate its own exemption.

    So the escape is recorded PER VERSION rather than as an org-level policy row: any
    version may be single-signed if it carries ``single_signed_reason``, and every artifact
    computed against it discloses that. This is a refinement of D7 discovered in
    implementation - it keeps both honest resolutions expressible, removes the bootstrap
    paradox, and makes the disclosure the control rather than a toggle somebody can set once
    and forget. Recorded in DECISIONS.md rather than changed silently.

    ``config_key`` is a free string validated in the service layer, never a DB CHECK - the
    same discipline D6 applies to topside categories, and the same one Wave 2 applied to
    ConsolidationEdge.method. The key SET is platform-determined (what the platform needs to
    know), but putting it in schema would make adding a key a migration.
    """

    __tablename__ = "consolidation_config_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "config_key", "version",
            name="uq_consolidation_config_version",
        ),
        Index("ix_consolidation_config_org_key", "organization_id", "config_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    config_key = Column(String, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    # The client's own values. Opaque here by design: the registry stores and approves, it
    # never interprets, so it cannot smuggle a default into a client's policy.
    payload = Column(JSON, nullable=False)
    notes = Column(Text, nullable=True)

    prepared_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    prepared_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # NULL until approved. An unapproved version can never be pinned by a run.
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    # Present only when preparer == approver. Forces disclosure on every artifact computed
    # against this version (D7).
    single_signed_reason = Column(Text, nullable=True)

    superseded_by_version = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GroupAccount(Base):
    """One line of a client's group reporting taxonomy.

    Plan 20260911-client-config-spine, AC-2. **SHIPS WITH ZERO ROWS.** There is no seed, no
    starter chart, no "minimal GAAP default" and no derive-from-member-charts anywhere: the
    node list is the archetypal EG-4 value, and a platform-supplied one would be exactly the
    "universal COA" the guard names.

    The CONTAINER is mechanism. A reporting line has a code, a label, a parent, a sort
    position, the statement it sits on, and the set of QBO classifications it will legally
    accept. That is the structure of any reporting taxonomy, not a selection of one.

    Rows hang off a ``consolidation_config_versions`` row, so the taxonomy uses the ONE
    approval lifecycle in AC-1 rather than a second parallel one - a version is proposed,
    validated at publish, second-signed, and pinned by a run as an integer.

    ``permitted_classifications`` is the load-bearing column: it is what lets Layer 0 reject
    a mapping into an incompatible line using QBO's own authoritative classification, rather
    than a name heuristic. The live-prod incident at ``app/lbo/coa_categorizer.py:1`` (a
    name-keyword matcher putting "Cost of Sales" into revenue, breaking a balance by $5.47M)
    is why that wall is type-driven and client-declared rather than inferred.
    """

    __tablename__ = "group_accounts"
    __table_args__ = (
        UniqueConstraint("config_version_id", "code", name="uq_group_account_code"),
        Index("ix_group_accounts_version", "config_version_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    config_version_id = Column(
        Integer, ForeignKey("consolidation_config_versions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    code = Column(String, nullable=False)
    label = Column(String, nullable=False)
    parent_code = Column(String, nullable=True)
    sort_position = Column(Integer, nullable=False, default=0)
    statement = Column(String, nullable=False)
    # JSON list of QBO classifications this line will legally accept.
    permitted_classifications = Column(JSON, nullable=False, default=list)
    # A residual / catch-all line, declared by the client. Never inferred from the label.
    is_residual = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EntityAccountGroupMapping(Base):
    """One entity's one account, mapped to one group taxonomy line, confirmed by a human.

    Plan 20260911-client-config-spine, AC-4.

    GRAIN — REALM-KEYED, and that is forced by authorization rather than convenience. A
    mapping is a fact about one realm's account, so it carries ``realm_id`` and is therefore
    a guard table: every read must bind the realm at the top level. That is correct and
    deliberate, the mirror of ``ConsolidationNode`` being org-keyed for the opposite reason.

    It ALSO carries ``organization_id``, which is NOT the RLS key but is required by
    ``MODEL_SCOPES``: a spec lacking either key is skipped by the tenant re-scope walker, so
    a realm-only registration is never repointed when a realm moves tenants. That is the live
    ``LboCoaMapping`` bug class (``app/services/tenant_data_scope.py``), and this table must
    not reproduce it.

    APPEND-ONLY. A confirmed mapping is never updated in place; a change is a new version and
    the prior row is superseded. ``LboCoaMapping`` is wholesale delete-and-reinserted
    (``app/routers/lbo/inputs.py:394``), which destroys the record of who decided what and
    when - the opposite of what a mapping needs to be auditable.

    ``match_type`` records HOW the mapping was arrived at (CARRIED from the entity's own
    prior confirmed decision, or MANUAL). There is no fuzzy, semantic or LLM layer in this
    wave: those change no output a human has not confirmed and only shrink the review queue,
    and their quality is the one thing that cannot be measured without a real client chart.
    """

    __tablename__ = "entity_account_group_mappings"
    __table_args__ = (
        UniqueConstraint(
            "realm_id", "qbo_account_id", "version",
            name="uq_entity_account_mapping_version",
        ),
        Index("ix_entity_account_mappings_realm_account", "realm_id", "qbo_account_id"),
        Index("ix_entity_account_mappings_org", "organization_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # Present for MODEL_SCOPES, NOT the RLS key. See the class docstring.
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    organization_company_id = Column(
        Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    realm_id = Column(String, nullable=False, index=True)
    qbo_account_id = Column(String, nullable=False, index=True)

    version = Column(Integer, nullable=False, default=1)
    superseded_by_version = Column(Integer, nullable=True)

    group_node_code = Column(String, nullable=True)
    # How this row came to exist: CARRIED | MANUAL. Validated in the service layer.
    match_type = Column(String, nullable=False)
    # A client-declared disposition for an account that maps to no line: the client's
    # unmapped_policy decides what happens, the platform does not.
    disposition = Column(String, nullable=True)

    # Human-declared, NEVER inferred from the account name. A name lexicon deciding what is
    # intercompany is a bookkeeping-convention judgement about a client's books.
    is_intercompany = Column(Boolean, nullable=False, default=False)
    ic_counterparty_organization_company_id = Column(
        Integer, ForeignKey("organization_companies.id", ondelete="SET NULL"), nullable=True,
    )

    confirmed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    confirmed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    manual_override_reason = Column(Text, nullable=True)

    # What QBO said about the account when the mapping was confirmed, so a later drift in the
    # source is visible rather than silently changing what a past run meant.
    source_classification_snapshot = Column(String, nullable=True)
    source_account_type_snapshot = Column(String, nullable=True)
    source_currency_snapshot = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class IcAccountDeclaration(Base):
    """A human declares: THIS realm's account faces THAT member entity.

    Plan 20260911-client-config-spine, AC-5. **Declaration only - there is no detection in
    this wave.** A name lexicon ("due to", "due from", "intercompany") deciding what is
    intercompany is a bookkeeping-convention judgement about a client's books, and it fails
    silently on a non-English chart while reporting a control RESULT of zero.

    WHY AN EMPTY TABLE IS THE EG-4-PERMITTED STATE: it designates nothing and presumes no
    group transacts at all. Second-order benefit nothing else in this wave has - running it
    PRODUCES the evidence that closes the gate constraining it, because the multi-entity
    discovery gate asks for exactly this list.

    ONE-SIDED BY CONSTRUCTION. The row is a fact about one realm's account: its own account,
    its own declared direction, and an identifier for the counterparty. It carries NO amount
    from the other side. A PAIR - this realm's balance beside that realm's - is neither group
    grain nor entity grain, so it is never persisted; pair arithmetic is recomputed at read
    time under two realm pins with the per-entity ladder re-run on each.
    """

    __tablename__ = "ic_account_declarations"
    __table_args__ = (
        UniqueConstraint("realm_id", "qbo_account_id", "version",
                         name="uq_ic_account_declaration_version"),
        Index("ix_ic_account_decl_realm", "realm_id", "qbo_account_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    qbo_account_id = Column(String, nullable=False, index=True)
    counterparty_organization_company_id = Column(
        # CASCADE, not RESTRICT: a declaration names a counterparty, and when that company is
        # offboarded the declaration asserts a relationship to an entity that no longer
        # exists. RESTRICT made the sibling company undeletable instead - run_delete_company
        # filters each realm-keyed table on the DEPARTING realm, so a declaration living in
        # ANOTHER realm was never swept, and the final delete of the company row raised
        # IntegrityError and rolled the whole offboarding back. Found by adversarial review.
        Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"),
        nullable=False, index=True)
    # RECEIVABLE | PAYABLE | REVENUE | EXPENSE — declared, never inferred. Service-validated.
    declared_direction = Column(String, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    superseded_by_version = Column(Integer, nullable=True)
    # What QBO said when declared, so a later disagreement is visible rather than silent.
    observed_classification = Column(String, nullable=True)
    # Set when the declaration disagrees with QBO's classification. A contra account (an
    # allowance against an IC receivable is an Asset carrying a credit) is legitimate, so
    # this WARNS and requires a reason - refusing would make contra accounts undeclarable.
    direction_disagreement_reason = Column(Text, nullable=True)
    declared_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    declared_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class IcPartyDeclaration(Base):
    """A human declares: THIS realm's QBO customer/vendor IS that member entity.

    The other half of AC-5, and the one that makes a leg attributable. Without it an
    intercompany balance is known to exist but not who it faces, because a QBO Customer named
    "Summit Manufacturing" is a string until somebody says which member it is.

    Also declaration-only. A fuzzy name match here is precisely the false-positive class that
    pairs an unrelated third-party vendor with a similarly-named sibling.
    """

    __tablename__ = "ic_party_declarations"
    __table_args__ = (
        UniqueConstraint("realm_id", "party_type", "qbo_party_id", "version",
                         name="uq_ic_party_declaration_version"),
        Index("ix_ic_party_decl_realm", "realm_id", "party_type", "qbo_party_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    party_type = Column(String, nullable=False)          # CUSTOMER | VENDOR
    qbo_party_id = Column(String, nullable=False, index=True)
    observed_party_name = Column(String, nullable=True)  # provenance, not a matching key
    counterparty_organization_company_id = Column(
        # CASCADE, not RESTRICT: a declaration names a counterparty, and when that company is
        # offboarded the declaration asserts a relationship to an entity that no longer
        # exists. RESTRICT made the sibling company undeletable instead - run_delete_company
        # filters each realm-keyed table on the DEPARTING realm, so a declaration living in
        # ANOTHER realm was never swept, and the final delete of the company row raised
        # IntegrityError and rolled the whole offboarding back. Found by adversarial review.
        Integer, ForeignKey("organization_companies.id", ondelete="CASCADE"),
        nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    superseded_by_version = Column(Integer, nullable=True)
    declared_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    declared_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TopsideEntry(Base):
    """A group-level adjustment: an elimination, a reclassification, a basis adjustment.

    Plan 20260911-client-config-spine, AC-6, under D6.

    NAMED FOR THE WIDER CATEGORY, not "eliminations". Without reclassifications there is no
    legal way to close a month in which any member wires money near a period end - the choice
    collapses to leaving a creditor-less intercompany liability in the statements or blocking
    every close. Purchase-accounting entries are also topside and also never written back, so
    "elimination" was never the right name for the surface.

    ``kind`` AND ``category`` ARE FREE STRINGS validated against the client's approved
    configuration version - never a DB CHECK enum. To write that CHECK you would have to
    enumerate the category set in schema (PURCHASE_ACCOUNTING_BASIS, TAX_EFFECT_OF_ELIMINATION,
    EQUITY_METHOD_PICKUP, UNREALIZED_PROFIT_IN_INVENTORY ...), and a closed set decomposing how
    consolidation adjustments are classified IS the "elimination logic" and "consolidation
    design" EG-4 names. Two clients disagreeing about whether a tax effect is its own category
    or a line inside the elimination would force a migration. Wave 2 kept
    ``ConsolidationEdge.method`` in a Python tuple for the same reason.

    NEVER WRITTEN BACK. There is deliberately no action-type column, no executor arm and no
    QBO client path anywhere near this table. A topside entry is the platform's record of a
    group-level adjustment; posting it into a member's books would make the platform a writer
    of the client's ledger, which is a different product with a different approval surface.
    """

    __tablename__ = "topside_entries"
    __table_args__ = (
        Index("ix_topside_entries_org_period", "organization_id", "period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    # The subtree this adjustment belongs to; placement within it is NOT decided in this wave.
    root_node_id = Column(
        Integer, ForeignKey("consolidation_nodes.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    period = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    category = Column(String, nullable=True)
    memo = Column(Text, nullable=True)
    # The exact config versions this entry was authored against.
    config_versions = Column(JSON, nullable=False, default=dict)

    prepared_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    prepared_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    single_signed_reason = Column(Text, nullable=True)
    # Nullable only for genuine legacy rows: new service drafts require explicit context.
    # No currency, scope or signature is inferred for historical entries.
    as_of = Column(Date, nullable=True)
    currency = Column(String(3), nullable=True)
    scope_manifest = Column(JSON, nullable=True)
    approval_format = Column(String(32), nullable=True)
    approved_content_hash = Column(String(64), nullable=True)
    # Voiding preserves the original signature/seal. Authorized capture checks current
    # content against that seal; this is not database-wide immutability or durable replay.
    voided_at = Column(DateTime(timezone=True), nullable=True)
    voided_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    void_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TopsideEntryLine(Base):
    """One group-grain debit/credit line of a topside entry.

    Group grain: it names a taxonomy line and an amount, never a member's rows. Attribution
    is a validated member reference only; the contribution runner does not apply it.
    """

    __tablename__ = "topside_entry_lines"
    __table_args__ = (
        Index("ix_topside_lines_entry", "topside_entry_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    topside_entry_id = Column(
        Integer, ForeignKey("topside_entries.id", ondelete="CASCADE"), nullable=False, index=True)
    group_node_code = Column(String, nullable=False)
    # Exact decimal. A consolidation adjustment that does not balance to the cent is not one.
    debit = Column(Numeric(precision=18, scale=2), nullable=False, default=0)
    credit = Column(Numeric(precision=18, scale=2), nullable=False, default=0)
    # WHICH MEMBER this line's effect belongs to. Omitting this column would silently elect
    # "entirely to the controlling interest" on every run, by construction - the one place
    # where NOT building the mechanism IS choosing the value, because ASC 810-10-45-18
    # permits either treatment and a schema with nowhere to record the choice has made it.
    #
    # THE COLUMN EXISTS; NOTHING CONSUMES IT YET. The attribution arithmetic that would read
    # it is deferred with the rest of NCI (plan section 8), and `attribution_election` is a
    # configuration key no run is currently required to pin. An earlier version of this
    # comment claimed the election was "a required, non-defaulted run field"; no such field
    # exists. Recorded rather than quietly corrected because the gap is the point: the
    # column is here so the election REMAINS open, not because it is already honoured.
    attributable_to_entity_node_id = Column(
        Integer, ForeignKey("consolidation_nodes.id", ondelete="RESTRICT"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ConsolidationRun(Base):
    """What one consolidation pass pinned, verdicted, and could not compute.

    Plan 20260911-client-config-spine, AC-7.

    GROUP GRAIN, AND IT DELIBERATELY HOLDS NO AMOUNT. The run records what was pinned and
    what the completeness verdict was; every figure lives at MEMBER grain in
    ``member_contributions``, realm-keyed. Storing a group total here would create a readable
    artifact at a grain no authorization key covers - and since a consolidated total is a
    function of its members, anyone who could read it could derive an unauthorized member by
    differencing. Keeping the total unstored makes D4's strict-coverage rule structural: the
    group figure is recomputed in pure Python from rows read under N separate realm pins, so
    it cannot be produced at all without authorization for every member.
    """

    __tablename__ = "consolidation_runs"
    __table_args__ = (
        Index("ix_consolidation_runs_org_period", "organization_id", "period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    root_node_id = Column(
        Integer, ForeignKey("consolidation_nodes.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    period = Column(String, nullable=False, index=True)
    as_of = Column(Date, nullable=False)
    # The fan-in manifest hash: which members, which edges, which source payloads.
    manifest_hash = Column(String, nullable=False)
    taxonomy_version = Column(Integer, nullable=False)
    # Exact integer config versions, never "current".
    config_versions = Column(JSON, nullable=False, default=dict)
    # False whenever any member could not be fetched or carries an unallocated amount.
    is_complete = Column(Boolean, nullable=False, default=False)
    # [[node_id, why_code], ...] - a named reason per incomplete member, never a bare count.
    incompleteness = Column(JSON, nullable=False, default=list)
    # D7: set when any pinned config version was approved by its own preparer.
    requires_single_signed_disclosure = Column(Boolean, nullable=False, default=False)
    prepared_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MemberContribution(Base):
    """One member's amount against one line of the client's taxonomy.

    REALM-KEYED, therefore a guard table: this is a fact derived from ONE member's ledger,
    and the grain of a stored artifact must equal the grain of its authorization key.
    Reading a group's worth of these takes N realm-scoped reads, which is the fan-in
    choreography rather than a limitation of it.

    NOT SCALED BY OWNERSHIP. Under full consolidation a member contributes 100% of its
    amounts and the non-controlling share is an ATTRIBUTION of the result, not a scaling of
    the input. Multiplying here would silently produce proportionate consolidation, which US
    GAAP permits only for undivided interests.
    """

    __tablename__ = "member_contributions"
    __table_args__ = (
        UniqueConstraint("consolidation_run_id", "realm_id", "group_node_code",
                         name="uq_member_contribution_line"),
        Index("ix_member_contrib_run_realm", "consolidation_run_id", "realm_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    consolidation_run_id = Column(
        Integer, ForeignKey("consolidation_runs.id", ondelete="CASCADE"),
        nullable=False, index=True)
    entity_node_id = Column(
        Integer, ForeignKey("consolidation_nodes.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    # A code from the CLIENT's published taxonomy, or the reserved UNALLOCATED destination.
    group_node_code = Column(String, nullable=False)
    # Debit-positive exact Decimal. Never a float: a consolidation that rounds is not one.
    amount = Column(Numeric(precision=18, scale=2), nullable=False)
    source_account_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RunUnmappedAccount(Base):
    """An account that reached no taxonomy line, named on the run that could not place it.

    The loud exception report, pinned to the run rather than recomputed. The failure it
    exists to prevent: an account silently absent from the roll-up while every footing
    control passes, because a total that omits a line still foots perfectly.

    Realm-keyed for the same reason as the contribution: it names one member's account.
    """

    __tablename__ = "run_unmapped_accounts"
    __table_args__ = (
        Index("ix_run_unmapped_run_realm", "consolidation_run_id", "realm_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    consolidation_run_id = Column(
        Integer, ForeignKey("consolidation_runs.id", ondelete="CASCADE"),
        nullable=False, index=True)
    entity_node_id = Column(
        Integer, ForeignKey("consolidation_nodes.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    realm_id = Column(String, nullable=False, index=True)
    qbo_account_id = Column(String, nullable=False)
    account_name = Column(String, nullable=True)
    classification = Column(String, nullable=True)
    why_code = Column(String, nullable=False)
    amount = Column(Numeric(precision=18, scale=2), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

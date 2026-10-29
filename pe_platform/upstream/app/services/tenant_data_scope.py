"""
app/services/tenant_data_scope.py
─────────────────────────────────
Single source of truth for which rows belong to one tenant (organization).

Strict tenant isolation is THE critical invariant of the data export / deletion
lifecycle. Every org-scoped model is enumerated here exactly once together with
the column(s) that bind a row to a tenant, so that export and deletion share an
identical scoping definition — there is no way for the two paths to drift and
let one tenant's rows leak into another tenant's export or survive its deletion.

Tenant ownership is expressed through three mechanisms in this schema:
  * ``organization_id``           — direct FK to organizations.id
  * ``organization_company_id``   — FK to organization_companies.id
  * ``realm_id`` / ``company_id`` / ``qbo_realm_id`` — the QBO realm string

A handful of child tables carry no tenant column of their own; they are reached
through their parent's primary key (``ModelScope.parent_links``).

A row belongs to a tenant when ANY of its tenant keys points at that tenant:
its ``organization_id`` is the org, its ``organization_company_id`` is one of the
org's companies, or its realm column is one of the org's realms. Several tables
carry a nullable ``organization_id`` that writers leave NULL when the realm has
no org mapping yet, so an org-only predicate would silently miss those rows in
both export and deletion.

Every mapped table that carries a tenant key is either registered in
``MODEL_SCOPES`` or listed with its reason in ``EXCLUDED_TABLES``; the export
manifest publishes that exclusion list, and the registry ratchet test fails on
any tenant-keyed table that is in neither.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from sqlalchemy import and_, false, or_
from sqlalchemy.orm import Session

from .. import models


# Tables that carry a tenant key but are deliberately NOT walked through the
# registry by export, deletion or realm re-scope. Each entry is a decision, not
# an omission; the export manifest lists every one the bundle does not contain,
# so a bundle never implies more coverage than it has.
EXCLUDED_TABLES: dict[str, str] = {
    "organizations": "Tenant root row; export serializes it and hard delete removes it explicitly.",
    "users": "Shared identity rows that can belong to several tenants; hard delete detaches them, never deletes them.",
    "tenant_data_exports": "Export lifecycle records; hard delete removes them and their stored bundles explicitly.",
    "tenant_deletion_requests": "Deletion lifecycle records; removed with the organization row.",
    "assent_records": "Legal proof-of-assent evidence (append-only, keyed on the Stytch org id); retained after termination.",
    "connection_events": "Legal connected-entity lifecycle evidence (append-only); retained after termination.",
    "org_legal_state": "Cache rebuilt from retained assent evidence; retained with it.",
    "legal_idempotency": "Idempotency records written with retained assent evidence; retained with it.",
    "legal_notice_log": "Legal change-notice evidence (append-only); retained after termination.",
    "legal_notice_delivery_event": "Delivery evidence for retained legal notices; retained with them.",
    "legal_assent_read_audit": "Audit of reads of retained legal evidence; retained with it.",
    "legal_documents": "Platform-wide legal document manifest; not tenant data.",
    "legal_document_versions": "Platform-wide legal document versions; not tenant data.",
    "debug_incidents": "Platform operations telemetry; not tenant data.",
    "debug_triage_runs": "Platform operations telemetry; not tenant data.",
    "debug_notification_deliveries": "Platform operations telemetry; not tenant data.",
    "debug_notification_subscriptions": "Platform operations telemetry; not tenant data.",
    "sentry_webhook_events": "Platform webhook ledger; not tenant data.",
    "stripe_webhook_events": "Platform webhook ledger; not tenant data.",
}


@dataclass(frozen=True)
class TenantScope:
    """Resolved identifiers that bind rows to one organization."""

    organization_id: int
    realm_ids: tuple[str, ...]
    organization_company_ids: tuple[int, ...]
    membership_ids: tuple[int, ...] = ()
    agent_run_ids: tuple[int, ...] = ()
    chat_conversation_ids: tuple[int, ...] = ()
    proposed_action_ids: tuple[int, ...] = ()
    fpa_model_ids: tuple[int, ...] = ()
    fpa_scenario_ids: tuple[int, ...] = ()
    lbo_scenario_ids: tuple[int, ...] = ()
    provisioning_request_ids: tuple[int, ...] = ()
    provisioning_run_ids: tuple[int, ...] = ()
    review_policy_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ModelScope:
    """How one org-scoped model is filtered down to a single tenant."""

    model: type
    # Column on the model that holds the tenant key, paired with the scope
    # attribute that supplies the allowed values. The first matching column is
    # used so a model with several scope columns is still filtered by exactly
    # one well-defined predicate.
    org_id_attr: Optional[str] = None
    org_company_attr: Optional[str] = None
    realm_attrs: tuple[str, ...] = ()
    # For child tables: (fk_attr_on_model, scope_attr_holding_parent_ids)
    parent_links: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # Neutral review rows require BOTH owners. They never enter a lifecycle
    # bypass query, even when the legacy export/deletion caller uses one.
    review_owned: bool = False


def _review_model_scopes() -> tuple[ModelScope, ...]:
    """Neutral review registry, in child-before-parent retention order.

    Each later review wave must register its own rows here before intake. W1
    intentionally refuses deletion of these rows; this is not a purge recipe.
    """
    return (
        ModelScope(
            models.AccountingSourceControlBinding, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceCoverageReceipt, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceHead, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceInvalidation, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceOutbox, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceEvent, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceSnapshot, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceRecord, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourcePage, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceRun, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceOAuthState, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingBook, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
        ModelScope(
            models.AccountingSourceConnection, org_id_attr="organization_id",
            org_company_attr="organization_company_id", review_owned=True,
        ),
    )


REVIEW_MODEL_SCOPES: tuple[ModelScope, ...] = _review_model_scopes()
REVIEW_RETENTION_STATE = "review_retention_lifecycle_required"


class ReviewRetentionLifecycleRequired(ValueError):
    """No destructive lifecycle action is safe until retention is implemented."""

    def __init__(self, reason: str, *, row_counts=None, object_prefixes=()):
        self.reason = reason
        self.row_counts = row_counts or {}
        self.object_prefixes = tuple(object_prefixes)
        # Do not expose a storage SDK/DB exception or connection configuration.
        super().__init__(f"{REVIEW_RETENTION_STATE}: {reason}")


def resolve_tenant_scope(db: Session, organization_id: int) -> TenantScope:
    """Resolve every identifier needed to scope queries to one organization.

    The realm set is the org's own companies plus its ``primary_realm_id`` unless
    that realm is mapped to ANOTHER org's company. Every parent id is gathered
    with the parent model's own registry predicate, so a child table is always
    walked over exactly the parent rows that export and deletion see.
    """
    company_rows = (
        db.query(models.OrganizationCompany.id, models.OrganizationCompany.realm_id)
        .filter(models.OrganizationCompany.organization_id == organization_id)
        .all()
    )
    organization_company_ids = tuple(cid for cid, _ in company_rows)
    realm_set = {realm for _, realm in company_rows if realm}

    org = db.query(models.Organization).filter(models.Organization.id == organization_id).first()
    if org and org.primary_realm_id and org.primary_realm_id not in realm_set:
        owner = (
            db.query(models.OrganizationCompany.organization_id)
            .filter(models.OrganizationCompany.realm_id == org.primary_realm_id)
            .first()
        )
        if owner is None:
            realm_set.add(org.primary_realm_id)
    realm_ids = tuple(sorted(realm_set))

    membership_ids = tuple(
        mid
        for (mid,) in db.query(models.OrganizationMembership.id)
        .filter(models.OrganizationMembership.organization_id == organization_id)
        .all()
    )

    scope = TenantScope(
        organization_id=organization_id,
        realm_ids=realm_ids,
        organization_company_ids=organization_company_ids,
        membership_ids=membership_ids,
    )
    scope = replace(
        scope,
        agent_run_ids=_owned_ids(db, models.AgentRun, scope),
        chat_conversation_ids=_owned_ids(db, models.ChatConversation, scope),
        proposed_action_ids=_owned_ids(db, models.ProposedAction, scope),
        fpa_model_ids=_owned_ids(db, models.FPAModel, scope),
        lbo_scenario_ids=_owned_ids(db, models.LboScenario, scope),
        provisioning_request_ids=_owned_ids(db, models.TenantProvisioningRequest, scope),
        review_policy_ids=_owned_ids(db, models.ReviewPolicy, scope),
    )
    # Second-level parents hang off ids resolved above.
    return replace(
        scope,
        fpa_scenario_ids=_owned_ids(db, models.FPAScenario, scope),
        provisioning_run_ids=_owned_ids(db, models.TenantProvisioningRun, scope),
    )


def _owned_ids(db: Session, model: type, scope: TenantScope) -> tuple[int, ...]:
    """Primary keys of ``model`` rows owned by ``scope``, via the model's own spec."""
    spec = _SPEC_BY_MODEL[model]
    return tuple(sorted(pk for (pk,) in db.query(model.id).filter(tenant_predicate(spec, scope)).all()))


def _model_scopes() -> list[ModelScope]:
    """Enumerate every tenant-owned model and its tenant-binding column(s).

    Order is intentionally LEAF → ROOT so deletion can walk it top-to-bottom in
    FK-safe order (children before parents). Export does not care about order.
    """
    m = models
    return [
        # ── Work graph, PBC binder, agent planning and memory ─────────────────
        # Leaves first: handoffs → work_items, PBC evidence → items → requests,
        # plan steps → plans, checkpoints → agent_runs, overrides → policies.
        # Every one carries BOTH keys so a realm move repoints its organization_id.
        ModelScope(m.PBCEvidenceLink, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.PBCItem, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.PBCRequest, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.PBCTemplate, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.Handoff, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.WorkItem, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.AgentEpisode, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.ToolFailureMemory, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.FinancialHypothesis, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.AgentRunCheckpoint, parent_links=(("agent_run_id", "agent_run_ids"),)),
        ModelScope(m.ReviewPolicyCheckOverride, parent_links=(("review_policy_id", "review_policy_ids"),)),
        ModelScope(m.ReviewPolicy, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("qbo_realm_id",)),
        ModelScope(m.AgentPlanStep, realm_attrs=("realm_id",)),
        ModelScope(m.AgentPlan, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.AgentPlanRunLock, realm_attrs=("realm_id",)),
        ModelScope(m.CloseRunLock, realm_attrs=("realm_id",)),
        # ── ASC 606 / ASC 842 schedules (rows → obligations → contracts) ──────
        ModelScope(m.RevenueRecognitionRow, realm_attrs=("realm_id",)),
        ModelScope(m.PerformanceObligation, realm_attrs=("realm_id",)),
        ModelScope(m.ScheduleAccountCoverage, realm_attrs=("realm_id",)),
        ModelScope(m.RevenueContract, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.LeaseScheduleRow, realm_attrs=("realm_id",)),
        ModelScope(m.LeaseSchedule, realm_attrs=("realm_id",)),
        # ── Per-company config, ledgers and snapshots ─────────────────────────
        ModelScope(m.BillPaymentExecution, realm_attrs=("company_id",)),
        ModelScope(m.LboSourcesUsesOverride, realm_attrs=("company_id",)),
        ModelScope(m.CompanyCeclConfig, realm_attrs=("company_id",)),
        ModelScope(m.CompanyEbitdaConfig, realm_attrs=("company_id",)),
        ModelScope(m.CompanyFinanceProfile, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("qbo_realm_id",)),
        ModelScope(m.MetricSnapshot, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.QBOAccountBalanceSnapshot, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.ARAPSummarySnapshot, org_id_attr="organization_id", realm_attrs=("company_id",)),
        # organization_id is NOT NULL with ON DELETE SET NULL: deleting the org row
        # while one of these survives aborts the whole hard delete.
        ModelScope(m.AICorrectionEvent, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("qbo_realm_id",)),
        ModelScope(m.FirmOperationsSnapshot, org_id_attr="organization_id"),
        ModelScope(m.AgentObjectiveOccurrence, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.BriefingDelivery, org_id_attr="organization_id", realm_attrs=("company_id",)),
        # ── Leaf / child tables reached through a parent primary key ──────────
        ModelScope(m.AgentStep, parent_links=(("agent_run_id", "agent_run_ids"),)),
        ModelScope(m.ChatMessage, parent_links=(("conversation_id", "chat_conversation_ids"),)),
        ModelScope(m.LboCell, parent_links=(("scenario_id", "lbo_scenario_ids"),)),
        ModelScope(m.LboPeriod, parent_links=(("scenario_id", "lbo_scenario_ids"),)),
        ModelScope(m.FPAAssumption, parent_links=(("scenario_id", "fpa_scenario_ids"),)),
        ModelScope(m.FPADebtTranche, parent_links=(("model_id", "fpa_model_ids"),)),
        ModelScope(m.FPAScenario, parent_links=(("model_id", "fpa_model_ids"),)),
        ModelScope(m.TenantProvisioningStep, parent_links=(("run_id", "provisioning_run_ids"),)),
        ModelScope(m.TenantProvisioningExternalResource, parent_links=(("request_id", "provisioning_request_ids"),)),
        ModelScope(m.TenantProvisioningNotification, parent_links=(("request_id", "provisioning_request_ids"),)),
        ModelScope(m.TenantProvisioningRun, parent_links=(("request_id", "provisioning_request_ids"),)),
        # C1-a bank-parser business relations are realm-owned and stay ahead of
        # every FK parent in this leaf-to-root registry.
        ModelScope(m.ReconciliationSessionStatementLine, realm_attrs=("realm_id",)),
        ModelScope(m.BankStatementParseCandidate, realm_attrs=("realm_id",)),
        ModelScope(m.BankStatementControlCertificate, realm_attrs=("realm_id",)),
        # C1-a reverses the durable edge to delivery → chunk and adds RESTRICT
        # lineage edges from sessions/feed rows to imports/chunks. Keep every
        # child ahead of its parent so tenant deletion remains FK-safe:
        # provider → session/feed → delivery → chunk → import.
        ModelScope(
            m.BankReconciliationSuggestionAttempt,
            org_id_attr="organization_id",
            realm_attrs=("qbo_realm_id",),
        ),
        ModelScope(
            m.BankStatementParseProviderAttempt,
            org_id_attr="organization_id",
            realm_attrs=("realm_id",),
        ),
        ModelScope(m.ReconciliationSession, realm_attrs=("realm_id",)),
        ModelScope(m.BankFeedItem, realm_attrs=("realm_id",)),
        ModelScope(
            m.BankStatementParseDeliveryAttempt,
            org_id_attr="organization_id",
            realm_attrs=("realm_id",),
        ),
        ModelScope(
            m.BankStatementParseChunk,
            org_id_attr="organization_id",
            realm_attrs=("realm_id",),
        ),
        ModelScope(m.BankStatementImport, realm_attrs=("realm_id",)),
        # ── Realm-keyed financial / module tables ─────────────────────────────
        ModelScope(m.AccountingSchedule, realm_attrs=("realm_id",)),
        ModelScope(m.AdjustingEntry, realm_attrs=("realm_id",)),
        ModelScope(m.AgentConfirmationToken, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.AgentObjective, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.AgentRun, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.ApprovalToken, realm_attrs=("company_id",)),
        ModelScope(m.AssistantContextCache, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.AuditEntry, realm_attrs=("company_id",)),
        ModelScope(m.AuditLog, realm_attrs=("realm_id",)),
        ModelScope(m.BankDescriptionRule, realm_attrs=("realm_id",)),
        ModelScope(m.BankReconAccountConfig, realm_attrs=("realm_id",)),
        ModelScope(m.BillPayment, realm_attrs=("company_id",)),
        ModelScope(m.BudgetAuditLog, realm_attrs=("company_id",)),
        ModelScope(m.BudgetDriver, realm_attrs=("company_id",)),
        ModelScope(m.BudgetLine, realm_attrs=("company_id",)),
        ModelScope(m.BudgetMonthLock, realm_attrs=("company_id",)),
        ModelScope(m.CapTableEntry, realm_attrs=("company_id",)),
        ModelScope(m.CashForecastActual, realm_attrs=("company_id",)),
        ModelScope(
            m.CashForecastExportSnapshot,
            org_id_attr="organization_id",
            realm_attrs=("company_id",),
        ),
        ModelScope(m.CashForecastOverride, realm_attrs=("company_id",)),
        ModelScope(m.CashForecastSnapshot, realm_attrs=("company_id",)),
        ModelScope(
            m.CashForecastSnapshotVersion,
            org_id_attr="organization_id",
            realm_attrs=("company_id",),
        ),
        ModelScope(m.CashForecastWeekStatus, realm_attrs=("company_id",)),
        ModelScope(m.ChatConversation, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.ClassificationPrediction, realm_attrs=("company_id",)),
        ModelScope(m.ClassificationRule, realm_attrs=("realm_id",)),
        # Wave 2 consolidation hierarchy. Org-keyed with NO realm column - deliberately,
        # so subtree resolution can read many nodes across many realms in one org-scoped
        # query (a realm column would make them guard tables). They are therefore scoped
        # by organization_id alone here, which is the whole of their tenant identity.
        # Registering them is what makes company deletion, tenant export and realm
        # re-scope see them at all; Wave 2 shipped without this and they were invisible
        # to all three.
        ModelScope(m.ConsolidationConfigVersion, org_id_attr="organization_id"),
        # BOTH keys: a realm-only spec is skipped by the re-scope walker and never
        # repointed when a realm moves tenants - the live LboCoaMapping defect.
        ModelScope(m.EntityAccountGroupMapping, org_id_attr="organization_id",
                   realm_attrs=("realm_id",)),
        # ORDER IS LOAD-BEARING FROM HERE TO ConsolidationNode. The deletion loop
        # (app/tasks/data_deletion.py) walks this registry emitting one bulk DELETE per
        # table in list order, and its contract is leaf -> root. Every table below holds an
        # ON DELETE RESTRICT foreign key into consolidation_nodes, which is non-deferrable
        # in Postgres: deleting the parent first raises ForeignKeyViolation, the whole
        # transaction rolls back, and the org becomes permanently undeletable. Wave 3
        # originally appended these five AFTER ConsolidationNode and shipped exactly
        # that defect.
        #
        # RESTRICT rather than CASCADE is deliberate: a run, a topside entry or a
        # contribution that silently lost the entity it refers to is a corrupted audit
        # record, so the database refuses. That makes the registry order the mechanism
        # that keeps deletion possible, and test_deletion_order_is_leaf_to_root derives
        # the invariant from the FK graph rather than re-checking these names.
        ModelScope(m.RunUnmappedAccount, org_id_attr="organization_id",
                   realm_attrs=("realm_id",)),
        ModelScope(m.MemberContribution, org_id_attr="organization_id",
                   realm_attrs=("realm_id",)),
        ModelScope(m.ConsolidationRun, org_id_attr="organization_id"),
        ModelScope(m.TopsideEntryLine, org_id_attr="organization_id"),
        ModelScope(m.TopsideEntry, org_id_attr="organization_id"),
        ModelScope(m.ConsolidationEdge, org_id_attr="organization_id"),
        ModelScope(m.ConsolidationNode, org_id_attr="organization_id"),
        ModelScope(m.GroupAccount, org_id_attr="organization_id"),
        # Wave 3 AC-5 declarations: realm-keyed facts about ONE realm's account or party,
        # registered with BOTH keys for the same reason as the mapping spine above.
        ModelScope(m.IcAccountDeclaration, org_id_attr="organization_id",
                   realm_attrs=("realm_id",)),
        ModelScope(m.IcPartyDeclaration, org_id_attr="organization_id",
                   realm_attrs=("realm_id",)),
        ModelScope(m.CloseTask, realm_attrs=("realm_id",)),
        ModelScope(m.CloseTemplate, realm_attrs=("realm_id",)),
        ModelScope(m.CompanyAgreement, realm_attrs=("company_id",)),
        ModelScope(m.CompanyKnowledge, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.CompanyMemory, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.CovenantDefinition, realm_attrs=("company_id",)),
        ModelScope(m.CovenantRecord, realm_attrs=("company_id",)),
        ModelScope(m.CustomKPI, realm_attrs=("company_id",)),
        ModelScope(m.DebtCovenant, realm_attrs=("company_id",)),
        ModelScope(m.Document, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.EntityBilling, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.ExecutiveBriefing, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.FPAModel, realm_attrs=("company_id",)),
        ModelScope(m.FPAWorkbenchSession, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.FinancialCalendar, realm_attrs=("realm_id",)),
        ModelScope(m.InsightAnomaly, realm_attrs=("company_id",)),
        ModelScope(m.InsightArtifact, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.InsightCashflow, realm_attrs=("company_id",)),
        ModelScope(m.InsightKPI, realm_attrs=("company_id",)),
        ModelScope(m.InsightTrend, realm_attrs=("company_id",)),
        ModelScope(m.LboCapexDriver, realm_attrs=("company_id",)),
        ModelScope(m.LboCoaMapping, realm_attrs=("company_id",)),
        ModelScope(m.LboCompany, realm_attrs=("company_id",)),
        ModelScope(m.LboDebtTranche, realm_attrs=("company_id",)),
        ModelScope(m.LboDriver, realm_attrs=("company_id",)),
        ModelScope(m.LboEmployee, realm_attrs=("company_id",)),
        # LboHistorical row REMOVED (C2-a G10, plan 20260717-three-statement-spine):
        # the table is dead (superseded by the LboPeriod axis) and its last readers
        # are gone; the class + table drop is C3-e's.
        ModelScope(m.LboPayrollDriver, realm_attrs=("company_id",)),
        ModelScope(m.LboRevenueStream, realm_attrs=("company_id",)),
        ModelScope(m.LboScenario, realm_attrs=("company_id",)),
        ModelScope(m.LLMUsageEvent, org_id_attr="organization_id", realm_attrs=("qbo_realm_id",)),
        ModelScope(m.MonthlyFinancials, realm_attrs=("realm_id",)),
        ModelScope(m.PaymentMethodBehavior, realm_attrs=("company_id",)),
        ModelScope(m.InternalActionRecoveryCase, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.InternalActionEffectReceipt, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.InternalActionExecution, org_id_attr="organization_id",
                   org_company_attr="organization_company_id", realm_attrs=("company_id",)),
        ModelScope(m.ProposedAction, realm_attrs=("company_id",)),
        ModelScope(m.QBOAccount, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.QBOActionExecution, org_id_attr="organization_id", realm_attrs=("company_id", "qbo_realm_id")),
        ModelScope(m.QBOCustomer, realm_attrs=("realm_id",)),
        ModelScope(m.QBOGLLine, realm_attrs=("realm_id",)),
        ModelScope(m.QBOItem, realm_attrs=("realm_id",)),
        ModelScope(m.QBOJournalEntry, realm_attrs=("realm_id",)),
        ModelScope(m.QBOOAuthState, org_id_attr="organization_id"),
        ModelScope(m.QBOReport, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.QBOSearchRecord, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.QBOTransaction, realm_attrs=("realm_id",)),
        ModelScope(m.QBOVendor, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        ModelScope(m.QBOWebhookEvent, realm_attrs=("realm_id",)),
        ModelScope(m.QBOCompanyInfo, realm_attrs=("realm_id",)),
        ModelScope(m.RealmContextArtifact, org_id_attr="organization_id", realm_attrs=("qbo_realm_id",)),
        ModelScope(m.RealmContextProfile, org_id_attr="organization_id", realm_attrs=("qbo_realm_id",)),
        ModelScope(m.ReconciliationMatch, realm_attrs=("realm_id",)),
        ModelScope(m.RecurrencePattern, realm_attrs=("company_id",)),
        ModelScope(m.Report, realm_attrs=("company_id",)),
        ModelScope(m.ReportSchedule, realm_attrs=("company_id",)),
        ModelScope(m.ReportSection, realm_attrs=("company_id",)),
        ModelScope(m.ScheduleCreditMapping, realm_attrs=("realm_id",)),
        ModelScope(m.ScheduleEntry, realm_attrs=("realm_id",)),
        ModelScope(m.SyncEvent, realm_attrs=("company_id",)),
        ModelScope(m.SystemAuditLog, realm_attrs=("company_id",)),
        ModelScope(m.SystemSettings, realm_attrs=("realm_id",)),
        ModelScope(m.TenantUsageEvent, org_id_attr="organization_id", realm_attrs=("company_id",)),
        ModelScope(m.TransactionFeature, realm_attrs=("company_id",)),
        ModelScope(m.VendorProfile, realm_attrs=("realm_id",)),
        ModelScope(m.WatchdogAlert, realm_attrs=("company_id",)),
        # ── Provisioning request root, billing, auth mirrors, tokens, org rows ─
        ModelScope(m.TenantProvisioningRequest, org_id_attr="organization_id"),
        ModelScope(m.TenantBilling, org_id_attr="organization_id"),
        ModelScope(m.AuthAuditEvent, org_id_attr="organization_id"),
        ModelScope(m.AuthSession, org_id_attr="organization_id"),
        ModelScope(m.QBOToken, org_id_attr="organization_id", realm_attrs=("realm_id",)),
        # Org-level credentials and settings. Export carries their metadata only:
        # the secret columns are in data_export.SENSITIVE_COLUMNS.
        ModelScope(m.ApiKey, org_id_attr="organization_id"),
        ModelScope(m.TenantLLMCredential, org_id_attr="organization_id"),
        ModelScope(m.TenantTaskAutonomy, org_id_attr="organization_id"),
        # Per-company grants: organization_id has no ON DELETE action, so a
        # surviving grant blocks the organization delete.
        ModelScope(m.UserCompanyAccess, org_id_attr="organization_id",
                   org_company_attr="organization_company_id"),
        ModelScope(m.OrganizationMembership, org_id_attr="organization_id"),
        ModelScope(m.OrganizationCompany, org_id_attr="organization_id"),
    ]


# Public, ordered (leaf → root) registry shared by export and deletion.
MODEL_SCOPES: list[ModelScope] = _model_scopes()
_SPEC_BY_MODEL: dict[type, ModelScope] = {spec.model: spec for spec in MODEL_SCOPES}


def realm_in(column, realm_ids) -> object:
    """``column IN (realm_ids)``, or a constant FALSE for an org with no realm.

    An empty realm set must never reach the driver as a placeholder value: an
    org without a QuickBooks company is a normal state, and psycopg2 refuses
    any string literal that contains a NUL byte.
    """
    realms = list(realm_ids)
    return column.in_(realms) if realms else false()


def tenant_predicate(spec: ModelScope, scope: TenantScope):
    """The WHERE clause selecting ``spec.model`` rows owned by ``scope``.

    Child tables match on their parent primary keys. Every other table matches
    when ANY of its registered tenant keys points at the tenant (see the module
    docstring). A spec with no key at all raises: that is the guardrail against
    exporting or deleting an unscoped, cross-tenant table.
    """
    model = spec.model
    if spec.parent_links:
        return and_(*(
            getattr(model, fk_attr).in_(list(getattr(scope, scope_attr)))
            if getattr(scope, scope_attr) else false()
            for fk_attr, scope_attr in spec.parent_links
        ))
    if not (spec.org_id_attr or spec.org_company_attr or spec.realm_attrs):
        raise ValueError(f"No tenant-scoping predicate defined for model {model.__name__}")

    clauses = []
    if spec.org_id_attr:
        clauses.append(getattr(model, spec.org_id_attr) == scope.organization_id)
    if spec.org_company_attr and scope.organization_company_ids:
        clauses.append(getattr(model, spec.org_company_attr).in_(list(scope.organization_company_ids)))
    if scope.realm_ids:
        clauses.extend(realm_in(getattr(model, attr), scope.realm_ids) for attr in spec.realm_attrs)
    if not clauses:
        return false()
    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def scoped_query(db: Session, spec: ModelScope, scope: TenantScope):
    """Return a query for ``spec.model`` filtered to exactly one tenant."""
    return db.query(spec.model).filter(tenant_predicate(spec, scope))

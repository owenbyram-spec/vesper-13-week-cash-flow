"""The four-tier SideEffectTier gate (plan 20260716-five-domain-remediation, AC-B1/B4).

Every ProposedAction ``action_type`` is classified into exactly ONE side-effect tier,
and the tier decides the approval channel:

- ``EXTERNAL_FINANCIAL`` (tier 1) — QBO/GL + customer-facing writes. UNCHANGED:
  proposal-first, dashboard human approval only; the ``external_side_effect`` kill
  switch (tool_policy.py) and every existing safety fixture stay byte-identical.
- ``INTERNAL_MATERIAL`` (tier 2) — destructive/high-blast-radius internal-DB writes.
  STILL draft a ProposedAction(PENDING); the fingerprint-bound confirmation-token
  machinery (Control-3) is an additional approval CHANNEL that flips PENDING→APPROVED
  in-chat (app/routers/actions.py) — NEVER a token-gated direct write.
- ``INTERNAL_ROUTINE`` (tier 3) — reversible internal-DB writes. May execute on draft
  through ``gate_decision`` (task_autonomy.py) only when the tenant has opted in
  (default is proposal-first) AND the drafting user holds the role the action's own
  HTTP route requires (``ROUTINE_ACTION_REQUIRED_ROLES``); closed-period guard
  honored, AuditEntry + InternalActionExecution ledger rows written;
  ``require_allowed`` + scope strip + RLS remain on the path.
- ``FINDING`` (tier 4) — review-only rows with NO executor (surfaced insight, not a
  write). They leave approval semantics: excluded from /actions/stats approval counts.

The map is FROZEN: the internal tiers are literal enumerations, and
``validate_action_tier_integrity`` (wired into the registry-build startup gate,
app/agent/runtime/registry_integrity.py — the ensure_registry_integrity mirror) FAILS
on any action type that is not classified. At runtime an UNKNOWN action_type defaults
to EXTERNAL_FINANCIAL — the most-approval-gated tier (fail-closed, never fail-open).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, TypedDict

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..models import ProposedAction


class SideEffectTier(str, Enum):
    EXTERNAL_FINANCIAL = "external_financial"
    INTERNAL_MATERIAL = "internal_material"
    INTERNAL_ROUTINE = "internal_routine"
    FINDING = "finding"


class RoutineAutoExecutionOutcome(TypedDict):
    """The tier-3 execute-on-draft result the chat lane reports honestly."""

    executed: bool | None
    execution_status: str
    action_id: int
    message: str
    autonomy_level: str | None
    policy_origin: str | None
    authority_actor: str


# ── Tier 2: INTERNAL_MATERIAL (frozen literal — AC-B1) ────────────────────────────────
# Every *_DELETE internal type (the C3-DELETES wave-3.3b eight PLUS the pre-existing
# SCHEDULE_ROW_DELETE — a delete is destructive regardless of which wave shipped it, so
# the 9th delete is deliberately classified MATERIAL, the conservative direction: more
# human approval, never auto-execution of a destroy), the ASC-842 lease lifecycle, the
# covenant/EBITDA breach-detection config, bulk budget overwrite, LBO assumptions,
# agent-plan activation, recon bulk-confirm, the permanent-memory write
# (MEMORY_UPDATE stays proposal-drafted — AWB-06 is structural with NO escape), and
# the writes that change amounts feeding auto-posted JEs, lease posting setup, or
# close/reconciliation/alert control evidence.
INTERNAL_MATERIAL_ACTION_TYPES = frozenset(
    {
        # destructive single-entity deletes
        "SCHEDULE_ROW_DELETE",
        "BUDGET_LINE_DELETE",
        "BUDGET_DRIVER_DELETE",
        "CLOSE_TASK_DELETE",
        "COVENANT_DELETE",
        "KPI_DELETE",
        "CASHFLOW_OVERRIDE_DELETE",
        "DOCUMENT_DELETE",
        "KNOWLEDGE_DELETE",
        # ASC-842 lease lifecycle (statement impact)
        "LEASE_ACTIVATE",
        "LEASE_REMEASURE",
        "LEASE_TERMINATE",
        # breach-detection / covenant configuration
        "COVENANT_UPDATE",
        "EBITDA_CONFIG_UPDATE",
        # bulk overwrite / model assumptions
        "BUDGET_APPLY_DRIVERS",
        "LBO_ASSUMPTIONS_UPDATE",
        # multi-step plan activation + bulk recon confirm
        "AGENT_PLAN",
        "RECON_BULK_CONFIRM",
        # permanent memory (AWB-06: proposal-drafted, structural, no escape)
        "MEMORY_UPDATE",
        # amount/life edits flow into auto-posted straight-line JEs
        "SCHEDULE_ROW_UPDATE",
        # lease accounts / automation policy drive the monthly lease JE beat
        "LEASE_UPDATE",
        # control evidence: alerts, watchdogs, reconciliation, close sign-off
        "ALERT_DISMISS",
        "WATCHDOG_ACK",
        "RECON_MATCH_CONFIRM",
        "CLOSE_TASK_COMPLETE",
    }
)

# ── Tier 3: INTERNAL_ROUTINE (frozen literal — AC-B1) ─────────────────────────────────
# Every remaining INTERNAL_DB_ACTION_TYPES entry: reversible creates/updates whose
# executors re-validate under the pinned realm. A NEW internal-DB type added without a
# tier assignment here (or in the MATERIAL set) FAILS the startup integrity check — it
# can never silently land in the auto-execute tier.
INTERNAL_ROUTINE_ACTION_TYPES = frozenset(
    {
        "BUDGET_MONTHS_UPDATE",
        "CLOSE_TASK_CREATE",
        "COVENANT_CREATE",
        "SCHEDULE_ROW_CREATE",
        "SCHEDULE_MONTH_VALUES_UPDATE",
        "BUDGET_DRIVER_BULK_UPDATE",
        "CASHFLOW_CELL_UPDATE",
        "CASHFLOW_SCHEDULE_LAYER",
        "CASHFLOW_ACTUALS_CATEGORIZATION",
        "CASHFLOW_SPREAD",
        "MACRO_SCENARIO_CREATE",
        "BUDGET_LINE_CREATE",
        "KPI_CREATE",
        "KPI_UPDATE",
        "CLOSE_TASK_STATE_UPDATE",
        "RECON_MATCH_UNCONFIRM",
        "CLOSE_TASKS_GENERATE",
        "BUDGET_REFORECAST_CREATE",
        "BUDGET_DRIVER_UPDATE",
        "LEASE_CREATE",
        "REVENUE_CONTRACT_CREATE",
        "RECON_SESSION_OPEN",
    }
)

# The minimum role each routine type's own HTTP route requires (the human path the
# auto lane stands in for). Auto-execution re-checks the DRAFTING user against this
# row: the lane may never apply a change its requester could not make directly.
# Every write route behind these types gates on EDITOR (budget.py, close_tasks.py,
# covenants.py, custom_kpis.py, cashflow_forecast.py, accounting_schedules.py,
# bank_reconciliation_sessions.py, leases.py, revenue_contracts.py). A routine type
# without a row fails the startup integrity check.
ROUTINE_ACTION_REQUIRED_ROLES: Mapping[str, str] = MappingProxyType(
    {
        "BUDGET_MONTHS_UPDATE": "EDITOR",
        "CLOSE_TASK_CREATE": "EDITOR",
        "COVENANT_CREATE": "EDITOR",
        "SCHEDULE_ROW_CREATE": "EDITOR",
        "SCHEDULE_MONTH_VALUES_UPDATE": "EDITOR",
        "BUDGET_DRIVER_BULK_UPDATE": "EDITOR",
        "CASHFLOW_CELL_UPDATE": "EDITOR",
        "CASHFLOW_SCHEDULE_LAYER": "EDITOR",
        "CASHFLOW_ACTUALS_CATEGORIZATION": "EDITOR",
        "CASHFLOW_SPREAD": "EDITOR",
        "MACRO_SCENARIO_CREATE": "EDITOR",
        "BUDGET_LINE_CREATE": "EDITOR",
        "KPI_CREATE": "EDITOR",
        "KPI_UPDATE": "EDITOR",
        "CLOSE_TASK_STATE_UPDATE": "EDITOR",
        "RECON_MATCH_UNCONFIRM": "EDITOR",
        "CLOSE_TASKS_GENERATE": "EDITOR",
        "BUDGET_REFORECAST_CREATE": "EDITOR",
        "BUDGET_DRIVER_UPDATE": "EDITOR",
        "LEASE_CREATE": "EDITOR",
        "REVENUE_CONTRACT_CREATE": "EDITOR",
        "RECON_SESSION_OPEN": "EDITOR",
    }
)


def actor_may_auto_execute(action_type: str | None, actor_role: str | None) -> bool:
    """True only when ``actor_role`` meets the routine type's required route role.

    Fail-closed: an unknown type, a missing/unknown role, or a type without a
    ``ROUTINE_ACTION_REQUIRED_ROLES`` row never qualifies.
    """
    from ..roles import role_weight

    required = ROUTINE_ACTION_REQUIRED_ROLES.get(action_type or "")
    if required is None or role_weight(actor_role) == 0:
        return False
    return role_weight(actor_role) >= role_weight(required)


# ── Tier 4: FINDING (frozen literal — AC-B1) ──────────────────────────────────────────
# The five review-only no-executor types: approving one records an acknowledgment
# (_mark_review_only_approved) and mutates nothing. They are surfaced insight, not
# writes, so they leave approval semantics (excluded from /actions/stats approval
# counts). Draft sites: app/tasks/proactive.py (REVIEW_CHECK :499, SCHEDULE_DETECTION
# :676, BUDGET_UPDATE :1065), app/assistant/handlers.py (BUDGET_UPDATE :677),
# app/agent/runtime/close_orchestration.py (CLOSE_STEP_REVIEW :712,
# CLOSE_RUN_BUDGET_EXCEEDED :871).
FINDING_ACTION_TYPES = frozenset(
    {
        "REVIEW_CHECK",
        "CLOSE_STEP_REVIEW",
        "CLOSE_RUN_BUDGET_EXCEEDED",
        "SCHEDULE_DETECTION",
        "BUDGET_UPDATE",
    }
)


def _external_financial_action_types() -> frozenset[str]:
    """Tier 1 — derived from the executor source sets (one source of truth, AC-B1).

    ``EXECUTABLE_ACTION_TYPES`` already unions the AR + AP sets; the explicit re-union
    (including ``EXTERNAL_SEND_ACTION_TYPES``, the truthful executor-side record that
    SEND_INVOICE_REMINDER has an outward side effect) keeps every named source visible
    and is idempotent. Derivation is the fail-closed direction here: a NEW executable
    type automatically lands in the MOST approval-gated tier.
    """
    from ..qbo_actions.ap_actions import AP_EXECUTABLE_ACTION_TYPES
    from ..qbo_actions.ar_actions import AR_EXECUTABLE_ACTION_TYPES, EXTERNAL_SEND_ACTION_TYPES
    from ..qbo_actions.executor import EXECUTABLE_ACTION_TYPES

    return frozenset(
        EXECUTABLE_ACTION_TYPES
        | AR_EXECUTABLE_ACTION_TYPES
        | AP_EXECUTABLE_ACTION_TYPES
        | EXTERNAL_SEND_ACTION_TYPES
    )


def action_tier_map() -> Mapping[str, SideEffectTier]:
    """The FROZEN action_type → SideEffectTier map (read-only view)."""
    mapping: dict[str, SideEffectTier] = {}
    for action_type in _external_financial_action_types():
        mapping[action_type] = SideEffectTier.EXTERNAL_FINANCIAL
    for action_type in INTERNAL_MATERIAL_ACTION_TYPES:
        mapping[action_type] = SideEffectTier.INTERNAL_MATERIAL
    for action_type in INTERNAL_ROUTINE_ACTION_TYPES:
        mapping[action_type] = SideEffectTier.INTERNAL_ROUTINE
    for action_type in FINDING_ACTION_TYPES:
        mapping[action_type] = SideEffectTier.FINDING
    return MappingProxyType(mapping)


def tier_for_action_type(action_type: str | None) -> SideEffectTier:
    """Resolve an action_type's tier; UNCLASSIFIED defaults EXTERNAL_FINANCIAL.

    Legacy aliases (RECLASSIFICATION / VENDOR_UPDATE) canonicalize first. The default
    is deliberately the most approval-gated tier: an unknown type (including the
    dynamic ``ASSISTANT_<TOOL>`` review-only drafts) can never auto-execute and never
    leaves the approval count — fail-closed, never fail-open.
    """
    from ..qbo_actions.executor import canonical_action_type

    canonical = canonical_action_type(action_type)
    return action_tier_map().get(canonical, SideEffectTier.EXTERNAL_FINANCIAL)


class ActionTierIntegrityError(RuntimeError):
    """The SideEffectTier map violates a structural invariant. Fail closed — never serve."""


def validate_action_tier_integrity() -> list[str]:
    """Return ALL tier-map structural violations (empty list == healthy map).

    Mirrors ``registry_integrity.validate_registry_integrity`` and is invoked from it,
    so every registry build (startup + CI) enforces:

    1. the four tier sets are pairwise disjoint;
    2. every INTERNAL_DB_ACTION_TYPES entry is classified (MATERIAL or ROUTINE) —
       an unclassified internal type FAILS startup;
    3. no phantom internal classification (a tier member that is not a real
       internal-DB type);
    4. FINDING types have NO executor in either lane (review-only by construction);
    5. external and internal executor sets do not overlap;
    6. every INTERNAL_ROUTINE type names a real required role, and no role row
       exists for a non-routine type.
    """
    from .internal_action_execution import INTERNAL_DB_ACTION_TYPES

    external = _external_financial_action_types()
    violations: list[str] = []

    tier_sets = {
        "EXTERNAL_FINANCIAL": external,
        "INTERNAL_MATERIAL": INTERNAL_MATERIAL_ACTION_TYPES,
        "INTERNAL_ROUTINE": INTERNAL_ROUTINE_ACTION_TYPES,
        "FINDING": FINDING_ACTION_TYPES,
    }
    labels = list(tier_sets)
    for i, first in enumerate(labels):
        for second in labels[i + 1 :]:
            overlap = tier_sets[first] & tier_sets[second]
            for action_type in sorted(overlap):
                violations.append(
                    f"action_type '{action_type}' is classified in BOTH {first} and {second}"
                )

    internal_classified = INTERNAL_MATERIAL_ACTION_TYPES | INTERNAL_ROUTINE_ACTION_TYPES
    for action_type in sorted(INTERNAL_DB_ACTION_TYPES - internal_classified):
        violations.append(
            f"unclassified internal action_type '{action_type}': every "
            "INTERNAL_DB_ACTION_TYPES entry must be INTERNAL_MATERIAL or "
            "INTERNAL_ROUTINE in app/services/action_tiers.py (an unclassified "
            "internal write must never silently gain or lose an approval gate)"
        )
    for action_type in sorted(internal_classified - INTERNAL_DB_ACTION_TYPES):
        violations.append(
            f"phantom internal tier classification '{action_type}': named in an "
            "internal tier set but not an INTERNAL_DB_ACTION_TYPES entry"
        )

    for action_type in sorted(FINDING_ACTION_TYPES & (external | INTERNAL_DB_ACTION_TYPES)):
        violations.append(
            f"FINDING action_type '{action_type}' has an executor — findings are "
            "review-only by construction and may never execute"
        )
    for action_type in sorted(external & INTERNAL_DB_ACTION_TYPES):
        violations.append(
            f"action_type '{action_type}' is in BOTH the external executor set and "
            "INTERNAL_DB_ACTION_TYPES — one action type, one executor lane"
        )

    from ..roles import ROLE_WEIGHTS

    role_rows = set(ROUTINE_ACTION_REQUIRED_ROLES)
    for action_type in sorted(INTERNAL_ROUTINE_ACTION_TYPES - role_rows):
        violations.append(
            f"routine action_type '{action_type}' has no ROUTINE_ACTION_REQUIRED_ROLES "
            "row — auto-execution cannot re-check the drafting user's role"
        )
    for action_type in sorted(role_rows - INTERNAL_ROUTINE_ACTION_TYPES):
        violations.append(
            f"ROUTINE_ACTION_REQUIRED_ROLES names '{action_type}', which is not an "
            "INTERNAL_ROUTINE action type"
        )
    for action_type, role in sorted(ROUTINE_ACTION_REQUIRED_ROLES.items()):
        if str(role).upper() not in ROLE_WEIGHTS:
            violations.append(
                f"routine action_type '{action_type}' requires unknown role '{role}'"
            )
    return violations


def ensure_action_tier_integrity() -> None:
    """Raise :class:`ActionTierIntegrityError` unless the tier map is structurally sound."""
    violations = validate_action_tier_integrity()
    if violations:
        detail = "\n  - ".join(violations)
        raise ActionTierIntegrityError(
            f"SideEffectTier map failed integrity validation ({len(violations)} violation(s)):\n"
            f"  - {detail}\n"
            "Fix the tier sets in app/services/action_tiers.py — do not ship a tier map "
            "whose approval semantics are untrue."
        )


# ── Tier 3: the audited auto-execute lane (AC-B4) ─────────────────────────────────────

AUTONOMY_INTERNAL_ROUTINE_REVIEWER = "autonomy:internal_routine"

# A retried chat turn drafts a FRESH proposal (the PENDING-dedup collapse deliberately
# ignores disposed rows), so without this guard a retry would auto-execute the same
# non-idempotent internal write twice (different action ids → different ledger keys).
# An EXECUTED sibling with the same inputs_fingerprint inside the dedup window means
# "this exact change just ran" — the new draft stays PENDING for a human instead.
_RECENT_EXECUTION_GUARD_HOURS = 24


def _recently_executed_duplicate(db: "Session", action: "ProposedAction") -> bool:
    from ..models import ProposedAction as ProposedActionModel

    if not action.inputs_fingerprint:
        return False
    window_start = datetime.utcnow() - timedelta(hours=_RECENT_EXECUTION_GUARD_HOURS)
    return (
        db.query(ProposedActionModel.id)
        .filter(
            ProposedActionModel.company_id == action.company_id,
            ProposedActionModel.action_type == action.action_type,
            ProposedActionModel.inputs_fingerprint == action.inputs_fingerprint,
            ProposedActionModel.id != action.id,
            ProposedActionModel.status == "EXECUTED",
            ProposedActionModel.created_at >= window_start,
        )
        .first()
        is not None
    )


def maybe_auto_execute_routine_action(
    db: "Session", action: "ProposedAction", *, actor_email: str, actor_role: str | None
) -> RoutineAutoExecutionOutcome | None:
    """Tier-3 execute-on-draft: run one INTERNAL_ROUTINE draft through the gate.

    Returns ``None`` when the draft stays PENDING (proposal-first fallback: wrong tier,
    the drafting user (``actor_role``) lacks the action's route role, routine autonomy
    not enabled for the tenant, closed period, unresolvable organization, expired, a
    lost claim race, or a recently-executed duplicate) — the caller's existing
    proposal-first copy is then the truth. Returns a :class:`RoutineAutoExecutionOutcome`
    when the gate passed and the action was claimed + executed (honestly EXECUTED or
    FAILED).

    AWB-01 is satisfied STRUCTURALLY: this runs only after ``require_allowed`` + the
    scope strip admitted the drafting tool call; the claim flips PENDING→APPROVED via
    the atomic transition (Control-2 — the executor side never sees a PENDING row);
    ``execute_internal_db_action_core`` pins org+realm RLS scope, claims the
    InternalActionExecution idempotency ledger row, honors the closed-period/suspension
    gates, and the ``post_execute`` callback writes the AUTO_EXECUTE_ACTION AuditEntry
    inside the same per-action transaction.
    """
    from fastapi import HTTPException
    from ..services.proposal_provenance import validate_autonomous_approval
    from ..models import AuditEntry
    from ..qbo_actions.transitions import action_is_expired, claim_action_transition
    from ..services.internal_action_execution import (
        INTERNAL_DB_ACTION_TYPES,
        execute_internal_db_action_core,
        InternalExecutionOutcome, REVIEW_MESSAGE,
    )
    from ..services.task_autonomy import INTERNAL_ROUTINE_TASK_TYPE, gate_decision
    from ..services.tenant_companies import organization_for_company

    if tier_for_action_type(action.action_type) is not SideEffectTier.INTERNAL_ROUTINE:
        return None
    if action.action_type not in INTERNAL_DB_ACTION_TYPES:
        return None  # defensive: routine tier is internal-DB by integrity check
    if action.status != "PENDING" or action_is_expired(action):
        return None
    if action.plan_step_id is not None:
        return None
    if not actor_may_auto_execute(action.action_type, actor_role):
        return None  # the requester could not make this change directly → human approval
    try:
        validate_autonomous_approval(action)
    except HTTPException:
        return None
    expected_revision = action.revision
    organization = organization_for_company(db, action.company_id)
    if organization is None:
        return None  # cannot resolve tenant scope → never auto-execute
    if _recently_executed_duplicate(db, action):
        return None

    decision = gate_decision(
        db,
        organization_id=organization.id,
        task_type=INTERNAL_ROUTINE_TASK_TYPE,
        realm_id=action.company_id,
        raw_confidence=action.confidence_score,
    )
    if not decision.get("auto"):
        return None  # not enabled / closed period → unchanged proposal-first path

    # Atomic PENDING→APPROVED claim (F-07): a concurrent human decision wins; this
    # lane must never execute an already-decided action.
    try:
        if not claim_action_transition(
            db,
            action_id=action.id,
            from_status="PENDING",
            to_status="APPROVED",
            reviewed_by=AUTONOMY_INTERNAL_ROUTINE_REVIEWER,
            expected_revision=expected_revision,
            autonomous=True,
        ):
            db.rollback()
            return None
    except HTTPException:
        db.rollback()
        return None
    db.commit()

    def _audit_auto_execution(txn_db: "Session", succeeded: bool, message: str) -> None:
        txn_db.add(
            AuditEntry(
                company_id=action.company_id,
                user_email=actor_email,
                action="AUTO_EXECUTE_ACTION",
                entity_type="proposed_action",
                entity_id=str(action.id),
                details={
                    "action_type": action.action_type,
                    "tier": SideEffectTier.INTERNAL_ROUTINE.value,
                    "source": "autonomy",
                    "autonomy_level": decision.get("level"),
                    "executed": succeeded,
                },
            )
        )

    try:
        outcome = execute_internal_db_action_core(
            db,
            action,
            actor_email=actor_email,
            failure_audit_source="autonomy",
            post_execute=_audit_auto_execution,
        )
        succeeded, message = outcome
    except Exception:
        db.rollback()
        logger.warning("Routine internal execution outcome unavailable for action %s", action.id)
        outcome = InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
        succeeded, message = outcome

    return RoutineAutoExecutionOutcome(
        executed=outcome.executed if isinstance(outcome, InternalExecutionOutcome) else succeeded,
        execution_status=outcome.execution_status if isinstance(outcome, InternalExecutionOutcome) else ("succeeded" if succeeded else "not_applied"),
        action_id=action.id,
        message=str(message),
        autonomy_level=decision.get("level"),
        policy_origin=decision.get("origin"),
        authority_actor=AUTONOMY_INTERNAL_ROUTINE_REVIEWER,
    )

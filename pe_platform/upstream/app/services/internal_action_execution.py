# PLAN-EXEMPT: dispatched one-time remediation (Steven dispatch 2026-07-06) — stuck
# APPROVED-not-EXECUTED internal-DB ProposedActions (the #257 class). AC-1 extracts the
# router's execution core into this request-agnostic module so the remediation script and
# the live approve endpoint share ONE execution path and can never drift.
"""Shared internal execution admission and conservative recovery boundary.

The exact persisted intent holds a durable owner claim before handler entry. Domain
handlers retain their own transactions; a post-entry failure therefore requires
review. Ledger, action projection, completion audit and hold release commit together.
"""
from __future__ import annotations

# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R5 — execution-ledger imports
import hashlib
import logging
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.orm import Session as DurableSession
from .internal_action_intent import action_intent_fingerprint, approval_fingerprint, canonical_json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from ..models import AuditEntry, InternalActionExecution, ProposedAction
from ..tenant_rls import tenant_scope_for_realm
from .redaction import redact_to_string
from .tenant_companies import organization_company_for_realm, organization_for_company

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The full internal-DB proposal class: action_type -> assistant tool_name. This is the
# exact inverse of the proposer's tool->action_type map in
# app/agent/runtime/tool_executor.py::_propose_tool_action — keep the two in lockstep.
ACTION_TYPE_TO_TOOL = {
    "BUDGET_MONTHS_UPDATE": "set_budget_months",
    "CLOSE_TASK_CREATE": "create_close_task",
    "COVENANT_CREATE": "add_covenant",
    "SCHEDULE_ROW_CREATE": "add_schedule_row",
    "SCHEDULE_ROW_UPDATE": "update_schedule_row",
    "SCHEDULE_MONTH_VALUES_UPDATE": "update_schedule_month_values",
    "BUDGET_DRIVER_BULK_UPDATE": "bulk_set_budget_drivers",
    "CLOSE_TASK_COMPLETE": "complete_close_task",
    "SCHEDULE_ROW_DELETE": "delete_schedule_row",
    "CASHFLOW_CELL_UPDATE": "update_cashflow_cells",
    "CASHFLOW_SCHEDULE_LAYER": "layer_cashflow_schedule",
    "CASHFLOW_ACTUALS_CATEGORIZATION": "categorize_cashflow_actuals",
    "CASHFLOW_SPREAD": "spread_cashflow_balance",
    "MACRO_SCENARIO_CREATE": "create_macro_scenario",
    # C3-TOOLS wave 3.1 (plan 20260706-cfo-grade-financial-workflows, gap-register rows 4-13,
    # lower-risk batch): the 8 new proposal-gated internal-DB writes. Approval resolves each to
    # its TOOL_HANDLERS handler via this map; the proposer's inverse
    # (_INTERNAL_DB_ACTION_TYPE_BY_TOOL, tool_executor.py) is DERIVED from this dict — one source
    # of truth, so the two stay in lockstep by construction.
    "BUDGET_LINE_CREATE": "create_budget_line",             # row 4
    "KPI_CREATE": "create_custom_kpi",                      # row 5a
    "KPI_UPDATE": "update_custom_kpi",                      # row 5b
    "CLOSE_TASK_STATE_UPDATE": "update_close_task_state",   # row 6
    "ALERT_DISMISS": "dismiss_alert",                       # row 10a
    "WATCHDOG_ACK": "acknowledge_watchdog",                 # row 10b
    "RECON_MATCH_CONFIRM": "recon_confirm_match",           # row 12a
    "RECON_MATCH_UNCONFIRM": "recon_unconfirm",             # row 12b
    # C3-TOOLS wave 3.1c (plan 20260706-cfo-grade-financial-workflows, gap-register rows 7,8,9,11,13,
    # the extracted-service batch): 5 more proposal-gated internal-DB writes. Approval resolves each
    # to its TOOL_HANDLERS handler (which calls the frozen service) via this map; the proposer's
    # inverse (_INTERNAL_DB_ACTION_TYPE_BY_TOOL, tool_executor.py) is DERIVED from this dict.
    "CLOSE_TASKS_GENERATE": "generate_close_tasks",         # row 7
    "BUDGET_REFORECAST_CREATE": "create_reforecast",        # row 8
    "BUDGET_DRIVER_UPDATE": "update_budget_driver",         # row 9
    "LEASE_UPDATE": "update_lease",                         # row 11
    # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker
    # RATIFICATION-2026-07-14.md). TG-5 create half — LEASE_CREATE resolves
    # _handle_create_lease → create_lease_service (SIMPLE single-name internal-DB
    # write; the proposer's inverse _INTERNAL_DB_ACTION_TYPE_BY_TOOL is DERIVED from
    # this dict). Auto-executes at approval (INTERNAL_DB_EXECUTABLE = the full set); no
    # actions.py gate entry is added.
    "LEASE_CREATE": "create_lease",
    # PLAN-EXEMPT: chunk 5 of ratified plan 20260714-wave3-2-asc606-engine (marker
    # RATIFICATION-2026-07-14.md). ASC 606 create tool — REVENUE_CONTRACT_CREATE resolves
    # _handle_create_revenue_contract → create_revenue_contract_service (SIMPLE single-name
    # internal-DB write; the proposer's inverse _INTERNAL_DB_ACTION_TYPE_BY_TOOL is DERIVED
    # from this dict). Auto-executes at approval (INTERNAL_DB_EXECUTABLE = the full set); no
    # actions.py gate entry is added.
    "REVENUE_CONTRACT_CREATE": "create_revenue_contract",
    "BUDGET_APPLY_DRIVERS": "apply_budget_drivers",         # row 13
    # C3-TOOLS wave 3.2b (plan 20260706-cfo-grade-financial-workflows, gap-register rows 14-16, the
    # HIGH-risk batch: breach-detection / ASC-842 statement impact). Each drafts a PENDING
    # ProposedAction from chat (NOTHING executes); approval resolves each to its TOOL_HANDLERS
    # handler (which resolves the entity by id under realm scope + calls the frozen service, which
    # MUTATES + commits at that point) via this map. The proposer's inverse
    # (_INTERNAL_DB_ACTION_TYPE_BY_TOOL, tool_executor.py) is DERIVED from this dict — one source of
    # truth, so the two stay in lockstep by construction. dispose_schedule (row 17) is NOT here — it
    # is a HANDLER-lane tool whose own handler self-drafts a JE proposal (like create_journal_entry).
    "COVENANT_UPDATE": "update_covenant",                   # row 14
    "EBITDA_CONFIG_UPDATE": "update_ebitda_config",         # row 15
    "LEASE_ACTIVATE": "activate_lease",                     # row 16a
    "LEASE_REMEASURE": "remeasure_lease",                   # row 16b
    "LEASE_TERMINATE": "terminate_lease",                   # row 16c
    # wave 3.3a (plan 20260706-cfo-grade-financial-workflows, completing AC-2A.2): the LBO
    # assumption-change proposal write. Approval resolves it to _handle_propose_lbo_assumptions
    # (TOOL_HANDLERS), which calls the shared write core apply_lbo_assumptions (app/lbo/
    # assumption_write.py) — the SAME path the live PUT /drivers save runs. The proposer's inverse
    # (_INTERNAL_DB_ACTION_TYPE_BY_TOOL, tool_executor.py) is DERIVED from this dict — one source
    # of truth, so the two stay in lockstep by construction.
    "LBO_ASSUMPTIONS_UPDATE": "propose_lbo_assumptions",
    # C3-DELETES wave 3.3b (plan 20260706-cfo-grade, gap-register row 18): the 8 DESTRUCTIVE
    # single-entity delete action types, shipped LAST per the matrix risk-ordering. Approval
    # resolves each to its TOOL_HANDLERS handler via this map; the handler re-checks existence +
    # tenant ownership of the ONE named entity under the pinned realm BEFORE deleting (404/skip if
    # gone or not owned). Single-id only — NEVER bulk. The proposer's inverse
    # (_INTERNAL_DB_ACTION_TYPE_BY_TOOL, tool_executor.py) is DERIVED from this dict — one source of
    # truth, so the two stay in lockstep by construction. SCHEDULE_ROW_DELETE already exists above
    # for delete_schedule_row (not duplicated here).
    "BUDGET_LINE_DELETE": "delete_budget_line",
    "BUDGET_DRIVER_DELETE": "delete_budget_driver",
    "CLOSE_TASK_DELETE": "delete_close_task",
    "COVENANT_DELETE": "delete_covenant",
    "KPI_DELETE": "delete_custom_kpi",
    "CASHFLOW_OVERRIDE_DELETE": "delete_cashflow_override",
    "DOCUMENT_DELETE": "delete_document",
    "KNOWLEDGE_DELETE": "delete_knowledge",
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
    # AC-4/decision-4: MEMORY_UPDATE approvals EXECUTE. teach_vesper_bot is intercepted at chat
    # time into a MEMORY_UPDATE PENDING proposal (_propose_memory_update, tool_executor.py);
    # before this entry the type was in NEITHER executor map, so approval fell through to the
    # review-only no-op (actions.py:954-955) and the chat teach loop never wrote a memory.
    # Approval now resolves _handle_teach_vesper_bot → agent_memory.remember (the approver is
    # recorded as accepted_by via the action_tool_input normalization below).
    "MEMORY_UPDATE": "teach_vesper_bot",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.3 (TG-4/WF-4, decision 3
    # TWO-NAME architecture — the E1 crit-eval fix): these rows resolve the HIDDEN
    # execution-only tools. The VISIBLE chat tools (open_recon_session /
    # propose_bulk_matches) are HANDLER_LANE_SELF_DRAFTERS whose handlers draft at
    # CHAT time with payload["tool_name"] = these exec names (a mismatch fails loud
    # in resolve_internal_db_tool_name below); registry-integrity check #6
    # (a self-drafter must not own an executor row) is satisfied by the split.
    # The executors re-check the open guards (E4) / re-validate per-pair liveness
    # (E5 steal-guard) at approval — they NEVER re-run the suggest engine.
    "RECON_SESSION_OPEN": "recon_session_open_exec",
    "RECON_BULK_CONFIRM": "recon_bulk_confirm_exec",
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 3, E1 — the two-name split): this row
    # resolves the HIDDEN execution-only agent_plan_activate_exec. The VISIBLE chat tool
    # (propose_agent_plan) is a HANDLER_LANE_SELF_DRAFTER whose handler drafts at CHAT
    # time with payload["tool_name"] = this exec name (a mismatch fails loud in
    # resolve_internal_db_tool_name); registry-integrity check #6 (a self-drafter owns no
    # executor row) is satisfied by the split. The executor re-hashes the persisted step
    # rows against AgentPlan.steps_hash and REFUSES on tamper (E3), creates the holder
    # objective, claim-flips the plan ACTIVE, and enqueues run_agent_plan_walk. The
    # proposer's inverse (_INTERNAL_DB_ACTION_TYPE_BY_TOOL, tool_executor.py) is DERIVED
    # from this dict — one source of truth, so the two stay in lockstep by construction.
    "AGENT_PLAN": "agent_plan_activate_exec",
}

INTERNAL_DB_ACTION_TYPES = frozenset(ACTION_TYPE_TO_TOOL)


class InternalActionNotWiredError(Exception):
    """The action has no unambiguous internal-DB handler — a wiring/payload defect.

    Raised BEFORE any tenant data is touched; callers fail loud (the router maps it to
    the ``internal_action_not_wired`` 500 envelope; the script reports the action under
    ``needs_manual_review``) rather than silently no-op'ing.
    """

    def __init__(self, action_type: str, reason: str) -> None:
        self.action_type = action_type
        self.reason = reason
        super().__init__(f"{action_type}: {reason}")


def action_tool_input(action: ProposedAction) -> dict:
    """The proposer-recorded handler input (``payload["tool_input"]``), or ``{}``.

    MEMORY_UPDATE is the one internal-DB type whose proposer
    (``ToolExecutor._propose_memory_update``) records the handler params at the payload
    TOP LEVEL (``category``/``content``/``source``/``source_metadata``) rather than under
    ``tool_input`` — normalize it to the ``_handle_teach_vesper_bot`` param shape here so
    the shared execution core stays payload-shape-agnostic. ``_approved_by`` carries the
    approver (``reviewed_by`` is stamped by the atomic PENDING→APPROVED claim BEFORE the
    executor runs) so the executed memory records who accepted it.
    """
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
    payload = action.payload if isinstance(action.payload, dict) else {}
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        # Never forward a JSON/model-supplied trusted-plan marker. Only this
        # server-side boundary may construct the immutable approval wrapper.
        params = {k: v for k, v in tool_input.items() if k not in {"_reviewed_budget_plan", "budget_calculation_plan"}}
        if action.action_type == "BUDGET_APPLY_DRIVERS":
            from .budget_reforecast import ReviewedBudgetPlan
            stored_plan = payload.get("budget_calculation_plan")
            params["_reviewed_budget_plan"] = (
                ReviewedBudgetPlan.from_payload(stored_plan) if isinstance(stored_plan, dict) else None
            )
        return params
    if action.action_type == "MEMORY_UPDATE":
        return {
            "fact": payload.get("content"),
            "category": payload.get("category"),
            "source": payload.get("source"),
            "source_metadata": payload.get("source_metadata"),
            "_approved_by": action.reviewed_by,
        }
    return {}


# The subset of internal-DB action types that write to a specific accounting
# period, mapped to the tool_input key(s) that carry an explicit ``YYYY-MM``
# period. These are schedule/budget EDITS — the exact class the HARD-lock edit
# guard (task_autonomy.period_is_hard_locked / assert_period_writable) gates:
# soft-CLOSED (auto-rolled) months stay editable, only an explicit lock refuses
# (B2-6, task_autonomy.py:190-205). The gate is applied ONCE at the chokepoint
# from this table, never per-handler. A type absent here carries no period-keyed
# field to lock — there is nothing to derive, so it is not period-checked here.
# SCHEDULE_ROW_UPDATE is such a type even though it rewrites original_amount and
# useful_life_months (which reprice every future straight-line JE); that is why it is
# INTERNAL_MATERIAL (action_tiers.py) and always needs a human approval. SCHEDULE_MONTH_VALUES_UPDATE
# is the primary target: its ``month_values`` dict is keyed by ``YYYY-MM``
# (handlers.py:1517,1530 — the keys ARE the target periods).
_PERIOD_BEARING_TOOL_INPUT_KEYS = {
    "SCHEDULE_MONTH_VALUES_UPDATE": ("month_values",),
}

# A period string is a target only if it is an explicit ``YYYY-MM`` — the exact
# shape the FinancialCalendar lock floor is keyed on (task_autonomy.latest_hard_locked_period).
_YYYY_MM_RE = re.compile(r"^\d{4}-\d{2}$")


def target_locked_periods(action: ProposedAction) -> list[str]:
    """Derive the explicit ``YYYY-MM`` periods this action would write, from its payload.

    Returns the sorted, de-duplicated list of period keys the action targets (empty
    when the action type carries no period-keyed field). Only well-formed ``YYYY-MM``
    keys are returned; a malformed key is ignored HERE and left to fail downstream in
    the handler — this function's job is to surface the periods that CAN be matched
    against the lock floor, not to validate payloads. The caller (the chokepoint) fails
    CLOSED on ambiguity by treating any returned period at/under the hard-lock floor as
    a refusal.
    """
    keys = _PERIOD_BEARING_TOOL_INPUT_KEYS.get(action.action_type)
    if not keys:
        return []
    tool_input = action_tool_input(action)
    periods: set[str] = set()
    for key in keys:
        value = tool_input.get(key)  # SA-OK: bounded-loop over a fixed-N key tuple; plain dict.get, not a DB query
        # month_values is a dict keyed by period; a period-list field would be a list.
        candidates = value.keys() if isinstance(value, dict) else value if isinstance(value, list) else ()
        for candidate in candidates:
            text = str(candidate).strip()
            if _YYYY_MM_RE.match(text):
                periods.add(text)
    return sorted(periods)


def resolve_internal_db_tool_name(action: ProposedAction) -> str:
    """Resolve the assistant tool for an internal-DB action — trusted-key first.

    The server-written ``action_type`` column is the authoritative key (the proposer
    derived it from the tool name, and the model cannot rewrite it post-insert). The
    proposer also records ``payload["tool_name"]``; when present it MUST agree with the
    action_type-derived tool — a mismatch means a corrupt/tampered payload, and running
    a DIFFERENT tool than the one reviewed is never acceptable, so we fail loud.
    """
    tool_name = ACTION_TYPE_TO_TOOL.get(action.action_type)
    if tool_name is None:
        raise InternalActionNotWiredError(
            action.action_type, "action_type is not an internal-DB executable type"
        )
    payload = action.payload if isinstance(action.payload, dict) else {}
    payload_tool = payload.get("tool_name")
    if payload_tool and payload_tool != tool_name:
        raise InternalActionNotWiredError(
            action.action_type,
            f"payload tool_name {payload_tool!r} does not match the "
            f"action_type-derived tool {tool_name!r}",
        )
    return tool_name


def gate_refusal_reason(db: Session, action: ProposedAction) -> str | None:
    """Return an honest refusal message if either shared gate blocks this action, else None.

    Unifies the internal-DB executor onto the QBO executor's gate set (AC-1/AC-2). Runs
    INSIDE the caller's tenant scope (the period read is realm-keyed under FORCE-RLS, so it
    needs the pin — task_autonomy.py:278). Both gates REUSE existing predicates — no new lock
    check is authored (AC-1):

    - SUSPENSION (AC-2): ``_ensure_company_can_execute`` (qbo_actions/executor.py:225-228) — the
      SAME ``company_is_suspended`` predicate the QBO lane enforces. The approve endpoint's
      ``_company_eligibility`` (actions.py:270-291) computed suspension for DISPLAY only and never
      blocked; wiring it here enforces it for BOTH the single (actions.py:738) and bulk
      (actions.py:1047) lanes, which both funnel through this core. ``_ensure_company_can_execute``
      raises ``PermissionError`` on a suspended company.
    - CLOSED-PERIOD (AC-1): ``period_is_hard_locked`` (task_autonomy.py:297) — the HARD-lock edit
      floor these schedule/budget EDITS are gated on (matching ``assert_period_writable``;
      soft-CLOSED auto-rolled months stay editable per B2-6, task_autonomy.py:190-205). Distinct
      from the QBO lane's soft-CLOSED-inclusive ``period_is_closed`` (a JE POST into the GL is a
      different write). Target periods are derived from the payload (``target_locked_periods``);
      the predicate fails CLOSED on any indeterminate read (task_autonomy.py:319-328), so an
      unresolvable lock state refuses.
    - REQUEST (F037): a tool with a ``PROPOSAL_REQUEST_VALIDATORS`` entry (handlers.py) is
      re-validated against CURRENT state, so a proposal drafted before the entity changed
      (e.g. a lease activated since) is refused cleanly instead of half-applied.
    """
    # Lazy import — qbo_actions.executor pulls in the qbo_client + services graph; a
    # module-scope import risks the same cycle the handler import below avoids.
    from ..assistant.handlers import PROPOSAL_REQUEST_VALIDATORS
    from ..qbo_actions.executor import _ensure_company_can_execute
    from ..services.task_autonomy import period_is_hard_locked
    from .tool_outcomes import ToolFailure

    try:
        _ensure_company_can_execute(db, action)
    except PermissionError:
        return (
            "This company is suspended or unpaid, so the change was not applied. "
            "Reactivate the company to approve changes."
        )

    for period in target_locked_periods(action):
        if period_is_hard_locked(db, realm_id=action.company_id, period=period):
            return (
                f"{period} is a locked accounting period, so this change was not applied. "
                "Enter the corrective amount in an open period, or ask an admin to unlock the period."
            )

    request_validator = PROPOSAL_REQUEST_VALIDATORS.get(resolve_internal_db_tool_name(action))
    if request_validator is not None:
        try:
            request_validator(db, action.company_id, action_tool_input(action))
        except ToolFailure as exc:
            return f"This change was not applied: {exc.text}"
    return None


REVIEW_MESSAGE = "Review required — the result could not be confirmed. Check records before retrying."
BLOCKED_MESSAGE = "This action was not run because an earlier matching action requires review. Check records before retrying."


@dataclass(frozen=True)
class InternalExecutionOutcome:
    succeeded: bool
    message: str
    execution_status: str
    executed: bool | None
    blocking_action_id: int | None = None

    def __iter__(self):
        return iter((self.succeeded, self.message))

    def __getitem__(self, index):
        return (self.succeeded, self.message)[index]

    def result(self):
        value = {"execution_mode": "internal_db", "execution_status": self.execution_status,
                 "executed": self.executed, "retryable": False, "message": self.message}
        if self.blocking_action_id is not None:
            value["blocking_action_id"] = self.blocking_action_id
        return value


def _internal_execution_idempotency_key(action, tool_name, tool_input):
    # Per-proposal identity is distinct from the cross-proposal active hold.
    from .internal_action_intent import intent_fingerprint
    intent = intent_fingerprint(action.company_id, action.action_type, tool_name, tool_input)
    return hashlib.sha256(canonical_json({"protocol": 2, "action_id": action.id,
                                         "revision": action.revision, "intent": intent}).encode()).hexdigest()


def _claim_internal_execution(db, action, tool_name, tool_input):
    from .internal_action_intent import executable_inputs
    intent = action_intent_fingerprint(action, tool_name)
    key = _internal_execution_idempotency_key(action, tool_name, executable_inputs(action.action_type, action.payload))
    query = db.query(InternalActionExecution).filter(InternalActionExecution.company_id == action.company_id)
    # Every legacy attempt for this action requires classification, even when its old
    # key cannot be reconstructed from today's proposal. Never adopt another action.
    existing = query.filter(InternalActionExecution.action_id == action.id).order_by(InternalActionExecution.id.desc()).first()
    if existing is not None:
        return existing
    legacy = query.filter(InternalActionExecution.status != "succeeded",
                          InternalActionExecution.active_intent_key.is_(None),
                          InternalActionExecution.status != "not_applied").first()
    if legacy is not None:
        return legacy
    held = query.filter(InternalActionExecution.active_intent_key == intent).first()
    if held is not None:
        return held
    organization = organization_for_company(db, action.company_id)
    company = organization_company_for_realm(db, organization_id=organization.id, realm_id=action.company_id) if organization else None
    execution = InternalActionExecution(action_id=action.id, company_id=action.company_id,
        organization_id=organization.id if organization else None,
        organization_company_id=company.id if company else None,
        action_type=action.action_type, tool_name=tool_name, idempotency_key=key,
        protocol_version=2, intent_fingerprint=intent, active_intent_key=intent,
        proposal_revision=action.revision, approval_fingerprint=approval_fingerprint(action, intent),
        status="pending", attempts=0)
    from .internal_action_effects import capture_source
    capture_source(action, execution)
    db.add(execution)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = query.filter(InternalActionExecution.idempotency_key == key).populate_existing().first()
        if existing is None:
            existing = query.filter(InternalActionExecution.active_intent_key == intent).populate_existing().first()
        if existing is None:
            raise
        return existing
    return execution


def _mark_internal_execution_running(db, execution):
    token = str(uuid.uuid4())
    result = db.execute(update(InternalActionExecution).where(
        InternalActionExecution.id == execution.id,
        InternalActionExecution.company_id == execution.company_id,
        InternalActionExecution.protocol_version == 2,
        InternalActionExecution.status == "pending",
        InternalActionExecution.owner_claim_token.is_(None),
        InternalActionExecution.effect_revoked_at.is_(None),
        InternalActionExecution.active_intent_key == execution.intent_fingerprint,
    ).values(status="running", owner_claim_token=token, attempts=InternalActionExecution.attempts + 1,
             started_at=datetime.now(timezone.utc), finished_at=None, error=None)
      .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise RuntimeError("Internal execution ownership unavailable")
    db.commit()  # Admission must be durable before the handler is entered.
    db.refresh(execution)
    return token


def _compatible(action, execution):
    if execution.effect_contract is not None:
        from .internal_action_effects import source_matches
        if not source_matches(action, execution):
            return False
    return (execution.protocol_version == 2 and action.status in {"APPROVED", "EXECUTED", "FAILED"}
            and resolve_internal_db_tool_name(action) == execution.tool_name
            and execution.proposal_revision == action.revision
            and execution.intent_fingerprint == action_intent_fingerprint(action, execution.tool_name)
            and execution.approval_fingerprint == approval_fingerprint(action, execution.intent_fingerprint))


def _fresh_durable_state(db, company_id, action_id, execution_id):
    """Independent transaction; the caller's failed identity map is never evidence."""
    with DurableSession(bind=db.get_bind()) as fresh:
        with tenant_scope_for_realm(fresh, company_id):
            execution = fresh.query(InternalActionExecution).filter(
                InternalActionExecution.company_id == company_id,
                InternalActionExecution.id == execution_id,
                InternalActionExecution.action_id == action_id).first()
            action = fresh.query(ProposedAction).filter(ProposedAction.company_id == company_id,
                                                       ProposedAction.id == action_id).first()
            if execution is None or action is None:
                return None
            return {"status": execution.status, "owner": execution.owner_claim_token,
                    "compatible": _compatible(action, execution), "action_status": action.status,
                    "result": action.execution_result, "message": execution.result_message}


def _durable_success(state, token=None):
    return bool(state and state["status"] == "succeeded" and state["compatible"]
                and (token is None or state["owner"] == token)
                and state["action_status"] == "EXECUTED"
                and isinstance(state["result"], dict) and state["result"].get("executed") is True)


def _finish_internal_execution(db, execution, *, outcome, token, action, actor_email,
                               failure_status="FAILED", failure_audit_source="dashboard", post_execute=None):
    """Owned terminal evidence, projection, audit and hold release share one commit."""
    # Lock in the same order as replay: ledger, then the current proposal. The
    # caller's ORM snapshot is not authority after a self-committing handler.
    with db.no_autoflush:
        held = db.query(InternalActionExecution).filter(
            InternalActionExecution.id == execution.id,
            InternalActionExecution.company_id == action.company_id,
            InternalActionExecution.action_id == action.id,
            InternalActionExecution.owner_claim_token == token,
            InternalActionExecution.protocol_version == 2,
            InternalActionExecution.status == "running",
            InternalActionExecution.effect_revoked_at.is_(None),
        ).populate_existing().with_for_update().first()
        if held is None:
            raise RuntimeError("Internal execution completion ownership unavailable")
        action = db.query(ProposedAction).filter(
            ProposedAction.id == action.id, ProposedAction.company_id == action.company_id,
        ).populate_existing().with_for_update().one()
        binding_matches = action.status == "APPROVED" and _compatible(action, held)
        if outcome.succeeded and held.effect_contract is not None:
            from .internal_action_effects import matching_receipt, scope_matches, source_matches
            if (not scope_matches(db, held) or not source_matches(action, held)
                    or matching_receipt(db, held, lock_target=True) is None):
                raise RuntimeError("Required committed effect receipt unavailable")
        if not binding_matches and outcome.execution_status != "recovery_required":
            raise RuntimeError("Internal execution proposal binding changed")
    result = db.execute(update(InternalActionExecution).where(
        InternalActionExecution.id == execution.id,
        InternalActionExecution.company_id == action.company_id,
        InternalActionExecution.action_id == action.id,
        InternalActionExecution.protocol_version == 2,
        InternalActionExecution.owner_claim_token == token,
        InternalActionExecution.status == "running",
        InternalActionExecution.effect_revoked_at.is_(None),
    ).values(status=outcome.execution_status, result_message=redact_to_string(outcome.message, max_length=1000),
             error=None if outcome.succeeded else outcome.message,
             active_intent_key=None if outcome.execution_status in {"succeeded", "not_applied"} else execution.intent_fingerprint,
             finished_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise RuntimeError("Internal execution completion ownership unavailable")
    # If a newer proposal appeared, quarantine only this old attempt. Never adopt
    # or rewrite the newer revision/approval as part of uncertainty recording.
    if binding_matches:
        if outcome.succeeded:
            action.status = "EXECUTED"
        elif outcome.execution_status == "not_applied" and failure_status is not None:
            action.status = failure_status
        else:
            action.status = "APPROVED"
        action.execution_result = outcome.result()
    if not outcome.succeeded:
        db.add(AuditEntry(company_id=action.company_id, user_email=actor_email,
            action="INTERNAL_ACTION_RECOVERY_REQUIRED" if outcome.executed is None else "APPROVE_ACTION_EXECUTION_FAILED",
            entity_type="proposed_action", entity_id=str(action.id),
            details={"status": action.status, "action_type": action.action_type,
                     "source": failure_audit_source, "reason": "internal_execution_gate_refused" if outcome.execution_status == "not_applied" else outcome.execution_status}))
    if post_execute is not None and outcome.executed is not None:
        post_execute(db, outcome.succeeded, outcome.message)
    db.commit()
    db.expire(execution)


def _uncertain_after_error(db, action, execution, token, *, actor_email, source, identity=None):
    db.rollback()
    try:
        company_id, action_id, execution_id = identity or (action.company_id, action.id, execution.id)
        state = _fresh_durable_state(db, company_id, action_id, execution_id)
    except Exception:
        return InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
    if _durable_success(state, token):
        db.expire_all()
        return InternalExecutionOutcome(True, state["message"] or "Change applied.", "succeeded", True)
    outcome = InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
    if not state or state["owner"] != token or state["status"] != "running":
        return outcome
    try:
        _finish_internal_execution(db, execution, outcome=outcome, token=token, action=action,
                                   actor_email=actor_email, failure_audit_source=source)
    except Exception:
        db.rollback()
        # An uncertainty commit can itself land then raise. Read, never overwrite.
        try:
            state = _fresh_durable_state(db, company_id, action_id, execution_id)
            if _durable_success(state, token):
                return InternalExecutionOutcome(True, state["message"] or "Change applied.", "succeeded", True)
        except Exception:
            pass
    return outcome


def _replay_success(db, action, execution, actor_email):
    # Serialize projection repair so the reconciliation audit is idempotent.
    execution = db.query(InternalActionExecution).filter(
        InternalActionExecution.id == execution.id, InternalActionExecution.company_id == action.company_id
    ).populate_existing().with_for_update().one()
    action = db.query(ProposedAction).filter(
        ProposedAction.id == action.id, ProposedAction.company_id == action.company_id
    ).populate_existing().with_for_update().one()
    if not _compatible(action, execution):
        return InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
    outcome = InternalExecutionOutcome(True, execution.result_message or "Already applied.", "succeeded", True)
    if action.status == "EXECUTED" and isinstance(action.execution_result, dict) and action.execution_result.get("executed") is True:
        return outcome
    action.status = "EXECUTED"
    action.execution_result = outcome.result()
    db.add(AuditEntry(company_id=action.company_id, user_email=actor_email,
        action="INTERNAL_ACTION_OUTCOME_RECONCILED", entity_type="proposed_action", entity_id=str(action.id),
        details={"execution_id": execution.id, "revision": execution.proposal_revision}))
    try:
        db.commit()
    except Exception:
        db.rollback()
        try:
            if _durable_success(_fresh_durable_state(db, action.company_id, action.id, execution.id)):
                return outcome
        except Exception:
            pass
        return InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
    return outcome


def internal_execution_summary(db, action):
    """Read-only, scoped, redacted durable projection; no recovery mutation."""
    if action.action_type not in INTERNAL_DB_ACTION_TYPES:
        return None
    execution = db.query(InternalActionExecution).filter(
        InternalActionExecution.company_id == action.company_id,
        InternalActionExecution.action_id == action.id).order_by(InternalActionExecution.id.desc()).first()
    blocker = None
    if execution is None:
        try:
            intent = action_intent_fingerprint(action, resolve_internal_db_tool_name(action))
            execution = db.query(InternalActionExecution).filter(
                InternalActionExecution.company_id == action.company_id,
                InternalActionExecution.active_intent_key == intent).first()
            blocker = execution.action_id if execution else None
        except (ValueError, InternalActionNotWiredError):
            return {"execution_status": "unknown", "executed": None, "retryable": False,
                    "attempts": 0, "started_at": None, "finished_at": None, "message": REVIEW_MESSAGE}
    if execution is None:
        return None
    status = "blocked" if blocker else execution.status if execution.protocol_version == 2 else "legacy"
    if status == "succeeded":
        try:
            if not _compatible(action, execution):
                status = "recovery_required"
        except (ValueError, InternalActionNotWiredError):
            status = "recovery_required"
    executed = False if blocker or status == "not_applied" else True if status == "succeeded" else None
    summary = {"execution_status": status, "executed": executed, "retryable": False,
               "attempts": execution.attempts,
               "started_at": execution.started_at.isoformat() if execution.started_at else None,
               "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
               "message": BLOCKED_MESSAGE if blocker else "Change applied." if executed else
                          "This action was not applied." if executed is False else REVIEW_MESSAGE}
    if blocker:
        summary["blocking_action_id"] = blocker
    return summary


def execute_internal_db_action_core(db, action, *, actor_email, failure_status="FAILED",
                                    failure_audit_source="dashboard", post_execute=None):
    """Execute once per owned admission; uncertain effects retain their exact hold."""
    from ..assistant.handlers import TOOL_HANDLERS
    from .proposal_provenance import validate_material_execution
    tool_name = resolve_internal_db_tool_name(action)
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise InternalActionNotWiredError(action.action_type, "No registered handler")
    # JSON identity is evaluated before building any trusted runtime wrapper.
    intent = action_intent_fingerprint(action, tool_name)
    tool_input = action_tool_input(action)
    with tenant_scope_for_realm(db, action.company_id):
        execution = _claim_internal_execution(db, action, tool_name, tool_input)
        if execution.action_id != action.id:
            return InternalExecutionOutcome(False, BLOCKED_MESSAGE, "blocked", False, execution.action_id)
        if execution.status == "succeeded":
            return _replay_success(db, action, execution, actor_email)
        if execution.status == "not_applied" and _compatible(action, execution):
            return InternalExecutionOutcome(False, "This action was not applied.", "not_applied", False)
        validate_material_execution(action)
        if (execution.protocol_version != 2 or execution.status != "pending"
                or execution.intent_fingerprint != intent or not _compatible(action, execution)
                or action.status != "APPROVED"):
            return InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
        # Any raised admission commit has zero handler entry, even when it landed.
        try:
            token = _mark_internal_execution_running(db, execution)
        except Exception:
            db.rollback()
            try:
                _fresh_durable_state(db, action.company_id, action.id, execution.id)
            except Exception:
                pass
            return InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
        identity = (action.company_id, action.id, execution.id)
        try:
            refusal = gate_refusal_reason(db, action)
        except Exception:
            return _uncertain_after_error(db, action, execution, token, actor_email=actor_email, source=failure_audit_source, identity=identity)
        if refusal is not None:
            outcome = InternalExecutionOutcome(False, refusal, "not_applied", False)
        else:
            try:
                from .internal_action_effects import admitted_effect_context
                context = admitted_effect_context(db, execution, token) if execution.effect_contract else nullcontext()
                with context:
                    message = handler(db, action.company_id, tool_input)
            except Exception:
                return _uncertain_after_error(db, action, execution, token, actor_email=actor_email, source=failure_audit_source, identity=identity)
            if str(message).startswith("Error:"):
                db.rollback()
                outcome = InternalExecutionOutcome(False, REVIEW_MESSAGE, "recovery_required", None)
            else:
                outcome = InternalExecutionOutcome(True, str(message), "succeeded", True)
        try:
            _finish_internal_execution(db, execution, outcome=outcome, token=token, action=action,
                actor_email=actor_email, failure_status=failure_status,
                failure_audit_source=failure_audit_source, post_execute=post_execute)
        except Exception:
            return _uncertain_after_error(db, action, execution, token, actor_email=actor_email, source=failure_audit_source, identity=identity)
        return outcome

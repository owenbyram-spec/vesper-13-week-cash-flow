# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — docstring hunk.
"""Agent-plan sibling walker (Wave 3.1 chunk B — decisions 2/6/7/8/9, AC-3).

Drives a human-approved ``AgentPlan`` DAG through the EXISTING proposal funnel;
the close-orchestration walk is the copied PATTERN (derived cursor, per-realm
TTL lease, per-step commit, fail-loud budget) — ``close_orchestration.py`` is
never touched or imported. THE ONE RULE (decision 6): **this walker NEVER
executes an approved action** (``execute_proposed_action`` is never called).
Reads run via ``ToolExecutor`` (policy gate live, scope strip intact); a write
step drafts ONE PENDING internal-DB ``ProposedAction`` carrying ``plan_step_id``
(the dedup uq) and parks ``awaiting_approval``; re-polls only OBSERVE:
EXECUTED→complete, FAILED/REJECTED→failed, PENDING past ``expires_at``→claim
PENDING→EXPIRED, APPROVED-but-not-executed→park ``blocked_retryable`` for the
human (no automated re-invoker exists; this module creates none). Recorded
decision-8 residual: a proposal already APPROVED at cancel time still executes
in the approver's synchronous request (individually human-approved). SYSTEM
Celery driver, not a model-invocable tool; the plan lane's ``safety_expectation``
``AgentEvalFixture`` class lands in lockstep chunk D (app/agent/evals.py).
Budget (decision 9): steps bind ``bind_llm_task_context("plan", ...)`` so LLM
calls carry this run's ``run_id`` on the ledger ("plan" is outside the closed
task_type vocabulary → records as "other"; run/step attribution is what the
budget needs); the walker enforces the per-run token budget itself, before each
step, fail-loud terminal (never retried).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any  # CRAFT-OK: dynamic tool-output + runner summary boundary (mirrors close_orchestration/agent.py contract)

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ...config import _env_int
from ...models import AgentRun, AgentPlan, AgentPlanRunLock, AgentPlanStep, AuditLog, ProposedAction, WatchdogAlert
from ...qbo_actions.proposal_fingerprint import compute_proposal_fingerprint
from ...qbo_actions.transitions import (
    action_is_expired,
    claim_action_transition,
    expire_pending_plan_step_proposals,
)
from ...services.internal_action_execution import ACTION_TYPE_TO_TOOL
from ...services.llm_task_context import bind_llm_task_context, close_run_tokens_spent
from ...services.redaction import redact_to_string
from ...services.task_autonomy import period_is_hard_locked
from ...services.tool_outcomes import ToolFailure
from ..tracking import AgentRunTracker
from .plan_validation import steps_hash_matches
from .tool_executor import ToolExecutor
from .tool_policy import ToolPolicyGate
from .tool_registry import ToolRegistry, canonicalize_intent
from .types import AgentIntent, ContextPacket, OutputMode, RiskLevel, ToolDefinition

logger = logging.getLogger(__name__)

# tool -> internal-DB action_type: the exact inverse of the executor-side map
# (DERIVED, never hand-copied — the tool_executor.py precedent). Validation
# restricts plan write steps to PROPOSAL_ONLY_WRITE_TOOLS (decision 5 E3), so a
# missing row here is drift/tamper and degrades honestly (step failed).
_ACTION_TYPE_BY_TOOL = {tool: action for action, tool in ACTION_TYPE_TO_TOOL.items()}

_APPROVAL_EFFECT = (
    "Approval executes this change in Peregrine immediately; the outcome is recorded "
    "honestly as EXECUTED or FAILED on the action."
)

MAX_STEP_ATTEMPTS = 3
_STEP_TERMINAL = frozenset({"complete", "failed", "skipped", "expired"})
_STEP_PARKED = frozenset({"awaiting_approval", "blocked_retryable", "bounded_daily"})
_WALKER_IDENTITY = "system:agent_plan_walker"


class PlanLeaseLostError(RuntimeError):
    """Further step work is forbidden after ownership or heartbeat persistence fails."""


def plan_lock_ttl():
    return timedelta(seconds=_env_int("AGENT_PLAN_LOCK_TTL_SECONDS", 1800))


class PlanBudgetExceededError(RuntimeError):
    """The plan run spent its LLM token budget — FAIL LOUD, terminal, never retried."""

    def __init__(self, *, run_id: int, spent_tokens: int, budget_tokens: int) -> None:
        self.run_id, self.spent_tokens, self.budget_tokens = run_id, spent_tokens, budget_tokens
        super().__init__(f"Agent-plan run {run_id} exceeded its LLM token budget: spent {spent_tokens} >= budget {budget_tokens}")


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — compaction.
def _as_utc(value: datetime | None) -> datetime | None:
    return None if value is None else (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — comment trim.
# Per-realm single-flight TTL lease (AgentPlanRunLock — the CloseRunLock mirror).
def _lock_plan_owner(db, *, realm_id, run_id, plan_id=None):
    """Stable owner row precedes lease locking, including when no lease exists."""
    if run_id is not None:
        return db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.company_id == realm_id).populate_existing().with_for_update().first()
    if plan_id is not None:
        return db.query(AgentPlan).filter(AgentPlan.id == plan_id, AgentPlan.realm_id == realm_id).populate_existing().with_for_update().first()
    return None


def _bind_plan_run_claim(db, realm_id, plan_id, run_id, token):
    if run_id is None:
        return
    run = db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.company_id == realm_id).with_for_update().first()
    if run is None or run.status not in ("running", "queued"):
        raise PlanLeaseLostError("Cannot bind inactive plan run")
    run.permission_scope = {**(run.permission_scope or {}), "agent_plan_id": plan_id, "plan_lease_token": _as_utc(token).isoformat()}


def _try_acquire_plan_lock(db, realm_id: str, plan_id: int, run_id: int | None) -> bool:
    """Claim (or CAS-steal a stale) per-realm walk lease. Non-blocking."""
    now = datetime.now(timezone.utc)
    owner = _lock_plan_owner(db, realm_id=realm_id, run_id=run_id, plan_id=plan_id)
    if owner is None or (run_id is not None and owner.status not in ("queued", "running")):
        raise PlanLeaseLostError("Cannot acquire a lease for an inactive owner")
    lease = db.query(AgentPlanRunLock).filter(AgentPlanRunLock.realm_id == realm_id).populate_existing().with_for_update().first()
    if lease is None:
        db.add(AgentPlanRunLock(realm_id=realm_id, plan_id=plan_id, agent_run_id=run_id, locked_at=now, heartbeat_at=now))
        try:
            _bind_plan_run_claim(db, realm_id, plan_id, run_id, now)
            db.commit()
            db.info["plan_lease_claim"] = (realm_id, run_id, plan_id, now)
            return True
        except IntegrityError:
            db.rollback()  # a sibling won the PK race — it holds the lease
            return False
    heartbeat = _as_utc(lease.heartbeat_at)
    ttl = plan_lock_ttl()
    if heartbeat is not None and (now - heartbeat) < ttl:
        db.commit()
        return False  # a live walk holds the lease
    # Stale: optimistic CAS on the observed heartbeat — of two stealers exactly one wins.
    observed = lease.heartbeat_at
    stolen = (
        db.query(AgentPlanRunLock)
        .filter(AgentPlanRunLock.realm_id == realm_id, AgentPlanRunLock.heartbeat_at == observed)
        .update({AgentPlanRunLock.plan_id: plan_id, AgentPlanRunLock.agent_run_id: run_id, AgentPlanRunLock.locked_at: now, AgentPlanRunLock.heartbeat_at: now}, synchronize_session=False)
    )
    if stolen:
        _bind_plan_run_claim(db, realm_id, plan_id, run_id, now)
    db.commit()
    if stolen:
        db.info["plan_lease_claim"] = (realm_id, run_id, plan_id, now)
        logger.warning("[plan_walker] stole a stale plan lease (realm=%s)", realm_id)
    return bool(stolen)


def _heartbeat_plan_lock(db, *, realm_id: str, run_id: int | None) -> None:
    """Own the stable run first, then validate and advance its lease claim."""
    expected = db.info.get("plan_lease_claim")
    if expected is None or expected[:2] != (realm_id, run_id):
        raise PlanLeaseLostError("Plan lease has no owned claim in this worker")
    owner = _lock_plan_owner(db, realm_id=realm_id, run_id=run_id, plan_id=expected[2])
    if owner is None or (run_id is not None and owner.status not in ("queued", "running")):
        raise PlanLeaseLostError("Plan run no longer owns an active lease")
    lease = db.query(AgentPlanRunLock).filter(AgentPlanRunLock.realm_id == realm_id).populate_existing().with_for_update().first()
    if lease is None or expected[:3] != (realm_id, lease.agent_run_id, lease.plan_id) or _as_utc(expected[3]) != _as_utc(lease.locked_at):
        raise PlanLeaseLostError("Plan lease claim was lost or replaced")
    lease.heartbeat_at = datetime.now(timezone.utc)
    db.flush()


def reap_stale_plan_run(db, run_id: int, realm_id: str) -> bool:
    """Run -> lease is shared by acquisition/heartbeat, even for an absent lease."""
    run = _lock_plan_owner(db, realm_id=realm_id, run_id=run_id)
    lease = db.query(AgentPlanRunLock).filter(AgentPlanRunLock.realm_id == realm_id).populate_existing().with_for_update().first()
    if run is None or run.status not in ("queued", "running"):
        return False
    now = datetime.now(timezone.utc)
    identity = run.permission_scope or {}
    if lease and lease.agent_run_id == run.id and identity.get("agent_plan_id") == lease.plan_id and identity.get("plan_lease_token") == _as_utc(lease.locked_at).isoformat() and _as_utc(lease.heartbeat_at) and now - _as_utc(lease.heartbeat_at) < plan_lock_ttl():
        return False
    run.status = "failed"
    run.error = "Plan lease expired or ownership was lost; review incomplete work."
    run.finished_at = now
    if lease and lease.agent_run_id == run.id:
        db.delete(lease)
    db.flush()
    return True


def _release_plan_lock(db, realm_id: str, run_id: int | None) -> None:
    """Delete OUR lease row (finally-path; never raises; TTL reclaims on failure)."""
    try:
        db.rollback()
        claim = db.info.pop("plan_lease_claim", None)
        if claim is None or claim[:2] != (realm_id, run_id):
            return
        _lock_plan_owner(db, realm_id=realm_id, run_id=run_id, plan_id=claim[2])
        db.query(AgentPlanRunLock).filter(
            AgentPlanRunLock.realm_id == realm_id,
            AgentPlanRunLock.agent_run_id == run_id,
            AgentPlanRunLock.plan_id == claim[2],
            AgentPlanRunLock.locked_at == claim[3],
        ).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — release must not mask the walk's error
        try:
            db.rollback()
        except Exception:  # noqa: BLE001  # CRAFT-OK: best-effort rollback inside the finally-path lease release; the warning below surfaces the failure and the TTL reclaims the realm
            logger.debug("[plan_walker] rollback during lease-release cleanup failed", exc_info=True)
        logger.warning("[plan_walker] lease release failed (realm=%s): %s", realm_id, exc)


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — comment trim.
# Claim-style plan-status flips (E9 — no read-then-write).
def _claim_plan_status(db, plan_id: int, *, from_statuses: tuple[str, ...], to_status: str) -> bool:
    """Conditionally flip the plan status; exactly one concurrent flipper wins."""
    result = db.execute(update(AgentPlan).where(AgentPlan.id == plan_id, AgentPlan.status.in_(from_statuses)).values(status=to_status))
    won = result.rowcount == 1
    if won:
        db.expire_all()
    db.commit()
    return won


def _plan_status(db, plan_id: int, realm_id: str) -> str | None:
    return db.query(AgentPlan.status).filter(AgentPlan.id == plan_id, AgentPlan.realm_id == realm_id).scalar()


def _finding(db, *, realm_id: str, plan_id: int, run_id: int | None, alert_type: str, title: str, description: str) -> None:
    """Surface an honest plan-terminal finding (WatchdogAlert + audit). Committed."""
    db.rollback()  # discard any half-flushed step state; the finding gets a clean txn
    db.add(WatchdogAlert(company_id=realm_id, alert_type=alert_type, severity="critical", title=title, description=description, context={"agent_plan_id": plan_id, "agent_run_id": run_id}))
    db.add(AuditLog(realm_id=realm_id, action="UPDATE", entity_type="AgentPlan", entity_id=str(plan_id), user_id=_WALKER_IDENTITY, details={"event": alert_type.lower(), "agent_plan_id": plan_id, "agent_run_id": run_id}))
    db.commit()


def _enforce_plan_budget(db, *, run_id: int | None, realm_id: str) -> None:
    """Pre-step ledger-spend check vs the env budget (decision 9). FAIL-LOUD on a
    genuine over-budget; a spend-read infra error logs loudly and allows; no run
    id → nothing metered under this walk, nothing to enforce."""
    if not run_id:
        return
    budget = _env_int("AGENT_PLAN_RUN_TOKEN_BUDGET", 500000)
    try:
        spent = close_run_tokens_spent(run_id, db)
    except Exception as exc:  # noqa: BLE001 — infra error reading spend: allow, loudly
        logger.error("[plan_walker] budget read failed for run=%s (allowing): %s", run_id, redact_to_string(str(exc), max_length=300))
        return
    if spent >= budget:
        raise PlanBudgetExceededError(run_id=run_id, spent_tokens=spent, budget_tokens=budget)


def _mark_step(db, step: AgentPlanStep, *, status: str, evidence: str, run_id: int | None, bump_attempts: bool = False) -> None:
    """Set step execution-state (walker-mutable only, E4) + heartbeat, commit per step."""
    step.status = status
    step.evidence = evidence[:2000]
    if bump_attempts:
        step.attempts = int(step.attempts or 0) + 1
    _heartbeat_plan_lock(db, realm_id=step.realm_id, run_id=run_id)
    db.commit()


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — compaction.
def build_plan_context(db, plan: AgentPlan, registry: ToolRegistry, *, run_id: int | None, organization_id: int | None, organization_company_id: int | None) -> ContextPacket:
    """Realm-pinned ContextPacket (trusted scope, server-side only): allowed_tools =
    the FROZEN hash-sealed step tool set (decision 9, walker-enforced); intent = the
    deterministic covering intent over the steps' allowed_intents (honest "general"
    fallback → the per-step gate refuses loudly); risk ceiling DRAFT_ONLY."""
    steps = db.query(AgentPlanStep).filter(AgentPlanStep.plan_id == plan.id, AgentPlanStep.realm_id == plan.realm_id).order_by(AgentPlanStep.step_index.asc()).all()
    tools = [registry.get(s.tool_name) for s in steps]  # SA-OK: bounded in-memory registry dict lookups over ≤20 plan steps, not DB queries
    constrained = [t for t in tools if t is not None and "general" not in t.allowed_intents]
    if not constrained:
        intent = AgentIntent.GENERAL.value
    else:
        common = set.intersection(*(set(t.allowed_intents) for t in constrained))
        intent = sorted(common)[0] if common else AgentIntent.GENERAL.value
    return ContextPacket(
        run_id=run_id, conversation_id=None, user_id=None, user_email=None, tenant_id=None,
        company_id=plan.realm_id, qbo_realm_id=plan.realm_id, user_role="ANALYST",
        request_text=f"agent-plan walk: {plan.title}", intent=canonicalize_intent(intent),
        risk_level=RiskLevel.DRAFT_ONLY, output_mode=OutputMode.JSON,
        allowed_tools=sorted({s.tool_name for s in steps}),
        organization_id=organization_id, organization_company_id=organization_company_id,
    )


def cancel_agent_plan(db, plan: AgentPlan, *, cancelled_by: str = _WALKER_IDENTITY) -> dict[str, Any]:  # CRAFT-OK: service-callable summary-dict contract, mirrors the runner returns
    # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — compaction.
    """Decision-8 cancel (service-callable): FIRST claim-expire the plan's PENDING
    step proposals (a concurrent approve's claim wins its row → skipped), THEN
    claim-flip the plan CANCELLED — the ordering is load-bearing."""
    expired = expire_pending_plan_step_proposals(db, plan.id, expired_by=cancelled_by)
    won = _claim_plan_status(db, plan.id, from_statuses=("DRAFT", "PENDING_APPROVAL", "ACTIVE", "PAUSED"), to_status="CANCELLED")
    if won:
        db.add(AuditLog(realm_id=plan.realm_id, action="UPDATE", entity_type="AgentPlan", entity_id=str(plan.id), user_id=cancelled_by, details={"event": "agent_plan_cancelled", "expired_step_proposals": expired}))
        db.commit()
    return {"cancelled": won, "expired_proposals": expired}


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — comment trim.
def _drafts_today(db, realm_id: str) -> int:
    """Walker-drafted proposals (plan_step_id NOT NULL) for this realm since UTC midnight."""
    midnight = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(db.query(ProposedAction).filter(ProposedAction.company_id == realm_id, ProposedAction.plan_step_id.isnot(None), ProposedAction.created_at >= midnight).count())


def _find_step_proposal(db, step: AgentPlanStep) -> ProposedAction | None:
    return db.query(ProposedAction).filter(ProposedAction.plan_step_id == step.id, ProposedAction.company_id == step.realm_id).order_by(ProposedAction.id.desc()).first()


def _require_reviewed_budget_proposal(action: ProposedAction) -> None:
    if action.action_type == "BUDGET_APPLY_DRIVERS":
        calculation = (action.payload or {}).get("budget_calculation_plan")
        if not isinstance(calculation, dict) or not all(k in calculation for k in ("before", "after", "source_fingerprint", "plan_fingerprint")):
            raise ToolFailure("budget_review_missing", "Budget proposal has no reviewed calculation; draft a fresh plan.")


def _draft_step_proposal(db, plan: AgentPlan, step: AgentPlanStep, action_type: str, run_id: int | None) -> ProposedAction:
    """FIND-OR-CREATE the step's ONE proposal (decision 4 — the ap_actions adopt
    shape): a deterministic payload → a deterministic fingerprint → the
    ``uq(plan_step_id, inputs_fingerprint)`` collapses a kill-after-draft
    redelivery to one PENDING row; an IntegrityError loser adopts the winner."""
    existing = _find_step_proposal(db, step)
    if existing is not None:
        _require_reviewed_budget_proposal(existing)
        return existing
    payload = {
        "tool_name": step.tool_name,
        "summary": f"Agent-plan step {step.step_index} of '{plan.title}': {action_type}",
        "execution_mode": "internal_db",
        "approval_effect": _APPROVAL_EFFECT,
        "tool_input": {k: v for k, v in dict(step.tool_input or {}).items() if not k.startswith("_") and k != "budget_calculation_plan"},
        "affected_entity": step.tool_name,
        "agent_plan": {"plan_id": plan.id, "plan_step_id": step.id, "step_index": step.step_index},
        "required_role": "EDITOR",
    }
    if action_type == "BUDGET_APPLY_DRIVERS":
        from ...services.budget_reforecast import BudgetPlanError, plan_budget_drivers
        inputs = payload["tool_input"]
        try:
            calculation = plan_budget_drivers(db, company_id=plan.realm_id,
                budget_year=int(inputs.get("budget_year") or datetime.utcnow().year),
                scenario=str(inputs.get("scenario") or "base"), force=bool(inputs.get("force")))
        except (BudgetPlanError, ValueError, TypeError) as exc:
            raise ToolFailure("budget_plan_invalid", str(exc)) from exc
        payload["budget_calculation_plan"] = calculation
        payload["before_state"] = {"budget_lines": calculation["before"]}
        payload["after_state"] = {"budget_lines": calculation["after"]}
    action = ProposedAction(  # WRITEBACK-OK: system walker drafts a PENDING proposal only (proposal-first, decision 6); a human approves it in the existing inbox — never a direct write
        company_id=plan.realm_id,
        action_type=action_type,
        payload=payload,
        reasoning=f"Drafted by the agent-plan walker for human approval (plan #{plan.id}, step {step.step_index}).",
        confidence_score=0.8,
        status="PENDING",
        source="system",
        category="agent_plan",
        priority="medium",
        expires_at=datetime.utcnow() + timedelta(days=7),
        plan_step_id=step.id,
        agent_run_id=run_id,
        step_index=step.step_index,
        inputs_fingerprint=compute_proposal_fingerprint(action_type, payload),
    )
    db.add(action)
    try:
        db.flush()  # surface the uq collision here
    except IntegrityError:
        db.rollback()  # a redelivery already drafted this exact proposal — adopt it
        winner = _find_step_proposal(db, step)
        if winner is not None:
            _require_reviewed_budget_proposal(winner)
            return winner
        raise
    return action


def _advance_write_step(db, plan: AgentPlan, step: AgentPlanStep, tool: ToolDefinition, packet: ContextPacket, gate: ToolPolicyGate, run_id: int | None) -> bool:
    """Draft-or-poll one write step. Returns True when the step's state changed."""
    proposal = _find_step_proposal(db, step)
    if proposal is None:
        # DRAFT lane (pending / bounded_daily retry / period-lock retry).
        cap = _env_int("AGENT_PLAN_MAX_DRAFTS_PER_DAY", 10)  # decision 9 E6 — read at call time
        if _drafts_today(db, plan.realm_id) >= cap:
            if step.status == "bounded_daily":
                return False
            _mark_step(db, step, status="bounded_daily", run_id=run_id, evidence=f"daily draft cap reached ({cap}); will retry on a later walk — never a silent skip")
            return True
        if period_is_hard_locked(db, realm_id=plan.realm_id, when=datetime.now(timezone.utc)):
            changed = step.status != "blocked_retryable"
            _mark_step(db, step, status="blocked_retryable", run_id=run_id, evidence="refused to draft: the current accounting period is locked (guard before proposing)")
            return changed
        decision = gate.check(tool, dict(step.tool_input or {}), packet, preflight=False)
        if not decision.allowed:
            _mark_step(db, step, status="failed", run_id=run_id, bump_attempts=True, evidence=f"policy denies tool '{tool.name}': {decision.reason}")
            return True
        action_type = _ACTION_TYPE_BY_TOOL.get(tool.name)
        if action_type is None:
            _mark_step(db, step, status="failed", run_id=run_id, evidence=f"write tool '{tool.name}' has no internal-DB executor row — honest degradation, nothing drafted")
            return True
        try:
            proposal = _draft_step_proposal(db, plan, step, action_type, run_id)
        except ToolFailure as exc:
            _mark_step(db, step, status="failed", run_id=run_id, bump_attempts=True, evidence=exc.text)
            return True
        step.proposed_action_id = proposal.id
        db.add(AuditLog(realm_id=plan.realm_id, action="CREATE", entity_type="ProposedAction", entity_id=str(proposal.id), user_id=_WALKER_IDENTITY, details={"event": "agent_plan_step_proposal_drafted", "plan_id": plan.id, "plan_step_id": step.id, "action_type": action_type, "agent_run_id": run_id}))
        _mark_step(db, step, status="awaiting_approval", run_id=run_id, bump_attempts=True, evidence=f"drafted ProposedAction #{proposal.id} ({action_type}); parked for human approval")
        return True

    if proposal.status in {"PENDING", "APPROVED"}:
        try:
            _require_reviewed_budget_proposal(proposal)
        except ToolFailure as exc:
            _mark_step(db, step, status="failed", run_id=run_id, evidence=exc.text)
            return True

    # RE-POLL lane — observe the disposition; NEVER execute (decision 6).
    status = str(proposal.status or "")
    if status == "EXECUTED":
        _mark_step(db, step, status="complete", run_id=run_id, evidence=f"ProposedAction #{proposal.id} approved and executed by the human lane")
        return True
    if status in ("FAILED", "REJECTED"):
        _mark_step(db, step, status="failed", run_id=run_id, evidence=f"ProposedAction #{proposal.id} ended {status}")
        return True
    if status == "EXPIRED":
        _mark_step(db, step, status="expired", run_id=run_id, evidence=f"ProposedAction #{proposal.id} expired unapproved")
        return True
    if status == "PENDING":
        if action_is_expired(proposal):
            won = claim_action_transition(db, action_id=proposal.id, from_status="PENDING", to_status="EXPIRED", reviewed_by=_WALKER_IDENTITY)
            db.commit()
            if not won:
                return False  # a concurrent approve claimed it — re-poll next walk
            db.add(AuditLog(realm_id=plan.realm_id, action="UPDATE", entity_type="ProposedAction", entity_id=str(proposal.id), user_id=_WALKER_IDENTITY, details={"event": "agent_plan_step_proposal_expired", "plan_id": plan.id, "plan_step_id": step.id}))
            _mark_step(db, step, status="expired", run_id=run_id, evidence=f"ProposedAction #{proposal.id} passed expires_at while PENDING; claim-expired")
            return True
        if step.status != "awaiting_approval":
            _mark_step(db, step, status="awaiting_approval", run_id=run_id, evidence=f"ProposedAction #{proposal.id} still awaits human approval")
            return True
        return False
    # APPROVED-but-not-executed (retryable_failed / uncertain execution_result) or an
    # unknown net-new status: park for the HUMAN — re-invoking the executor from here
    # would be a NEW background-write surface (decision 6; out of scope by ratified plan).
    if step.status != "blocked_retryable":
        _mark_step(db, step, status="blocked_retryable", run_id=run_id, evidence=f"ProposedAction #{proposal.id} is {status} but not EXECUTED — surfaced to the human retry lane; the walker never executes")
        return True
    return False


def _run_read_step(db, step: AgentPlanStep, tool: ToolDefinition, packet: ContextPacket, executor: ToolExecutor, run_id: int | None) -> bool:
    """Execute one read step via ToolExecutor. Returns True when state changed."""
    decision = executor.policy_gate.check(tool, dict(step.tool_input or {}), packet, preflight=False)
    if not decision.allowed:
        _mark_step(db, step, status="failed", run_id=run_id, bump_attempts=True, evidence=f"policy denies tool '{tool.name}': {decision.reason}")
        return True
    label = f"plan:{step.plan_id}:step:{step.step_index}"
    try:
        with bind_llm_task_context("plan", run_id=run_id, step=label, realm_id=step.realm_id):
            result = executor.execute_result(tool, packet, dict(step.tool_input or {}))
    except Exception as exc:  # noqa: BLE001 — one bad step must not kill the walk; bounded attempts below
        db.rollback()
        attempts = int(step.attempts or 0) + 1
        if attempts >= MAX_STEP_ATTEMPTS:
            _mark_step(db, step, status="failed", run_id=run_id, bump_attempts=True, evidence=f"read failed {attempts} times; giving up: {redact_to_string(str(exc), max_length=300)}")
        else:
            _mark_step(db, step, status="pending", run_id=run_id, bump_attempts=True, evidence=f"read failed (attempt {attempts}/{MAX_STEP_ATTEMPTS}); will re-attempt on the next walk: {redact_to_string(str(exc), max_length=300)}")
        return True
    text_result = result.text.strip()
    if not result.succeeded:
        attempts = int(step.attempts or 0) + 1
        status = "pending" if result.retryable and attempts < MAX_STEP_ATTEMPTS else "failed"
        _mark_step(db, step, status=status, run_id=run_id, bump_attempts=True, evidence=text_result[:2000])
        return True
    bounded = text_result if len(text_result) <= 4000 else text_result[:4000] + "...[truncated]"
    _mark_step(db, step, status="complete", run_id=run_id, bump_attempts=True, evidence=bounded)
    return True


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — comment trim.
def walk_agent_plan(db, plan: AgentPlan, registry: ToolRegistry, packet: ContextPacket, tracker: AgentRunTracker | None) -> dict[str, Any]:  # CRAFT-OK: runner summary-dict contract, mirrors run_close_orchestration
    # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans — compaction.
    """One resumable, single-flight, proposal-first walk over the plan's DAG. Derived
    cursor: the step rows ARE the state (per-step commit keeps a killed walk
    resumable); ready = every dep complete; a failed/skipped/expired dep skips its
    dependents honestly. Plan-status flips are claim-style (E9)."""
    realm_id, plan_id = plan.realm_id, plan.id
    run_id = tracker.run_id if tracker else None
    if not _try_acquire_plan_lock(db, realm_id, plan_id, run_id):
        logger.info("[plan_walker] realm lease held by a sibling walk; exiting clean (realm=%s)", realm_id)
        return {"status": "skipped", "reason": "run_lock_held", "realm_id": realm_id, "plan_id": plan_id}
    summary: dict[str, Any] = {"status": "succeeded", "realm_id": realm_id, "plan_id": plan_id, "walked": 0, "completed": 0, "drafted_or_polled": 0, "skipped": 0}
    try:
        _heartbeat_plan_lock(db, realm_id=realm_id, run_id=run_id)
        db.commit()
        steps = db.query(AgentPlanStep).filter(AgentPlanStep.plan_id == plan_id, AgentPlanStep.realm_id == realm_id).order_by(AgentPlanStep.step_index.asc()).all()
        # Tamper seal (E3/E4): the hash-covered DEFINITION must match the approved seal.
        if not steps or not steps_hash_matches(steps, plan.steps_hash):
            _finding(db, realm_id=realm_id, plan_id=plan_id, run_id=run_id, alert_type="AGENT_PLAN_TAMPER_REFUSED",
                     title="Agent plan refused: step definitions do not match the approved seal",
                     description=f"Plan {plan_id}: steps_hash mismatch (or no steps) at walk time; the walk refused to run and the plan is FAILED.")
            _claim_plan_status(db, plan_id, from_statuses=("ACTIVE",), to_status="FAILED")
            return {"status": "failed", "reason": "steps_hash_mismatch", "realm_id": realm_id, "plan_id": plan_id}

        # Plan TTL (decision 7): any step parked past the TTL → honest-terminal EXPIRED.
        ttl_days = _env_int("AGENT_PLAN_STEP_PARK_TTL_DAYS", 14)
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        stale = [s for s in steps if s.status in _STEP_PARKED and (_as_utc(s.updated_at) or datetime.now(timezone.utc)) <= cutoff]
        if stale:
            expired_count = expire_pending_plan_step_proposals(db, plan_id, expired_by=_WALKER_IDENTITY)
            for s in stale:
                _mark_step(db, s, status="expired", run_id=run_id, evidence=f"parked > {ttl_days}d without disposition; plan TTL expired it")
            _claim_plan_status(db, plan_id, from_statuses=("ACTIVE", "PAUSED"), to_status="EXPIRED")
            _finding(db, realm_id=realm_id, plan_id=plan_id, run_id=run_id, alert_type="AGENT_PLAN_EXPIRED",
                     title="Agent plan expired: a step waited too long for disposition",
                     description=f"Plan {plan_id}: {len(stale)} step(s) parked past the {ttl_days}-day TTL; {expired_count} pending proposal(s) claim-expired. Re-compose the plan if the work is still wanted.")
            return {"status": "expired", "reason": "step_park_ttl", "realm_id": realm_id, "plan_id": plan_id}

        # Frozen allowlist (decision 9): the hash-sealed definition tool set, optionally
        # narrowed by the packet's allowed_tools. ENFORCED per step below.
        frozen = {s.tool_name for s in steps}
        allowlist = frozen & set(packet.allowed_tools) if packet.allowed_tools else frozen
        gate = ToolPolicyGate()
        executor = ToolExecutor(db=db, policy_gate=gate, tracker=tracker)
        by_index = {s.step_index: s for s in steps}
        attempted: set[int] = set()  # at most one operation per step per walk (no tight retry loops)

        progressed = True
        while progressed:
            progressed = False
            for step in steps:
                if step.status in _STEP_TERMINAL or step.id in attempted:
                    continue
                current = _plan_status(db, plan_id, realm_id)
                if current != "ACTIVE":  # PAUSED / CANCELLED / terminal — stop honestly per step
                    summary.update({"status": "stopped", "reason": f"plan_status_{(current or 'missing').lower()}"})
                    return summary
                deps = [by_index.get(d) for d in (step.depends_on or [])]
                if any(d is None for d in deps):
                    attempted.add(step.id)
                    _mark_step(db, step, status="failed", run_id=run_id, evidence="depends_on references a step index that does not exist — refused")
                    progressed = True
                    continue
                if any(d.status in ("failed", "skipped", "expired") for d in deps):
                    attempted.add(step.id)
                    _mark_step(db, step, status="skipped", run_id=run_id, evidence="upstream dependency did not complete; step skipped")
                    summary["skipped"] += 1
                    progressed = True
                    continue
                if any(d.status != "complete" for d in deps):
                    continue  # deps not ready yet (e.g. a parked write) — wait
                _heartbeat_plan_lock(db, realm_id=realm_id, run_id=run_id)
                db.commit()
                attempted.add(step.id)
                summary["walked"] += 1
                _enforce_plan_budget(db, run_id=run_id, realm_id=realm_id)  # fail-loud terminal
                tool = registry.get(step.tool_name)  # SA-OK: in-memory registry dict lookup, not a DB query
                if tool is None:
                    _mark_step(db, step, status="failed", run_id=run_id, evidence=f"tool '{step.tool_name}' is no longer registered — honest degradation")
                    progressed = True
                    continue
                if step.tool_name not in allowlist:
                    _mark_step(db, step, status="failed", run_id=run_id, evidence=f"tool '{step.tool_name}' is not in the plan's frozen allowlist — refused")
                    progressed = True
                    continue
                derived_write = bool(tool.mutates_state or tool.creates_proposed_action)
                if derived_write != bool(step.is_write) or not tool.send_to_model:
                    _mark_step(db, step, status="failed", run_id=run_id, evidence=f"registry classification for '{step.tool_name}' drifted from the approved definition — refused")
                    progressed = True
                    continue
                if step.is_write:
                    changed = _advance_write_step(db, plan, step, tool, packet, gate, run_id)
                    summary["drafted_or_polled"] += 1
                else:
                    changed = _run_read_step(db, step, tool, packet, executor, run_id)
                if step.status == "complete":
                    summary["completed"] += 1
                progressed = progressed or changed

        # Terminal evaluation (claim-flips; skips only follow failed/expired deps).
        statuses = {s.status for s in steps}
        if statuses <= _STEP_TERMINAL:
            if "failed" in statuses:
                _claim_plan_status(db, plan_id, from_statuses=("ACTIVE",), to_status="FAILED")
                summary["plan_status"] = "FAILED"
            elif "expired" in statuses:
                expire_pending_plan_step_proposals(db, plan_id, expired_by=_WALKER_IDENTITY)
                _claim_plan_status(db, plan_id, from_statuses=("ACTIVE",), to_status="EXPIRED")
                summary["plan_status"] = "EXPIRED"
            elif statuses == {"complete"}:
                _claim_plan_status(db, plan_id, from_statuses=("ACTIVE",), to_status="COMPLETE")
                summary["plan_status"] = "COMPLETE"
            else:  # defensive: skipped without a failed/expired ancestor should be unreachable
                _claim_plan_status(db, plan_id, from_statuses=("ACTIVE",), to_status="FAILED")
                summary["plan_status"] = "FAILED"
        else:
            summary["plan_status"] = "ACTIVE"
        if summary.get("plan_status") in ("FAILED", "EXPIRED"):
            summary["status"] = summary["plan_status"].lower()
        return summary
    except PlanBudgetExceededError as exc:
        _finding(db, realm_id=realm_id, plan_id=plan_id, run_id=run_id, alert_type="AGENT_PLAN_BUDGET_EXCEEDED",
                 title="Agent plan stopped: LLM token budget exceeded",
                 description=(f"Plan {plan_id} run {run_id} spent {exc.spent_tokens} tokens against a budget of "
                              f"{exc.budget_tokens} and was stopped. The plan is FAILED (terminal — a retry would "
                              "open a fresh run and re-spend). Raise AGENT_PLAN_RUN_TOKEN_BUDGET only if this plan "
                              "legitimately needs more."))
        _claim_plan_status(db, plan_id, from_statuses=("ACTIVE",), to_status="FAILED")
        raise
    finally:
        # The task wrapper terminalizes a real run before releasing its lease.
        # Standalone utility callers without a run own their own cleanup here.
        if run_id is None:
            _release_plan_lock(db, realm_id, run_id)

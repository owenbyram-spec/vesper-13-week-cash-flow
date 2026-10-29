from __future__ import annotations

import logging
from datetime import datetime
from time import perf_counter
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import AgentRun, AgentRunCheckpoint, AgentStep, TenantUsageEvent
from ..services.redaction import redact_to_string
from ..services.tenant_companies import organization_company_for_realm, organization_for_company
from .performance import estimate_model_cost_usd, summarize_steps

logger = logging.getLogger(__name__)

# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B3 — INV-STATE (SYNTHESIS GAP-B5): the
# explicit run/step state machine. Status columns are free-form strings with transitions that
# were pure caller convention; this is the single source of truth the trackers (and tests)
# validate against. Vocabulary matches what is live in the codebase today — "succeeded" and
# "completed" are both accepted terminal-success spellings (22 call sites use "completed");
# collapsing them is a data migration, not a validation concern. Terminal states are immutable.
#
# PLAN-EXEMPT: plan 20260712-agentic-finance-os W3 (GAP-3, CRITICAL-REVIEW E1) — EXTEND the machine
# (do not rebuild it) with the directive's richer mission vocabulary: the queued→running→terminal
# core is preserved byte-for-byte (existing transitions stay valid); new WAITING/ACTIVE states and
# new terminals (partially_completed / cancelled / expired / requires_manual_reconciliation) are
# ADDED so a run can park for data/user/review/approval, execute an approved action, reconcile an
# uncertain external outcome, and resume — each transition validated + audited via AgentRunCheckpoint.
RUN_STATE_QUEUED = "queued"
RUN_STATE_RUNNING = "running"

# Non-terminal parked / active states (W3). A run in any of these is resumable.
RUN_STATE_WAITING_FOR_DATA = "waiting_for_data"          # a tool enqueued a background warm (e.g. AR/AP "computing")
RUN_STATE_WAITING_FOR_USER = "waiting_for_user"          # needs user input to proceed
RUN_STATE_WAITING_FOR_REVIEW = "waiting_for_review"      # a material output awaits human review
RUN_STATE_WAITING_FOR_APPROVAL = "waiting_for_approval"  # a ProposedAction is pending approval
RUN_STATE_EXECUTING = "executing"                        # an approved action is being executed
RUN_STATE_RECONCILING = "reconciling"                    # an uncertain external outcome is being reconciled

RUN_WAITING_STATES = frozenset({
    RUN_STATE_WAITING_FOR_DATA,
    RUN_STATE_WAITING_FOR_USER,
    RUN_STATE_WAITING_FOR_REVIEW,
    RUN_STATE_WAITING_FOR_APPROVAL,
})
# States from which a killed run can be RESUMED (nothing terminal, nothing pre-start).
RUN_RESUMABLE_STATES = frozenset({RUN_STATE_RUNNING, RUN_STATE_EXECUTING, RUN_STATE_RECONCILING}) | RUN_WAITING_STATES

RUN_TERMINAL_STATES = frozenset({
    "succeeded", "completed", "partially_completed",
    "failed", "failed_stale", "cancelled", "expired",
    "requires_manual_reconciliation",
})

# From queued the run may start, or be cancelled/expired before it ever ran (a stale queue entry).
_QUEUED_NEXT = frozenset({RUN_STATE_RUNNING}) | RUN_TERMINAL_STATES
# An active/running run may park in any waiting state, move to executing/reconciling, or terminate.
_ACTIVE_NEXT = RUN_WAITING_STATES | frozenset({RUN_STATE_EXECUTING, RUN_STATE_RECONCILING}) | RUN_TERMINAL_STATES
# A parked (waiting) run may resume to running, jump straight to executing (an approval that
# arrived), or terminate (cancelled / expired / failed).
_WAITING_NEXT = frozenset({RUN_STATE_RUNNING, RUN_STATE_EXECUTING}) | RUN_TERMINAL_STATES
# Executing may reconcile its result or terminate; reconciling only terminates.
_EXECUTING_NEXT = frozenset({RUN_STATE_RECONCILING, RUN_STATE_RUNNING}) | RUN_TERMINAL_STATES
_RECONCILING_NEXT = RUN_TERMINAL_STATES

RUN_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    RUN_STATE_QUEUED: _QUEUED_NEXT,
    RUN_STATE_RUNNING: _ACTIVE_NEXT,
    RUN_STATE_EXECUTING: _EXECUTING_NEXT,
    RUN_STATE_RECONCILING: _RECONCILING_NEXT,
    **{waiting: _WAITING_NEXT for waiting in RUN_WAITING_STATES},
}

STEP_TERMINAL_STATES = frozenset({"succeeded", "failed", "blocked"})
STEP_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": STEP_TERMINAL_STATES,
}


def is_valid_run_transition(current: str, new: str) -> bool:
    """True iff current→new is a legal AgentRun transition (terminal states immutable)."""
    if current in RUN_TERMINAL_STATES:
        return False
    return new in RUN_VALID_TRANSITIONS.get(current, frozenset())


def is_valid_step_transition(current: str, new: str) -> bool:
    """True iff current→new is a legal AgentStep transition (terminal states immutable)."""
    if current in STEP_TERMINAL_STATES:
        return False
    return new in STEP_VALID_TRANSITIONS.get(current, frozenset())


def _json_safe(value: Any) -> Any:
    """Best-effort conversion for JSON columns without leaking huge payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _truncate_text(value: Optional[str], limit: int = 4000) -> Optional[str]:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + "...[truncated]"


def _safe_error_text(value: Optional[str], limit: int = 4000) -> Optional[str]:
    if value is None:
        return None
    return _truncate_text(redact_to_string(value, max_length=limit), limit)


def _truncate_payload(value: Any, limit: int = 12000) -> Any:
    safe = _json_safe(value)
    rendered = str(safe)
    if len(rendered) <= limit:
        return safe
    return {"truncated": True, "preview": rendered[:limit]}


def _token_counts(token_usage: Optional[dict[str, Any]]) -> tuple[int, int, int]:
    if not token_usage:
        return 0, 0, 0
    input_tokens = int(token_usage.get("input_tokens") or token_usage.get("prompt_tokens") or 0)
    output_tokens = int(token_usage.get("output_tokens") or token_usage.get("completion_tokens") or 0)
    total_tokens = int(token_usage.get("total_tokens") or input_tokens + output_tokens)
    return input_tokens, output_tokens, total_tokens


class AgentRunTracker:
    """Small persistence wrapper for agent observability.

    Tracking should never break the assistant. Methods swallow their own
    failures after rolling back the tracking transaction.
    """

    def __init__(
        self,
        db: Session,
        *,
        company_id: str,
        trigger_type: str,
        input_summary: str,
        conversation_id: Optional[int] = None,
        objective_id: Optional[int] = None,
        model_provider: Optional[str] = None,
        model_name: Optional[str] = None,
        prompt_version: str = "assistant-v1",
        user_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        organization_company_id: Optional[int] = None,
        permission_scope: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ):
        self.db = db
        self.run: Optional[AgentRun] = None
        self.company_id = company_id
        self.trigger_type = trigger_type
        self.input_summary = input_summary
        self.conversation_id = conversation_id
        self.objective_id = objective_id
        self.model_provider = model_provider
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.user_id = user_id
        self.organization_id = organization_id
        self.organization_company_id = organization_company_id
        self.permission_scope = permission_scope
        self.request_id = request_id
        # Plain-Python mirror of the run PK, captured at flush so callers can read
        # the id without an expired-on-commit ORM access that would re-open an
        # autobegun transaction and hold a pooled connection.
        self._run_id: int | None = None
        self.degraded_reasons: list[str] = []

    def mark_degraded(self, reason: str) -> None:
        if reason not in self.degraded_reasons:
            self.degraded_reasons.append(reason)

    @property
    def run_id(self) -> Optional[int]:
        if self._run_id is not None:
            return self._run_id
        return self.run.id if self.run else None

    def start(self) -> Optional[AgentRun]:
        try:
            org_id = self.organization_id
            org_company_id = self.organization_company_id
            if org_id is None:
                org = organization_for_company(self.db, self.company_id)
                org_id = org.id if org else None
            if org_company_id is None and org_id is not None:
                org_company = organization_company_for_realm(self.db, organization_id=org_id, realm_id=self.company_id)
                org_company_id = org_company.id if org_company else None
            self.run = AgentRun(
                organization_id=org_id,
                organization_company_id=org_company_id,
                company_id=self.company_id,
                user_id=self.user_id,
                objective_id=self.objective_id,
                conversation_id=self.conversation_id,
                trigger_type=self.trigger_type,
                status="running",
                model_provider=self.model_provider,
                model_name=self.model_name,
                prompt_version=self.prompt_version,
                input_summary=_truncate_text(self.input_summary),
                permission_scope=_truncate_payload(self.permission_scope),
                request_id=self.request_id,
                started_at=datetime.utcnow(),
            )
            self.db.add(self.run)
            # flush() assigns the PK without ending the transaction (so we can
            # capture run.id while it is live); commit() then persists the row AND
            # releases the pooled connection. No db.refresh() — refresh re-reads an
            # expired-on-commit attribute, re-opening an autobegun transaction that
            # would idly hold a pooled connection through the rest of the run
            # build / model loop (the run-level analogue of the AC-2.1 step fix).
            self.db.flush()
            self._run_id = self.run.id
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            logger.warning("[AgentRun] start failed: %s", redact_to_string(str(exc), max_length=500))
            self.run = None
            self._run_id = None
            self.mark_degraded("run_start_failed")
        return self.run

    def finish(
        self,
        *,
        status: str = "succeeded",
        output_summary: Optional[str] = None,
        token_usage: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        if not self.run:
            return
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B3 — INV-STATE: run status
        # transitions are validated against the explicit state machine. A terminal run
        # (succeeded/completed/failed/failed_stale) is never silently overwritten — the
        # invalid transition is logged loudly and REFUSED, preserving the first terminal
        # outcome. Tracking stays fail-open for infra errors (the except below), but an
        # impossible transition is a caller bug, not an infra error.
        current = str(self.run.status or "")
        if not is_valid_run_transition(current, status):
            logger.error(
                "[AgentRun] REFUSED invalid status transition %s -> %s for run %s "
                "(terminal states are immutable; fix the caller)",
                current, status, self.run_id,
            )
            return
        try:
            steps = (
                self.db.query(AgentStep)
                .filter(AgentStep.agent_run_id == self.run.id)
                .all()
            )
            usage_payload = dict(token_usage or {})
            usage_payload["performance"] = summarize_steps(steps)
            estimated_cost = estimate_model_cost_usd(self.model_name, usage_payload)

            self.run.status = status
            self.run.output_summary = _truncate_text(output_summary)
            self.run.token_usage = _truncate_payload(usage_payload) if usage_payload else None
            self.run.cost_estimate = estimated_cost
            self.run.error = _safe_error_text(error)
            self.run.finished_at = datetime.utcnow()
            self._record_usage_event(usage_payload, estimated_cost)
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            self.mark_degraded("run_finish_failed")
            logger.warning("[AgentRun] finish failed: %s", redact_to_string(str(exc), max_length=500))

    def _record_usage_event(self, token_usage: dict[str, Any], estimated_cost: Any) -> None:
        if not self.run:
            return
        input_tokens, output_tokens, total_tokens = _token_counts(token_usage)
        if total_tokens <= 0:
            return
        org = organization_for_company(self.db, self.company_id)
        if not org:
            return
        org_company = organization_company_for_realm(self.db, organization_id=org.id, realm_id=self.company_id)
        self.db.add(TenantUsageEvent(
            organization_id=org.id,
            organization_company_id=org_company.id if org_company else None,
            company_id=self.company_id,
            agent_run_id=self.run.id,
            source="agent_run",
            provider=self.model_provider,
            model=self.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_estimate=estimated_cost,
            event_metadata={
                "trigger_type": self.trigger_type,
                "prompt_version": self.prompt_version,
            },
        ))

    def step(
        self,
        *,
        step_type: str,
        tool_name: Optional[str] = None,
        input_payload: Any = None,
    ) -> "AgentStepTracker":
        return AgentStepTracker(
            self.db,
            agent_run_id=self.run_id,
            step_type=step_type,
            tool_name=tool_name,
            input_payload=input_payload,
            observation_tracker=self,
        )


def annotate_run_usage(tracker: "AgentRunTracker", **extra: int | float | str | bool) -> None:
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R2 (round-2, PR #381 remediation
    # row): merge post-finish DATA annotations (claim-verification counts) into the run's
    # token_usage JSON. This is an annotation, never a state transition — status is not
    # touched, and the eval exporter reads these keys into eval records. Reassignment (not
    # in-place mutation) so the SQLAlchemy JSON column registers the change.
    run = getattr(tracker, "run", None)
    if run is None:
        return
    try:
        merged = dict(run.token_usage or {})
        merged.update(extra)
        run.token_usage = merged
        tracker.db.commit()
    except Exception as exc:  # CRAFT-OK: tracking is fail-open by design; annotation loss is logged, never breaks the answer path
        tracker.db.rollback()
        if callable(getattr(tracker, "mark_degraded", None)):
            tracker.mark_degraded("run_annotation_failed")
        logger.warning("[AgentRun] annotate_run_usage failed: %s", redact_to_string(str(exc), max_length=300))


# ─────────────────────────────────────────────────────────────────────────────
# W3 (plan 20260712-agentic-finance-os, GAP-3): validated transitions + durable
# checkpoints for mission resume. These EXTEND the state machine above with an
# audit + resume ledger (AgentRunCheckpoint, a run-child scoped transitively
# through agent_run_id like agent_steps). Like the trackers, they are fail-open —
# a checkpoint-write failure is logged and never breaks the run — with ONE
# exception: an INVALID state transition is a caller bug and is REFUSED (returns
# False) so a terminal run can never be silently reopened.
# ─────────────────────────────────────────────────────────────────────────────


def _next_checkpoint_sequence(db: Session, run_id: int) -> int:
    from ..models import AgentRunCheckpoint

    current_max = (
        db.query(func.max(AgentRunCheckpoint.sequence))  # TENANT-OK: run-child scoped via agent_run_id, transitively tenant-owned like agent_steps (no tenant column)
        .filter(AgentRunCheckpoint.agent_run_id == run_id)
        .scalar()
    )
    return int(current_max or 0) + 1


def transition_run(
    db: Session,
    run: "AgentRun",
    to_state: str,
    *,
    actor: str = "system",
    reason: str | None = None,
    state: Any = None,  # CRAFT-OK: dynamic per-mission resumable state snapshot (JSON), mirrors input_payload/output_payload in this file
    checkpoint_key: str | None = None,
) -> bool:
    """Validate + persist a run state transition, writing an audited checkpoint row.

    Returns True on a legal, persisted transition; False (with a loud log) on an
    illegal one — terminal states are immutable, so a late/duplicate transition is
    refused rather than overwriting the first terminal outcome. ``state`` (when
    given) is the resumable snapshot stored on the checkpoint for a later resume.
    """
    from ..models import AgentRunCheckpoint

    if run is None:
        return False
    current = str(run.status or "")
    if not is_valid_run_transition(current, to_state):
        logger.error(
            "[AgentRun] REFUSED invalid transition %s -> %s for run %s "
            "(terminal states are immutable; fix the caller)",
            current, to_state, getattr(run, "id", None),
        )
        return False
    try:
        run.status = to_state
        db.add(AgentRunCheckpoint(
            agent_run_id=run.id,
            sequence=_next_checkpoint_sequence(db, run.id),
            from_state=current or None,
            to_state=to_state,
            actor=actor,
            reason=_truncate_text(reason, 2000),
            checkpoint_key=checkpoint_key,
            state_json=_truncate_payload(state) if state is not None else None,
        ))
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.warning("[AgentRun] transition_run failed: %s", redact_to_string(str(exc), max_length=300))
        return False


def save_checkpoint(
    db: Session,
    run: "AgentRun",
    *,
    checkpoint_key: str,
    state: Any,  # CRAFT-OK: dynamic per-mission resumable state snapshot (JSON), mirrors input_payload/output_payload in this file
    actor: str = "system",
    reason: str | None = None,
) -> bool:
    """Persist a resumable savepoint WITHOUT changing run state (from==to==current).

    Use to record "this deterministic phase is done, here is the accumulated
    state" so a resumed run skips it. Fail-open: a write failure is logged, never
    raised, and the run proceeds (it just cannot resume that phase).
    """
    from ..models import AgentRunCheckpoint

    if run is None:
        return False
    current = str(run.status or "") or None
    try:
        db.add(AgentRunCheckpoint(
            agent_run_id=run.id,
            sequence=_next_checkpoint_sequence(db, run.id),
            from_state=current,
            to_state=current,
            actor=actor,
            reason=_truncate_text(reason, 2000),
            checkpoint_key=checkpoint_key,
            state_json=_truncate_payload(state) if state is not None else None,
        ))
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.warning("[AgentRun] save_checkpoint failed: %s", redact_to_string(str(exc), max_length=300))
        return False


def latest_checkpoint(db: Session, run_id: int) -> AgentRunCheckpoint | None:
    """The highest-sequence checkpoint for a run (its most recent durable point).
    Scoped by agent_run_id — the run is already tenant-owned (transitive scope)."""
    return (
        db.query(AgentRunCheckpoint)  # TENANT-OK: run-child scoped via agent_run_id, transitively tenant-owned like agent_steps (no tenant column)
        .filter(AgentRunCheckpoint.agent_run_id == run_id)
        .order_by(AgentRunCheckpoint.sequence.desc())
        .first()
    )


def completed_checkpoint_keys(db: Session, run_id: int) -> set[str]:
    """The set of named checkpoint keys already recorded for a run — a resumed run
    consults this to SKIP the deterministic phases it already finished."""
    from ..models import AgentRunCheckpoint

    rows = (
        db.query(AgentRunCheckpoint.checkpoint_key)  # TENANT-OK: run-child scoped via agent_run_id, transitively tenant-owned like agent_steps (no tenant column)
        .filter(
            AgentRunCheckpoint.agent_run_id == run_id,
            AgentRunCheckpoint.checkpoint_key.isnot(None),
        )
        .all()
    )
    return {row[0] for row in rows}


def resume_state(db: Session, run_id: int) -> Any:  # CRAFT-OK: dynamic per-mission resumable state snapshot (JSON), mirrors input_payload/output_payload in this file
    """The accumulated ``state_json`` of the latest checkpoint that carried one, or
    None if the run has no resumable state. This is what a restarted worker rehydrates."""
    from ..models import AgentRunCheckpoint

    row = (
        db.query(AgentRunCheckpoint)  # TENANT-OK: run-child scoped via agent_run_id, transitively tenant-owned like agent_steps (no tenant column)
        .filter(
            AgentRunCheckpoint.agent_run_id == run_id,
            AgentRunCheckpoint.state_json.isnot(None),
        )
        .order_by(AgentRunCheckpoint.sequence.desc())
        .first()
    )
    return row.state_json if row is not None else None


class AgentStepTracker:
    def __init__(
        self,
        db: Session,
        *,
        agent_run_id: Optional[int],
        step_type: str,
        tool_name: Optional[str] = None,
        input_payload: Any = None,
        observation_tracker: AgentRunTracker | None = None,
    ):
        self.db = db
        self.observation_tracker = observation_tracker
        self.agent_run_id = agent_run_id
        self.step_type = step_type
        self.tool_name = tool_name
        self.input_payload = input_payload
        self.step: Optional[AgentStep] = None
        # Plain-Python mirror of the step PK + status, captured while the row is
        # live so NO post-commit ORM attribute access (which would re-open an
        # autobegun transaction and pin a pooled connection across the model
        # network call) is needed to read either. ``step_id`` is consumed by the
        # model loop's per-step usage attribution; ``_status`` drives ``__exit__``.
        self.step_id: int | None = None
        self._status: str | None = None
        self._started_perf = 0.0

    def __enter__(self) -> "AgentStepTracker":
        self._started_perf = perf_counter()
        if not self.agent_run_id:
            return self
        try:
            self.step = AgentStep(
                agent_run_id=self.agent_run_id,
                step_type=self.step_type,
                tool_name=self.tool_name,
                input=_truncate_payload(self.input_payload),
                status="running",
                started_at=datetime.utcnow(),
            )
            self.db.add(self.step)
            # flush() assigns the PK (and emits the INSERT) WITHOUT ending the
            # transaction, so we can capture step.id while it is live; commit()
            # then persists the row AND releases the pooled connection. We do NOT
            # db.refresh() afterward — refresh re-reads an expired-on-commit
            # attribute, which re-opens an autobegun transaction that would stay
            # open across the subsequent model network call, idling a pooled
            # connection for the whole round-trip (AC-2.1).
            self.db.flush()
            self.step_id = self.step.id
            self._status = "running"
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            logger.warning("[AgentStep] start failed: %s", redact_to_string(str(exc), max_length=500))
            self.step = None
            self.step_id = None
            if self.observation_tracker:
                self.observation_tracker.mark_degraded("step_start_failed")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.finish(status="failed", error=str(exc))
        elif self.step and self._status == "running":
            self.finish(status="succeeded")
        return False

    def finish(self, *, status: str, output_payload: Any = None, error: Optional[str] = None) -> None:
        if not self.step:
            return
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B3 — INV-STATE step analog: a
        # terminal step (succeeded/failed/blocked) is never overwritten; refuse + log loudly.
        current = str(self._status or self.step.status or "")
        if not is_valid_step_transition(current, status):
            logger.error(
                "[AgentStep] REFUSED invalid status transition %s -> %s for step %s",
                current, status, self.step_id,
            )
            return
        try:
            self._status = status
            self.step.status = status
            self.step.output = _truncate_payload(output_payload)
            self.step.error = _safe_error_text(error)
            self.step.finished_at = datetime.utcnow()
            self.step.latency_ms = int((perf_counter() - self._started_perf) * 1000)
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            if self.observation_tracker:
                self.observation_tracker.mark_degraded("step_finish_failed")
            logger.warning("[AgentStep] finish failed: %s", redact_to_string(str(exc), max_length=500))

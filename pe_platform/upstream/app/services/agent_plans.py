# PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md)
"""
app/services/agent_plans.py
───────────────────────────
The tenant-guarded AgentPlan lifecycle service (plan 20260714-wave3-1-agent-plans,
chunk C — decisions 8/E9). Owns reads (get/list) and the human control surface
(cancel / pause / resume) over the WF-12 agent-authored checkpointed plans, plus
the approve-lane consult ``plan_status_blocks_execution`` (decision 8 defense in
depth — the claim in ``qbo_actions/transitions.py`` is the real race defense).

TRANSITION DISCIPLINE (E9): every plan-status transition here is a CLAIM-STYLE
conditional UPDATE (the F-07 pattern — never read-then-write). ``rowcount == 0``
means another actor decided the plan first; the caller gets a domain conflict
(the router maps it to 409), never a silent overwrite.

CANCEL ORDERING (decision 8): FIRST expire the plan's still-PENDING step
proposals atomically via the pinned chunk-B seam
``app.qbo_actions.transitions.expire_pending_plan_step_proposals`` (a concurrent
approve's ``claim_action_transition`` loses against the expiry flip, or wins and
keeps its individually-human-approved execution — the recorded residual), THEN
claim-flip the plan CANCELLED. Both ride ONE transaction: a lost plan claim
rolls the expiries back.

TENANT DISCIPLINE: every entry point tenant-asserts via the session-derived
membership checks (``check_entity_tenant_access`` / ``check_tenant_access`` —
app/dependencies.py); body/path scope is never trusted as authorization. The
service's OWN failures are ``AgentPlanError`` domain errors (the router maps
them to the single envelope) — it raises no HTTPException of its own.

WALKER SEAMS (decision 10 / E8): the walk enqueue is the pinned Celery task
``app.tasks.agent_plans.run_agent_plan_walk(plan_id: int, realm_id: str)``
(chunk B), imported lazily at call time so this module stays importable before
chunk B lands in the same wave; chunk-C tests mock both pinned seams.
"""
from __future__ import annotations

import importlib
import logging

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..dependencies import check_entity_tenant_access, check_tenant_access
from ..models import AgentPlan, AgentPlanStep, User
from ..services.tenant_companies import organization_for_company

logger = logging.getLogger(__name__)

# Claim-source vocabularies (decision 8 status machine — AgentPlan.STATUSES).
CANCELLABLE_FROM_STATUSES: tuple[str, ...] = ("ACTIVE", "PAUSED", "PENDING_APPROVAL")
PAUSABLE_FROM_STATUSES: tuple[str, ...] = ("ACTIVE",)
RESUMABLE_FROM_STATUSES: tuple[str, ...] = ("PAUSED",)

# A step whose parent plan sits in one of these states must NOT have its
# proposal executed at approval (decision 8 — the approve-lane consult).
EXECUTION_BLOCKING_PLAN_STATUSES: tuple[str, ...] = ("PAUSED", "CANCELLED", "EXPIRED", "FAILED")


class AgentPlanError(Exception):
    """Domain error raised by the agent-plan service.

    The router translates this into the one HTTP error envelope
    (``user_safe_http_exception``); the service stays FastAPI-unaware.
    ``status`` carries the intended HTTP status (409 state-conflict default —
    lost claims are conflicts, not bad input).
    """

    def __init__(self, message: str, *, error_code: str, status: int = 409) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status


# ── Pinned chunk-B seams (lazy — chunk B lands in the same wave) ──────────────

def _expire_pending_step_proposals(db: Session, plan_id: int) -> int:
    """Decision-8 seam: expire the plan's still-PENDING step proposals.

    Pinned signature (plan decision 8 / E2):
    ``app.qbo_actions.transitions.expire_pending_plan_step_proposals(db, plan_id) -> int``
    — each flip rides ``claim_action_transition`` (PENDING→EXPIRED), so a
    concurrent approve's claim and this expiry can never both win one row.
    Resolved at call time: the attribute lands with chunk B in this wave.
    """
    transitions = importlib.import_module("app.qbo_actions.transitions")
    return int(transitions.expire_pending_plan_step_proposals(db, plan_id))


def _enqueue_plan_walk(plan_id: int, realm_id: str) -> None:
    """Decision-10/E8 seam: enqueue one walk of the (re)activated plan.

    Pinned signature: ``app.tasks.agent_plans.run_agent_plan_walk(plan_id: int,
    realm_id: str)`` (a Celery task — enqueued via ``.delay``). Imported lazily:
    the module lands with chunk B in this wave; tests stub the module.
    """
    walk_module = importlib.import_module("app.tasks.agent_plans")
    walk_module.run_agent_plan_walk.delay(plan_id=plan_id, realm_id=realm_id)


# ── Claim-style plan transition (E9 — the F-07 pattern for AgentPlan) ─────────

def _claim_plan_transition(
    db: Session,
    *,
    plan_id: int,
    from_statuses: tuple[str, ...],
    to_status: str,
) -> bool:
    """Atomically flip one AgentPlan out of ``from_statuses`` into ``to_status``.

    Returns True iff THIS caller won (exactly one row moved). False = another
    actor (a concurrent cancel/pause, the walker's terminal flip, approval)
    decided the plan first — the caller must refuse, never proceed. Mirrors
    ``claim_action_transition`` (qbo_actions/transitions.py); the surrounding
    transaction owns the commit.
    """
    result = db.execute(  # FASTAPI-OK: in-function conditional UPDATE inside the request transaction, not module-import I/O
        update(AgentPlan)
        .where(AgentPlan.id == plan_id)
        .where(AgentPlan.status.in_(from_statuses))
        .values(status=to_status)
    )
    won = result.rowcount == 1
    if won:
        # Keep identity-mapped copies consistent with the DB write so the
        # caller's refresh-free reads see the new status (the F-07 shape).
        db.expire_all()
    return won


def _tenant_assert_plan(db: Session, current: User, plan_id: int, *, minimum_role: str) -> AgentPlan:
    """Resolve the plan by id under the session-derived membership gate.

    ``check_entity_tenant_access`` 404s a missing OR cross-tenant row
    (existence-hiding), 403s an insufficient role, and additively cross-checks
    the row's ``organization_id`` — the house row-resolves-tenant gate.
    """
    return check_entity_tenant_access(
        minimum_role,
        db=db,
        user=current,
        model=AgentPlan,
        entity_id=plan_id,
        tenant_attr="realm_id",
        not_found_detail="Agent plan not found",
    )


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_plans(
    db: Session,
    current: User,
    company_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[AgentPlan]:
    """The realm's plans, newest first (VIEWER; realm + org scoped).

    The realm filter derives from the membership-authorized ``company_id`` and
    the org filter from the realm's resolved Organization — never from body
    scope. An optional ``status`` narrows to one lifecycle state.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current)
    organization = organization_for_company(db, company_id)
    query = db.query(AgentPlan).filter(AgentPlan.realm_id == company_id)
    if organization is not None:
        query = query.filter(AgentPlan.organization_id == organization.id)
    if status:
        query = query.filter(AgentPlan.status == status)
    return query.order_by(AgentPlan.created_at.desc(), AgentPlan.id.desc()).limit(limit).all()


def get_plan(db: Session, current: User, plan_id: int) -> tuple[AgentPlan, list[AgentPlanStep]]:
    """One plan + its ordered steps (VIEWER; row-resolves-tenant gate)."""
    plan = _tenant_assert_plan(db, current, plan_id, minimum_role="VIEWER")
    steps = (
        db.query(AgentPlanStep)
        .filter(
            AgentPlanStep.plan_id == plan.id,
            AgentPlanStep.realm_id == plan.realm_id,
        )
        .order_by(AgentPlanStep.step_index.asc())
        .all()
    )
    return plan, steps


# ── The human control surface (EDITOR; claim-style — E9) ─────────────────────

def cancel_plan(db: Session, current: User, plan_id: int) -> tuple[AgentPlan, int]:
    """Cancel a plan (EDITOR). Returns ``(plan, expired_step_proposal_count)``.

    Decision-8 ordering: FIRST expire the plan's PENDING step proposals via the
    pinned ``claim_action_transition``-backed seam, THEN claim-flip the plan
    CANCELLED (conditional UPDATE from ACTIVE/PAUSED/PENDING_APPROVAL). A lost
    plan claim (rowcount 0 — a concurrent cancel or a terminal walker flip won)
    rolls the whole transaction back (the expiries included) and raises the 409
    domain conflict. Recorded residual (plan decision 8): a step proposal
    already APPROVED at cancel time still executes in the approver's synchronous
    request — it was individually human-approved.
    """
    plan = _tenant_assert_plan(db, current, plan_id, minimum_role="EDITOR")
    expired_count = _expire_pending_step_proposals(db, plan.id)
    if not _claim_plan_transition(
        db, plan_id=plan.id, from_statuses=CANCELLABLE_FROM_STATUSES, to_status="CANCELLED"
    ):
        db.rollback()
        raise AgentPlanError(
            "This plan can no longer be cancelled — another decision (a concurrent "
            "cancel, completion, failure, or expiry) already moved it out of a "
            "cancellable state.",
            error_code="agent_plan_not_cancellable",
            status=409,
        )
    db.commit()
    db.refresh(plan)
    return plan, expired_count


def pause_plan(db: Session, current: User, plan_id: int) -> AgentPlan:
    """Pause an ACTIVE plan (EDITOR; claim-style ACTIVE→PAUSED, rowcount 0 = 409)."""
    plan = _tenant_assert_plan(db, current, plan_id, minimum_role="EDITOR")
    if not _claim_plan_transition(
        db, plan_id=plan.id, from_statuses=PAUSABLE_FROM_STATUSES, to_status="PAUSED"
    ):
        db.rollback()
        raise AgentPlanError(
            "Only an ACTIVE plan can be paused — this plan is not currently ACTIVE.",
            error_code="agent_plan_not_pausable",
            status=409,
        )
    db.commit()
    db.refresh(plan)
    return plan


def resume_plan(db: Session, current: User, plan_id: int) -> AgentPlan:
    """Resume a PAUSED plan (EDITOR; claim-style PAUSED→ACTIVE, rowcount 0 = 409).

    On the won claim, one walk is enqueued via the pinned decision-10 task seam
    AFTER the commit (the 15-minute beat sweep would eventually re-walk an
    ACTIVE plan anyway; the enqueue just removes the wait).
    """
    plan = _tenant_assert_plan(db, current, plan_id, minimum_role="EDITOR")
    if not _claim_plan_transition(
        db, plan_id=plan.id, from_statuses=RESUMABLE_FROM_STATUSES, to_status="ACTIVE"
    ):
        db.rollback()
        raise AgentPlanError(
            "Only a PAUSED plan can be resumed — this plan is not currently PAUSED.",
            error_code="agent_plan_not_resumable",
            status=409,
        )
    db.commit()
    db.refresh(plan)
    _enqueue_plan_walk(plan.id, plan.realm_id)
    return plan


# ── The approve-lane consult (decision 8 defense in depth) ────────────────────

def plan_status_blocks_execution(
    db: Session, plan_step_id: int | None, *, realm_id: str
) -> bool:
    """True when the step's parent plan is PAUSED/CANCELLED/EXPIRED/FAILED.

    Consulted by the approve lanes (app/routers/actions.py) BEFORE executing a
    proposal that carries ``plan_step_id`` — defense in depth; the atomic
    PENDING→EXPIRED claim at cancel time is the real race defense. FAIL-CLOSED:
    a ``plan_step_id`` whose step or plan row no longer resolves returns True
    (an orphaned plan-step proposal cannot prove a live plan authorized it).
    ``plan_step_id is None`` (the non-plan lane) never blocks.

    ``realm_id`` is required and is the realm of the ProposedAction being approved
    (``action.company_id``); a step whose plan belongs to another realm resolves no row
    and therefore blocks, which is the fail-closed direction.
    """
    if plan_step_id is None:
        return False
    row = (
        db.query(AgentPlan.status)
        .join(AgentPlanStep, AgentPlanStep.plan_id == AgentPlan.id)
        # The realm is REQUIRED, not optional: agent_plans is an org-keyed guard table, so
        # the RLS policy alone cannot separate realms on it, and the step-id deref has no
        # ceiling of its own (AgentPlanStep.id is another table's PK, which does not bound
        # agent_plans). Binding the plan's own realm makes this fail closed for a
        # plan_step_id belonging to a sibling company rather than silently resolving it.
        .filter(AgentPlanStep.id == plan_step_id, AgentPlan.realm_id == realm_id)
        .first()
    )
    if row is None:
        logger.warning(
            "plan_status_blocks_execution: plan_step_id=%s resolves no step/plan row — blocking (fail-closed)",
            plan_step_id,
        )
        return True
    return row[0] in EXECUTION_BLOCKING_PLAN_STATUSES

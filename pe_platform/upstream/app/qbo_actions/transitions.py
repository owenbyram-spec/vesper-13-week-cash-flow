"""Atomic ProposedAction state-transition claim (F-07).

The approve/reject/execute lifecycle used to flip ``ProposedAction.status`` with
a read-then-write: a handler read ``status == "PENDING"`` and then, some lines
later, assigned ``status = "APPROVED"`` (or ``"REJECTED"``). Two concurrent
requests could both pass the read and both proceed — the dangerous case being a
concurrent approve+reject where the approve fires the QBO write while the reject
wins the status column, leaving a REJECTED proposal that actually posted.

``claim_action_transition`` closes that window with a single conditional UPDATE:
the database serializes the two writers on the row, exactly one flips
``PENDING`` → the target status, and the loser observes ``rowcount == 0`` and is
told (by the caller) that the action was already decided.

Why Core ``update()`` and not an ORM attribute assignment: an ORM
``action.status = "APPROVED"`` mutates the identity-mapped instance and would
mask the race (the in-session object always "wins" locally). The Core statement
goes straight to the DB and reports the true affected-row count, which is the
only authoritative signal of who won. It is an ordinary DML statement inside the
request's already-open (RLS-pinned) transaction, so the RLS policy predicate
ANDs in transparently, and ``UPDATE ... WHERE`` is portable across Postgres
(prod) and SQLite (test).
"""
# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import and_, update
from sqlalchemy.orm import Session

from ..models import AgentPlanStep, AuditLog, ProposedAction

logger = logging.getLogger(__name__)


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXPIRE (SYNTHESIS GAP-A3).
# ProposedAction.expires_at was display-only: no approve lane checked it and no sweep flipped
# stale rows, so a 7-day-stale draft stayed approvable+executable indefinitely against
# long-changed books. Every approve lane now refuses an expired proposal via this predicate
# (reject lanes deliberately still work — rejecting a stale draft is always safe).
def action_is_expired(action: ProposedAction, *, now: datetime | None = None) -> bool:
    expires_at = getattr(action, "expires_at", None)
    if expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        # SQLite (tests) stores naive datetimes despite the timezone=True column; compare in
        # naive UTC there. Postgres returns aware values and takes the aware branch.
        current = current.replace(tzinfo=None)
    return expires_at <= current


def claim_action_transition(
    db: Session,
    *,
    action_id: int,
    from_status: str,
    to_status: str,
    # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md) — modernized alias only; Optional import removed above.
    reviewed_by: str | None = None,
    expected_revision: int | None = None,
    approver_user_id: int | None = None,
    autonomous: bool = False,
) -> bool:
    """Atomically flip one ProposedAction from ``from_status`` to ``to_status``.

    Returns ``True`` iff THIS caller won the transition (exactly one row moved
    from ``from_status`` to ``to_status``). Returns ``False`` when the row was
    not in ``from_status`` — i.e. someone else already decided it, or it does
    not exist. The caller MUST branch on the return value and must NOT proceed
    to execute / schedule-mutate on ``False``.

    ``reviewed_by`` / ``reviewed_at`` are stamped in the SAME statement as the
    status flip so attribution and state move atomically together. The caller
    is responsible for committing (the win is flushed here so ``rowcount`` is
    authoritative, but the surrounding transaction owns the commit — matching
    the existing approve-lane ordering of commit-before-execute).
    """
    values = {"status": to_status, "reviewed_at": datetime.now(timezone.utc)}
    if reviewed_by is not None:
        values["reviewed_by"] = reviewed_by

    statement = update(ProposedAction).where(
        ProposedAction.id == action_id, ProposedAction.status == from_status,
    )
    if to_status == "APPROVED":
        from ..services.proposal_provenance import validate_approval, validate_autonomous_approval
        with db.no_autoflush:
            action = db.get(ProposedAction, action_id)
        if action is None or action.status != from_status:
            return False
        if autonomous:
            validate_autonomous_approval(action)
            if type(expected_revision) is not int or expected_revision != action.revision:
                return False
        else:
            validate_approval(action, approver_user_id=approver_user_id, expected_revision=expected_revision)
        # All provenance checks above are bound to this exact inspected revision.
        statement = statement.where(
            ProposedAction.company_id == action.company_id,
            ProposedAction.revision == action.revision,
            ProposedAction.provenance_version == action.provenance_version,
            ProposedAction.human_preparer_user_id == action.human_preparer_user_id,
            ProposedAction.last_human_editor_user_id == action.last_human_editor_user_id,
        )
    with db.no_autoflush:
        result = db.execute(statement.values(**values).execution_options(synchronize_session=False))
    won = result.rowcount == 1
    if won:
        # Keep any identity-mapped copy of this row consistent with the DB write
        # so a subsequent ``db.refresh``-free read in the same handler sees the
        # new status. expire_all is cheap here (single-row handlers) and avoids
        # a stale ORM instance masking the just-committed transition.
        db.expire_all()
    return won


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md on disk) — decisions 6/8: the PENDING→EXPIRED status
# flip. "EXPIRED" is a NET-NEW ProposedAction status value (the historical closed
# vocabulary was {PENDING, APPROVED, REJECTED, EXECUTED, FAILED}; ``expires_at``
# was only an approve-lane refusal predicate — ``action_is_expired`` above). It is
# reachable ONLY via ``claim_action_transition(from_status="PENDING", ...)``, so a
# proposal that was already APPROVED/EXECUTED can never be retro-expired: the
# claim's conditional UPDATE loses (rowcount 0) and the caller skips.
def expire_pending_plan_step_proposals(
    db: Session, plan_id: int, *, expired_by: str = "system:agent_plan"
) -> int:
    """Claim-expire every still-PENDING proposal drafted for ``plan_id``'s steps.

    THE PINNED SEAM (plan 20260714-wave3-1-agent-plans decision 8; chunk C mocks
    exactly this callable). Used by BOTH the walker's expiry observation and the
    cancel path. Ordering contract for cancel: call this FIRST, then claim-flip
    the plan CANCELLED — a step proposal a human concurrently APPROVED keeps its
    approval (the per-row PENDING→EXPIRED claim loses, rowcount 0, and this
    function skips it; the recorded decision-8 residual).

    Returns the number of proposals THIS caller expired. Each win writes an
    ``AuditLog`` row; the whole batch is committed here (the per-row claim is the
    atomic unit — the DB serializes each row's writers regardless of the commit).
    """
    rows = (
        db.query(ProposedAction.id, ProposedAction.company_id, ProposedAction.plan_step_id)
        .join(
            AgentPlanStep,
            and_(
                ProposedAction.plan_step_id == AgentPlanStep.id,
                # Realm-consistency predicate: a proposal only matches its OWN
                # step's realm (both sides are tenant-keyed; the join is the
                # tenant boundary — plan_id below scopes to one plan's steps).
                ProposedAction.company_id == AgentPlanStep.realm_id,
            ),
        )
        .filter(AgentPlanStep.plan_id == plan_id, ProposedAction.status == "PENDING")
        .all()
    )
    expired = 0
    for action_id, company_id, plan_step_id in rows:
        won = claim_action_transition(  # SA-OK: bounded-loop ≤5 write steps per plan (MAX_WRITE_STEPS); per-row conditional UPDATE IS the claim semantics — each row must win or lose individually against a concurrent approve
            db,
            action_id=action_id,
            from_status="PENDING",
            to_status="EXPIRED",
            reviewed_by=expired_by,
        )
        if not won:
            # A concurrent approve (or another expirer) already decided this row —
            # the loser must NOT proceed (claim contract above).
            logger.info(
                "[transitions] plan %s proposal %s already decided; expiry skipped",
                plan_id,
                action_id,
            )
            continue
        expired += 1
        db.add(
            AuditLog(
                realm_id=company_id,
                action="UPDATE",
                entity_type="ProposedAction",
                entity_id=str(action_id),
                user_id=expired_by,
                details={
                    "event": "agent_plan_step_proposal_expired",
                    "plan_id": plan_id,
                    "plan_step_id": plan_step_id,
                    "from_status": "PENDING",
                    "to_status": "EXPIRED",
                },
            )
        )
    db.commit()
    return expired

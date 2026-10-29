"""Committed objective outbox and owned completion. No automatic execution takeover."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from ..models import (
    AgentObjective,
    AgentObjectiveOccurrence,
    AgentRun,
    Organization,
    OrganizationCompany,
)
from .agent_schedule import compute_next_run_at
from .redaction import redact_sensitive

ACTIVE = ("pending", "running", "recovery_required")
TERMINAL = ("succeeded", "partial", "failed")
DEDICATED_TYPES = ("close_orchestration", "agent_plan")


def utcnow():
    return datetime.now(timezone.utc)


def _utc(value):
    return (
        (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        if value is not None
        else None
    )


def fingerprint(schedule):
    return hashlib.sha256(
        json.dumps(schedule or {}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def policy_supported(objective):
    return objective.approval_policy in (None, {}, {"external_writes": "never"})


def effective_policy(objective):
    return {
        "status": "supported" if policy_supported(objective) else "blocked",
        "financial_writes": "proposal_only",
        "external_writes": "never",
        "notifications": "disabled"
        if (objective.notification_policy or {}).get("email") is False
        else "enabled",
        "origin": "server",
    }


def occurrence_payload(row):
    if row is None:
        return None
    return {
        "id": row.id,
        "status": row.status,
        "dispatch_status": row.dispatch_status,
        "run_id": row.run_id,
        "recovery_required": row.status == "recovery_required",
        "reason": (row.outcome or {}).get("reason"),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


class OccurrenceConflict(RuntimeError):
    def __init__(self, row):
        self.detail = {
            "code": "objective_occurrence_active",
            "occurrence_id": row.id,
            "run_id": row.run_id,
            "status": row.status,
        }
        super().__init__("An objective occurrence requires completion or review")


def active_occurrence(db, objective_id):
    objective = (
        db.query(AgentObjective).filter(AgentObjective.id == objective_id).first()
    )
    if objective is None:
        return None
    return (
        db.query(AgentObjectiveOccurrence)
        .filter(
            AgentObjectiveOccurrence.source_objective_id == objective_id,
            AgentObjectiveOccurrence.company_id == objective.company_id,
            AgentObjectiveOccurrence.status.in_(ACTIVE),
        )
        .first()
    )


def _trusted_scope(db, objective):
    company = (
        db.query(OrganizationCompany)
        .filter(
            OrganizationCompany.id == objective.organization_company_id,
            OrganizationCompany.organization_id == objective.organization_id,
            OrganizationCompany.realm_id == objective.company_id,
        )
        .first()
    )
    org = (
        db.query(Organization)
        .filter(Organization.id == objective.organization_id)
        .first()
    )
    if not company or not org or not org.is_active or company.status != "active":
        raise PermissionError("objective_scope_unavailable")
    return company


def reserve_occurrence(db, objective_id, *, manual_key=None, now=None):
    """Own a short objective lock, persist intent, then release before publication/work."""
    now = now or utcnow()
    objective = (
        db.query(AgentObjective)
        .filter(AgentObjective.id == objective_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if objective is None:
        db.rollback()
        return None
    _trusted_scope(db, objective)
    if not policy_supported(objective):
        db.rollback()
        raise ValueError("unsupported_objective_policy")
    if objective.objective_type in DEDICATED_TYPES:
        db.rollback()
        raise ValueError("objective_requires_dedicated_entry")
    if manual_key is None:
        if (
            not objective.enabled
            or objective.next_run_at is None
            or _utc(objective.next_run_at) > _utc(now)
        ):
            db.rollback()
            return None
        key = "scheduled:" + _utc(objective.next_run_at).isoformat()
    else:
        if not isinstance(manual_key, str) or not 1 <= len(manual_key) <= 128:
            raise ValueError("invalid_idempotency_key")
        key = "manual:" + manual_key
    existing = (
        db.query(AgentObjectiveOccurrence)
        .filter(
            AgentObjectiveOccurrence.source_objective_id == objective.id,
            AgentObjectiveOccurrence.company_id == objective.company_id,
            AgentObjectiveOccurrence.identity_key == key,
        )
        .first()
    )
    if existing:
        db.commit()
        return existing
    active = active_occurrence(db, objective.id)
    if active:
        db.rollback()
        raise OccurrenceConflict(active)
    row = AgentObjectiveOccurrence(
        id=str(uuid.uuid4()),
        organization_id=objective.organization_id,
        organization_company_id=objective.organization_company_id,
        company_id=objective.company_id,
        objective_id=objective.id,
        source_objective_id=objective.id,
        identity_key=key,
        trigger_type="manual" if manual_key is not None else "schedule",
        due_at=objective.next_run_at,
        schedule_snapshot=dict(objective.schedule or {}),
        schedule_fingerprint=fingerprint(objective.schedule),
        enabled_snapshot=bool(objective.enabled),
        objective_snapshot={
            "id": objective.id,
            "type": objective.objective_type,
            "name": objective.name,
        },
        status="pending",
        dispatch_status="manual" if manual_key is not None else "pending",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(AgentObjectiveOccurrence)
            .filter(
                AgentObjectiveOccurrence.source_objective_id == objective_id,
                AgentObjectiveOccurrence.company_id == objective.company_id,
                AgentObjectiveOccurrence.identity_key == key,
            )
            .first()
        )
        if existing:
            return existing
        active = active_occurrence(db, objective_id)
        if active:
            raise OccurrenceConflict(active)
        raise
    return row


def publish_occurrence(db, row, publish):
    """An ambiguous broker result only repeats an already durable identity."""
    occurrence_id = row.id
    if row.status != "pending" or row.dispatch_status == "manual":
        return False
    # Persist attempt before leaving the transaction. A broker callback can see it.
    row.publish_attempts += 1
    db.commit()
    publish(occurrence_id)
    db.query(AgentObjectiveOccurrence).filter(
        AgentObjectiveOccurrence.id == occurrence_id
    ).update({"dispatch_status": "published"}, synchronize_session=False)
    db.commit()
    return True


def claim_occurrence(db, occurrence_id):
    row = (
        db.query(AgentObjectiveOccurrence)
        .filter(AgentObjectiveOccurrence.id == occurrence_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if row is None or row.status != "pending":
        db.commit()
        return None
    objective = (
        db.query(AgentObjective).filter(AgentObjective.id == row.objective_id).first()
    )
    if objective is None:
        raise PermissionError("objective_missing")
    _trusted_scope(db, objective)
    if (
        objective.organization_id,
        objective.organization_company_id,
        objective.company_id,
    ) != (row.organization_id, row.organization_company_id, row.company_id):
        raise PermissionError("occurrence_scope_mismatch")
    if not policy_supported(objective):
        raise ValueError("unsupported_objective_policy")
    token = str(uuid.uuid4())
    now = utcnow()
    run = AgentRun(
        organization_id=row.organization_id,
        organization_company_id=row.organization_company_id,
        company_id=row.company_id,
        objective_id=objective.id,
        user_id=objective.created_by_user_id,
        trigger_type=row.trigger_type,
        status="running",
        started_at=now,
        model_provider="system",
        prompt_version="objective-v2",
        input_summary=objective.name,
        permission_scope={
            "execution_mode": "fixed_runner",
            "writeback": "proposal_only",
            "occurrence_id": row.id,
        },
    )
    db.add(run)
    db.flush()
    # SQLite also needs a conditional claim, not only PostgreSQL's row lock.
    changed = (
        db.query(AgentObjectiveOccurrence)
        .filter(
            AgentObjectiveOccurrence.id == row.id,
            AgentObjectiveOccurrence.status == "pending",
            AgentObjectiveOccurrence.revision == row.revision,
        )
        .update(
            {
                "status": "running",
                "claim_token": token,
                "run_id": run.id,
                "started_at": now,
                "heartbeat_at": now,
                "attempts": row.attempts + 1,
                "revision": row.revision + 1,
            },
            synchronize_session=False,
        )
    )
    if changed != 1:
        db.rollback()
        return None
    db.commit()
    db.refresh(row)
    return row, objective, run, token, row.revision


def finish_occurrence(
    db, occurrence_id, *, token, revision, status, result=None, reason=None
):
    if status not in (*TERMINAL, "recovery_required"):
        raise ValueError("invalid_occurrence_outcome")
    # Lock order is objective -> occurrence everywhere that needs both locks.
    hint = (
        db.query(AgentObjectiveOccurrence)
        .filter(AgentObjectiveOccurrence.id == occurrence_id)
        .first()
    )
    if hint is None:
        return False
    objective = (
        db.query(AgentObjective)
        .filter(AgentObjective.id == hint.objective_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    row = (
        db.query(AgentObjectiveOccurrence)
        .filter(AgentObjectiveOccurrence.id == occurrence_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if (
        row.claim_token != token
        or row.revision != revision
        or row.status not in ("running", "recovery_required")
    ):
        db.rollback()
        return False
    now = utcnow()
    row.status = status
    row.outcome = {
        "status": status,
        "reason": reason,
        "result": redact_sensitive(result, max_length=2000),
    }
    row.finished_at = now if status in TERMINAL else None
    if status in TERMINAL:
        row.revision += 1
        if (
            objective
            and bool(objective.enabled) == row.enabled_snapshot
            and fingerprint(objective.schedule) == row.schedule_fingerprint
            and _utc(objective.next_run_at) == _utc(row.due_at)
        ):
            objective.last_run_at = now
            objective.next_run_at = (
                compute_next_run_at(row.schedule_snapshot, after=now)
                if objective.enabled
                else None
            )
    run = (
        db.query(AgentRun)
        .filter(
            AgentRun.id == row.run_id,
            AgentRun.company_id == row.company_id,
            AgentRun.organization_id == row.organization_id,
            AgentRun.organization_company_id == row.organization_company_id,
        )
        .first()
    )
    if run:
        run.status = {
            "partial": "partially_completed",
            "recovery_required": "requires_manual_reconciliation",
        }.get(status, status)
        run.finished_at = row.finished_at
        run.error = reason
    db.commit()
    return True


def result_status(result):
    """Recognized fixed-runner vocabulary; missing/unknown coverage is incomplete."""
    if not isinstance(result, dict):
        return "partial"
    reported = str(result.get("status", "")).lower()
    if reported in ("failed", "error", "expired"):
        return "failed"
    if (
        result.get("errors")
        or result.get("missing_signals")
        or result.get("indeterminate")
        or reported
        in (
            "partial",
            "partially_completed",
            "degraded",
            "uncertain",
            "stopped",
            "blocked",
            "unavailable",
            "missing_token",
            "could_not_verify",
        )
    ):
        return "partial"
    if reported == "skipped":
        return (
            "succeeded"
            if result.get("reason") in ("disabled", "not_configured", "closed_period")
            else "partial"
        )
    return (
        "succeeded"
        if reported
        in (
            "succeeded",
            "ok",
            "completed",
            "complete",
            "clean",
            "drafted",
            "pulled",
            "already_pulled",
            "attention_needed",
            "no_indicators_fired",
            "indicators_fired",
            "dispatched",
        )
        else "partial"
    )


def _stored_outcome(db, identity, *, reused, reason=None):
    row = (
        db.query(AgentObjectiveOccurrence)
        .filter(AgentObjectiveOccurrence.id == identity)
        .populate_existing()
        .first()
    )
    return {
        "status": row.status if row else "recovery_required",
        "occurrence_id": identity,
        "run_id": row.run_id if row else None,
        "reused": reused,
        "reason": reason or (row.outcome or {}).get("reason") if row else reason,
        "result": (row.outcome or {}).get("result") if row else None,
    }


def execute_occurrence(db, occurrence_id, execute):
    claimed = claim_occurrence(db, occurrence_id)
    if claimed is None:
        return _stored_outcome(db, occurrence_id, reused=True)
    row, objective, run, token, revision = claimed
    try:
        result = execute(
            db, objective, trigger_type=row.trigger_type, occurrence_run=run
        )
        status = result_status(result)
        finished = finish_occurrence(
            db,
            occurrence_id,
            token=token,
            revision=revision,
            status=status,
            result=result,
        )
        return _stored_outcome(
            db,
            occurrence_id,
            reused=False,
            reason=None if finished else "completion_ownership_lost",
        )
    except Exception:
        db.rollback()
        # Runner entry is the uncertainty boundary. Effects may already exist.
        finish_occurrence(
            db,
            occurrence_id,
            token=token,
            revision=revision,
            status="recovery_required",
            reason="runner_completion_unknown",
        )
        return _stored_outcome(db, occurrence_id, reused=False)


def mark_stale_occurrences(db, *, now=None):
    cutoff = (now or utcnow()) - timedelta(minutes=30)
    rows = (
        db.query(AgentObjectiveOccurrence)
        .filter(
            AgentObjectiveOccurrence.status == "running",
            AgentObjectiveOccurrence.heartbeat_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    for row in rows:
        # Keep claim/revision: the still-running owner alone can report actual completion.
        row.status = "recovery_required"
        row.outcome = {"reason": "progress_unobserved_30_minutes"}
    db.commit()
    return len(rows)

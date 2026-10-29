# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
"""Celery tasks for the agent-plan walker (Wave 3.1 chunk B — decisions 7/10).

``run_agent_plan_walk(plan_id: int, realm_id: str)`` is THE PINNED SIGNATURE
(decision 10 E8): the AGENT_PLAN approval handler (chunk D) enqueues it, and the
15-minute ``process-active-agent-plans`` beat sweep re-enqueues it for every
still-ACTIVE plan (covers park re-polls). Terminal plans (COMPLETE / FAILED /
CANCELLED / EXPIRED) stop matching the sweep — a finished plan never re-fires.

Reliability: rides the app-level acks_late + reject_on_worker_lost pair
(celery_app.py). At-least-once delivery is safe here because the walk is
idempotent by construction — the derived step cursor skips terminal steps, the
``uq(plan_step_id, inputs_fingerprint)`` find-or-create makes a redelivered
draft adopt instead of double-draft, and the per-realm ``AgentPlanRunLock``
lease makes a concurrent redelivery exit clean (``run_lock_held``). Budget
exhaustion is TERMINAL — a Celery retry would open a fresh AgentRun (fresh
run_id → fresh budget window) and re-spend, so it is never retried.
"""
from __future__ import annotations

import logging
import random
from typing import Any  # CRAFT-OK: dynamic task summary-dict boundary, mirrors app/tasks/agent.py

from celery.exceptions import MaxRetriesExceededError

from ..agent.tracking import AgentRunTracker
from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import AgentPlan
from ..services.redaction import redact_to_string
from ..services.tenant_companies import organization_company_for_realm, organization_for_company
from ..tenant_rls import tenant_scope

logger = logging.getLogger(__name__)


def _build_registry():
    """Lazy import: the assistant handler graph is heavy; import at call time keeps
    app.tasks.agent_plans import-light (the close-orchestration precedent)."""
    from ..agent.runtime.tool_registry import build_assistant_tool_registry
    from ..assistant.handlers import TOOL_HANDLERS
    from ..assistant.tools import ASSISTANT_TOOLS

    return build_assistant_tool_registry(ASSISTANT_TOOLS, TOOL_HANDLERS)


def _walk_summary(result: dict[str, Any]) -> str:  # CRAFT-OK: defensive read of the walker's dynamic summary dict
    status = result.get("status")
    if status == "skipped":
        return f"Plan walk skipped ({result.get('reason') or 'unknown reason'})."
    if status in ("succeeded", "stopped", "expired", "failed"):
        return (
            f"Plan walk {status}: walked {int(result.get('walked') or 0)} step(s), "
            f"completed {int(result.get('completed') or 0)}, "
            f"drafted/polled {int(result.get('drafted_or_polled') or 0)}, "
            f"plan status {result.get('plan_status') or result.get('reason') or 'ACTIVE'}."
        )
    return f"Plan walk finished (status={status})."


@celery_app.task(bind=True, max_retries=2, name="app.tasks.agent_plans.run_agent_plan_walk")
def run_agent_plan_walk(self, plan_id: int, realm_id: str) -> dict[str, Any]:  # CRAFT-OK: dynamic summary-dict return, mirrors run_agent_objective
    """Walk one ACTIVE agent plan (THE PINNED SIGNATURE — decision 10 E8).

    Own SessionLocal in try/finally; tenant scope resolved from the given realm
    and pinned for the whole walk; packet + tracker built here; the walker does
    the rest. Idempotent under acks_late redelivery (module docstring).
    """
    from ..agent.runtime.plan_walker import PlanBudgetExceededError, build_plan_context, walk_agent_plan, _release_plan_lock

    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() below guarantees close
    try:
        with tenant_scope(bypass=True):
            org = organization_for_company(db, realm_id)  # CROSS-TENANT-OK: resolve the realm's owning org for the scope pin below; all work re-scoped
        org_id = org.id if org else None
        with tenant_scope(org_id=org_id, realm_id=realm_id):
            plan = (
                db.query(AgentPlan)
                .filter(AgentPlan.id == plan_id, AgentPlan.realm_id == realm_id)
                .first()
            )
            if plan is None:
                return {"status": "skipped", "reason": "not_found", "plan_id": plan_id}
            if plan.status != "ACTIVE":
                # Terminal/paused plans never walk — the sweep is quiescent on them too.
                return {"status": "skipped", "reason": f"plan_status_{plan.status.lower()}", "plan_id": plan_id}
            org_company = (
                organization_company_for_realm(db, organization_id=org_id, realm_id=realm_id)
                if org_id is not None
                else None
            )
            registry = _build_registry()
            tracker = AgentRunTracker(
                db,
                company_id=realm_id,
                trigger_type="agent_plan",
                input_summary=f"agent-plan walk #{plan_id}: {plan.title}",
                model_provider="system",
                prompt_version="agent-plan-v1",
                organization_id=org_id,
                organization_company_id=org_company.id if org_company else None,
            )
            tracker.start()
            packet = build_plan_context(
                db, plan, registry,
                run_id=tracker.run_id,
                organization_id=org_id,
                organization_company_id=org_company.id if org_company else None,
            )
            try:
                result = walk_agent_plan(db, plan, registry, packet, tracker)
            except PlanBudgetExceededError as exc:
                tracker.finish(status="failed", error=str(exc))
                raise
            except Exception as exc:
                tracker.finish(status="failed", error=str(exc))
                raise
            else:
                walk_status = result.get("status")
                status = "failed" if walk_status in ("failed", "expired") else "partially_completed" if walk_status in ("stopped", "skipped") else "succeeded"
                tracker.finish(status=status, output_summary=_walk_summary(result))
                return {"status": status, "plan_id": plan_id, "result": result}
            finally:
                _release_plan_lock(db, realm_id, tracker.run_id)

    except PlanBudgetExceededError:
        # TERMINAL, never retried — a retry opens a fresh AgentRun (fresh run_id →
        # fresh per-run budget) and would re-spend. The walk already failed the
        # plan, wrote the WatchdogAlert finding, and released the realm lease.
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        try:
            # Transient failure → exponential backoff + jitter (house pattern) so
            # concurrent failures don't retry in lockstep against a struggling
            # downstream; the resumable derived cursor makes the retry safe.
            backoff = min(600, 60 * (2 ** self.request.retries))
            countdown = backoff + random.uniform(0, backoff * 0.25)
            raise self.retry(exc=exc, countdown=countdown)  # CRAFT-OK: self.retry(exc=exc) carries the cause and itself raises before any `from` clause could apply
        except MaxRetriesExceededError:
            # Retries exhausted: surface the terminal failure loudly. The sweep
            # will re-enqueue on its next tick while the plan remains ACTIVE.
            raise
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="app.tasks.agent_plans.process_active_agent_plans")
def process_active_agent_plans(self, limit: int = 25) -> dict[str, str | int]:
    """Beat sweep (decision 7): re-enqueue a walk for every ACTIVE plan.

    Covers park re-polls (awaiting_approval / blocked_retryable / bounded_daily)
    between approvals. The status filter is the quiescence guarantee: terminal
    plans (COMPLETE / FAILED / CANCELLED / EXPIRED) and PAUSED/DRAFT plans never
    match, so a finished plan never re-fires. Idempotent under redelivery: the
    child walk is lease-guarded and skips non-ACTIVE plans on arrival.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive page size")
    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() below guarantees close
    try:
        # CROSS-TENANT SWEEP (dispatcher): spans all tenants under bypass; each
        # dispatched run_agent_plan_walk re-scopes itself to its own realm.
        with tenant_scope(bypass=True):
            from sqlalchemy import func
            upper_bound = db.query(func.max(AgentPlan.id)).filter(AgentPlan.status == "ACTIVE").scalar()
            cursor = 0
            queued = checked = 0
            while upper_bound is not None and cursor < upper_bound:
                rows = (db.query(AgentPlan.id, AgentPlan.realm_id)
                    .filter(AgentPlan.status == "ACTIVE", AgentPlan.id > cursor, AgentPlan.id <= upper_bound)
                    .order_by(AgentPlan.id.asc()).limit(limit).all())
                if not rows:
                    break
                checked += len(rows)
                for pid, realm in rows:
                    run_agent_plan_walk.delay(pid, realm)
                    queued += 1
                cursor = rows[-1][0]
        return {"status": "done", "checked": checked, "queued": queued}
    except Exception as exc:
        db.rollback()
        logger.error("[agent_plans] sweep failed: %s", redact_to_string(str(exc), max_length=500))
        backoff = min(600, 60 * (2 ** self.request.retries))
        countdown = backoff + random.uniform(0, backoff * 0.25)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        db.close()

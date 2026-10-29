# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1
"""Stuck-close-run watchdog (F-1, wave W-1 item 4).

A close-orchestration walk is LONG-RUNNING BY DESIGN (a 40+-step checklist),
so the generic 15-minute ``sweep_stale_agent_runs`` deliberately excludes it
(app/tasks/agent.py). This hourly beat task owns the close runs instead: any
close ``AgentRun`` still queued/running with NO step progress for more than
``CLOSE_RUN_STALE_SECONDS`` (default 2h) is reaped —

  1. the run is marked ``failed_stale`` (terminal; the cockpit's run status
     passes through verbatim) with a FAILED_STALE error message,
  2. a critical ``WatchdogAlert`` (``CLOSE_RUN_STALE``) surfaces it, and
  3. the realm's close single-flight lease (``close_run_locks``) is released
     so the next close for that realm can start immediately instead of
     waiting out the lease TTL.

"Step progress" is the lease heartbeat: the walk bumps ``heartbeat_at`` on
every per-step commit (``_mark_progress``), so a live-but-slow close is never
reaped while a wedged/dead one always is. Idempotent under acks_late
at-least-once delivery: the status flip is conditional on the run still being
queued/running, and both the alert write and the lease delete are re-run-safe.
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import AgentObjective, AgentRun, CloseRunLock, WatchdogAlert
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope

logger = logging.getLogger(__name__)

# No-step-progress cutoff for a close run (seconds). Sized WELL above both a
# normal close walk's per-step latency and the lease TTL (close_lock_ttl_seconds,
# 30 min) so the watchdog only ever reaps a run the lease machinery already
# considers dead. Env-overridable for ops tuning.
CLOSE_RUN_STALE_SECONDS = int(os.getenv("CLOSE_RUN_STALE_SECONDS", "7200"))

FAILED_STALE_STATUS = "failed_stale"
FAILED_STALE_ERROR = (
    "FAILED_STALE: close run reaped by the close watchdog "
    f"(no step progress for over {CLOSE_RUN_STALE_SECONDS // 60} minutes)."
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a loaded timestamp to aware-UTC (SQLite returns naive)."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@celery_app.task(bind=True, max_retries=2, name="app.tasks.close_watchdog.reap_stale_close_runs")
def reap_stale_close_runs(self) -> dict[str, Any]:  # CRAFT-OK: dynamic task summary-dict return, mirrors the sibling sweeps in app/tasks/agent.py
    """Reap close runs stuck IN_PROGRESS with no step progress for >2h."""
    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() below guarantees close
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=CLOSE_RUN_STALE_SECONDS)
        cutoff_naive = cutoff.replace(tzinfo=None)
        reaped = 0
        # SYSTEM SWEEP across ALL tenants: a stuck close blocks its realm's
        # closes regardless of org, so the reaper must see every tenant.
        with tenant_scope(bypass=True):
            close_objective_ids = [
                row[0]
                for row in db.query(AgentObjective.id)  # CROSS-TENANT-OK: close-objective census for the system reaper; vetted backstop, not a per-run path
                .filter(AgentObjective.objective_type == "close_orchestration")
                .all()
            ]
            if not close_objective_ids:
                return {"status": "done", "reaped": 0}

            candidates = (
                db.query(AgentRun)  # CROSS-TENANT-OK: system stale-close reaper spans every tenant; vetted backstop, not a per-run path
                .filter(
                    AgentRun.objective_id.in_(close_objective_ids),
                    AgentRun.status.in_(["queued", "running"]),
                    AgentRun.created_at < cutoff_naive,
                )
                .all()
            )

            for run in candidates:
                try:
                    lease = (
                        db.query(CloseRunLock)
                        .filter(CloseRunLock.realm_id == run.company_id)
                        .first()
                    )
                    # Step progress = OUR run's lease heartbeat within the
                    # cutoff. A lease held by a DIFFERENT run means this run
                    # already lost the realm (stolen after TTL) — reap it.
                    heartbeat = _as_utc(lease.heartbeat_at) if lease is not None else None
                    if (
                        lease is not None
                        and lease.agent_run_id == run.id
                        and heartbeat is not None
                        and heartbeat >= cutoff
                    ):
                        continue  # still progressing — leave it alone

                    # 1. Terminal status flip, conditional so a redelivery /
                    # concurrent reaper cannot re-process an already-flipped row.
                    flipped = (
                        db.query(AgentRun)
                        .filter(
                            AgentRun.id == run.id,
                            AgentRun.status.in_(["queued", "running"]),
                        )
                        .update(
                            {
                                AgentRun.status: FAILED_STALE_STATUS,
                                AgentRun.error: FAILED_STALE_ERROR,
                                AgentRun.finished_at: datetime.utcnow(),
                            },
                            synchronize_session=False,
                        )
                    )
                    if not flipped:
                        db.commit()
                        continue

                    # 2. Loud surface: a critical watchdog alert for the realm.
                    db.add(
                        WatchdogAlert(
                            company_id=run.company_id,
                            alert_type="CLOSE_RUN_STALE",
                            severity="critical",
                            title="Close run stalled and was reaped",
                            description=(
                                f"Close run {run.id} made no step progress for over "
                                f"{CLOSE_RUN_STALE_SECONDS // 60} minutes and was marked "
                                "failed_stale. The realm's close lock has been released; "
                                "the close can be restarted and will resume from the last "
                                "completed step without re-spending finished work."
                            ),
                            context={
                                "agent_run_id": run.id,
                                "objective_id": run.objective_id,
                                "realm_id": run.company_id,
                                "stale_seconds": CLOSE_RUN_STALE_SECONDS,
                            },
                        )
                    )

                    # 3. Release the realm lease (only the reaped run's own row —
                    # never a successor's freshly stolen lease).
                    if lease is not None and lease.agent_run_id == run.id:
                        db.query(CloseRunLock).filter(
                            CloseRunLock.realm_id == run.company_id,
                            CloseRunLock.agent_run_id == run.id,
                        ).delete(synchronize_session=False)

                    db.commit()
                    reaped += 1
                    logger.warning(
                        "[close_watchdog] reaped stale close run %s (realm=%s)",
                        run.id,
                        run.company_id,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad realm must not kill the sweep
                    db.rollback()
                    logger.error(
                        "[close_watchdog] failed to reap close run %s: %s",
                        run.id,
                        redact_to_string(str(exc), max_length=300),
                    )

        return {"status": "done", "reaped": reaped}
    except Exception as exc:
        db.rollback()
        logger.error(
            "[close_watchdog] sweep failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
        # Exponential backoff + jitter (AP-CEL-4) so a transient DB blip does
        # not retry in a tight constant-cadence loop.
        backoff = min(600, 60 * (2 ** self.request.retries))
        countdown = backoff + random.uniform(0, backoff * 0.25)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        db.close()

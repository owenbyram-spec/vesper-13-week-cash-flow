"""Close-orchestration objective driver (Phase B, C3).

A resumable, single-flight, **proposal-first** step-walker over a realm's
``CloseTask`` checklist. Registered as the 7th ``OBJECTIVE_RUNNERS`` entry
(``"close_orchestration"``) in ``app/tasks/agent.py`` and run inside the existing
``AgentRunTracker`` (which commits per step — the durability the resume relies on).

The control flow (DEC-CAR-B-1/2/5, the SYNTHESIS Proposed-pattern + the four
critical-review conditions C1–C4):

1.  **Per-realm run-lock (single-flight, TTL lease — W-1 F-1).** A
    ``close_run_locks`` DB lease keyed on the realm is claimed BEFORE the walk
    (C2) and heartbeat-bumped on every per-step commit. ``acks_late``
    redelivery (the resume trigger) can start a second copy of the same task
    concurrently; the lease makes the walk genuinely single-flight per realm.
    Not acquired → exit clean. A lease whose heartbeat is older than the TTL
    (``settings.close_lock_ttl_seconds``, default 30 min) is STEALABLE — so a
    dead or wedged session can never hold a realm past the TTL (the old
    session-level advisory lock's failure mode), and the close watchdog
    (``app/tasks/close_watchdog.py``) can release it when reaping a stale run.

2.  **Resume cursor = CloseTask business state (DEC-CAR-B-2).** The realm's
    ``CloseTask`` rows ordered by ``sort_order``; SKIP any row whose ``status`` is
    ``COMPLETE``/``SKIPPED`` OR whose ``verified_at`` is already set (this driver
    marked it on a prior run). Derived from business state → can't drift,
    self-heals between runs.

3.  **Per step:** ``task_type → tool`` → a realm-pinned ``ContextPacket`` → the
    executor reads the review tool → a bounded finding.

4.  **Guard BEFORE proposing (C4, the #1 build AC).** ``period_is_hard_locked`` is
    consulted (it self-pins realm scope, fail-closed) for the close task's own
    period; if HARD-locked, NO proposal — an ``AuditLog`` refusal row is written,
    ``verified_at`` + a "skipped: period locked" message are set, and the walk
    continues. D-B6-1 (2026-07-16): the guard keys off the EXPLICIT hard lock, so the
    close driver keeps working a soft-CLOSED (auto-rolled) month until the human locks
    it — coherent with "nothing auto-closes; only the human locks."

5.  **One PENDING ProposedAction per step,** carrying the C0 step-link columns
    (``close_task_id`` / ``agent_run_id`` / ``step_index`` / ``inputs_fingerprint``)
    and the evidence in ``payload``. Idempotent: the
    ``uq(close_task_id, inputs_fingerprint)`` constraint makes a duplicate
    physically impossible — an ``IntegrityError`` (a kill-after-propose redelivery)
    is caught and treated as already-done, no double-book. The driver NEVER calls
    a GL-writing handler directly — every mutation is a PENDING proposal a human
    approves later.

6.  **Mark agent progress, not human-complete.** ``verified_at`` +
    ``verification_message`` are set (committed per step so resume sees it). The
    ``status`` is left ``OPEN`` — humans flip it to ``COMPLETE`` on approval
    (proposal-first).

Safety note: this is a SYSTEM Celery driver, NOT a model-invocable agent tool —
there is no LLM tool-loop here, the driver itself is the trusted principal, and it
writes ONLY a PENDING ``ProposedAction`` (never a direct GL write). The behavioral
proposal-first / resume / guard eval coverage is plan chunk C4.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any  # CRAFT-OK: dynamic tool-output + objective-runner summary boundary (mirrors the existing OBJECTIVE_RUNNERS Dict[str, Any] contract in agent.py)

from sqlalchemy.exc import IntegrityError

from ...config import settings
from ...models import AgentRun, AuditLog, CloseRunLock, CloseTask, ProposedAction, WatchdogAlert
from ...services.llm_task_context import (  # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1
    CloseBudgetExceededError,
    bind_llm_task_context,
)
from ...services.redaction import redact_to_string
from ...services.task_autonomy import period_is_hard_locked
from ...services.review_checks.protocol import PROGRAMMING_ERRORS
from ..tracking import AgentRunTracker, RUN_TERMINAL_STATES
from .tool_policy import ToolPolicyGate, ToolPolicyViolation
from .tool_registry import build_assistant_tool_registry
from .tool_executor import ToolExecutor, sanitize_payload
from .types import AgentIntent, ContextPacket, OutputMode, RiskLevel

logger = logging.getLogger(__name__)


# task_type → the READ-ONLY review tool that produces the step's finding.
# These tools are all READ_ONLY, VIEWER-role, in the CLOSE_MANAGEMENT bundle
# (tool_registry.py — the four originals plus get_ar_ap_summary / get_executive_briefing
# added for AC-4a) so the realm-pinned CLOSE_MANAGEMENT ContextPacket below passes the
# policy gate. Every value here MUST be an EXISTING READ_ONLY/VIEWER registry tool in
# the bundle — adding a brand-new agent tool is a separate policy-reviewed change.
# task_types that have no review tool (BANK_RECONCILIATION, APPROVE_JES, LOCK_PERIOD,
# GENERATE_REPORT, CUSTOM, …) are skipped for proposal-generation: the driver still
# marks verified_at so resume advances past them, but proposes nothing.
TASK_TYPE_TO_TOOL: dict[str, str] = {
    "REVIEW_ANOMALIES": "get_intelligence_snapshot",
    "REVIEW_COVENANTS": "get_covenants_status",
    "REVIEW_BUDGET_VARIANCE": "get_budget_vs_actuals",
    "REVIEW_WEEKLY": "get_weekly_review",
    # AC-4a — two added READ-ONLY review handlers, each mapped to an EXISTING
    # READ_ONLY/VIEWER tool now in the CLOSE_MANAGEMENT bundle. get_ar_ap_summary
    # is the read-only combined AR/AP snapshot; get_executive_briefing is the
    # read-only cached/live briefing. Both reuse the SAME proposal-first, period-
    # guarded, fingerprint-idempotent walk as the four originals (no new code path).
    "REVIEW_AR_AP_AGING": "get_ar_ap_summary",
    "REVIEW_EXEC_BRIEFING": "get_executive_briefing",
}

# ── Per-realm single-flight TTL lease (W-1 F-1; replaces the advisory lock) ──
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1
#
# The old session-level pg advisory lock auto-released on a DEAD connection but
# had no defense against a stuck-but-ALIVE session: a wedged walk held its
# realm indefinitely. The DB lease (close_run_locks, one row per realm) is
# portable (SQLite tests drive the same code path), heartbeat-refreshed on
# every per-step commit, STEALABLE once the heartbeat is older than
# settings.close_lock_ttl_seconds, and releasable by the close watchdog — a
# dead OR wedged session can never hold a realm past the TTL. Even in the rare
# steal-while-wedged overlap, the walk stays safe: the verified_at resume
# cursor + the uq(close_task_id, inputs_fingerprint) proposal constraint make
# a double-walk unable to double-book.


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a loaded timestamp to aware-UTC (SQLite returns naive)."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _try_acquire_realm_lock(db, realm_id: str, run_id: int | None) -> bool:
    """Claim (or steal) the realm's single-flight close lease. Non-blocking.

    Returns False when a LIVE walk (heartbeat within the TTL) holds the lease —
    the caller exits clean rather than walking the same checklist twice
    (``acks_late`` redelivery). Race arms:
      * no row → INSERT; a concurrent inserter loses via the PK IntegrityError.
      * stale row (heartbeat older than the TTL) → optimistic CAS UPDATE keyed
        on the observed heartbeat; of two stealers exactly one matches a row.
    """
    now = datetime.now(timezone.utc)
    lease = db.query(CloseRunLock).filter(CloseRunLock.realm_id == realm_id).first()
    if lease is None:
        db.add(
            CloseRunLock(
                realm_id=realm_id, agent_run_id=run_id, locked_at=now, heartbeat_at=now
            )
        )
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()  # a sibling won the PK race — it holds the lease
            return False

    heartbeat = _as_utc(lease.heartbeat_at)
    ttl = timedelta(seconds=int(settings.close_lock_ttl_seconds))
    if heartbeat is not None and (now - heartbeat) < ttl:
        return False  # a live walk holds the lease

    # Stale lease: steal via an optimistic CAS on the observed heartbeat so two
    # concurrent stealers cannot both win (the second matches zero rows).
    observed_heartbeat = lease.heartbeat_at
    stolen = (
        db.query(CloseRunLock)
        .filter(
            CloseRunLock.realm_id == realm_id,
            CloseRunLock.heartbeat_at == observed_heartbeat,
        )
        .update(
            {
                CloseRunLock.agent_run_id: run_id,
                CloseRunLock.locked_at: now,
                CloseRunLock.heartbeat_at: now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if stolen:
        logger.warning(
            "[close_orchestration] stole a stale close lease (realm=%s, heartbeat older than %ss)",
            realm_id,
            settings.close_lock_ttl_seconds,
        )
    return bool(stolen)


def _heartbeat_realm_lock(db, *, realm_id: str, run_id: int | None) -> None:
    """Bump the lease heartbeat for OUR run (persisted by the caller's commit).

    Filtered on ``agent_run_id == run_id`` so a walk whose lease was stolen can
    never extend the successor's lease. Best-effort: a heartbeat failure must
    not kill the walk (the TTL then simply reclaims the realm sooner).
    """
    try:
        db.query(CloseRunLock).filter(
            CloseRunLock.realm_id == realm_id,
            CloseRunLock.agent_run_id == run_id,
        ).update(
            {CloseRunLock.heartbeat_at: datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    except Exception as exc:  # noqa: BLE001 — heartbeat is best-effort by design
        logger.warning(
            "[close_orchestration] lease heartbeat failed (realm=%s): %s", realm_id, exc
        )


def _release_realm_lock(db, realm_id: str, run_id: int | None) -> None:
    """Delete OUR lease row (finally-path). Never raises.

    Rolls back first: on an exception exit the session may hold a failed
    transaction, and every durable step outcome was already committed per step
    (``_mark_progress``) or in ``_fail_budget_exceeded``. Filtered on
    ``agent_run_id == run_id`` so we never delete a successor's stolen lease.
    A failed release is non-fatal — the TTL reclaims the realm.
    """
    try:
        db.rollback()
        db.query(CloseRunLock).filter(
            CloseRunLock.realm_id == realm_id,
            CloseRunLock.agent_run_id == run_id,
        ).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — release is finally-path, must not mask the walk's error
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "[close_orchestration] lease release failed (realm=%s; TTL %ss will reclaim): %s",
            realm_id,
            settings.close_lock_ttl_seconds,
            exc,
        )


def _period_to_when(period: str | None) -> datetime | None:
    """Map a CloseTask ``period`` ("YYYY-MM") to a datetime on the 1st of it.

    ``period_is_hard_locked`` keys on ``when.strftime("%Y-%m")`` (task_autonomy.py),
    so the close task's OWN period (not "now") is what the guard must check. A
    malformed/absent period returns None → the caller passes ``when=None`` and the
    guard falls back to the current period (still fail-closed on error).
    """
    if not period:
        return None
    try:
        year_str, month_str = period.split("-", 1)
        return datetime(int(year_str), int(month_str), 1, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _inputs_fingerprint(*, close_task_id: int, task_type: str, tool_name: str, finding: str) -> str:
    """A stable SHA-256 over the step's load-bearing inputs (dedupe key).

    Folds the close task id, its task_type, the tool used, and the finding text.
    The same task + the same finding ⇒ the same fingerprint ⇒ the
    ``uq(close_task_id, inputs_fingerprint)`` constraint collapses a kill-mid-step
    redelivery to a single PENDING proposal.
    """
    canonical = json.dumps(
        {
            "close_task_id": close_task_id,
            "task_type": task_type,
            "tool_name": tool_name,
            "finding": finding,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_close_context(
    *,
    realm_id: str,
    run_id: int | None,
    organization_id: int | None,
    organization_company_id: int | None,
) -> ContextPacket:
    """A realm-pinned, CLOSE_MANAGEMENT, READ_ONLY ContextPacket for tool reads.

    Trusted scope is injected server-side (never from model input — there is no
    model here, the driver IS the principal). ``intent=close_management`` +
    ``risk_level=READ_ONLY`` + ``user_role=ANALYST`` clear the policy gate for the
    four READ_ONLY review tools (tool_policy.py:42-82): READ_ONLY tool weight ≤
    READ_ONLY run weight, VIEWER required-role ≤ ANALYST, and the tools are in the
    close_management bundle so the intent check passes.
    """
    return ContextPacket(
        run_id=run_id,
        conversation_id=None,
        user_id=None,
        user_email=None,
        tenant_id=None,
        company_id=realm_id,
        qbo_realm_id=realm_id,
        user_role="ANALYST",
        request_text="close-orchestration step review",
        intent=AgentIntent.CLOSE_MANAGEMENT.value,
        risk_level=RiskLevel.READ_ONLY,
        output_mode=OutputMode.JSON,
        organization_id=organization_id,
        organization_company_id=organization_company_id,
    )


def _bounded_finding(result: Any, *, limit: int = 4000) -> str:  # CRAFT-OK: result is the opaque tool-handler return (handler shapes vary); coerced to str immediately below
    text_value = str(result or "").strip()
    if len(text_value) <= limit:
        return text_value
    return text_value[:limit] + "...[truncated]"


def run_close_orchestration(
    db,
    objective,
    tracker: AgentRunTracker,
    *,
    period: str,
) -> dict[str, Any]:  # CRAFT-OK: the OBJECTIVE_RUNNERS summary-dict contract is dynamic (mirrors the existing runners in agent.py)
    """Resumable, single-flight, proposal-first close-checklist driver.

    Args mirror the OBJECTIVE_RUNNERS contract: ``(db, objective)`` plus the
    ``tracker`` (this runner needs the run id for the proposal step-link + the
    per-step ``tracker.step`` trace). Returns a summary dict.
    """
    from ...services.close_run_start import (
        close_start_period_for_realm,
        close_start_refusal_message,
        period_is_before_close_start,
        validate_close_period,
    )

    realm_id = objective.company_id
    selected_period = validate_close_period(period)
    run_id = tracker.run_id

    # 1. PER-REALM RUN-LOCK (single-flight) — the close_run_locks TTL lease,
    # claimed BEFORE the walk and released in the finally (_release_realm_lock).
    # A DB row survives every commit on this session (the per-step _mark_progress
    # commits AND the tracker's pre-walk __enter__ commit), is heartbeat-bumped
    # per step, and is stealable once its heartbeat exceeds the TTL — a dead or
    # wedged holder cannot block the realm past settings.close_lock_ttl_seconds.
    if not _try_acquire_realm_lock(db, realm_id, run_id):
        logger.info(
            "[close_orchestration] realm lock held by a sibling run; exiting clean (realm=%s)",
            realm_id,
        )
        return {
            "status": "skipped",
            "reason": "run_lock_held",
            "realm_id": realm_id,
            "period": selected_period,
        }

    proposed = 0
    refused_locked = 0
    skipped_no_tool = 0
    skipped_policy = 0
    walked = 0

    # The try OPENS right after the lock is acquired (covering the setup too) so the
    # finally's _release_realm_lock runs on EVERY exit path — a session-level lock is not
    # freed by a commit, so a setup-time raise must not leak it until the connection closes.
    try:
        # The durable run can be terminalized by an overlapping delivery after
        # this worker's initial payload load/adoption but before it wins the realm
        # lease.  Re-read with populate_existing *after* lease acquisition and
        # refuse before every prep write when the first terminal outcome already
        # won.  A missing row is equally non-actionable and fails safe.
        durable_run = None
        if run_id is not None:
            durable_run = (
                db.query(AgentRun)
                .filter(
                    AgentRun.id == run_id,
                    AgentRun.company_id == realm_id,
                )
                .populate_existing()
                .first()
            )
        if durable_run is None or str(durable_run.status or "") in RUN_TERMINAL_STATES:
            return {
                "status": "skipped",
                "reason": "run_already_terminal",
                "realm_id": realm_id,
                "period": selected_period,
                "run_id": run_id,
                "run_status": (
                    str(durable_run.status) if durable_run is not None else "missing"
                ),
            }

        # A hard lock (including a later lock's as-of cascade) is a total prep
        # no-op.  This gate deliberately precedes checklist generation, calendar
        # timestamps/counts, registry construction, review checks, and proposal
        # drafting.  ``period_is_hard_locked`` is realm-pinned and fail-closed.
        if period_is_hard_locked(
            db,
            realm_id=realm_id,
            period=selected_period,
        ):
            return {
                "status": "skipped",
                "reason": "period_locked",
                "realm_id": realm_id,
                "period": selected_period,
                "walked": 0,
                "proposed": 0,
                "refused_locked": 0,
            }

        # A period before the realm's close start boundary predates this
        # platform's responsibility: never a checklist, never an obligation.
        # Existing rows for such a period are left untouched; a TenantAdmin opts
        # one in by moving the close start period (PUT /agent/close-start-period).
        if period_is_before_close_start(
            db,
            realm_id=realm_id,
            period=selected_period,
        ):
            return {
                "status": "skipped",
                "reason": "period_before_close_start",
                "message": close_start_refusal_message(
                    selected_period, close_start_period_for_realm(db, realm_id)
                ),
                "realm_id": realm_id,
                "period": selected_period,
                "walked": 0,
                "proposed": 0,
                "refused_locked": 0,
            }

        # The entire prep pipeline is inside the same realm lease as the walk.
        # This keeps a concurrent Celery redelivery from racing checklist creation
        # or recurring-vendor proposal drafting before it discovers the live walk.
        from ...services.close_run_start import (
            complete_close_period_prep,
            prepare_close_period,
        )
        from ...services.close_task_generation import generate_close_tasks_service

        checklist_summary = generate_close_tasks_service(
            db,
            realm_id,
            selected_period,
            replace_existing=False,
        )
        prep_summary = prepare_close_period(
            db,
            realm_id=realm_id,
            period=selected_period,
            agent_run_id=run_id,
        )
        registry = _build_registry()
        policy_gate = ToolPolicyGate()
        executor = ToolExecutor(db=db, policy_gate=policy_gate, tracker=tracker)
        context = _build_close_context(
            realm_id=realm_id,
            run_id=run_id,
            organization_id=getattr(objective, "organization_id", None),
            organization_company_id=getattr(objective, "organization_company_id", None),
        )

        # 2. RESUME CURSOR — ordered checklist, skip already-processed rows.
        steps = (
            db.query(CloseTask)
            .filter(
                CloseTask.realm_id == realm_id,
                CloseTask.period == selected_period,
            )
            .order_by(CloseTask.sort_order.asc(), CloseTask.id.asc())
            .all()
        )
        try:
            for index, step in enumerate(steps):
                if step.status in ("COMPLETE", "SKIPPED") or step.verified_at is not None:
                    continue  # already processed (human-done or a prior driver run)
                walked += 1

                # W-1: every LLM call made underneath this step — the one-LLM
                # check draft, a read-tool handler's lane call, an embedding —
                # inherits task_type="close" + this run/step identity on its
                # ledger row, and the wrapper's per-run budget gate sees it.
                step_label = f"{step.task_type or 'STEP'}:{step.id}"
                with bind_llm_task_context(
                    "close", run_id=run_id, step=step_label, realm_id=realm_id
                ):
                    # 3a. REGISTERED ANALYTICAL-REVIEW CHECK (C3, the shared registry).
                    # If this task_type maps to a Phase-C Check (review_checks registry),
                    # run the SHARED run_check — the same check the nightly proactive run
                    # invokes — instead of the legacy read-tool path. run_check owns the
                    # scan / could_not_verify / clean / closed-period / materiality / one-LLM
                    # / propose gates; the driver injects a `propose` that writes the SAME
                    # idempotent PENDING ProposedAction (close_task_id / agent_run_id /
                    # step_index / inputs_fingerprint) as the read-tool path. No double-fire:
                    # a checked task_type takes THIS branch and never the read-tool path
                    # below (period guard + propose live inside run_check, not duplicated).
                    check = _get_check_for_task_type(step.task_type or "")  # SA-OK: bounded-loop in-memory registry dict lookup, not a DB query
                    if check is not None:
                        outcome = _run_step_check(
                            db,
                            check=check,
                            realm_id=realm_id,
                            run_id=run_id,
                            step=step,
                            step_index=index,
                            objective=objective,
                        )
                        if outcome == "proposed":
                            proposed += 1
                        elif outcome == "refused_locked":
                            refused_locked += 1
                        elif outcome == "skipped_policy":
                            skipped_policy += 1
                        continue

                    tool_name = TASK_TYPE_TO_TOOL.get(step.task_type or "")  # SA-OK: bounded-loop in-memory dict lookup over a 4-entry task_type map, not a DB query
                    if not tool_name:
                        # No review tool for this task_type — advance the cursor so resume
                        # moves past it, but propose nothing.
                        skipped_no_tool += 1
                        # Human-display note (a CPA reads verification_message
                        # verbatim in the close cockpit). The machine-consumed
                        # task_type code stays in the structured CloseTask.task_type
                        # column on the same row — not re-encoded in display text.
                        _mark_progress(
                            db,
                            step,
                            message="Marked reviewed — this step is completed manually (no automated check for this task type).",
                            run_id=run_id,
                        )
                        continue

                    tool = registry.get(tool_name)  # SA-OK: bounded-loop in-memory ToolRegistry dict lookup, not a DB query
                    if tool is None:
                        skipped_no_tool += 1
                        _mark_progress(db, step, message=f"review tool '{tool_name}' not registered", run_id=run_id)
                        continue

                    # 3. PULL the module via the executor (READ handler, realm-pinned).
                    try:
                        result = executor.execute(tool, context, {})
                    except ToolPolicyViolation as exc:
                        _mark_progress(db, step, message=f"review tool blocked: {exc.reason}", run_id=run_id)
                        continue
                    except CloseBudgetExceededError:
                        # W-1 F-1: budget exhaustion is FAIL-LOUD — never folded
                        # into the "one bad step" continue below.
                        raise
                    except Exception as exc:  # noqa: BLE001 — one bad step must not kill the walk
                        db.rollback()
                        logger.warning(
                            "[close_orchestration] step review failed (realm=%s task=%s): %s",
                            realm_id,
                            step.id,
                            redact_to_string(str(exc), max_length=300),
                        )
                        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B2 — SYNTHESIS
                        # GAP-B2: _mark_progress set verified_at on FAILURE, so the resume
                        # cursor silently skipped a failed review forever (indistinguishable
                        # from a reviewed step). A failed step now stays cursor-visible and is
                        # re-attempted on the next run, bounded to 3 attempts so a poison step
                        # cannot re-spend tokens indefinitely — the 3rd failure is marked
                        # terminally with an honest gave-up message.
                        _mark_failed_attempt(db, step, run_id=run_id)
                        continue
                    finding = _bounded_finding(result)

                    # 4. GUARD BEFORE PROPOSING — never propose into a HARD-locked period
                    # (D-B6-1, 2026-07-16 — the explicit hard lock only).
                    if period_is_hard_locked(db, realm_id=realm_id, when=_period_to_when(step.period)):
                        refused_locked += 1
                        _audit_refusal(db, realm_id=realm_id, step=step, run_id=run_id)
                        _mark_progress(db, step, message="skipped: period locked", run_id=run_id)
                        continue

                    # 5. ONE PENDING PROPOSAL per step (idempotent via the uq constraint).
                    created = _propose_step(
                        db,
                        realm_id=realm_id,
                        run_id=run_id,
                        step=step,
                        step_index=index,
                        tool_name=tool_name,
                        finding=finding,
                    )
                    if created:
                        proposed += 1
                        _audit_proposal(db, realm_id=realm_id, step=step, run_id=run_id)

                    # 6. MARK AGENT PROGRESS (verified_at), NOT status=COMPLETE.
                    _mark_progress(db, step, message=f"reviewed via {tool_name}; drafted proposal for approval", run_id=run_id)
        except CloseBudgetExceededError as exc:
            # W-1 F-1 FAIL-LOUD: the run's token budget is spent. Write the
            # WatchdogAlert + the cockpit finding row, then re-raise so the
            # tracker marks the run failed. The wrapper raised BEFORE the
            # over-budget provider call, so no further tokens were spent; the
            # dispatching task treats this as terminal (no retry — a retry
            # would open a fresh run_id and re-spend).
            _fail_budget_exceeded(db, realm_id=realm_id, step=step, run_id=run_id, exc=exc)
            raise

        run_summary = {
            "status": "succeeded",
            "realm_id": realm_id,
            "period": selected_period,
            "walked": walked,
            "proposed": proposed,
            "refused_locked": refused_locked,
            "skipped_no_tool": skipped_no_tool,
            "skipped_policy": skipped_policy,
            "checklist": checklist_summary,
            "calendar_prep": prep_summary,
        }
        complete_close_period_prep(
            db,
            realm_id=realm_id,
            period=selected_period,
            run_summary=run_summary,
        )
    finally:
        # Release OUR lease row on every exit path (success, failure, budget
        # stop, cancellation). _release_realm_lock rolls back any failed
        # transaction first (per-step outcomes were already committed per step),
        # deletes only our own agent_run_id row, and commits — a failed release
        # is non-fatal because the TTL reclaims the realm.
        _release_realm_lock(db, realm_id, run_id)

    return run_summary


def _build_registry():
    # Lazy import: app.assistant.{tools,handlers} pull in the full handler graph;
    # importing at call time keeps app.tasks.agent import-light.
    from ...assistant.handlers import TOOL_HANDLERS
    from ...assistant.tools import ASSISTANT_TOOLS

    return build_assistant_tool_registry(ASSISTANT_TOOLS, TOOL_HANDLERS)


def _get_check_for_task_type(task_type: str):
    """Resolve a close-task ``task_type`` to its registered analytical-review Check.

    Lazy import: importing ``app.services.review_checks`` fires the module-level
    ``register_check`` side-effects (builtin_checks + new_checks) that populate the
    registries — doing it at call time keeps ``app.tasks.agent`` import-light, and
    the checks lazy-import their own detectors so this pulls in no heavy graph.
    Returns None for an unmapped task_type → the caller falls back to the read-tool
    path (additive; the existing loop is unchanged for unchecked task_types).
    """
    from ...services.review_checks import get_check_for_task_type

    return get_check_for_task_type(task_type)


def _run_step_check(
    db,
    *,
    check,
    realm_id: str,
    run_id: int | None,
    step: CloseTask,
    step_index: int,
    objective,
) -> str:
    """Run ONE registered Check for a close step via the SHARED ``run_check``.

    Builds a ``CheckContext`` whose injected ``propose`` writes the SAME idempotent
    PENDING ``ProposedAction`` (carrying the C0 step-link columns) as the read-tool
    path's ``_propose_step`` — so the close driver and the nightly proactive run
    SHARE both the checks AND the proposal shape, differing only by the injected
    writer. PROPOSAL-FIRST: ``run_check`` drafts a PENDING proposal only; it NEVER
    posts to the GL. The close-driver passes ``qbo_client=None`` today, so a check
    whose detector needs a live client honestly degrades (could_not_verify) rather
    than crashing. Returns "proposed" / "refused_locked" / "" for the run summary.
    """
    from ...services.review_checks import (
        CheckContext,
        CheckRunStatus,
        Finding,
        check_runs_under,
        get_effective_aggressiveness,
        get_effective_materiality,
        get_tier,
        run_check,
    )

    # AGGRESSIVENESS SKIP (Phase E AC-2) — resolve the company's review_aggressiveness
    # once for this (realm, step) and HONESTLY skip the whole check BEFORE run_check
    # when its tier is below appetite. A POLICY-SKIPPED AuditLog row + a "skipped:
    # policy" progress mark are written; NO scan, NO run_check, NO LLM. NEVER a clean
    # pass — a skipped check has not reviewed the step. Fail-soft to "balanced"
    # (get_effective_aggressiveness never raises), so a policy-read issue can never
    # silently skip a check.
    aggressiveness = get_effective_aggressiveness(db, realm_id=realm_id)
    tier = get_tier(check.check_id)
    if not check_runs_under(aggressiveness, tier):
        _audit_policy_skip(
            db,
            realm_id=realm_id,
            step=step,
            run_id=run_id,
            check_id=check.check_id,
            tier=tier.value,
            aggressiveness=aggressiveness,
        )
        _mark_progress(
            db,
            step,
            message=f"skipped: policy (tier={tier.value} aggressiveness={aggressiveness})",
            run_id=run_id,
        )
        return "skipped_policy"

    outcome = ""

    def _propose(*, ctx, finding: "Finding", reasoning: str) -> int | None:
        nonlocal outcome
        # The driver's injected ProposedAction writer: reuses the same idempotent,
        # step-linked _propose_step path as the read-tool branch (uq(close_task_id,
        # inputs_fingerprint) collapses a kill-mid-step redelivery to one proposal).
        created = _propose_step(
            db,
            realm_id=realm_id,
            run_id=run_id,
            step=step,
            step_index=step_index,
            tool_name=f"review_check:{finding.check_id}",
            finding=reasoning or finding.message,
        )
        if created:
            outcome = "proposed"
            _audit_proposal(db, realm_id=realm_id, step=step, run_id=run_id)
        return step.id if created else None

    materiality = get_effective_materiality(db, check.check_id, realm_id=realm_id)
    if materiality is None:
        # A registered check always has a registered materiality (register_check
        # binds them together); a None here means a registry inconsistency — fail
        # closed (mark progress, propose nothing) rather than crash the walk.
        _mark_progress(db, step, message=f"review check {check.check_id} has no registered materiality", run_id=run_id)
        return ""

    ctx = CheckContext(
        db=db,
        realm_id=realm_id,
        period=step.period or "",
        propose=_propose,
        materiality=materiality,
        qbo_client=None,  # the close driver has no live QBO client today (honest degradation in scan)
        company_name="",
        close_task_id=step.id,
        agent_run_id=run_id,
        step_index=step_index,
    )

    try:
        result = run_check(db, check, ctx)
    except CloseBudgetExceededError:
        # W-1 F-1: budget exhaustion propagates LOUDLY — never degraded into
        # the "one bad check" continue below (the walk converts it into a
        # failed run + WatchdogAlert + finding row).
        raise
    except PROGRAMMING_ERRORS as exc:
        # OUR defect, not a provider failure. The walk still continues (one bad check
        # must not kill it) but this is logged with a trace at ERROR so it cannot hide
        # among routine degradations.
        db.rollback()
        logger.error(
            "[close_orchestration] step check raised a code defect (realm=%s task=%s check=%s): %s",
            realm_id,
            step.id,
            check.check_id,
            redact_to_string(str(exc), max_length=300),
            exc_info=True,
        )
        _mark_progress(db, step, message=f"review check {check.check_id} raised a code defect; see trace", run_id=run_id)
        return ""
    except Exception as exc:  # noqa: BLE001 — one bad check must not kill the walk
        db.rollback()
        logger.warning(
            "[close_orchestration] step check failed (realm=%s task=%s check=%s): %s",
            realm_id,
            step.id,
            check.check_id,
            redact_to_string(str(exc), max_length=300),
        )
        _mark_progress(db, step, message=f"review check {check.check_id} failed; see trace", run_id=run_id)
        return ""

    # run_check's own closed-period gate fired — record the refusal (the read-tool
    # path's _audit_refusal analogue) so a locked-period skip is auditable here too.
    if result.status is CheckRunStatus.PERIOD_LOCKED:
        _audit_refusal(db, realm_id=realm_id, step=step, run_id=run_id)
        _mark_progress(db, step, message="skipped: period locked", run_id=run_id)
        return "refused_locked"

    # Human-display progress note (a CPA reads verification_message verbatim in
    # the close cockpit — no "check_id: status (detail)" engineer-speak). The
    # machine-consumed check identifier stays in structured fields (the drafted
    # ProposedAction's tool_name "review_check:<id>" and the audit rows); the
    # trailing "(check: …)" is a traceability hint only. PERIOD_LOCKED returned
    # above; unknown/future statuses fall back to naming the raw status value.
    status_display = {
        CheckRunStatus.CLEAN: "Automated check passed — no exceptions found",
        CheckRunStatus.DRAFTED: "Automated check flagged an item — a proposal was drafted for your review",
        CheckRunStatus.IMMATERIAL: "Automated check found only differences below the materiality threshold — no action needed",
        CheckRunStatus.COULD_NOT_VERIFY: "Automated check could not verify this step (source data unavailable) — please review manually",
        CheckRunStatus.BLOCKED: "Automated check stopped before drafting — the AI review budget was reached",
        CheckRunStatus.ERROR: "Automated check could not complete — please review this step manually",
    }.get(result.status, f"Automated check finished ({result.status.value})")
    detail = (result.detail or "").strip()
    message = f"{status_display}. {detail}" if detail else f"{status_display}."
    _mark_progress(
        db,
        step,
        message=f"{message} (check: {check.check_id})",
        run_id=run_id,
    )
    return outcome


def _propose_step(
    db,
    *,
    realm_id: str,
    run_id: int | None,
    step: CloseTask,
    step_index: int,
    tool_name: str,
    finding: str,
) -> bool:
    """Insert exactly one PENDING ProposedAction for the step. Returns True if a
    NEW row was created; False if the uq constraint already had it (idempotent).

    PROPOSAL-FIRST: this writes a PENDING ``ProposedAction`` only — it NEVER calls
    a GL-writing handler. A human approves it later via the existing inbox.
    """
    fingerprint = _inputs_fingerprint(
        close_task_id=step.id,
        task_type=step.task_type or "",
        tool_name=tool_name,
        finding=finding,
    )
    action = ProposedAction(  # TEST-OK: system Celery driver, not a model-invocable tool; PENDING proposal only, never a direct GL write; behavioral proposal-first/resume/guard eval is plan chunk C4
        company_id=realm_id,
        action_type="CLOSE_STEP_REVIEW",
        payload={
            "summary": f"Close-step review: {step.title}",
            "execution_mode": "review_only",
            "approval_effect": "Records approval for the close step; does not execute a financial write automatically.",
            "close_task": {
                "id": step.id,
                "task_type": step.task_type,
                "title": step.title,
                "period": step.period,
            },
            "evidence": {
                "modules": [tool_name],
                "check": f"Agent reviewed {step.task_type} for period {step.period} via {tool_name}.",
                "rationale": "Drafted by the close-orchestration driver for human approval (proposal-first).",
                "finding": sanitize_payload(finding),
                "inputs_fingerprint": fingerprint,
                # PROPOSER IDENTITY (C2C-CLOSE design seam for chunk C3-LIFECYCLE's
                # preparer≠approver block, wave 3.2): every proposal names its
                # preparer explicitly so the approve endpoint can refuse
                # approver == proposer. The agent lane is a distinct principal —
                # a human approver can never equal it, which is exactly the
                # system-enforced SoD the proposal-first ladder provides.
                "proposer_identity": f"system:close_orchestration:run:{run_id}",
            },
        },
        reasoning=f"Close-orchestration step review for {step.task_type} ({step.period}).",
        confidence_score=0.8,
        status="PENDING",
        source="system",
        category="close_orchestration",
        priority="medium",
        close_task_id=step.id,
        agent_run_id=run_id,
        step_index=step_index,
        inputs_fingerprint=fingerprint,
    )
    db.add(action)
    try:
        db.flush()  # surface the uq collision here
    except IntegrityError:
        # A kill-after-propose redelivery already wrote this exact proposal. The
        # uq(close_task_id, inputs_fingerprint) constraint makes a double-book
        # impossible; treat it as already-done.
        db.rollback()
        logger.info(
            "[close_orchestration] proposal already exists for close_task=%s (idempotent skip)",
            step.id,
        )
        return False
    return True


# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B2 — bounded failed-step re-attempts
# (SYNTHESIS GAP-B2). The attempt count is carried in verification_message with a structured
# prefix (no schema migration); parsing is defensive — an unparseable message counts as 0.
_FAILED_ATTEMPT_PREFIX = "REVIEW-FAILED attempt="
_MAX_REVIEW_ATTEMPTS = 3


def _failed_attempts(step: CloseTask) -> int:
    message = step.verification_message or ""
    if not message.startswith(_FAILED_ATTEMPT_PREFIX):
        return 0
    digits = message[len(_FAILED_ATTEMPT_PREFIX):].split(":", 1)[0]
    try:
        return max(0, int(digits))
    except ValueError:
        return 0


def _mark_failed_attempt(db, step: CloseTask, *, run_id: int | None = None) -> None:
    """Record a failed review WITHOUT hiding it from the resume cursor.

    Attempts 1..N-1 leave ``verified_at`` unset so the next run re-attempts the step;
    attempt N sets ``verified_at`` with an honest gave-up message so a poison step stops
    consuming the run's token budget. Heartbeats + commits like ``_mark_progress``.
    """
    attempts = _failed_attempts(step) + 1
    if attempts >= _MAX_REVIEW_ATTEMPTS:
        step.verified_at = datetime.now(timezone.utc)
        step.verification_message = (
            f"{_FAILED_ATTEMPT_PREFIX}{attempts}: review failed {attempts} times; giving up — "
            "see agent step trace and review this task manually"
        )[:2000]
    else:
        step.verification_message = (
            f"{_FAILED_ATTEMPT_PREFIX}{attempts}: review failed; will re-attempt on the next close run"
        )[:2000]
    if run_id is not None:
        _heartbeat_realm_lock(db, realm_id=step.realm_id, run_id=run_id)
    db.commit()


def _mark_progress(db, step: CloseTask, *, message: str, run_id: int | None = None) -> None:
    """Set ``verified_at`` + ``verification_message`` (agent progress) and commit.

    Does NOT flip ``status`` to COMPLETE — proposal-first: a human completes the
    task on approval. Committing per step is what makes the walk resumable (the
    next run's cursor skips ``verified_at``-set rows). This IS the step-state
    memoization (W-1 item 3): the CloseTask row is the persisted per-step
    record — keyed by the close task itself, stronger than (run_id, step) — and
    a retried/resumed run's cursor short-circuits every marked step BEFORE any
    LLM work, so a retry never re-spends a completed step's tokens (the step's
    drafted output survives as its fingerprint-deduped PENDING ProposedAction).

    When ``run_id`` is given the realm's close-lease heartbeat is bumped in the
    SAME commit — per-step progress is exactly what keeps the single-flight
    lease alive (W-1 item 5).
    """
    step.verified_at = datetime.now(timezone.utc)
    step.verification_message = message[:2000]
    if run_id is not None:
        _heartbeat_realm_lock(db, realm_id=step.realm_id, run_id=run_id)
    db.commit()


def _fail_budget_exceeded(
    db, *, realm_id: str, step: CloseTask, run_id: int | None, exc: CloseBudgetExceededError
) -> None:
    """FAIL-LOUD surface for a budget-stopped close run (W-1 F-1).

    Writes (1) a critical ``WatchdogAlert`` and (2) a cockpit finding row (a
    PENDING ``ProposedAction`` — the close-runs UI renders ProposedActions as
    findings), then commits. The caller re-raises so the tracker marks the run
    failed. The step is deliberately NOT ``_mark_progress``-ed: it did not run,
    and the resume cursor must revisit it if the budget is raised. Idempotent
    via the uq(close_task_id, inputs_fingerprint) constraint.
    """
    db.rollback()  # discard any half-flushed step state; alert rows get a clean txn
    db.add(
        WatchdogAlert(
            company_id=realm_id,
            alert_type="CLOSE_RUN_BUDGET_EXCEEDED",
            severity="critical",
            title="Close run stopped: LLM token budget exceeded",
            description=(
                f"Close run {run_id} spent {exc.spent_tokens} tokens against a per-run "
                f"budget of {exc.budget_tokens} and was stopped at step '{step.title}' "
                f"({step.task_type}, period {step.period}). No further LLM calls were made "
                "and the run is marked failed. Review the run's token ledger; raise the "
                "per-run budget (ReviewPolicy.close_run_token_budget or "
                "CLOSE_RUN_TOKEN_BUDGET) only if this close legitimately needs more."
            ),
            context={
                "agent_run_id": run_id,
                "close_task_id": step.id,
                "period": step.period,
                "spent_tokens": exc.spent_tokens,
                "budget_tokens": exc.budget_tokens,
            },
        )
    )
    fingerprint = hashlib.sha256(
        f"close_budget_exceeded:{run_id}:{step.id}".encode("utf-8")
    ).hexdigest()
    db.add(
        ProposedAction(  # TEST-OK: system driver finding row; PENDING review-only, never a GL write
            company_id=realm_id,
            action_type="CLOSE_RUN_BUDGET_EXCEEDED",
            payload={
                "summary": f"Close run stopped: token budget exceeded at '{step.title}'",
                "execution_mode": "review_only",
                "approval_effect": "Acknowledges the budget stop; does not execute any financial write.",
                "close_task": {
                    "id": step.id,
                    "task_type": step.task_type,
                    "title": step.title,
                    "period": step.period,
                },
                "evidence": {
                    "check": (
                        f"Run {run_id} spent {exc.spent_tokens} of {exc.budget_tokens} "
                        "budgeted tokens; the walk stopped before the next LLM call."
                    ),
                    "spent_tokens": exc.spent_tokens,
                    "budget_tokens": exc.budget_tokens,
                    "agent_run_id": run_id,
                    "inputs_fingerprint": fingerprint,
                },
            },
            reasoning=(
                "The close run hit its per-run LLM token budget and failed loudly "
                "instead of silently continuing to spend (W-1 F-1)."
            ),
            confidence_score=1.0,
            status="PENDING",
            source="system",
            category="close_orchestration",
            priority="high",
            close_task_id=step.id,
            agent_run_id=run_id,
            step_index=None,
            inputs_fingerprint=fingerprint,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # The same (run, step) budget finding already exists (redelivery race);
        # the alert + failure are already surfaced — idempotent skip.
        db.rollback()
    except Exception as surface_exc:  # noqa: BLE001 — surfacing must not mask the budget error
        db.rollback()
        logger.error(
            "[close_orchestration] failed to surface budget-exceeded alert (run=%s): %s",
            run_id,
            redact_to_string(str(surface_exc), max_length=300),
        )


def _audit_proposal(db, *, realm_id: str, step: CloseTask, run_id: int | None) -> None:
    db.add(
        AuditLog(
            realm_id=realm_id,
            action="CREATE",
            entity_type="ProposedAction",
            entity_id=str(step.id),
            user_id="system:close_orchestration",
            details={
                "event": "close_step_proposal_drafted",
                "close_task_id": step.id,
                "task_type": step.task_type,
                "period": step.period,
                "agent_run_id": run_id,
            },
        )
    )
    db.commit()


def _audit_refusal(db, *, realm_id: str, step: CloseTask, run_id: int | None) -> None:
    db.add(
        AuditLog(
            realm_id=realm_id,
            action="UPDATE",
            entity_type="CloseTask",
            entity_id=str(step.id),
            user_id="system:close_orchestration",
            details={
                "event": "close_step_proposal_refused_period_locked",
                "close_task_id": step.id,
                "task_type": step.task_type,
                "period": step.period,
                "agent_run_id": run_id,
            },
        )
    )
    db.commit()


def _audit_policy_skip(
    db,
    *,
    realm_id: str,
    step: CloseTask,
    run_id: int | None,
    check_id: str,
    tier: str,
    aggressiveness: str,
) -> None:
    """Write a POLICY-SKIPPED AuditLog row (Phase E AC-2). HONEST: this records that
    a check was NOT run because its tier is below the company's review_aggressiveness
    — it is never a clean/NO_HIT result. Mirrors ``_audit_refusal`` (the period-locked
    refusal); the read-tool path has no analogue because only the check path tiers."""
    db.add(
        AuditLog(
            realm_id=realm_id,
            action="UPDATE",
            entity_type="CloseTask",
            entity_id=str(step.id),
            user_id="system:close_orchestration",
            details={
                "event": "close_step_review_policy_skipped",
                "close_task_id": step.id,
                "task_type": step.task_type,
                "period": step.period,
                "agent_run_id": run_id,
                "check_id": check_id,
                "tier": tier,
                "aggressiveness": aggressiveness,
                "reason": "tier below company review_aggressiveness",
            },
        )
    )
    db.commit()

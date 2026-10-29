# PLAN-EXEMPT: ratified plan 20260701-phase-a-generic-schedules (Wave 2 AC-4/AC-5 — this file is named in surface_scope; marker carried for the exact-path gate, mirroring covenant_snapshot.py)
"""Monthly scheduled generation of balance-sheet schedule entries (Wave 2 AC-4/AC-5).

Beat-scheduled fan-out (AP-CEL-9): ``generate_monthly_schedule_entries`` runs early on the
1st, enumerates mapped realms under bypass, and dispatches one realm-scoped child
``generate_realm_schedule_entries`` each (mirrors ``snapshot_all_covenants`` /
``capture_all_metric_snapshots``) — each realm's work stays short, independently retried,
and well under the broker visibility_timeout.

Each child, for ONE realm and the just-closed month:

  AC-4  runs the LIVE engine (``app/services/schedule_engine.generate_month_entries``,
        ``skip_manual_policy=True``) — one consolidated JE ProposedAction per
        (schedule_type, credit account) group, PENDING for human approval. Rate-limit
        aware BY CONSTRUCTION: generation makes NO QuickBooks calls (account resolution
        reads the local QBOAccount cache); the only QBO writes are the serial
        auto-post executions below — never an unbounded parallel fan-out per realm.
        FinancialCalendar-locked periods are SKIPPED and SURFACED (a structured log +
        a ``SCHEDULE_AUTOGEN_PERIOD_LOCKED`` WatchdogAlert row) — never silent, never a
        lock bypass. IDEMPOTENT-OK: a re-run / at-least-once redelivery re-generates
        nothing — the unique (schedule_id, period) constraint plus the engine's
        existing-entry skip and concurrent-run IntegrityError rollback guarantee no
        duplicate entries or proposals.

  AC-5  applies each schedule's ``automation_policy``:
          * manual                  → excluded from generation entirely (surfaced in
                                      skip_reasons as ``manual_policy``)
          * auto_propose (default)  → the drafted proposal stays PENDING — today's
                                      human-approval behavior, now scheduled
          * auto_post_deterministic → the batch's proposal is auto-approved ONLY when
                                      ``batch_auto_post_eligibility`` says every entry
                                      is a plain unmodified straight-line period;
                                      otherwise the WHOLE batch falls back to propose.
        The auto-post QBO write flows through the EXISTING executor
        (``execute_proposed_action``: durable QBOActionExecution ledger, idempotency
        key, closed-period gate) + ``run_post_execution_schedule_hook`` — the policy
        changes WHEN approval happens, never the write path.

The child pins the tenant RLS scope (``tenant_scope_for_realm``) and every query
carries its realm predicate.
"""
from __future__ import annotations

import logging
from ..qbo_actions.transitions import claim_action_transition
from ..services.proposal_provenance import validate_autonomous_approval

from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..services.schedule_types import AUTOMATION_POLICY_AUTO_POST
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)

AUTO_POST_ACTOR = "system:auto_post_deterministic"
ALERT_PERIOD_LOCKED = "SCHEDULE_AUTOGEN_PERIOD_LOCKED"


# ─────────────────────────────────────────────────────────────────────────────
# Period helper
# ─────────────────────────────────────────────────────────────────────────────

def period_for_date(today: date | None = None) -> str:
    """The month scheduled generation targets: the last fully-closed calendar
    month, ``YYYY-MM`` (running on July 1 → ``2026-06``). ``today`` is
    injectable for deterministic tests. Mirrors covenant_snapshot's helper."""
    today = today or date.today()
    last_month_end = date(today.year, today.month, 1) - timedelta(days=1)
    return f"{last_month_end.year}-{last_month_end.month:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# AC-5 — the exact auto-post eligibility predicate
# ─────────────────────────────────────────────────────────────────────────────

def batch_auto_post_eligibility(
    period: str,
    batch: list[dict],
    schedules_by_id: dict,
) -> tuple[bool, str]:
    """Whether ONE consolidated proposal's batch may auto-post, with the first
    disqualifying reason. Eligible iff EVERY entry in the batch is a plain
    unmodified straight-line period:

      * not a manual grid override folded into the JE      (``manual``)
      * not a B5-7 past-window catch-up true-up            (``trueup``)
      * not an over-amortization corrective                (``corrective``)
      * strictly positive amount                           (no negatives)
      * the schedule's policy is ``auto_post_deterministic`` (a single
        auto_propose/manual schedule in the group vetoes the whole batch)
      * the method is STRAIGHT_LINE (no declining-balance / other methods)
      * the amount equals the plain computed straight-line month to the cent
        (any clamp, cap, or edit breaks equality)
      * not the schedule's final in-range period (the final plug always
        proposes, even when it happens to equal the straight-line month)

    Anything else → (False, reason): the batch falls back to propose.
    """
    from ..services.schedule_engine import (
        compute_monthly_amount,
        is_final_schedule_period,
        period_in_schedule_range,
    )

    if not batch:
        return False, "empty_batch"

    for item in batch:
        if item.get("manual"):
            return False, "manual_override_in_batch"
        if item.get("trueup"):
            return False, "trueup_in_batch"
        if item.get("corrective"):
            return False, "corrective_in_batch"

        amount = round(float(item.get("amount") or 0), 2)
        if amount <= 0:
            return False, "non_positive_amount"

        schedule = schedules_by_id.get(item.get("schedule_id"))
        if schedule is None:
            return False, "schedule_not_found"
        policy = getattr(schedule, "automation_policy", None) or "auto_propose"
        if policy != AUTOMATION_POLICY_AUTO_POST:
            return False, "policy_not_auto_post"
        if (schedule.depreciation_method or "STRAIGHT_LINE") != "STRAIGHT_LINE":
            return False, "non_straight_line_method"
        if not period_in_schedule_range(schedule, period):
            return False, "period_out_of_range"
        if is_final_schedule_period(schedule, period):
            return False, "final_period_plug"
        if amount != round(compute_monthly_amount(schedule), 2):
            return False, "amount_not_plain_straight_line"

    return True, "ok"


def _auto_post_eligible_batches(db: Session, *, realm_id: str, period: str, result: dict) -> dict:
    """Group the engine result's entries by proposal and auto-approve+execute
    the eligible batches through the existing executor path. Returns a summary
    ``{auto_posted: [...], auto_post_declined: [...], auto_post_failed: [...]}``."""
    from ..models import AccountingSchedule, ProposedAction
    from ..qbo_actions.executor import execute_proposed_action
    from ..services.schedule_engine import run_post_execution_schedule_hook

    batches: dict[int, list[dict]] = {}
    for item in result.get("entries") or []:
        pid = item.get("proposal_id")
        if pid is not None:
            batches.setdefault(pid, []).append(item)

    if not batches:
        return {"auto_posted": [], "auto_post_declined": [], "auto_post_failed": []}

    schedule_ids = {i.get("schedule_id") for b in batches.values() for i in b}
    schedules_by_id = {
        s.id: s
        for s in db.query(AccountingSchedule)
        .filter(
            AccountingSchedule.realm_id == realm_id,
            AccountingSchedule.id.in_(schedule_ids),
        )
        .all()
    }

    auto_posted: list[dict] = []
    declined: list[dict] = []
    failed: list[dict] = []

    for proposal_id, batch in batches.items():
        eligible, reason = batch_auto_post_eligibility(period, batch, schedules_by_id)
        if not eligible:
            declined.append({"proposal_id": proposal_id, "reason": reason})
            continue

        proposal = (
            db.query(ProposedAction)
            .filter(
                ProposedAction.id == proposal_id,
                ProposedAction.company_id == realm_id,
            )
            .first()
        )
        if proposal is None or proposal.status != "PENDING":
            declined.append({"proposal_id": proposal_id, "reason": "proposal_not_pending"})
            continue

        # Approve, then execute through the EXISTING durable executor path
        # (QBOActionExecution ledger + idempotency key + closed-period gate) —
        # identical to a human clicking Approve, with a system actor recorded.
        try:
            validate_autonomous_approval(proposal)
            won = claim_action_transition(
                db, action_id=proposal.id, from_status="PENDING", to_status="APPROVED",
                reviewed_by=AUTO_POST_ACTOR, expected_revision=proposal.revision, autonomous=True,
            )
        except HTTPException:
            declined.append({"proposal_id": proposal.id, "reason": "human_review_required"})
            continue
        if not won:
            db.rollback()
            declined.append({"proposal_id": proposal.id, "reason": "proposal_changed"})
            continue
        db.commit()

        try:
            executed = execute_proposed_action(db, proposal_id)
        except HTTPException as exc:
            # AC-E1 (D-B6-2, 2026-07-16): a 409 period_locked is a clean refusal
            # (the proposal is dated in a HARD-locked period), NOT an execution
            # error — record it as declined, don't mislabel it as a failure.
            db.rollback()
            if exc.status_code == 409:
                declined.append({"proposal_id": proposal_id, "reason": "period_locked"})
            else:
                failed.append({"proposal_id": proposal_id, "error": "execution_error"})
            continue
        except Exception as exc:  # noqa: BLE001 — one failed batch must not sink the realm run
            db.rollback()
            logger.error(
                "[schedules] auto-post EXECUTION ERROR realm=%s proposal=%s: %s "
                "(proposal left APPROVED; visible in the proposals view for manual retry)",
                realm_id,
                proposal_id,
                redact_to_string(str(exc), max_length=500),
            )
            failed.append({"proposal_id": proposal_id, "error": "execution_error"})
            continue

        if executed.status == "FAILED":
            logger.error(
                "[schedules] auto-post FAILED realm=%s proposal=%s — QBO write did not "
                "succeed; failure recorded on the proposal's execution_result",
                realm_id,
                proposal_id,
            )
            failed.append({"proposal_id": proposal_id, "error": "qbo_write_failed"})
            continue

        # Mark linked entries POSTED (+ AC-6 auto-reversal drafting for
        # reversal-flavored types); hook failures surface loudly on their own.
        run_post_execution_schedule_hook(db, executed, actor=AUTO_POST_ACTOR)
        auto_posted.append({"proposal_id": proposal_id, "entries": len(batch)})

    return {
        "auto_posted": auto_posted,
        "auto_post_declined": declined,
        "auto_post_failed": failed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase-C AC-3 (plan 20260702-phase-c-asc842) — the lease leg of the realm child
# ─────────────────────────────────────────────────────────────────────────────


def lease_auto_post_eligibility(lease, row, *, final_period: str) -> tuple[bool, str]:
    """Whether ONE monthly lease JE proposal may auto-post, with the first
    disqualifying reason. Eligible iff the row is a PLAIN UNREMEASURED month:

      * the lease's policy is ``auto_post_deterministic``   (else propose)
      * the row was NOT rebuilt by a remeasurement          (``remeasured``)
      * not the lease's final month (the final row carries the engine-owned
        zeroing plugs — it always proposes)
      * strictly positive payment and expense

    Commencement / remeasurement / termination JEs NEVER enter this path —
    they are drafted by the lifecycle endpoints as PENDING proposals and this
    predicate only ever sees the beat's own monthly row proposals. The caller
    also requires the lease's commencement JE to be EXECUTED (the beat never
    drafts a month before that; the auto-post pass re-checks it).
    """
    policy = getattr(lease, "automation_policy", None) or "auto_propose"
    if policy != AUTOMATION_POLICY_AUTO_POST:
        return False, "policy_not_auto_post"
    if row.remeasured:
        return False, "remeasured_row"
    if row.period == final_period:
        return False, "final_period_plug"
    if float(row.payment or 0) <= 0 or float(row.expense or 0) <= 0:
        return False, "non_positive_amount"
    return True, "ok"


def _auto_post_lease_proposals(
    db: Session, *, realm_id: str, period: str, drafted: list[dict]
) -> dict:
    """Apply the auto-post policy to the monthly lease proposals just drafted,
    through the SAME approve-then-execute executor path as schedules (durable
    QBOActionExecution ledger, idempotency key, closed-period gate) + the
    post-execution hook (which marks the lease row POSTED via its lease leg)."""
    from ..models import LeaseSchedule, LeaseScheduleRow, ProposedAction
    from ..qbo_actions.executor import execute_proposed_action
    from ..services.lease_engine import COMMENCEMENT_POSTED, commencement_state, period_for_month
    from ..services.schedule_engine import run_post_execution_schedule_hook

    auto_posted: list[dict] = []
    declined: list[dict] = []
    failed: list[dict] = []

    for item in drafted:
        if item["period"] != period:
            # A caught-up earlier month (backdated lease, a month whose JE a
            # human rejected, a month a lock or failed run left behind) always
            # goes to a human — auto-posting it could override a rejection.
            declined.append({"proposal_id": item["proposal_id"], "reason": "catch_up_period"})
            continue
        lease = (
            db.query(LeaseSchedule)
            .filter(
                LeaseSchedule.id == item["lease_id"],
                LeaseSchedule.realm_id == realm_id,
            )
            .first()
        )
        row = (
            db.query(LeaseScheduleRow)
            .filter(
                LeaseScheduleRow.id == item["row_id"],
                LeaseScheduleRow.realm_id == realm_id,
            )
            .first()
        )
        if lease is None or row is None:
            declined.append({"proposal_id": item["proposal_id"], "reason": "lease_not_found"})
            continue
        if commencement_state(db, lease) != COMMENCEMENT_POSTED:
            # Never auto-post against an ROU asset / liability not yet booked.
            declined.append({"proposal_id": item["proposal_id"], "reason": "commencement_not_posted"})
            continue
        final_period = period_for_month(lease.commencement_date, lease.term_months)
        eligible, reason = lease_auto_post_eligibility(lease, row, final_period=final_period)
        if not eligible:
            declined.append({"proposal_id": item["proposal_id"], "reason": reason})
            continue

        proposal = (
            db.query(ProposedAction)
            .filter(
                ProposedAction.id == item["proposal_id"],
                ProposedAction.company_id == realm_id,
            )
            .first()
        )
        if proposal is None or proposal.status != "PENDING":
            declined.append({"proposal_id": item["proposal_id"], "reason": "proposal_not_pending"})
            continue

        try:
            validate_autonomous_approval(proposal)
            won = claim_action_transition(
                db, action_id=proposal.id, from_status="PENDING", to_status="APPROVED",
                reviewed_by=AUTO_POST_ACTOR, expected_revision=proposal.revision, autonomous=True,
            )
        except HTTPException:
            declined.append({"proposal_id": proposal.id, "reason": "human_review_required"})
            continue
        if not won:
            db.rollback()
            declined.append({"proposal_id": proposal.id, "reason": "proposal_changed"})
            continue
        db.commit()

        try:
            executed = execute_proposed_action(db, proposal.id)
        except HTTPException as exc:
            # AC-E1 (D-B6-2, 2026-07-16): a 409 period_locked is a clean refusal
            # (the proposal is dated in a HARD-locked period), NOT an execution
            # error — record it as declined, don't mislabel it as a failure.
            db.rollback()
            if exc.status_code == 409:
                declined.append({"proposal_id": proposal.id, "reason": "period_locked"})
            else:
                failed.append({"proposal_id": proposal.id, "error": "execution_error"})
            continue
        except Exception as exc:  # noqa: BLE001 — one failed lease must not sink the realm run
            db.rollback()
            logger.error(
                "[schedules] lease auto-post EXECUTION ERROR realm=%s proposal=%s: %s "
                "(proposal left APPROVED; visible in the proposals view for manual retry)",
                realm_id,
                proposal.id,
                redact_to_string(str(exc), max_length=500),
            )
            failed.append({"proposal_id": proposal.id, "error": "execution_error"})
            continue

        if executed.status == "FAILED":
            logger.error(
                "[schedules] lease auto-post FAILED realm=%s proposal=%s — QBO write did "
                "not succeed; failure recorded on the proposal's execution_result",
                realm_id,
                proposal.id,
            )
            failed.append({"proposal_id": proposal.id, "error": "qbo_write_failed"})
            continue

        run_post_execution_schedule_hook(db, executed, actor=AUTO_POST_ACTOR)
        auto_posted.append({"proposal_id": proposal.id, "lease_id": lease.id, "period": period})

    return {
        "lease_auto_posted": auto_posted,
        "lease_auto_post_declined": declined,
        "lease_auto_post_failed": failed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# The per-realm unit of work (task + tests both drive this)
# ─────────────────────────────────────────────────────────────────────────────

def generate_schedule_entries_for_realm(
    db: Session, *, realm_id: str, period: str | None = None
) -> dict:
    """Generate the period's schedule entries + JE proposals for ONE realm,
    honoring period locks and per-schedule automation policy."""
    from ..services.schedule_engine import generate_month_entries
    from ..services.task_autonomy import period_is_hard_locked

    period = period or period_for_date()

    # FinancialCalendar HARD lock (incl. the B5-8 lock-as-of cascade): SKIP the
    # locked period and SURFACE it — a structured log plus a WatchdogAlert row
    # (deduped per (realm, type, period) by the watchdog fingerprint). Never
    # bypass the lock, never a silent no-op. D-B6-1 (2026-07-16): the gate keys off
    # the EXPLICIT hard lock — a soft-CLOSED (auto-rolled) month is books-rolled, not
    # write-blocked, so generation still runs into it.
    if period_is_hard_locked(db, realm_id=realm_id, period=period):
        logger.warning(
            "[schedules] realm=%s period=%s is LOCKED — scheduled generation skipped "
            "(unlock the period or generate a later one from the grid)",
            realm_id,
            period,
        )
        from .proactive import create_watchdog_alert_if_new

        create_watchdog_alert_if_new(
            db,
            company_id=realm_id,
            alert_type=ALERT_PERIOD_LOCKED,
            period=period,
            severity="warning",
            title=f"Scheduled entry generation skipped — {period} is locked",
            description=(
                f"The monthly schedule automation did not generate entries for {period} "
                f"because the accounting period is locked. No entries were created and "
                f"the lock was not bypassed. Unlock the period or generate entries "
                f"manually from the Schedules grid (lease months: the lease's Draft "
                f"months action) once appropriate."
            ),
            related_entity_type="financial_calendar",
            context={"period": period, "source": "generate_monthly_schedule_entries"},
        )
        db.commit()
        return {
            "realm_id": realm_id,
            "period": period,
            "status": "skipped_locked",
            "generated": 0,
        }

    # The live engine: consolidated proposals + entries, manual-policy
    # schedules excluded (AC-5). Commits internally; idempotent on re-run.
    result = generate_month_entries(db, realm_id, period, skip_manual_policy=True)

    # AC-5 auto_post_deterministic pass over the batches just drafted.
    auto_post = _auto_post_eligible_batches(db, realm_id=realm_id, period=period, result=result)

    # Phase-C AC-3 (plan 20260702-phase-c-asc842): the SAME realm child also
    # walks ACTIVE non-exempt leases whose commencement JE is booked — one
    # multi-line JE proposal per still-DRAFT lease month up to the period
    # (earlier months are caught up; idempotent: the unique (lease_id, period)
    # row + its proposed_action_id guard), behind the SAME period-lock skip
    # above (a locked period returns before either leg runs, surfaced
    # identically). Manual-policy leases are drafted only on demand
    # (POST /leases/{id}/draft-months).
    from ..services.lease_engine import build_monthly_lease_proposals

    lease_result = build_monthly_lease_proposals(
        db, realm_id=realm_id, period=period, skip_manual_policy=True
    )
    lease_auto_post = _auto_post_lease_proposals(
        db, realm_id=realm_id, period=period, drafted=lease_result["proposals"]
    )

    return {
        "realm_id": realm_id,
        "period": period,
        "status": "ok",
        "generated": result.get("generated", 0),
        "skipped": result.get("skipped", 0),
        "errors": result.get("errors", []),
        "skip_reasons": result.get("skip_reasons", []),
        **auto_post,
        "lease_generated": lease_result.get("generated", 0),
        "lease_skip_reasons": lease_result.get("skip_reasons", []),
        **lease_auto_post,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Celery tasks (beat-scheduled fan-out)
# ─────────────────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=2, name="app.tasks.schedules.generate_realm_schedule_entries")
def generate_realm_schedule_entries(self, qbo_realm_id: str | None = None, period: str | None = None) -> dict:
    """Generate the just-closed month's schedule entries + JE proposals for ONE
    realm (AC-4) and apply its automation policies (AC-5). Strictly
    realm-scoped; never touches another realm.

    IDEMPOTENT-OK: the unique (schedule_id, period) constraint + the engine's
    existing-entry skip + the executor's idempotency ledger mean an
    at-least-once redelivery re-creates nothing — safe under acks_late."""
    if not qbo_realm_id:
        logger.warning("[schedules] missing qbo_realm_id; skipping")
        return {"status": "skipped", "reason": "missing_realm"}

    db = SessionLocal()
    try:
        # Pin org+realm so realm-keyed reads/writes pass FORCE RLS (the
        # systemic in-worker re-pin lesson — a worker has no request guard).
        with tenant_scope_for_realm(db, qbo_realm_id):
            summary = generate_schedule_entries_for_realm(db, realm_id=qbo_realm_id, period=period)
        logger.info(
            "[schedules] realm=%s period=%s status=%s generated=%s auto_posted=%s",
            qbo_realm_id,
            summary["period"],
            summary["status"],
            summary.get("generated", 0),
            len(summary.get("auto_posted") or []),
        )
        return summary
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error(
            "[schedules] realm=%s failed: %s",
            qbo_realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        # Capped exponential backoff (AP-CEL-4): 180s, 360s, bounded at 600s.
        raise self.retry(exc=exc, countdown=min(180 * (2 ** self.request.retries), 600)) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.schedules.generate_monthly_schedule_entries")
def generate_monthly_schedule_entries(period: str | None = None) -> dict:
    """Monthly fan-out (AP-CEL-9): enqueue ``generate_realm_schedule_entries``
    for every mapped, non-suspended realm — one short realm-scoped child each,
    serial within its realm (no unbounded parallel QBO work). Runs early on
    the 1st so the just-closed month's entries await review at close time."""
    from ..models import OrganizationCompany
    from ..services.tenant_companies import company_is_suspended

    period = period or period_for_date()
    db = SessionLocal()
    try:
        # SWEEP orchestration: the cross-tenant enumeration runs under bypass;
        # each fanned-out child re-pins its own realm scope.
        with tenant_scope(bypass=True):  # CROSS-TENANT-OK: vetted monthly sweep over OrganizationCompany (RLS-excluded mapping); each child re-pins its realm
            companies = (
                db.query(OrganizationCompany)
                .filter(OrganizationCompany.realm_id.isnot(None))
                .all()
            )
            dispatchable = [
                company
                for company in companies
                if not company_is_suspended(db, organization_company=company)
            ]
    finally:
        db.close()

    dispatched = 0
    for company in dispatchable:
        if not company.realm_id:
            continue
        generate_realm_schedule_entries.delay(qbo_realm_id=company.realm_id, period=period)
        dispatched += 1
    logger.info("[schedules] period=%s dispatched=%s", period, dispatched)
    return {"period": period, "dispatched": dispatched}

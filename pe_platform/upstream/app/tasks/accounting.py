"""
accounting.py — Celery tasks for the Accounting Module.

Tasks:
  run_accounting_detection(realm_id)      Scan recent txns for capitalizable items
  run_month_end_workflow(realm_id, period) Month-end CLOSE workflow (observe + close)
  check_financial_calendars()             Beat job — runs on 2nd of each month

Wave 3 AC-8 (plan 20260701-phase-a-generic-schedules): the generation leg was
RETIRED from this module. Schedule-entry generation + consolidated JE proposals
for the just-closed month are owned by the day-1 (01:00 UTC) beat fan-out in
app/tasks/schedules.py (generate_monthly_schedule_entries → the LIVE engine
app/services/schedule_engine.generate_month_entries), which is period-lock
aware and applies each schedule's automation_policy. The former
``accounting.generate_period_entries`` task (which generated through the now
deleted vestigial engine app/services/accounting/schedule_engine.py) had no
caller other than run_month_end_workflow's retired leg and was removed with it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from celery import shared_task
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)


@shared_task(name="accounting.run_detection", bind=True, max_retries=2)
def run_accounting_detection(self, realm_id: str) -> dict:
    """
    Scan transactions from the last 30 days for capitalizable/prepayable items.
    For each detection above 50% confidence, create an AccountingSchedule (DETECTED)
    and leave a pending ProposedAction for human confirmation.
    """
    from sqlalchemy import create_engine  # noqa: F401
    from sqlalchemy.orm import sessionmaker  # noqa: F401
    from ..db import SessionLocal
    from ..models import QBOTransaction, AccountingSchedule
    from ..services.accounting.detector import detect_with_ai
    from ..config import settings

    db = SessionLocal()
    detected = 0
    skipped = 0

    try:
        with tenant_scope_for_realm(db, realm_id):
            # Fetch recent transactions not already linked to a schedule
            existing_txn_ids = {
                s.qbo_txn_id for s in db.query(AccountingSchedule.qbo_txn_id)
                .filter(AccountingSchedule.realm_id == realm_id)
                .filter(AccountingSchedule.qbo_txn_id.isnot(None))
                .all()
            }

            cutoff = date.today().replace(day=1)  # Beginning of current month
            txns = db.query(QBOTransaction).filter(
                QBOTransaction.realm_id == realm_id,
                QBOTransaction.date >= cutoff,
                QBOTransaction.amount >= 500,  # Skip small transactions
            ).all()

            # Set up OpenAI client
            openai_client = None
            try:
                import openai
                openai_client = openai.OpenAI(api_key=settings.openai_api_key)
            except Exception:
                pass  # CRAFT-OK: best-effort optional OpenAI client init, pre-existing legacy swallow unchanged by this RLS-wiring edit

            for txn in txns:
                if str(txn.qbo_id) in existing_txn_ids:
                    skipped += 1
                    continue

                result = detect_with_ai(
                    description=txn.description or "",
                    vendor_name=txn.vendor_name or "",
                    amount=float(txn.amount or 0),
                    txn_date=txn.date,
                    openai_client=openai_client,
                )

                if result.schedule_type and result.confidence >= 0.50:
                    schedule = AccountingSchedule(
                        realm_id=realm_id,
                        schedule_type=result.schedule_type,
                        name=result.suggested_name or (txn.description or "")[:60],
                        qbo_txn_id=str(txn.qbo_id),
                        original_amount=float(txn.amount or 0),
                        remaining_amount=float(txn.amount or 0),
                        start_date=result.suggested_start_date or txn.date,
                        useful_life_months=result.suggested_useful_life_months,
                        asset_class=result.suggested_asset_class,
                        status="DETECTED",
                        detection_confidence=result.confidence,
                        ai_reasoning=result.reasoning,
                    )
                    db.add(schedule)
                    detected += 1
                else:
                    skipped += 1

            db.commit()
            logger.info(f"[AccountingTask] Detection complete: detected={detected} skipped={skipped}")
            return {"detected": detected, "skipped": skipped}

    except Exception as exc:
        logger.error(
            "[AccountingTask] Detection failed for realm %s: %s",
            realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        raise self.retry(exc=exc, countdown=60)  # RETRY-FIXED-OK: pre-existing legacy cadence, unchanged by this RLS-wiring edit
    finally:
        db.close()


@shared_task(name="accounting.run_month_end_workflow", bind=True, max_retries=2)
def run_month_end_workflow(self, realm_id: str, period: str) -> dict:
    """
    Month-end PREP workflow (prepares the close; does NOT close the period):
    1. Upsert FinancialCalendar to MONTH_END_IN_PROGRESS
    2. Record the period's observed schedule-entry / JE-proposal state
    3. Detect missing accruals (recurring vendors with no bill this period)
    4. Record prep complete — but do NOT auto-close. A month is closed only by the
       team's EXPLICIT LOCK_PERIOD action (POST /accounting/calendar/{period}/lock),
       never automatically at rollover (B2-6; Steven 2026-07-02: closes take days). The
       period stays MONTH_END_IN_PROGRESS (editable) until that explicit close.

    GENERATION LEG RETIRED (Wave 3 AC-8, plan 20260701-phase-a-generic-schedules):
    this workflow no longer generates ScheduleEntry rows or schedule JE
    proposals. The generation authority is the day-1 (01:00 UTC) beat fan-out
    ``app/tasks/schedules.py::generate_monthly_schedule_entries`` → the LIVE
    engine ``app/services/schedule_engine.generate_month_entries`` (period-lock
    aware, automation_policy aware). Running a day later (the 2nd, 08:00 UTC),
    this workflow only OBSERVES what day 1 produced and closes the calendar —
    it can never regenerate the period, double-propose an amount, or bypass an
    automation policy the beat honored. Ad-hoc generation for an open period
    remains the human grid path (POST /accounting/generate-entries).
    """
    from ..db import SessionLocal
    from ..models import FinancialCalendar, ScheduleEntry
    from ..services.close_run_start import draft_missing_vendor_accruals

    db = SessionLocal()

    try:
        with tenant_scope_for_realm(db, realm_id):
            # 1. Upsert FinancialCalendar
            cal = db.query(FinancialCalendar).filter(
                FinancialCalendar.realm_id == realm_id,
                FinancialCalendar.period == period,
            ).first()

            if not cal:
                cal = FinancialCalendar(realm_id=realm_id, period=period)
                db.add(cal)

            cal.status = "MONTH_END_IN_PROGRESS"
            cal.month_end_triggered_at = datetime.utcnow()
            db.commit()

            # 2. Observed schedule state for the period (generation itself is
            # the day-1 beat's job — see the docstring): schedules_processed =
            # schedules with an entry this period; adjusting_entries_count =
            # distinct consolidated JE proposals linked to those entries.
            period_entries = db.query(ScheduleEntry).filter(
                ScheduleEntry.realm_id == realm_id,
                ScheduleEntry.period == period,
            ).all()
            cal.schedules_processed = len({e.schedule_id for e in period_entries})
            cal.adjusting_entries_count = len(
                {e.proposed_action_id for e in period_entries if e.proposed_action_id}
            )

            # 3. Detect missing accruals — vendors that typically bill monthly but
            # have no transaction this period. The shared drafter (also used by the
            # on-demand close run) resolves the vendor's expense account and the
            # Accrued Liabilities account to real ids, never drafts an unpostable
            # entry (it surfaces a review finding instead), and dedupes by
            # fingerprint so a redelivered task drafts nothing twice.
            accruals = draft_missing_vendor_accruals(
                db,
                realm_id=realm_id,
                period=period,
                agent_run_id=None,
                hard_locked=False,
            )
            missing_count = accruals.missing
            if accruals.unresolved:
                logger.info(
                    "[AccountingTask] %s missing accrual(s) for %s/%s could not be drafted "
                    "(unresolved accounts); review findings surfaced",
                    accruals.unresolved,
                    realm_id,
                    period,
                )

            db.commit()
            cal.missing_accruals_count = missing_count

            # 4. Record prep complete — do NOT auto-close. Steven 2026-07-02 (B2-6): a month
            # is closed only by the team's EXPLICIT action (POST /accounting/calendar/{period}/lock
            # → LOCK_PERIOD), never automatically at rollover — closes take days. Setting
            # status='CLOSED' here was the B2-6 bug (June soft-CLOSED across ~20 realms → cascaded
            # edit-locked). The period stays MONTH_END_IN_PROGRESS (neither soft-CLOSED nor
            # hard-LOCKED → editable) until an explicit close; month_end_completed_at + notes
            # record that prep ran. (MONTH_END_IN_PROGRESS also stops check_financial_calendars
            # — which triggers only on OPEN prior-month periods — from re-running prep every tick.)
            cal.month_end_completed_at = datetime.utcnow()
            cal.notes = {
                "schedules_processed": cal.schedules_processed,
                "adjusting_entries": cal.adjusting_entries_count,
                "missing_accruals": missing_count,
                "missing_accrual_proposals": accruals.drafted,
                "missing_accrual_unresolved": accruals.unresolved,
            }
            db.commit()

            logger.info(f"[AccountingTask] Month-end complete for {realm_id}/{period}")
            return {
                "period": period,
                "schedules_processed": cal.schedules_processed,
                "adjusting_entries": cal.adjusting_entries_count,
                "missing_accruals": missing_count,
            }

    except Exception as exc:
        safe_error = redact_to_string(str(exc), max_length=1000)
        logger.error("[AccountingTask] Month-end workflow failed: %s", safe_error)
        if db:
            try:
                with tenant_scope_for_realm(db, realm_id):
                    cal2 = db.query(FinancialCalendar).filter(
                        FinancialCalendar.realm_id == realm_id,
                        FinancialCalendar.period == period,
                    ).first()
                    if cal2:
                        cal2.status = "OPEN"
                        cal2.notes = {"error": safe_error}
                        db.commit()
            except Exception:
                pass  # CRAFT-OK: best-effort calendar-status rollback, pre-existing legacy swallow unchanged by this RLS-wiring edit
        raise self.retry(exc=exc, countdown=120)  # RETRY-FIXED-OK: pre-existing legacy cadence, unchanged by this RLS-wiring edit
    finally:
        db.close()


@shared_task(name="accounting.check_financial_calendars")
def check_financial_calendars() -> dict:
    """
    Celery Beat job — runs on the 2nd of each month.
    Finds all realm_ids with OPEN prior-month periods and triggers month-end workflows.
    """
    from ..db import SessionLocal
    from ..models import QBOToken

    db = SessionLocal()
    triggered = 0

    try:
        today = date.today()
        # Prior month
        if today.month == 1:
            prior_year, prior_month = today.year - 1, 12
        else:
            prior_year, prior_month = today.year, today.month - 1
        prior_period = f"{prior_year}-{prior_month:02d}"

        # Cross-tenant orchestration: enumerate every connected realm.
        with tenant_scope(bypass=True):
            realms = db.query(QBOToken.realm_id).all()

        for (realm_id,) in realms:
            from ..models import FinancialCalendar
            # Per-tenant unit of work: re-scope the existence check to this realm.
            with tenant_scope_for_realm(db, realm_id):
                existing = db.query(FinancialCalendar).filter(
                    FinancialCalendar.realm_id == realm_id,
                    FinancialCalendar.period == prior_period,
                    FinancialCalendar.status == "CLOSED",
                ).first()

            if not existing:
                # Fan out to the per-tenant workflow (which self-scopes).
                run_month_end_workflow.delay(realm_id, prior_period)
                triggered += 1
                logger.info(f"[AccountingBeat] Triggered month-end for {realm_id}/{prior_period}")

        return {"triggered": triggered, "prior_period": prior_period}
    finally:
        db.close()

"""
app/tasks/reporting.py
──────────────────────
Background tasks for report generation and scheduled delivery.

Tasks:
  - generate_monthly_reports: Batch generate month-end reports for all companies
  - process_report_schedules: Hourly check — generate & email reports per schedule
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import QBOCompanyInfo, Report, ReportSchedule
from ..tenant_rls import clear_tenant_scope, pin_tenant_scope_live, tenant_scope, tenant_scope_live
from ..services.tenant_companies import organization_for_company
from ..reporting.report_builder import build_report_data
from ..reporting.pdf_renderer import render_pdf
from ..reporting.excel_renderer import render_excel
from ..services.email_service import send_report_email
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Batch month-end reports (existing)
# ─────────────────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3)
def generate_monthly_reports(self):
    """
    Background task to generate month-end reports for all active companies.
    Usually scheduled via celery-beat shortly after the month ends.
    """
    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() unchanged below (re-indent only)
    try:
        last_month = date.today().replace(day=1) - timedelta(days=1)
        period = last_month.strftime("%Y-%m")

        # SWEEP orchestration: enumerating all companies is cross-tenant → bypass.
        with tenant_scope(bypass=True):
            companies = db.query(QBOCompanyInfo).all()

        for company in companies:
            # Per-tenant unit re-scoped to this realm; never left under bypass.
            # §B: pin org+realm (qbo_reports/qbo_accounts/qbo_tokens are org-keyed
            # under live RLS). Use the LIVE variant: this loop has NO per-iteration
            # commit (the single db.commit() is after the loop), so the bypass sweep
            # above keeps this ONE transaction open — a plain ContextVar tenant_scope
            # would never re-emit (no new begin) and the whole batch would silently
            # run under the sweep's bypass (RLS disabled). tenant_scope_live emits
            # the org+realm GUC onto the already-open transaction now and restores on
            # exit. (Realm-keyed reads/writes are covered by the realm GUC; the
            # org-keyed qbo_tokens read happens via get_qbo_client_from_db, which
            # re-pins org+realm itself.)
            company_org = organization_for_company(db, company.realm_id)
            with tenant_scope_live(db, org_id=company_org.id if company_org else None, realm_id=company.realm_id):
                payload = build_report_data(db, company.realm_id, period)  # DOCGEN-OK: gated by the enclosing org+realm tenant_scope_live RLS pin emitted onto this transaction
                pdf_bytes = render_pdf(payload)  # noqa: F841

                existing = db.query(Report).filter(
                    Report.company_id == company.realm_id,
                    Report.period == period,
                ).first()

                if not existing:
                    report = Report(
                        company_id=company.realm_id,
                        period=period,
                        file_path="generated_from_batch",
                        generated_by="system_celery",
                    )
                    db.add(report)

        db.commit()

    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=300)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Scheduled report processing (Phase 6)
# ─────────────────────────────────────────────────────────────────────────────

def _should_fire(schedule: ReportSchedule, now: datetime) -> bool:
    """Check if a schedule should fire at the current hour."""
    # Must match the configured hour (UTC)
    target_hour = schedule.hour_utc if schedule.hour_utc is not None else 8
    if now.hour != target_hour:
        return False

    freq = (schedule.frequency or "").upper()

    if freq == "WEEKLY":
        # day_of_week: 0=Mon..6=Sun  (Python weekday() is 0=Mon..6=Sun)
        target_dow = schedule.day_of_week if schedule.day_of_week is not None else 0
        return now.weekday() == target_dow

    elif freq == "MONTHLY":
        target_dom = schedule.day_of_month or 1
        return now.day == target_dom

    elif freq == "QUARTERLY":
        target_dom = schedule.day_of_month or 1
        # Fire in Jan, Apr, Jul, Oct (months 1, 4, 7, 10)
        return now.day == target_dom and now.month in (1, 4, 7, 10)

    return False


def _was_sent_this_period(schedule: ReportSchedule, now: datetime) -> bool:
    """Guard against double-sends if the task runs more than once per hour."""
    if not schedule.last_sent_at:
        return False
    # If sent in the last 12 hours, skip
    return (now - schedule.last_sent_at.replace(tzinfo=None)) < timedelta(hours=12)


def _determine_period(freq: str) -> str:
    """Determine the report period based on frequency."""
    today = date.today()

    if freq == "WEEKLY":
        # Previous 7-day period ending yesterday
        end = today - timedelta(days=1)
        return end.strftime("%Y-%m")

    elif freq == "QUARTERLY":
        # Previous quarter
        month = today.month
        if month <= 3:
            return f"{today.year - 1}-Q4"
        elif month <= 6:
            return f"{today.year}-Q1"
        elif month <= 9:
            return f"{today.year}-Q2"
        else:
            return f"{today.year}-Q3"

    else:
        # MONTHLY — previous month
        prev = today.replace(day=1) - timedelta(days=1)
        return prev.strftime("%Y-%m")


@celery_app.task(bind=True, max_retries=2, name="app.tasks.reporting.process_report_schedules")
def process_report_schedules(self):
    """
    Hourly Celery beat task: check all enabled ReportSchedules and fire
    any that are due. Generates reports and emails them to recipients.
    """
    db = SessionLocal()
    now = datetime.utcnow()
    processed = 0
    errors = 0

    try:
        # SWEEP orchestration: enumerating all enabled schedules is cross-tenant → bypass.
        with tenant_scope(bypass=True):
            schedules = db.query(ReportSchedule).filter(  # CROSS-TENANT-OK: sweep orchestration enumerates every tenant's schedules under tenant_scope(bypass=True); each is re-pinned per-tenant below
                ReportSchedule.enabled == True,  # noqa: E712
            ).all()

        for sched in schedules:
            if not _should_fire(sched, now):
                continue

            if _was_sent_this_period(sched, now):
                logger.info(f"[Reporting] Schedule '{sched.name}' already sent this period — skipping")
                continue

            # Per-tenant unit: re-pin RLS to this schedule's realm; cleared in the finally
            # below so the next iteration never runs under a stale scope or under bypass.
            sched_org = organization_for_company(db, sched.company_id)
            # pin (emit-now) not set (emit-next-begin): the cross-tenant sweep
            # query above (and this iteration's org lookup) already opened the
            # transaction, so a plain set_tenant_scope would leave the GUC at the
            # sweep's stale scope and build_report_data would read the wrong/zero
            # rows under FORCE-RLS (INCIDENT-2). pin emits the realm+org GUC onto
            # the in-flight transaction now; clear_tenant_scope in the finally
            # still drops it before the next iteration / the next begin.
            pin_tenant_scope_live(db, org_id=sched_org.id if sched_org else None, realm_id=sched.company_id)
            try:
                freq = (sched.frequency or "MONTHLY").upper()
                period = _determine_period(freq)

                logger.info(
                    f"[Reporting] Firing schedule '{sched.name}' "
                    f"(id={sched.id}, freq={freq}) for period {period}"
                )

                # Generate report data
                payload = build_report_data(db, sched.company_id, period)  # DOCGEN-OK: gated by the pin_tenant_scope_live(org_id, realm_id) RLS pin emitted onto this transaction above
                kpis = payload.get("kpis", {})

                # Build attachments based on format
                attachments = []
                render_errors = []
                fmt = (sched.report_format or "pdf").lower()

                if fmt in ("pdf", "both"):
                    try:
                        pdf_bytes = render_pdf(payload)
                        attachments.append({
                            "filename": f"peregrine_report_{period}.pdf",
                            "content": pdf_bytes,
                            "mime_type": "application/pdf",
                        })
                    except Exception as e:
                        render_errors.append("pdf")
                        logger.error(
                            "[Reporting] PDF render failed for schedule %s: %s",
                            sched.id,
                            redact_to_string(str(e), max_length=500),
                        )

                if fmt in ("excel", "both"):
                    try:
                        excel_bytes = render_excel(payload)
                        attachments.append({
                            "filename": f"peregrine_report_{period}.xlsx",
                            "content": excel_bytes,
                            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        })
                    except Exception as e:
                        render_errors.append("excel")
                        logger.error(
                            "[Reporting] Excel render failed for schedule %s: %s",
                            sched.id,
                            redact_to_string(str(e), max_length=500),
                        )

                if fmt in ("pdf", "excel", "both") and not attachments:
                    requested = " and ".join(render_errors) if render_errors else fmt
                    raise RuntimeError(f"Scheduled report rendering failed for {requested}")

                # Send email
                recipients = sched.recipients or []
                if recipients and attachments:
                    send_report_email(
                        db=db,
                        company_id=sched.company_id,
                        period=period,
                        recipients=recipients,
                        attachments=attachments,
                        kpis=kpis,
                        schedule_name=sched.name,
                    )

                # Upsert Report record
                existing_report = db.query(Report).filter(
                    Report.company_id == sched.company_id,
                    Report.period == period,
                ).first()

                if not existing_report:
                    report = Report(
                        company_id=sched.company_id,
                        period=period,
                        file_path="generated_by_schedule",
                        report_type=freq.lower(),
                        generated_by=f"schedule:{sched.id}",
                    )
                    db.add(report)

                # Update last_sent_at
                sched.last_sent_at = now
                db.commit()
                processed += 1

                logger.info(
                    f"[Reporting] Schedule '{sched.name}' completed — "
                    f"sent to {len(recipients)} recipient(s), "
                    f"{len(attachments)} attachment(s)"
                )

            except Exception as e:
                errors += 1
                logger.error(
                    "[Reporting] Schedule '%s' (id=%s) failed: %s",
                    sched.name,
                    sched.id,
                    redact_to_string(str(e), max_length=500),
                )
                db.rollback()
            finally:
                # Drop the per-tenant scope before the next iteration / before the
                # sweep returns — never carry a stale realm pin forward.
                clear_tenant_scope()

    except Exception as exc:
        logger.error(
            "[Reporting] process_report_schedules failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
        db.rollback()
        raise self.retry(exc=exc, countdown=600)
    finally:
        db.close()

    return {"processed": processed, "errors": errors, "checked": len(schedules)}

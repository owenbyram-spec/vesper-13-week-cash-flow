"""
Background tasks for scheduled agent objectives.
"""
from __future__ import annotations

import logging
import math
import os
import random
from datetime import date, datetime, timedelta
from typing import Any, Dict

from sqlalchemy import or_

from ..agent.tracking import AgentRunTracker, RUN_TERMINAL_STATES, transition_run
from ..celery_app import celery_app
from ..config import settings
from ..db import SessionLocal
from ..models import AgentObjective, AgentRun, Organization, QBOCompanyInfo
from ..services.llm_task_context import CloseBudgetExceededError  # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1
from ..services.redaction import redact_to_string
from ..services.tenant_companies import (
    company_is_suspended,
    organization_company_for_realm,
    organization_for_company,
)
from ..tenant_rls import tenant_scope, tenant_scope_for_realm, tenant_scope_live

# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 — re-export: the
# stuck-close-run reaper lives in its own module (app/tasks/close_watchdog.py,
# registered via app/tasks/__init__.py), re-exported here because the close-run
# machinery's callers/tests reach for it alongside the other agent-run sweeps.
from .close_watchdog import reap_stale_close_runs  # noqa: F401

logger = logging.getLogger(__name__)


class CloseRunLeaseHeldError(RuntimeError):
    """A sibling delivery owns the live realm lease; this delivery must retry."""


class CloseRunPayloadError(RuntimeError):
    """The immutable task payload does not match its pre-created run."""


class CloseRunAlreadyTerminalError(RuntimeError):
    """A first terminal outcome won before this delivery could adopt the run."""

    def __init__(self, run_id: int, run_status: str):
        self.run_id = run_id
        self.run_status = run_status
        super().__init__(f"Close run {run_id} is already terminal ({run_status})")


class CloseRunTerminalPersistenceError(RuntimeError):
    """The close-specific terminal state could not be durably verified."""


CLOSE_TASK_MAX_FAILURE_RETRIES = 2
CLOSE_LEASE_RETRY_SECONDS = 15


def _close_lease_wait_limit() -> int:
    """Finite lease waits spanning at least one configured stale-lease TTL."""
    ttl_seconds = max(1, int(settings.close_lock_ttl_seconds))
    return max(1, math.ceil(ttl_seconds / CLOSE_LEASE_RETRY_SECONDS))


def _close_run_identity(run: AgentRun) -> dict[str, Any]:
    """Capture the exact tenant-bound identity used for every close-run lock."""
    return {
        "id": run.id,
        "objective_id": run.objective_id,
        "company_id": run.company_id,
        "organization_id": run.organization_id,
        "organization_company_id": run.organization_company_id,
    }


def _scoped_close_run_query(db, identity: dict[str, Any]):
    """Build an explicitly tenant-bound query for one durable close run."""
    return db.query(AgentRun).filter(
        AgentRun.id == identity["id"],
        AgentRun.objective_id == identity["objective_id"],
        AgentRun.company_id == identity["company_id"],
        AgentRun.organization_id == identity["organization_id"],
        AgentRun.organization_company_id == identity["organization_company_id"],
    )


def _locked_close_run(db, run: AgentRun) -> AgentRun | None:
    """Refresh one exact run while holding its database row lock."""
    identity = _close_run_identity(run)
    return (
        _scoped_close_run_query(db, identity)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )


def _lock_close_run_for_adoption(db, existing_run: AgentRun) -> AgentRun:
    """Atomically refresh a close run before queued/running adoption.

    ``populate_existing`` defeats a stale identity-map object, and the row lock
    serializes this decision with a concurrent terminal writer.  Terminal state
    is checked only on the fresh locked row, so it can never be reopened.
    """
    fresh_run = _locked_close_run(db, existing_run)
    if fresh_run is None:
        raise CloseRunPayloadError("Close run no longer matches its tenant scope")
    run_status = str(fresh_run.status or "")
    if run_status in RUN_TERMINAL_STATES:
        run_id = fresh_run.id
        db.rollback()
        raise CloseRunAlreadyTerminalError(run_id, run_status)
    return fresh_run


def _strict_finish_close_run(
    db,
    tracker: AgentRunTracker,
    *,
    output_summary: str,
) -> None:
    """Finish a close run and prove the terminal commit actually persisted.

    ``AgentRunTracker.finish`` intentionally remains fail-open for global
    observability.  Close orchestration is different: returning task success
    while its durable run is still ``running`` strands the cockpit forever.  We
    therefore row-lock a fresh active run, invoke the shared tracker, then issue
    an independent refresh and verify the exact terminal outcome.
    """
    current_run = tracker.run
    if current_run is None:
        raise CloseRunTerminalPersistenceError(
            "Close run tracker lost its durable run before terminal success"
        )
    identity = _close_run_identity(current_run)
    try:
        fresh_run = (
            _scoped_close_run_query(db, identity)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if fresh_run is None:
            raise CloseRunTerminalPersistenceError(
                "Close run disappeared before terminal success"
            )
        fresh_status = str(fresh_run.status or "")
        if fresh_status in RUN_TERMINAL_STATES:
            raise CloseRunAlreadyTerminalError(fresh_run.id, fresh_status)

        tracker.run = fresh_run
        tracker._run_id = fresh_run.id
        tracker.finish(status="succeeded", output_summary=output_summary)

        verified = (
            _scoped_close_run_query(db, identity)
            .populate_existing()
            .one_or_none()
        )
        if (
            verified is None
            or str(verified.status or "") != "succeeded"
            or verified.finished_at is None
            or verified.output_summary != output_summary
        ):
            raise CloseRunTerminalPersistenceError(
                "Close run terminal success was not durably persisted"
            )
        tracker.run = verified
    except CloseRunAlreadyTerminalError:
        db.rollback()
        raise
    except CloseRunTerminalPersistenceError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise CloseRunTerminalPersistenceError(
            "Close run terminal success could not be verified"
        ) from exc


def _resolve_objective_scope(db, objective: AgentObjective):
    """Validate that a scheduled objective is bound to the organization/entity it will run against."""
    organization = None
    if objective.organization_id:
        organization = db.query(Organization).filter(Organization.id == objective.organization_id).first()
        if not organization:
            raise PermissionError("Agent objective organization no longer exists")
    else:
        organization = organization_for_company(db, objective.company_id)

    if not organization:
        if objective.organization_id or objective.organization_company_id:
            raise PermissionError("Agent objective organization scope is invalid")
        return None, None
    if not organization.is_active:
        raise PermissionError("Agent objective organization is suspended")

    organization_company = organization_company_for_realm(
        db,
        organization_id=organization.id,
        realm_id=objective.company_id,
    )
    if not organization_company:
        raise PermissionError("Agent objective company does not belong to its organization")
    if company_is_suspended(db, organization_company=organization_company):
        raise PermissionError("Agent objective company is suspended")
    if objective.organization_company_id and objective.organization_company_id != organization_company.id:
        raise PermissionError("Agent objective entity scope does not match company mapping")

    if objective.organization_id is None:
        objective.organization_id = organization.id
    if objective.organization_company_id is None:
        objective.organization_company_id = organization_company.id
    return organization, organization_company


def _last_month_period(today: date | None = None) -> str:
    base = today or date.today()
    last_month = base.replace(day=1) - timedelta(days=1)
    return f"{last_month.year}-{last_month.month:02d}"


def _run_morning_briefing(db, objective: AgentObjective) -> Dict[str, Any]:
    from .proactive import _generate_executive_briefing, _send_briefing_email

    company_info = db.query(QBOCompanyInfo).filter_by(realm_id=objective.company_id).first()
    company_name = company_info.company_name if company_info else objective.company_id
    period = _last_month_period()

    _generate_executive_briefing(db, objective.company_id, company_name, period)

    notification_policy = objective.notification_policy or {}
    email_enabled = notification_policy.get("email", True)
    delivery = {"status": "disabled"}
    if email_enabled:
        delivery = _send_briefing_email(db, objective.company_id, company_name)

    return {
        "company_id": objective.company_id,
        "company_name": company_name,
        "period": period,
        "email_sent": delivery.get("status") in ("sent", "already_sent"),
        "delivery": delivery,
        "status": "partial" if delivery.get("status") in ("partial", "failed", "retryable", "uncertain", "unavailable", "sending", "ready") else "succeeded",
    }


def _run_cash_watch(db, objective: AgentObjective) -> Dict[str, Any]:
    from ..qbo_client import get_qbo_client_from_db
    from .proactive import _run_predictive_cash

    qbo_client = get_qbo_client_from_db(db, objective.company_id)
    result = _run_predictive_cash(db, objective.company_id, qbo_client) or {}
    summary = result.get("summary", {})
    return {
        "company_id": objective.company_id,
        "status": "succeeded" if summary else "unavailable",
        "runway_weeks": summary.get("runway_weeks"),
        "negative_cash_weeks": summary.get("negative_cash_weeks", []),
        "lowest_balance": summary.get("lowest_balance"),
        "lowest_balance_week": summary.get("lowest_balance_week"),
    }


def _run_budget_variance_watch(db, objective: AgentObjective) -> Dict[str, Any]:
    from ..qbo_client import get_qbo_client_from_db
    from .proactive import check_budget_variance_and_propose_updates

    company_info = db.query(QBOCompanyInfo).filter_by(realm_id=objective.company_id).first()
    company_name = company_info.company_name if company_info else objective.company_id
    period = _last_month_period()
    qbo_client = get_qbo_client_from_db(db, objective.company_id)
    check_budget_variance_and_propose_updates(db, objective.company_id, company_name, period, qbo_client)
    return {
        "company_id": objective.company_id,
        "company_name": company_name,
        "period": period,
        "status": "succeeded",
    }


def _run_qbo_sync_health(db, objective: AgentObjective) -> Dict[str, Any]:
    from ..models import QBOToken, WatchdogAlert

    token = db.query(QBOToken).filter(QBOToken.realm_id == objective.company_id).first()
    if not token:
        return {"company_id": objective.company_id, "status": "missing_token"}

    now = datetime.utcnow()
    access_expires = token.access_expires_at.replace(tzinfo=None)
    refresh_expires = token.refresh_expires_at.replace(tzinfo=None)
    access_minutes = int((access_expires - now).total_seconds() / 60)
    refresh_days = int((refresh_expires - now).total_seconds() / 86400)

    alert_created = False
    if refresh_days <= 7:
        existing = db.query(WatchdogAlert).filter(
            WatchdogAlert.company_id == objective.company_id,
            WatchdogAlert.alert_type == "QBO_REFRESH_TOKEN_EXPIRING",
            WatchdogAlert.acknowledged == False,  # noqa: E712
        ).first()
        if not existing:
            db.add(WatchdogAlert(
                company_id=objective.company_id,
                alert_type="QBO_REFRESH_TOKEN_EXPIRING",
                severity="critical" if refresh_days <= 1 else "warning",
                title="QuickBooks connection needs attention",
                description=f"QBO refresh token expires in {refresh_days} day(s). Reconnect QuickBooks to avoid sync interruption.",
                context={"refresh_days_remaining": refresh_days, "access_minutes_remaining": access_minutes},
            ))
            db.commit()
            alert_created = True

    return {
        "company_id": objective.company_id,
        "status": "ok" if refresh_days > 7 else "attention_needed",
        "access_minutes_remaining": access_minutes,
        "refresh_days_remaining": refresh_days,
        "alert_created": alert_created,
    }


def _run_report_pack(db, objective: AgentObjective) -> Dict[str, Any]:
    from ..models import Report
    from ..reporting.excel_renderer import render_excel
    from ..reporting.pdf_renderer import render_pdf
    from ..reporting.report_builder import build_report_data
    from ..services.email_service import send_report_email

    notification_policy = objective.notification_policy or {}
    period = notification_policy.get("period") or _last_month_period()
    report_format = str(notification_policy.get("report_format", "pdf")).lower()
    recipients = notification_policy.get("recipients") or []

    payload = build_report_data(db, objective.company_id, period)
    attachments = []
    if report_format in ("pdf", "both"):
        attachments.append({
            "filename": f"peregrine_report_{period}.pdf",
            "content": render_pdf(payload),
            "mime_type": "application/pdf",
        })
    if report_format in ("excel", "both"):
        attachments.append({
            "filename": f"peregrine_report_{period}.xlsx",
            "content": render_excel(payload),
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })

    email_sent = False
    if recipients and attachments:
        email_sent = send_report_email(
            db=db,
            company_id=objective.company_id,
            period=period,
            recipients=recipients,
            attachments=attachments,
            kpis=payload.get("kpis", {}),
            schedule_name=objective.name,
        )

    existing_report = db.query(Report).filter(
        Report.company_id == objective.company_id,
        Report.period == period,
    ).first()
    if not existing_report:
        db.add(Report(
            company_id=objective.company_id,
            period=period,
            file_path="generated_by_agent_objective",
            report_type="agent_report_pack",
            generated_by=f"agent_objective:{objective.id}",
        ))
        db.commit()

    return {
        "company_id": objective.company_id,
        "period": period,
        "status": "partial" if recipients and not email_sent else "succeeded",
        "format": report_format,
        "attachments": [a["filename"] for a in attachments],
        "email_sent": email_sent,
    }


def _run_transaction_review(db, objective: AgentObjective) -> Dict[str, Any]:
    # The pull service, not the HTTP route: a route handler called directly keeps its
    # Depends(...) defaults as the "user" and crashes in check_tenant_access. The
    # objective is already bound to its realm, so it runs under that realm's scope.
    from ..cashflow.actuals_pull import pull_week_actuals

    notification_policy = objective.notification_policy or {}
    week_ending = notification_policy.get("week_ending")
    with tenant_scope_for_realm(db, objective.company_id):
        result = pull_week_actuals(
            db,
            objective.company_id,
            date.fromisoformat(week_ending) if isinstance(week_ending, str) and week_ending else None,
        )
    transactions = result.get("transactions") or []
    categorized = sum(1 for item in transactions if item.get("category") == "categorized")
    uncategorized = sum(1 for item in transactions if item.get("category") == "uncategorized")
    excluded = sum(1 for item in transactions if item.get("category") == "excluded")
    # A partial pull (a source failed or was truncated) must not read as a clean run.
    data_quality = result.get("data_quality") or {}
    errors = result.get("errors") or [
        {"error_code": code} for code in data_quality.get("unavailable_sections") or []
    ]
    return {
        "company_id": objective.company_id,
        "week_ending": result.get("week_ending"),
        "week_start": result.get("week_start"),
        "status": result.get("status"),
        "count": result.get("count", len(transactions)),
        "categorized_count": categorized,
        "uncategorized_count": uncategorized,
        "excluded_count": excluded,
        "errors": errors,
    }


def _run_close_orchestration(
    db,
    objective: AgentObjective,
    *,
    tracker: AgentRunTracker,
    period: str,
) -> dict[str, Any]:  # CRAFT-OK: dynamic objective-runner summary-dict contract, mirrors every other _run_* runner in this module
    """Step-walk the realm's CloseTask checklist (resumable, single-flight,
    proposal-first). Distinct signature from the other runners: it needs the
    ``tracker`` for the per-step proposal step-link + the agent run id. The
    ``_execute_objective`` dispatch below passes the tracker for this one runner;
    the others are unchanged (additive)."""
    from ..agent.runtime.close_orchestration import run_close_orchestration

    return run_close_orchestration(db, objective, tracker, period=period)


# PLAN-EXEMPT: ratified plan 20260702-qa-batch2 B2-4 AC-1 covers app/tasks/agent.py (status.json
# surface_scope + plan.md); the plan-gate's active-plan.json surface_scope is stale-empty (its
# populator needs a genuine human ratify turn to re-derive scope, which this session cannot forge).
CLOSE_OBJECTIVE_TYPE = "close_orchestration"


def _close_run_summary(result: Dict[str, Any]) -> str:  # CRAFT-OK: result is the dynamic close-runner summary dict (run_close_orchestration return); read defensively
    """Human-readable terminal outcome for a close-orchestration run (B2-4 AC-1/AC-3).

    ``run_close_orchestration`` returns a summary dict whose shape depends on the
    exit path; ``_execute_objective`` persists this string on the AgentRun's
    ``output_summary`` (an EXISTING column — no new field, no migration) so
    ``GET /agent/close-runs`` can report WHY a start produced no visible run:

      - ``{"status":"skipped","reason":"run_lock_held"}`` → a prior run's lease is
        still live; this start deferred. The cockpit must say so, not fall silent.
      - ``{"status":"succeeded","walked":0}`` → every close task was already done
        (human-complete or a prior agent run) → nothing to close.
      - ``{"status":"succeeded","walked":N,"proposed":M,...}`` → a real walk; report
        what it reviewed / drafted / skipped so the outcome is never opaque.

    Defensive by design: a shape this does not recognize falls back to a compact,
    non-crashing description rather than raising inside the terminal-finish path.
    """
    status = result.get("status")
    if status == "skipped" and result.get("reason") == "run_lock_held":
        return "A close run is already in progress for this company; this start was skipped."
    if status == "skipped" and result.get("reason") == "period_locked":
        return "Close preparation was skipped because this period is explicitly locked."
    if status == "skipped":
        reason = result.get("reason") or "unknown reason"
        return f"Close run skipped ({reason})."
    if status == "succeeded":
        walked = int(result.get("walked") or 0)
        if walked == 0:
            return "Nothing to close — all close steps are already complete."
        proposed = int(result.get("proposed") or 0)
        refused_locked = int(result.get("refused_locked") or 0)
        parts = [
            f"Reviewed {walked} close {'step' if walked == 1 else 'steps'}",
            f"drafted {proposed} {'proposal' if proposed == 1 else 'proposals'} for approval",
        ]
        if refused_locked:
            parts.append(f"skipped {refused_locked} in a locked period")
        return "; ".join(parts) + "."
    # Unrecognized shape — describe it compactly without ever raising.
    return f"Close run finished (status={status})."


# ── Wave 2.1 collections_review runner (plan 20260713-wave2-1-ar-collections, AC-6) ──
# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C4 (marker
# RATIFICATION-2026-07-13.md on disk; dispatched session lacks active-plan state).

# Draft cap per run (decision 13): env-tunable, read at CALL time (never frozen
# at import) so an operator can retune without a redeploy.
AR_COLLECTIONS_MAX_DRAFTS_PER_RUN_DEFAULT = 10
# The result/artifact chase list is bounded so a 1000-invoice book cannot bloat
# the AgentStep output payload / InsightArtifact JSON; candidate_count still
# reports the FULL scored population honestly.
AR_COLLECTIONS_CHASE_LIST_LIMIT = 25


def _fetch_open_ar_invoices(qbo_client, limit: int = 1000) -> list:
    """Open AR invoices (Balance > 0) in the receivables_payables row shape.

    The GET /ar-ap/open-invoices handler cannot be called from a worker (its
    ``current_user`` Depends has no request context — the DI-MISUSE class), so
    the same bounded QBO query + row shape lives here for the runner lane.
    """
    data = qbo_client.query(
        f"SELECT * FROM Invoice WHERE Balance > '0' ORDERBY DueDate ASC MAXRESULTS {int(limit)}"
    )
    items = data.get("QueryResponse", {}).get("Invoice", [])
    today = date.today()
    rows = []
    for inv in items:
        due_date_str = inv.get("DueDate", "")  # SA-OK: dict access on QBO JSON — no DB query in this loop
        days_overdue = 0
        if due_date_str:
            try:
                due = date.fromisoformat(due_date_str)
                days_overdue = max(0, (today - due).days)
            except ValueError:
                pass
        try:
            balance = float(inv.get("Balance") or 0.0)  # SA-OK: dict access on QBO JSON — no DB query in this loop
        except (TypeError, ValueError):
            balance = 0.0
        rows.append(
            {
                "id": inv.get("Id"),  # SA-OK: dict access on QBO JSON — no DB query in this loop
                "doc_number": inv.get("DocNumber", "—"),  # SA-OK: dict access on QBO JSON — no DB query in this loop
                "customer": (inv.get("CustomerRef") or {}).get("name", "Unknown"),  # SA-OK: dict access on QBO JSON — no DB query in this loop
                "balance": balance,
                "txn_date": inv.get("TxnDate", ""),  # SA-OK: dict access on QBO JSON — no DB query in this loop
                "due_date": due_date_str,
                "days_overdue": days_overdue,
            }
        )
    return rows


def _run_collections_review(db, objective: AgentObjective, *, tracker: "AgentRunTracker | None" = None) -> dict:
    """AR collections review: prioritized chase list + top-N reminder DRAFTS.

    Composes the existing AR analytics (open invoices + aging + payment LATENESS
    + collection haircuts — each fetched in its OWN try so one failure degrades
    honestly instead of killing the run), scores them with the PURE
    ``prioritize_collections``, and drafts ``SEND_INVOICE_REMINDER`` proposals
    for the top-N via C1's ``propose_invoice_reminder`` (PENDING, human-gated —
    this runner NEVER executes, approves, or sends anything). A draft whose
    would-be ``inputs_fingerprint`` already has a PENDING row is SKIPPED, so a
    beat re-run / redelivery drafts ZERO duplicates (decision 13 / E8).
    """
    from ..analysis.ar_aging import ar_aging
    from ..analysis.collections_priority import prioritize_collections
    from ..analysis.payment_behavior import compute_customer_dso, derive_collection_haircuts
    from ..config import _env_int
    from ..models import ProposedAction
    from ..qbo_actions.proposal_fingerprint import compute_proposal_fingerprint
    from ..qbo_actions.proposer import propose_invoice_reminder
    from ..qbo_client import get_qbo_client_from_db

    qbo_client = get_qbo_client_from_db(db, objective.company_id)

    # Each analytic in its OWN try: a failure contributes NOTHING to the score
    # (the pure scorer names the None input in missing_signals) — never a
    # fabricated component (AC-6 honest degradation).
    open_invoices = None
    try:
        open_invoices = _fetch_open_ar_invoices(qbo_client)
    except Exception as exc:
        logger.warning(
            "[collections_review] open-invoice fetch failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
    aging = None
    try:
        aging = ar_aging(qbo_client)
    except Exception as exc:
        logger.warning(
            "[collections_review] ar_aging failed: %s", redact_to_string(str(exc), max_length=500)
        )
    # HONEST LABEL (AP-FIN-09): compute_customer_dso returns median payment
    # LATENESS (payment_date − due_date days), not a Days-Sales-Outstanding
    # ratio — it is named lateness everywhere downstream of this call.
    lateness_by_customer = None
    try:
        lateness_by_customer = compute_customer_dso(qbo_client)
    except Exception as exc:
        logger.warning(
            "[collections_review] payment-lateness fetch failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
    haircuts = None
    try:
        haircuts = derive_collection_haircuts(qbo_client)
    except Exception as exc:
        logger.warning(
            "[collections_review] collection-haircut fetch failed: %s",
            redact_to_string(str(exc), max_length=500),
        )

    scored = prioritize_collections(
        open_invoices=open_invoices,
        aging=aging,
        lateness_by_customer=lateness_by_customer,
        haircuts=haircuts,
    )
    chase_list = scored["chase_list"]
    missing_signals = scored["missing_signals"]

    # Call-time env read (decision 13): retunable without redeploy; malformed
    # values degrade to the default via _env_int.
    draft_cap = max(_env_int("AR_COLLECTIONS_MAX_DRAFTS_PER_RUN", AR_COLLECTIONS_MAX_DRAFTS_PER_RUN_DEFAULT), 0)
    reminder_date = date.today().isoformat()

    # Pre-compute every would-be fingerprint, then ONE batched existence query
    # (no per-draft query loop). The payload below is EXACTLY what the proposer
    # persists for reminders (no PrivateNote stamp on the send type), so the
    # pre-computed fingerprint matches the before_insert listener's stamp.
    draft_candidates = []
    for item in chase_list[:draft_cap]:
        payload = {
            "invoice_id": item["invoice_id"],
            "reminder_date": reminder_date,
            "CustomerRef": {"name": item["customer"]},
            "doc_number": item["doc_number"],
            "balance": item["balance"],
            "days_overdue": item["days_overdue"],
            "due_date": item["due_date"],
        }
        fingerprint = compute_proposal_fingerprint("SEND_INVOICE_REMINDER", payload)
        draft_candidates.append((item, payload, fingerprint))

    existing_fingerprints = set()
    if draft_candidates:
        fingerprint_rows = (
            db.query(ProposedAction.inputs_fingerprint)
            .filter(
                ProposedAction.company_id == objective.company_id,
                ProposedAction.action_type == "SEND_INVOICE_REMINDER",
                ProposedAction.status == "PENDING",
                ProposedAction.inputs_fingerprint.in_([fp for _, _, fp in draft_candidates]),
            )
            .all()
        )
        existing_fingerprints = {row[0] for row in fingerprint_rows}

    # Confidence reflects the evidence actually present: a degraded run's drafts
    # carry a lower score than a full-signal run's.
    draft_confidence = 0.9 if not missing_signals else 0.75

    drafted = []
    skipped_existing = 0
    for item, payload, fingerprint in draft_candidates:
        if fingerprint in existing_fingerprints:
            skipped_existing += 1
            continue
        action = propose_invoice_reminder(
            db,
            objective.company_id,
            payload,
            reasoning=f"Collections review: {item['rationale']}",
            confidence_score=draft_confidence,
            agent_run_id=tracker.run_id if tracker else None,
            source="system",
        )
        drafted.append(
            {
                "action_id": action.id,
                "invoice_id": item["invoice_id"],
                "doc_number": item["doc_number"],
                "customer": item["customer"],
                "balance": item["balance"],
            }
        )

    return {
        "company_id": objective.company_id,
        "as_of": scored.get("as_of") or reminder_date,
        "status": "degraded" if missing_signals else "ok",
        "candidate_count": scored["candidate_count"],
        "total_overdue_balance": scored["total_overdue_balance"],
        "chase_list": chase_list[:AR_COLLECTIONS_CHASE_LIST_LIMIT],
        "missing_signals": missing_signals,
        "draft_cap": draft_cap,
        "drafted": drafted,
        "skipped_existing": skipped_existing,
    }


# PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md). WF-12 (decision 7): the agent-plan holder objective's
# runner. The primary dispatch is direct — the AGENT_PLAN approval handler enqueues
# run_agent_plan_walk, and the process-active-agent-plans beat sweep re-enqueues it —
# so this runner is not on the hot path; it exists so the "agent_plan" objective_type is
# a KNOWN runner (never the _execute_objective "Unsupported objective_type" ValueError)
# and to keep AgentRun parentage/scope resolution coherent. It DELEGATES to
# run_agent_plan_walk (never walks inline — the walk owns its own SessionLocal + tracker),
# dispatching one walk per still-ACTIVE plan for the holder's realm.
def _run_agent_plan(db, objective: AgentObjective) -> Dict[str, Any]:  # CRAFT-OK: dynamic runner summary-dict boundary, mirrors the other OBJECTIVE_RUNNERS in this module
    from ..models import AgentPlan
    from .agent_plans import run_agent_plan_walk

    realm_id = objective.company_id
    rows = (
        db.query(AgentPlan.id)
        .filter(AgentPlan.realm_id == realm_id, AgentPlan.status == "ACTIVE")
        .order_by(AgentPlan.id.asc())
        .all()
    )
    for (plan_id,) in rows:
        run_agent_plan_walk.delay(plan_id, realm_id)
    return {"status": "dispatched", "objective_id": objective.id, "active_plans": len(rows), "queued": len(rows)}


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave2-7-lease-flux (marker
# RATIFICATION-2026-07-14.md). WF-7 (decision 3): the standalone actual-vs-prior
# flux review runner. It resolves the period (last complete month, or an explicit
# notification_policy["period"]) + company name, then delegates to the pure
# app/services/flux_review.py::flux_review — which builds a LIVE-client CheckContext,
# scans the registered ActualVsPriorFluxCheck DIRECTLY, and drafts one PENDING
# review-only ProposedAction per material variance. Proposes, never posts.
def _run_flux_review(db, objective: AgentObjective) -> Dict[str, Any]:  # CRAFT-OK: dynamic runner summary-dict boundary, mirrors the other OBJECTIVE_RUNNERS in this module
    from ..services.flux_review import flux_review

    company_info = db.query(QBOCompanyInfo).filter_by(realm_id=objective.company_id).first()
    company_name = company_info.company_name if company_info else objective.company_id
    notification_policy = objective.notification_policy or {}
    period = notification_policy.get("period") or _last_month_period()

    result = flux_review(db, objective.company_id, period, company_name=company_name)
    return {"company_id": objective.company_id, "company_name": company_name, **result}


# Wave 3.7 (plan 20260714-wave3-7-going-concern, AC-2): the standalone escalate-only
# going-concern INDICATOR runner. It gathers the existing covenant / ratio / runway
# substrate (honest-degrading absent QBO signals to ``indeterminate``), applies the
# pure ASC 205-40-55-2 indicator engine, and — ONLY when the triage severity is
# elevated/high — escalates via a ``GOING_CONCERN`` WatchdogAlert. It NEVER concludes
# going concern, drafts no ProposedAction, posts no JE, and executes nothing (SYNTHESIS
# §5). Keeps the ``(db, objective)`` signature — NOT tracker-aware.
def _run_going_concern(db, objective: AgentObjective) -> Dict[str, Any]:  # CRAFT-OK: dynamic runner summary-dict boundary, mirrors the other OBJECTIVE_RUNNERS in this module
    from ..qbo_client import get_qbo_client_from_db
    from ..services.going_concern import evaluate, gather_signals, severity_to_watchdog_severity
    from ..services.redaction import redact_to_string
    from .proactive import create_watchdog_alert_if_new

    company_info = db.query(QBOCompanyInfo).filter_by(realm_id=objective.company_id).first()
    company_name = company_info.company_name if company_info else objective.company_id

    qbo_client = None
    try:
        qbo_client = get_qbo_client_from_db(db, objective.company_id)
    except Exception as exc:  # CRAFT-OK: no live QBO connection → covenant-only pass; QBO signals honest-degrade to indeterminate
        logger.info("[going_concern] no QBO client for %s; covenant-only pass: %s", objective.company_id, redact_to_string(exc))

    result = evaluate(gather_signals(db, objective.company_id, qbo_client=qbo_client))
    fired_ids = result.fired_indicator_ids
    summary: Dict[str, Any] = {
        "company_id": objective.company_id,
        "company_name": company_name,
        "severity": result.severity,
        "fired_indicators": fired_ids,
        "indeterminate": [entry["indicator"] for entry in result.indeterminate],
        "requires_cpa_review": result.requires_cpa_review,
        "alert_created": False,
    }

    if result.severity == "none":
        # E3: zero fired screens → no fabricated alarm. Empty indicators ≠ all-clear.
        summary["status"] = "no_indicators_fired"
        return summary

    # E1: pass EXPLICIT WatchdogAlert columns (not a rich-dict splat, which would crash
    # WatchdogAlert(**kwargs)). The structured engine payload rides the ``context`` JSON.
    watchdog_severity = severity_to_watchdog_severity(result.severity)
    title = f"Going-concern indicators fired ({len(fired_ids)}) — CPA review required"
    description = (
        f"{len(fired_ids)} going-concern INDICATORS fired "
        f"({', '.join(fired_ids)}) — CPA review required. {result.notice}"
    )
    created = create_watchdog_alert_if_new(
        db,
        company_id=objective.company_id,
        alert_type="GOING_CONCERN",
        period=date.today().strftime("%Y-%m"),
        title=title,
        severity=watchdog_severity,
        metric_unit="score",
        metric_value=len(fired_ids),
        description=description,
        context=result.to_dict(),
    )
    if created:
        # Mirror the flux_review flush discipline: create_watchdog_alert_if_new only
        # db.add()s (the caller commits). Flush so the escalation is persisted within
        # this transaction; the runner never commits (no writeback beyond the alert).
        db.flush()
    summary["alert_created"] = bool(created)
    summary["status"] = "indicators_fired"
    return summary


# Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder, AC-1 / WF-8): the standalone
# tax-package HANDOFF assembler runner. Pure-reader — assembles the book-side trial
# balance + PY comparative + fixed-asset detail + open-items + the UNMAPPED
# books-to-tax stub for a LICENSED PREPARER; NEVER computes a tax number, drafts no
# ProposedAction, posts no JE, executes nothing. A failed section read degrades to a
# labeled-gap SECTION (never a fabricated $0). Keeps the (db, objective) signature —
# NOT tracker-aware.
def _run_tax_package(db, objective: AgentObjective) -> Dict[str, Any]:  # CRAFT-OK: dynamic runner summary-dict boundary, mirrors the other OBJECTIVE_RUNNERS in this module
    from ..services.tax_package import build_tax_package

    company_info = db.query(QBOCompanyInfo).filter_by(realm_id=objective.company_id).first()
    company_name = company_info.company_name if company_info else objective.company_id
    notification_policy = objective.notification_policy or {}
    # Optional explicit fiscal anchor; default = the last completed month within the
    # company's real fiscal year (resolve_period reads FiscalYearStartMonth, never Jan).
    year = notification_policy.get("fiscal_year")
    month = notification_policy.get("fiscal_month")

    result = build_tax_package(db, objective.company_id, as_of_year=year, as_of_month=month)
    result["company_id"] = objective.company_id
    result["company_name"] = company_name
    result["status"] = "partial" if any(section.get("status") != "OK" for section in result.get("sections", [])) else "succeeded"
    return result


# Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder, AC-2 / WF-9): the standalone
# pure-reader PBC evidence-binder assembler runner. For a realm's PBC requests it
# RE-RESOLVES each human-asserted evidence link UNDER the tenant scope (LINKED /
# NO_DATA); it NEVER fabricates or infers a link, marks a line satisfied, drafts a
# ProposedAction, or writes. Keeps the (db, objective) signature — NOT tracker-aware.
def _run_pbc_binder(db, objective: AgentObjective) -> Dict[str, Any]:  # CRAFT-OK: dynamic runner summary-dict boundary, mirrors the other OBJECTIVE_RUNNERS in this module
    from ..services.pbc_binder import run_pbc_binder

    company_info = db.query(QBOCompanyInfo).filter_by(realm_id=objective.company_id).first()
    company_name = company_info.company_name if company_info else objective.company_id
    notification_policy = objective.notification_policy or {}
    # Optional explicit target — a single PBCRequest id; default = every not-yet-
    # delivered request for the realm.
    pbc_request_id = notification_policy.get("pbc_request_id")

    result = run_pbc_binder(db, objective.company_id, pbc_request_id=pbc_request_id)
    result["company_id"] = objective.company_id
    result["company_name"] = company_name
    totals = result.get("totals", {})
    result["status"] = "partial" if totals.get("lines_linked", 0) < totals.get("lines", 0) else "succeeded"
    return result


OBJECTIVE_RUNNERS = {
    "morning_briefing": ("generate_morning_briefing", _run_morning_briefing),
    "cash_watch": ("analyze_cash_watch", _run_cash_watch),
    "budget_variance_watch": ("analyze_budget_variance", _run_budget_variance_watch),
    "qbo_sync_health": ("check_qbo_sync_health", _run_qbo_sync_health),
    "report_pack": ("generate_report_pack", _run_report_pack),
    "transaction_review": ("pull_and_prepare_transaction_review", _run_transaction_review),
    "close_orchestration": ("walk_close_checklist", _run_close_orchestration),
    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C4 / AC-6.
    "collections_review": ("review_ar_collections", _run_collections_review),
    # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave2-7-lease-flux (marker
    # RATIFICATION-2026-07-14.md). WF-7: the standalone actual-vs-prior flux review
    # runner (advance-once-on-success via run_agent_objective, mirrors collections_review;
    # keeps the (db, objective) signature — NOT tracker-aware).
    "flux_review": ("review_actual_vs_prior_flux", _run_flux_review),
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 7): the agent-plan holder runner
    # (delegates to run_agent_plan_walk — see _run_agent_plan above).
    "agent_plan": ("dispatch_agent_plan_walk", _run_agent_plan),
    # Plan 20260714-wave3-7-going-concern AC-2 (RK-6): the escalate-only ASC 205-40
    # going-concern INDICATOR runner. The first tuple element is the AgentRunTracker
    # step-label (NOT a celery task); run_agent_objective dispatches it via this lookup.
    "going_concern_review": ("review_going_concern_indicators", _run_going_concern),
    # Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder, AC-1 / WF-8): the standalone
    # pure-reader tax-package HANDOFF assembler. First tuple element is the
    # AgentRunTracker step-label; run_agent_objective dispatches via this lookup.
    # (db, objective) signature — NOT tracker-aware (stays OUT of _TRACKER_AWARE_RUNNERS).
    "tax_package": ("assemble_tax_package", _run_tax_package),
    # Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder, AC-2 / WF-9): the standalone
    # pure-reader PBC evidence-binder assembler. First tuple element is the
    # AgentRunTracker step-label; run_agent_objective dispatches via this lookup.
    # (db, objective) signature — NOT tracker-aware (stays OUT of _TRACKER_AWARE_RUNNERS).
    "pbc_binder": ("assemble_pbc_binder", _run_pbc_binder),
}

# Runners that need the AgentRunTracker passed (per-step proposal step-link + run
# id). All other runners keep the (db, objective) signature; the dispatch in
# _execute_objective forwards the tracker only for these.
_TRACKER_AWARE_RUNNERS = {"close_orchestration", "collections_review"}


def _execute_objective(
    db,
    objective: AgentObjective,
    *,
    trigger_type: str = "schedule",
    existing_run: AgentRun | None = None,
    close_period: str | None = None,
    defer_failure: bool = False,
    occurrence_run: AgentRun | None = None,
) -> Dict[str, Any]:
    organization, organization_company = _resolve_objective_scope(db, objective)
    if existing_run is not None:
        existing_run = _lock_close_run_for_adoption(db, existing_run)
    run_scope = existing_run.permission_scope if existing_run is not None else objective.permission_scope
    tracker = AgentRunTracker(
        db,
        company_id=objective.company_id,
        objective_id=objective.id,
        trigger_type=trigger_type,
        input_summary=(
            existing_run.input_summary
            if existing_run is not None and existing_run.input_summary
            else objective.instructions or objective.name
        ),
        model_provider="system",
        # `objective.objective_type` (e.g. "close_orchestration") is a TASK-TYPE
        # string, not a model name — passing it here was an arg transposition
        # that fed the pricing lookup a task type, producing
        # "No pricing entry for model 'close_orchestration'" and leaving the
        # run-level cost_estimate permanently unattributed. `model_provider`
        # already says "system" (no single pinned model for a multi-step
        # system-triggered objective run) — leave model_name unset; the real
        # per-step model attribution + cost happens at each LLM call site
        # (app/services/llm.py _record_usage / model_loop.py _record_step_usage),
        # which already carry the REAL model name. objective.objective_type
        # remains available via AgentRun.objective_id for task-type context.
        model_name=None,
        prompt_version="objective-v1",
        user_id=existing_run.user_id if existing_run is not None else objective.created_by_user_id,
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        permission_scope=run_scope,
        request_id=existing_run.request_id if existing_run is not None else None,
    )
    if occurrence_run is not None:
        tracker.run = occurrence_run
        tracker._run_id = occurrence_run.id
    elif existing_run is None:
        tracker.start()
    else:
        existing_run_id = existing_run.id
        if (
            existing_run.objective_id != objective.id
            or existing_run.company_id != objective.company_id
            or existing_run.organization_id != (organization.id if organization else None)
            or existing_run.organization_company_id
            != (organization_company.id if organization_company else None)
        ):
            raise CloseRunPayloadError("Close run does not match objective tenant scope")
        if existing_run.status == "queued":
            existing_run.started_at = existing_run.started_at or datetime.utcnow()
            if not transition_run(
                db,
                existing_run,
                "running",
                actor="close_worker",
                reason="Worker adopted durable queued close run",
                state={"period": close_period},
                checkpoint_key="close_worker_started",
            ):
                raise RuntimeError("Queued close run could not transition to running")
        elif existing_run.status != "running":
            raise CloseRunPayloadError(
                f"Close worker cannot adopt run in state {existing_run.status!r}"
            )
        else:
            # ``SELECT .. FOR UPDATE`` must not leak beyond the adoption
            # decision for an already-running redelivery.  A queued adoption's
            # transition_run commit releases it; this is the symmetric branch.
            db.commit()
        tracker.run = existing_run
        tracker._run_id = existing_run_id

    try:
        runner_config = OBJECTIVE_RUNNERS.get(objective.objective_type)
        if not runner_config:
            raise ValueError(f"Unsupported objective_type '{objective.objective_type}'")

        tool_name, runner = runner_config
        lease_held = False
        terminal_run: tuple[int, str] | None = None
        with tracker.step(
            step_type="tool_call",
            tool_name=tool_name,
            input_payload={
                "objective_id": objective.id,
                "schedule": objective.schedule,
                **({"period": close_period} if objective.objective_type == CLOSE_OBJECTIVE_TYPE else {}),
            },
        ) as step:
            # Tracker-aware runners (the close-orchestration step-walker) need the
            # tracker for the per-step proposal step-link + the agent run id; all
            # other runners keep the (db, objective) signature (additive).
            if objective.objective_type == CLOSE_OBJECTIVE_TYPE:
                if close_period is None:
                    raise CloseRunPayloadError("Close run requires an immutable period")
                result = runner(db, objective, tracker=tracker, period=close_period)
                if (
                    isinstance(result, dict)
                    and result.get("status") == "skipped"
                    and result.get("reason") == "run_lock_held"
                ):
                    # A duplicate delivery must not terminalize the shared run
                    # while the original holder is still working.  Leave it
                    # running and ask Celery to try this delivery later.
                    step.finish(status="blocked", output_payload=result)
                    lease_held = True
                elif (
                    isinstance(result, dict)
                    and result.get("status") == "skipped"
                    and result.get("reason") == "run_already_terminal"
                ):
                    step.finish(status="blocked", output_payload=result)
                    terminal_run = (
                        int(result.get("run_id") or tracker.run_id or 0),
                        str(result.get("run_status") or "unknown"),
                    )
            elif objective.objective_type in _TRACKER_AWARE_RUNNERS:
                result = runner(db, objective, tracker=tracker)
            else:
                result = runner(db, objective)
            if not lease_held and terminal_run is None:
                from ..services.agent_objective_dispatch import result_status
                step.finish(status=result_status(result), output_payload=result)

        if lease_held:
            raise CloseRunLeaseHeldError("A live close run owns the realm lease")
        if terminal_run is not None:
            raise CloseRunAlreadyTerminalError(*terminal_run)

        with tracker.step(
            step_type="derive",
            tool_name="create_insight_artifact",
            input_payload={"objective_type": objective.objective_type, "result": result},
        ) as step:
            from ..services.insight_artifacts import objective_result_to_artifact

            artifact = objective_result_to_artifact(
                db,
                objective_type=objective.objective_type,
                company_id=objective.company_id,
                result=result,
                agent_run_id=tracker.run_id,
                organization_id=organization.id if organization else None,
                organization_company_id=organization_company.id if organization_company else None,
            )
            step.finish(
                status="succeeded",
                output_payload={"artifact_id": artifact.id if artifact else None},
            )

        # B2-4 AC-1/AC-3: for a close-orchestration run, persist a HUMAN-READABLE terminal
        # outcome on the AgentRun's output_summary (an existing column) so GET /agent/close-runs
        # can report a deferred (run_lock_held) or nothing-to-close (walked==0) run instead of a
        # raw dict repr — the cockpit surfaces this string when a start yields no in-progress run.
        # PLAN-EXEMPT: ratified plan 20260702-qa-batch2 B2-4 AC-1 (app/tasks/agent.py in scope).
        if objective.objective_type == CLOSE_OBJECTIVE_TYPE:
            close_summary = _close_run_summary(result if isinstance(result, dict) else {})
            _strict_finish_close_run(
                db,
                tracker,
                output_summary=close_summary,
            )
        else:
            from ..services.agent_objective_dispatch import result_status
            status = result_status(result)
            tracker.finish(status="partially_completed" if status == "partial" else status, output_summary=str(result))
        return result

    except CloseRunLeaseHeldError:
        logger.info(
            "[AgentObjective] Close run %s deferred because its realm lease is live",
            tracker.run_id,
        )
        raise
    except Exception as exc:
        logger.error(
            "[AgentObjective] Objective %s failed: %s",
            objective.id,
            redact_to_string(str(exc), max_length=500),
        )
        if not defer_failure:
            tracker.finish(status="failed", error=str(exc))
        raise


@celery_app.task(bind=True, max_retries=2, name="app.tasks.agent.run_agent_objective")
def run_agent_objective(self, objective_id: int | None = None, *, occurrence_id: str | None = None):
    """Legacy messages admit only a due scheduled slot; new messages carry its identity."""
    from ..models import AgentObjectiveOccurrence
    from ..services.agent_objective_dispatch import OccurrenceConflict, execute_occurrence, reserve_occurrence
    db = SessionLocal()
    try:
        with tenant_scope_live(db, bypass=True):
            if occurrence_id:
                row = db.query(AgentObjectiveOccurrence).filter(AgentObjectiveOccurrence.id == occurrence_id).first()
                if row is None:
                    return {"status": "skipped", "reason": "not_found"}
                org_id, realm_id = row.organization_id, row.company_id
            else:
                objective = db.query(AgentObjective).filter(AgentObjective.id == objective_id).first()
                if objective is None:
                    return {"status": "skipped", "reason": "not_found"}
                org_id, realm_id = objective.organization_id, objective.company_id
        with tenant_scope_live(db, org_id=org_id, realm_id=realm_id):
            if occurrence_id is None:
                try:
                    row = reserve_occurrence(db, objective_id)
                except OccurrenceConflict as exc:
                    return {"status": "blocked", **exc.detail}
                if row is None:
                    return {"status": "skipped", "reason": "not_due", "objective_id": objective_id}
                occurrence_id = row.id
            return execute_occurrence(db, occurrence_id, _execute_objective)
    except (PermissionError, ValueError) as exc:
        db.rollback()
        return {"status": "blocked", "reason": str(exc)}
    except Exception as exc:
        db.rollback()
        # Only pre-run failures reach this handler. Runner uncertainty is durable.
        raise self.retry(exc=exc, countdown=min(600, 60 * (2 ** self.request.retries))) from exc
    finally:
        db.close()


def _terminalize_precreated_close_run(
    db,
    *,
    run: AgentRun,
    period: str,
    error: Exception,
) -> None:
    """Persist and verify one terminal failure without reopening a winner."""
    fresh_run = _locked_close_run(db, run)
    if fresh_run is None:
        db.rollback()
        raise CloseRunTerminalPersistenceError(
            "Close run disappeared before terminal failure could be persisted"
        )
    fresh_status = str(fresh_run.status or "")
    if fresh_status in RUN_TERMINAL_STATES:
        db.rollback()
        return

    identity = _close_run_identity(fresh_run)
    durable_run_id = fresh_run.id
    realm_id = fresh_run.company_id
    user_message = "Close preparation failed. Retry the start action."
    technical_error = redact_to_string(str(error), max_length=1000)
    fresh_run.error = user_message
    fresh_run.output_summary = user_message
    fresh_run.finished_at = datetime.utcnow()
    transitioned = transition_run(
        db,
        fresh_run,
        "failed",
        actor="close_worker",
        reason=user_message,
        state={"period": period, "retryable": True},
        checkpoint_key="close_worker_failed",
    )
    if not transitioned:
        raise CloseRunTerminalPersistenceError(
            "Close run terminal failure could not be persisted"
        )
    verified = (
        _scoped_close_run_query(db, identity)
        .populate_existing()
        .one_or_none()
    )
    if (
        verified is None
        or str(verified.status or "") != "failed"
        or verified.finished_at is None
        or verified.error != user_message
    ):
        db.rollback()
        raise CloseRunTerminalPersistenceError(
            "Close run terminal failure could not be verified"
        )
    logger.error(
        "[close_worker] terminal failure (run_id=%s, realm=%s, period=%s): %s",
        durable_run_id,
        realm_id,
        period,
        technical_error,
    )
    from ..services.close_run_start import fail_close_period_prep

    fail_close_period_prep(
        db,
        realm_id=realm_id,
        period=period,
        error=error,
    )


@celery_app.task(bind=True, max_retries=None, name="app.tasks.agent.run_close_objective_now")
def run_close_objective_now(
    self,
    objective_id: int,
    run_id: int,
    period: str,
    failure_attempt: int = 0,
    lease_wait: int = 0,
) -> dict[str, Any]:  # CRAFT-OK: dynamic objective-runner summary-dict return, mirrors run_agent_objective + _execute_objective in this module
    """Adopt and execute one durable, period-pinned on-demand close run.

    The request path creates ``AgentRun(status='queued')`` and commits it before
    publishing ``(objective_id, run_id, period)``.  This worker opens its own
    session, validates all three immutable references under bypass, then performs
    work only inside the run's organization+realm scope.  It never creates a
    second run and never arms the disabled holder's schedule.

    A duplicate delivery that meets a live realm lease stays nonterminal and
    receives a finite, TTL-derived wait budget; stale leases remain reclaimable,
    while a still-live owner is never terminalized at the bound.  Ordinary
    transient failures reuse this same run for two independent failure retries;
    only failure exhaustion terminalizes it as retryable ``failed``.
    """
    from ..services.close_run_start import close_period_from_run, validate_close_period

    failure_attempt = max(0, int(failure_attempt or 0))
    lease_wait = max(0, int(lease_wait or 0))
    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() below guarantees close
    run: AgentRun | None = None
    scope_org_id: int | None = None
    scope_realm_id: str | None = None
    selected_period = str(period)
    try:
        try:
            selected_period = validate_close_period(period)
        except ValueError as exc:
            with tenant_scope_live(db, bypass=True):
                run = db.query(AgentRun).filter(AgentRun.id == run_id).first()  # CROSS-TENANT-OK: payload run PK resolves owning tenant; exact scope is validated below
                if run is not None:
                    scope_org_id = run.organization_id
                    scope_realm_id = run.company_id
            if run is not None:
                with tenant_scope_live(db, org_id=scope_org_id, realm_id=scope_realm_id):
                    _terminalize_precreated_close_run(
                        db,
                        run=run,
                        period=str(period),
                        error=CloseRunPayloadError(str(exc)),
                    )
            return {"status": "failed", "reason": "invalid_period", "run_id": run_id}

        with tenant_scope_live(db, bypass=True):
            objective = db.query(AgentObjective).filter(AgentObjective.id == objective_id).first()  # CROSS-TENANT-OK: by-PK entity load only to validate against the run below
            run = db.query(AgentRun).filter(AgentRun.id == run_id).first()  # CROSS-TENANT-OK: by-PK entity load only to resolve its persisted tenant scope
            if run is not None:
                scope_org_id = run.organization_id
                scope_realm_id = run.company_id
        if run is None:
            return {"status": "skipped", "reason": "run_not_found", "run_id": run_id}
        if str(run.status or "") in RUN_TERMINAL_STATES:
            return {
                "status": "skipped",
                "reason": "already_terminal",
                "run_id": run_id,
                "run_status": run.status,
                "period": selected_period,
            }

        with tenant_scope_live(db, org_id=scope_org_id, realm_id=scope_realm_id):
            payload_error = None
            if objective is None:
                payload_error = "Close objective no longer exists"
            elif objective.objective_type != CLOSE_OBJECTIVE_TYPE:
                payload_error = "Objective is not a close-orchestration holder"
            elif run.objective_id != objective_id:
                payload_error = "Run does not belong to the dispatched objective"
            elif objective.company_id != run.company_id:
                payload_error = "Run and objective realm do not match"
            elif objective.organization_id != run.organization_id:
                payload_error = "Run and objective organization do not match"
            elif objective.organization_company_id != run.organization_company_id:
                payload_error = "Run and objective entity scope do not match"
            elif close_period_from_run(run) != selected_period:
                payload_error = "Task period does not match the run's immutable period"
            if payload_error is not None:
                error = CloseRunPayloadError(payload_error)
                _terminalize_precreated_close_run(
                    db,
                    run=run,
                    period=selected_period,
                    error=error,
                )
                return {
                    "status": "failed",
                    "reason": "payload_mismatch",
                    "run_id": run_id,
                    "period": selected_period,
                }

            # Deliberately NO enabled skip and NO next_run_at update: this holder
            # is on-demand only and must never become a recurring close.
            result = _execute_objective(
                db,
                objective,
                trigger_type="manual",
                existing_run=run,
                close_period=selected_period,
                defer_failure=True,
            )
            objective.last_run_at = datetime.utcnow()
            db.commit()
            return {
                "status": "succeeded",
                "objective_id": objective_id,
                "run_id": run_id,
                "period": selected_period,
                "result": result,
            }

    except CloseRunAlreadyTerminalError as exc:
        db.rollback()
        return {
            "status": "skipped",
            "reason": "already_terminal",
            "run_id": exc.run_id,
            "run_status": exc.run_status,
            "period": selected_period,
        }
    except CloseRunLeaseHeldError as exc:
        db.rollback()
        if lease_wait >= _close_lease_wait_limit():
            if run is not None:
                with tenant_scope_live(
                    db,
                    org_id=scope_org_id,
                    realm_id=scope_realm_id,
                ):
                    fresh_run = (
                        db.query(AgentRun)
                        .filter(
                            AgentRun.id == run_id,
                            AgentRun.company_id == scope_realm_id,
                        )
                        .populate_existing()
                        .first()
                    )
                    if (
                        fresh_run is not None
                        and str(fresh_run.status or "") in RUN_TERMINAL_STATES
                    ):
                        return {
                            "status": "skipped",
                            "reason": "already_terminal",
                            "run_id": run_id,
                            "run_status": fresh_run.status,
                            "period": selected_period,
                        }
            # The lease may still represent live work.  Exhausting this
            # delivery's finite TTL-derived wait budget must never declare that
            # shared run failed or overwrite its eventual terminal outcome.
            return {
                "status": "deferred",
                "reason": "lease_wait_exhausted",
                "run_id": run_id,
                "period": selected_period,
                "lease_wait": lease_wait,
            }
        raise self.retry(
            exc=exc,
            countdown=CLOSE_LEASE_RETRY_SECONDS,
            kwargs={
                "failure_attempt": failure_attempt,
                "lease_wait": lease_wait + 1,
            },
        )  # CRAFT-OK: retry itself raises
    except CloseBudgetExceededError as exc:
        db.rollback()
        if run is not None:
            with tenant_scope_live(db, org_id=scope_org_id, realm_id=scope_realm_id):
                _terminalize_precreated_close_run(
                    db,
                    run=run,
                    period=selected_period,
                    error=exc,
                )
        raise
    except Exception as exc:
        db.rollback()
        if run is not None and failure_attempt >= CLOSE_TASK_MAX_FAILURE_RETRIES:
            with tenant_scope_live(db, org_id=scope_org_id, realm_id=scope_realm_id):
                _terminalize_precreated_close_run(
                    db,
                    run=run,
                    period=selected_period,
                    error=exc,
                )
            raise
        backoff = min(600, 60 * (2**failure_attempt))
        countdown = backoff + random.uniform(0, backoff * 0.25)
        raise self.retry(
            exc=exc,
            countdown=countdown,
            kwargs={
                "failure_attempt": failure_attempt + 1,
                "lease_wait": lease_wait,
            },
        )  # CRAFT-OK: retry itself raises
    finally:
        db.close()


# Stale-run reap threshold: a run still queued/running past this many seconds is
# treated as abandoned (crash, OOM, deploy kill, a dropped streaming generator).
# Sized WELL above a normal interactive run AND above the duplicate-run window
# (config.agent_duplicate_run_window_seconds) so the sweep never reaps a run the
# in-band finalize would still terminate. Env-overridable for ops tuning.
AGENT_RUN_STALE_SECONDS = int(os.getenv("AGENT_RUN_STALE_SECONDS", "900"))


@celery_app.task(bind=True, max_retries=2, name="app.tasks.agent.sweep_stale_agent_runs")
def sweep_stale_agent_runs(self) -> dict[str, str | int]:
    """Backstop sweep: reap AgentRun rows stuck queued/running past the stale cutoff.

    The in-band finalize (the streaming endpoint finalizes under live scope on any
    exit) is primary; this is defense-in-depth for a finalize that never ran (a
    crashed worker, an OOM, a deploy kill, a dropped streaming generator). A stuck
    `running` row blocks that user's next conversation run via the duplicate-run
    guard for the whole stale window — and the guard ALSO matches
    `organization_id IS NULL` (runtime.py), so a null-org stuck row blocks ALL of
    that user's runs. The sweep therefore reaps stuck rows REGARDLESS of org,
    including null-org.

    Idempotent (acks_late-safe at-least-once delivery): the UPDATE is conditional
    on `status IN (queued, running)`, so a redelivery / concurrent run cannot
    re-process a row already flipped to `failed`, and never touches a fresh run.
    This is a vetted system sweep (NOT a per-run path), so it runs under
    `tenant_scope(bypass=True)` — AC-3 permits bypass HERE; the per-run body
    (the streaming endpoint) is never bypassed (AC-4).
    """
    db = SessionLocal()  # SA-OK: closed-elsewhere finally db.close() below guarantees close
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=AGENT_RUN_STALE_SECONDS)
        # SYSTEM SWEEP across ALL tenants incl. null-org: reap any abandoned run.
        with tenant_scope(bypass=True):
            # W-1 (plan 20260702-qa-remediation): close-orchestration runs are
            # LONG-RUNNING BY DESIGN (a 40+-step checklist walk can exceed the
            # 15-min interactive cutoff while progressing) — they are excluded
            # here and owned by the dedicated 2h no-progress close watchdog
            # (app/tasks/close_watchdog.py). This sweep exists to unblock the
            # interactive duplicate-run guard, whose window a close run does
            # not occupy past agent_duplicate_run_window_seconds.
            close_objective_ids = [
                row[0]
                for row in db.query(AgentObjective.id)  # CROSS-TENANT-OK: id census for the close-run exclusion of the system sweep
                .filter(AgentObjective.objective_type == "close_orchestration")
                .all()
            ]
            query = (
                db.query(AgentRun)  # CROSS-TENANT-OK: system stale-run reaper spans every tenant incl. organization_id IS NULL; vetted backstop, not a per-run path
                .filter(
                    AgentRun.status.in_(["queued", "running"]),
                    AgentRun.created_at < cutoff,
                )
            )
            if close_objective_ids:
                query = query.filter(
                    or_(
                        AgentRun.objective_id.is_(None),
                        AgentRun.objective_id.notin_(close_objective_ids),
                    )
                )
            from ..models import AgentObjectiveOccurrence
            from ..agent.runtime.plan_walker import reap_stale_plan_run
            owned_runs = [r[0] for r in db.query(AgentObjectiveOccurrence.run_id).filter(AgentObjectiveOccurrence.run_id.isnot(None)).all()]
            if owned_runs:
                query = query.filter(AgentRun.id.notin_(owned_runs))
            candidates = [(r.id, r.company_id, r.trigger_type) for r in query.all()]
            db.commit()
            reaped = 0
            for run_id, realm_id, trigger in candidates:
                if trigger == "agent_plan":
                    reaped += int(reap_stale_plan_run(db, run_id, realm_id))
                else:
                    reaped += db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.status.in_(("queued", "running")), AgentRun.created_at < cutoff).update({
                        AgentRun.status: "failed", AgentRun.error: "Agent run reaped by stale-run sweep (no terminal finalize observed).", AgentRun.finished_at: datetime.utcnow()}, synchronize_session=False)
                db.commit()
            db.commit()
        if reaped:
            logger.warning("[AgentRun] stale-run sweep reaped %s abandoned run(s)", reaped)
        return {"status": "done", "reaped": reaped}
    except Exception as exc:
        db.rollback()
        logger.error(
            "[AgentRun] stale-run sweep failed: %s",
            redact_to_string(str(exc), max_length=500),
        )
        # Exponential backoff + jitter so a transient DB blip doesn't retry in a
        # tight constant-cadence loop against a struggling database (AP-CEL-4).
        backoff = min(600, 60 * (2 ** self.request.retries))
        countdown = backoff + random.uniform(0, backoff * 0.25)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=2, name="app.tasks.agent.process_due_objectives")
def process_due_objectives(self, limit: int = 25, *, after_id: int = 0, upper_id: int | None = None, due_before: str | None = None) -> dict[str, str | int]:
    from ..models import AgentObjectiveOccurrence
    from ..services.agent_objective_dispatch import (
        DEDICATED_TYPES, OccurrenceConflict, mark_stale_occurrences, publish_occurrence, reserve_occurrence,
    )
    db = SessionLocal()
    checked = queued = blocked = 0
    limit = max(1, min(int(limit), 100))
    due_at = datetime.fromisoformat(due_before) if due_before else datetime.utcnow()
    try:
        with tenant_scope_live(db, bypass=True):
            if upper_id is None:
                from sqlalchemy import func
                upper_id = db.query(func.max(AgentObjective.id)).scalar() or 0
            recovered = mark_stale_occurrences(db)
            due_ids = [r[0] for r in db.query(AgentObjective.id).filter(
                AgentObjective.enabled.is_(True), AgentObjective.next_run_at <= due_at,
                AgentObjective.id > after_id, AgentObjective.id <= upper_id,
                AgentObjective.objective_type.notin_(DEDICATED_TYPES),
                ~AgentObjective.id.in_(db.query(AgentObjectiveOccurrence.source_objective_id).filter(
                    AgentObjectiveOccurrence.status.in_(("pending", "running", "recovery_required")))))
                .order_by(AgentObjective.id).limit(limit).all()]
            db.commit()
            for objective_id in due_ids:
                checked += 1
                try:
                    reserve_occurrence(db, objective_id)
                except (OccurrenceConflict, PermissionError, ValueError):
                    db.rollback()
                    blocked += 1
            # Pending/published intent may have been delivered ambiguously. Repeating
            # the immutable identity is safe; workers atomically claim before entry.
            rows = db.query(AgentObjectiveOccurrence).filter(
                AgentObjectiveOccurrence.status == "pending", AgentObjectiveOccurrence.dispatch_status != "manual"
            ).order_by(AgentObjectiveOccurrence.publish_attempts, AgentObjectiveOccurrence.created_at).limit(limit).all()
            for row in rows:
                try:
                    queued += int(publish_occurrence(db, row, lambda identity: run_agent_objective.delay(occurrence_id=identity)))
                except Exception:
                    db.rollback()
                    blocked += 1
            continued = len(due_ids) == limit and due_ids[-1] < upper_id
            if continued:
                # A bounded continuation carries the immutable scan ceiling.
                # Invalid legacy policies cannot occupy every future first page.
                db.commit()
                process_due_objectives.delay(limit=limit, after_id=due_ids[-1], upper_id=upper_id, due_before=due_at.isoformat())
            return {"status": "done", "checked": checked, "queued": queued, "blocked": blocked,
                    "recovery_required": recovered, "continued": continued}
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=300) from exc
    finally:
        db.close()

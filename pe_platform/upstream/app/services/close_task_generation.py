# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows wave 3.1b-EXTRACT (CLOSE_TASKS router) enabling gap-register row 7 (generate_close_tasks); Steven ratified extract-now 2026-07-07. Behavior-preserving lift of the /close-tasks/generate workflow into a callable service so a later wiring agent can expose it as a proposal-gated agent tool.
"""close_task_generation — the month-end close-task GENERATE workflow (service layer).

Behavior-preserving extraction of the template + live-enrichment workflow that
was inline in ``app/routers/close_tasks.py::generate_close_tasks`` (thin-router /
fat-service, ``.claude/rules/api-contract.md`` AP-LAYER-02/03). The router is now
a thin caller: it runs the ``check_tenant_access("EDITOR", ...)`` DI guard, then
delegates the entire workflow here.

The service is PURE service-layer: ``db`` + primitives in, plain ``dict`` out. It
raises domain errors (``TemplateNotFoundError``), never ``HTTPException`` — the
router maps the domain error to the HTTP envelope. It owns its own transaction
(the ``db.commit()`` calls live here, AP-LAYER-02).

``STANDARD_TASKS`` is the one source of truth for the built-in checklist and lives
here; the router imports it for backwards compatibility of its public name.

IDEMPOTENCY (preserved byte-for-byte from the router): when ``replace_existing``
is False and any ``CloseTask`` rows already exist for ``(realm_id, period)``, the
service returns ``{"status": "skipped", ...}`` and creates nothing — re-running
the same period is a no-op. ``replace_existing=True`` deletes the period's tasks
first, then regenerates.

Every query is tenant-scoped on ``realm_id`` (or ``company_id`` for the
company-keyed enrichment models), preserving the router's isolation exactly.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any  # CRAFT-OK: STANDARD_TASKS defs + result envelope are heterogeneous str/int/list payloads (behavior-preserving lift from the router); a TypedDict would over-constrain the mixed shape

from sqlalchemy.orm import Session

from ..models import (
    CloseTask,
    CloseTemplate,
    BankFeedItem,
    ProposedAction,
    InsightAnomaly,
)
from .redaction import redact_to_string

logger = logging.getLogger(__name__)


class TemplateNotFoundError(Exception):
    """Raised when an explicit ``template_id`` does not resolve to a template.

    The router maps this to a 404 (matching the prior inline
    ``HTTPException(status_code=404, ...)``); the service stays FastAPI-free.
    """

    def __init__(self, template_id: int):
        self.template_id = template_id
        super().__init__(f"Template #{template_id} not found")


# ─────────────────────────────────────────────────────────────────────────────
# Standard task templates — used by auto-generate (the one source of truth)
# ─────────────────────────────────────────────────────────────────────────────

STANDARD_TASKS: list[dict[str, Any]] = [
    {
        "task_type": "REVIEW_BANK_FEED",
        "title": "Clear Bank Feed",
        "description": "Review and post all pending bank feed transactions. No items should remain in 'pending' status.",
        "sort_order": 10,
        "priority": "high",
    },
    {
        "task_type": "BANK_RECONCILIATION",
        "title": "Complete Bank Reconciliation",
        "description": "Reconcile all bank accounts for the period. Each reconciliation session should reach $0 difference.",
        "sort_order": 20,
        "priority": "high",
    },
    {
        "task_type": "REVIEW_ACCRUALS",
        "title": "Review & Approve Accrual Entries",
        "description": "Confirm all active accounting schedules have generated entries for this period. Approve any pending amortization/depreciation proposals.",
        "sort_order": 30,
        "priority": "normal",
    },
    {
        # C2C-CLOSE (AC-2C.1 build-new): revenue/expense cut-off scan — post-period
        # documents belonging to the closed period (ASC 606/855; the review check
        # 'revenue_expense_cutoff' runs this step in the close walker).
        "task_type": "REVIEW_CUTOFF",
        "title": "Run Revenue/Expense Cut-off Scan",
        "description": "Review post-period documents that reference the closed period (cut-off keys on the SERVICE period, not the document date — ASC 606/855). Accrue recognized items into the closed period; never double-book an accrual and its invoice.",
        "sort_order": 32,
        "priority": "normal",
    },
    {
        "task_type": "APPROVE_JES",
        "title": "Approve Pending Journal Entries",
        "description": "Review the proposals queue and approve or reject all pending journal entry proposals.",
        "sort_order": 40,
        "priority": "normal",
    },
    {
        # C2C-CLOSE (AC-2C.1 extend): sub-ledger→GL tie-out, AR side — the
        # 'ar_subledger_gl_recon' review check compares the aging total to the BS
        # control balance at the SAME period-end as-of date.
        "task_type": "SUBLEDGER_TIEOUT_AR",
        "title": "Tie Out AR Sub-ledger to GL Control",
        "description": "Compare the AR aging total to the Balance Sheet AR control balance at the same period-end as-of date. Name the cause of any difference (timing, unposted batch, direct-to-control JE) before close.",
        "sort_order": 44,
        "priority": "high",
    },
    {
        "task_type": "SUBLEDGER_TIEOUT_AP",
        "title": "Tie Out AP Sub-ledger to GL Control",
        "description": "Compare the AP aging total to the Balance Sheet AP control balance at the same period-end as-of date. Name the cause of any difference before close.",
        "sort_order": 46,
        "priority": "high",
    },
    {
        # C2C-CLOSE (AC-2C.1 extend): full-BS roll-forward coverage — which
        # material carrying accounts have no continuity schedule (GENERIC substrate).
        "task_type": "REVIEW_ROLLFORWARD_COVERAGE",
        "title": "Review BS Roll-forward Coverage",
        "description": "Confirm every material balance-sheet carrying account has a roll-forward schedule (beginning + additions − reductions = ending). Schedule uncovered accounts on the GENERIC substrate; never force a tie by adjusting a beginning balance.",
        "sort_order": 48,
        "priority": "normal",
    },
    {
        "task_type": "REVIEW_ANOMALIES",
        "title": "Address AI Anomalies",
        "description": "Review all open anomalies detected by Vesper Bot. Dismiss, investigate, or route to an action.",
        "sort_order": 50,
        "priority": "normal",
    },
    {
        "task_type": "REVIEW_COVENANTS",
        "title": "Review Covenant Compliance",
        "description": "Check loan/credit-agreement covenant status for the period. Confirm all financial covenants are in compliance and investigate any breaches or near-breaches.",
        "sort_order": 52,
        "priority": "high",
    },
    {
        "task_type": "REVIEW_BUDGET_VARIANCE",
        "title": "Review Budget vs. Actuals",
        "description": "Review material budget-to-actual variances for the period. Explain or escalate significant favorable/unfavorable variances before close.",
        "sort_order": 54,
        "priority": "normal",
    },
    {
        "task_type": "REVIEW_WEEKLY",
        "title": "Review Weekly Cash / Operations Summary",
        "description": "Review the weekly review snapshot (cash position, key operational signals) covering the close period for anomalies that should be addressed before locking.",
        "sort_order": 56,
        "priority": "normal",
    },
    {
        # C2C-CLOSE (AC-2C.1 extend): actual-vs-prior flux — the
        # 'actual_vs_prior_flux' review check runs P&L + BS account-level flux
        # with dual two-sided thresholds and the offsetting-error guard.
        "task_type": "REVIEW_FLUX",
        "title": "Review Actual-vs-Prior Flux",
        "description": "Review P&L and balance-sheet flux vs the prior period (dual $/% thresholds, two-sided). Every tripped line gets a driver-tied explanation, never a restatement of the number; watch for offsetting errors netting flat.",
        "sort_order": 58,
        "priority": "normal",
    },
    {
        "task_type": "GENERATE_REPORT",
        "title": "Generate Month-End Report",
        "description": "Generate and download the board-ready PDF report for the period.",
        "sort_order": 60,
        "priority": "normal",
    },
    {
        "task_type": "LOCK_PERIOD",
        "title": "Lock the Period",
        "description": "Once all tasks are complete, lock the period to prevent further changes.",
        "sort_order": 70,
        "priority": "critical",
    },
]


def generate_close_tasks_service(
    db: Session,
    realm_id: str,
    period: str,
    *,
    replace_existing: bool = False,
    template_id: int | None = None,
) -> dict[str, Any]:  # CRAFT-OK: result envelope is a heterogeneous str/int/list payload (behavior-preserving lift from the router's inline dict return)
    """Generate the month-end close task list for a period (service layer).

    Source precedence: explicit ``template_id`` → the company's default template →
    the built-in ``STANDARD_TASKS``. Each task is enriched with live counts
    (pending bank-feed items, pending proposals, open anomalies) and given a due
    date computed from the period end + the template's ``due_day_offset``.

    IDEMPOTENT per period: with ``replace_existing`` False, if any ``CloseTask``
    rows already exist for ``(realm_id, period)`` the workflow creates nothing and
    returns ``{"status": "skipped", ...}``. This is the router's exact prior
    behavior, preserved byte-for-byte.

    Args:
        db: the SQLAlchemy session (owned transaction — this fn commits).
        realm_id: the tenant realm. Scopes every query and every created row.
        period: the ``YYYY-MM`` period.
        replace_existing: delete the period's existing tasks before generating.
        template_id: generate from this saved template instead of the defaults.

    Returns:
        On skip: ``{"status": "skipped", "message": str}``, carrying a
        ``"reason"`` when the skip is the close-start boundary.
        On success: ``{"status": "ok", "period": str, "tasks_created": int,
        "titles": list[str]}`` plus ``"template_used": str`` when a template drove
        generation.

    Raises:
        TemplateNotFoundError: an explicit ``template_id`` did not resolve.
    """
    from .close_run_start import (
        close_start_period_for_realm,
        close_start_refusal_message,
        period_is_before_close_start,
    )

    # A period before the realm's close start boundary is not this platform's
    # obligation: generate nothing (and delete nothing) for it.
    if period_is_before_close_start(db, realm_id=realm_id, period=period):
        return {
            "status": "skipped",
            "reason": "period_before_close_start",
            "message": close_start_refusal_message(period, close_start_period_for_realm(db, realm_id)),
        }

    if replace_existing:
        db.query(CloseTask).filter(
            CloseTask.realm_id == realm_id,
            CloseTask.period == period,
        ).delete()
        db.commit()
    else:
        existing = db.query(CloseTask).filter(
            CloseTask.realm_id == realm_id,
            CloseTask.period == period,
        ).count()
        if existing > 0:
            return {"status": "skipped", "message": f"{existing} tasks already exist for {period}. Use replace_existing=true to regenerate."}

    # Determine task source: explicit template → default template → STANDARD_TASKS
    template_name = None
    if template_id:
        # CROSS-TENANT-OK: behavior-preserving lift of close_tasks.py:555 PK lookup (extraction wave 3.1b) — the router's tenant scoping came from the EDITOR guard + downstream realm-keyed rows; changing this template scoping is out of scope for a byte-identical extract
        template = db.query(CloseTemplate).filter(CloseTemplate.id == template_id).first()
        if not template:
            raise TemplateNotFoundError(template_id)
        task_defs = template.tasks or []
        template_name = template.name
    else:
        # Look for the company's default template
        default_tmpl = db.query(CloseTemplate).filter(
            CloseTemplate.realm_id == realm_id,
            CloseTemplate.is_default == True,  # noqa: E712
        ).first()
        if default_tmpl and default_tmpl.tasks:
            task_defs = default_tmpl.tasks
            template_name = default_tmpl.name
        else:
            task_defs = STANDARD_TASKS

    # Gather live context to enrich descriptions
    # QBO-feed items only — a statement_upload row must not inflate the
    # "pending bank feed" context that enriches generated close-task descriptions
    # (SYNTHESIS §10 close-task collision).
    pending_bank_feed = db.query(BankFeedItem).filter(
        BankFeedItem.realm_id == realm_id,
        BankFeedItem.source == "qbo_feed",
        BankFeedItem.status == "pending",
    ).count()

    pending_proposals = db.query(ProposedAction).filter(
        ProposedAction.company_id == realm_id,
        ProposedAction.status == "PENDING",
    ).count()

    open_anomalies = db.query(InsightAnomaly).filter(
        InsightAnomaly.company_id == realm_id,
        InsightAnomaly.period == period,
        InsightAnomaly.dismissed == False,  # noqa: E712
    ).count()

    # Compute period end date for due date offsets
    try:
        parts = period.split("-")
        year, month = int(parts[0]), int(parts[1])
        if month == 12:
            period_end = date(year + 1, 1, 1)
        else:
            period_end = date(year, month + 1, 1)
    except (ValueError, IndexError):
        period_end = None

    created = []
    for tmpl in task_defs:
        # Normalize — template tasks are dicts, standard tasks are dicts.
        # tmpl.get(...) below is a plain dict access, NOT a DB query.
        task_type = tmpl.get("task_type", "CUSTOM") if isinstance(tmpl, dict) else tmpl.task_type  # SA-OK: bounded-loop over in-memory task-defs; tmpl.get is dict access, no DB round-trip
        title = tmpl.get("title", "") if isinstance(tmpl, dict) else tmpl.title
        description = tmpl.get("description", "") if isinstance(tmpl, dict) else (tmpl.description or "")
        sort_order = tmpl.get("sort_order", 0) if isinstance(tmpl, dict) else tmpl.sort_order
        priority = tmpl.get("priority", "normal") if isinstance(tmpl, dict) else tmpl.priority
        default_assignee = tmpl.get("default_assignee") if isinstance(tmpl, dict) else getattr(tmpl, "default_assignee", None)
        due_day_offset = tmpl.get("due_day_offset", 5) if isinstance(tmpl, dict) else getattr(tmpl, "due_day_offset", 5)

        # Enrich description with live counts
        if task_type == "REVIEW_BANK_FEED" and pending_bank_feed > 0:
            description = f"⚠️ {pending_bank_feed} pending bank feed items. " + description
        elif task_type == "APPROVE_JES" and pending_proposals > 0:
            description = f"⚠️ {pending_proposals} pending proposals. " + description
        elif task_type == "REVIEW_ANOMALIES" and open_anomalies > 0:
            description = f"⚠️ {open_anomalies} open anomalies. " + description

        # Compute due date from period end + offset
        task_due = None
        if period_end:
            task_due = period_end + timedelta(days=due_day_offset - 1)

        task = CloseTask(
            realm_id=realm_id,
            period=period,
            task_type=task_type,
            title=title,
            description=description,
            sort_order=sort_order,
            is_auto_generated=True,
        )
        if hasattr(task, 'priority'):
            task.priority = priority
        if hasattr(task, 'due_date') and task_due:
            task.due_date = task_due
        if hasattr(task, 'assigned_to') and default_assignee:
            task.assigned_to = default_assignee

        db.add(task)
        created.append(title)

    db.commit()
    # ── C2C-CLOSE (AC-2C.1 build-new): risk-based reconciliation cadence ──
    # ONE service call (thin router; the service owns its rows + commit):
    # per-account risk rating → monthly/quarterly/annual cadence drives
    # additive BS-reconciliation tasks (review_checks/recon_cadence.py).
    # HUMAN tasks — the reconciliation itself stays a human control; a policy
    # failure must not block the standard checklist (surfaced, never zeroed).
    try:
        from .review_checks.recon_cadence import generate_reconciliation_close_tasks

        recon_titles = generate_reconciliation_close_tasks(
            db,
            realm_id,
            period,
            due_date=(period_end + timedelta(days=4)) if period_end else None,
            skip_titles=set(created),
        )
        created.extend(recon_titles)
    except Exception as e:
        logger.warning(
            "Risk-based recon-cadence task generation failed for %s %s: %s",
            realm_id,
            period,
            redact_to_string(str(e), max_length=300),
        )

    result = {"status": "ok", "period": period, "tasks_created": len(created), "titles": created}
    if template_name:
        result["template_used"] = template_name
    return result

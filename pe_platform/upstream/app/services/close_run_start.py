"""Canonical start + calendar-prep services for an on-demand close run.

``start_close_run`` is the only user-facing dispatch seam.  It persists the
period-scoped ``AgentRun`` in ``queued`` state before publishing the Celery
message, so the Calendar and Close Cockpit can observe the accepted work even
before a worker receives it.  The disabled ``AgentObjective`` remains a reusable
realm holder; immutable per-click context lives on the run and in the task
payload, never on that shared objective.

``prepare_close_period`` preserves the useful Financial Calendar effects of the
retired Calendar route's worker path without invoking a second task:
status/timestamps, observed schedule/proposal counts, and its recurring-vendor
missing-accrual signal.  A ScheduleEntry-backed accrual is routed exclusively to
the canonical review check so the two detectors cannot double-propose the same
fact.  The close worker composes this with the existing idempotent checklist
generator and proposal-first walker.  Neither service locks or closes a period.
"""
from __future__ import annotations

import calendar as calendar_module
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import NoResultFound

from ..models import (
    AccountingSchedule,
    AgentObjective,
    AgentRun,
    CloseTask,
    FinancialCalendar,
    Organization,
    OrganizationCompany,
    ProposedAction,
    QBOTransaction,
    ScheduleEntry,
    User,
    VendorProfile,
)
from ..qbo_actions.je_lines import (
    accrual_journal_lines,
    accrual_unresolved_reason,
    resolve_accrued_liability_account,
    resolve_vendor_expense_account,
    unresolved_accrual_finding,
)
from ..qbo_actions.proposal_fingerprint import compute_proposal_fingerprint
from .redaction import redact_to_string
from .task_autonomy import period_is_hard_locked
from .tenant_companies import organization_company_for_realm, organization_for_company
from .accounting.je_memo import je_memo

logger = logging.getLogger(__name__)

CLOSE_OBJECTIVE_TYPE = "close_orchestration"
CLOSE_PERIOD_SCOPE_KEY = "close_period"
CLOSE_RUN_PROMPT_VERSION = "close-period-v1"
CLOSE_ACTIVE_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_for_data",
        "waiting_for_user",
        "waiting_for_review",
        "waiting_for_approval",
        "executing",
        "reconciling",
    }
)
_PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class CloseRunStartResult:
    objective_id: int
    run_id: int
    company_id: str
    period: str
    status: str
    reused: bool
    retryable: bool = False


class CloseRunScopeError(RuntimeError):
    """The already-authorized realm no longer resolves to a live tenant row."""


class CloseRunEnqueueError(RuntimeError):
    """The durable queued run exists, but broker publication failed."""

    def __init__(self, result: CloseRunStartResult):
        self.result = result
        super().__init__("Close preparation could not be queued")


def validate_close_period(period: str) -> str:
    """Return an exact ``YYYY-MM`` period or raise ``ValueError``."""
    value = str(period or "").strip()
    if not _PERIOD_RE.fullmatch(value):
        raise ValueError("period must be in YYYY-MM format")
    return value


def _month_of(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return f"{value.year:04d}-{value.month:02d}"


def _previous_month(period: str) -> str:
    year, month = (int(part) for part in validate_close_period(period).split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


@dataclass(frozen=True)
class CloseStartSetting:
    """The realm's close start boundary and where it comes from.

    ``source`` is ``"explicit"`` (``OrganizationCompany.close_start_period``),
    ``"default"`` (the month before the connection month) or ``"none"`` (no
    company mapping: no boundary, nothing is refused).
    """

    explicit_period: str | None
    effective_period: str | None
    connected_period: str | None
    source: str


def close_start_setting_for_company(company: OrganizationCompany | None) -> CloseStartSetting:
    if company is None:
        return CloseStartSetting(None, None, None, "none")
    created_at = company.created_at
    if created_at is None:
        raise CloseRunScopeError(
            "Company mapping has no created_at; the close start period is undetermined"
        )
    connected = _month_of(created_at)
    if company.close_start_period:
        explicit = validate_close_period(company.close_start_period)
        return CloseStartSetting(explicit, explicit, connected, "explicit")
    # A company is usually connected early in the month AFTER the one it came to
    # close, so the default boundary is the month before the connection month.
    return CloseStartSetting(None, _previous_month(connected), connected, "default")


def close_start_period_for_realm(db: Session, realm_id: str) -> str | None:
    """Return the first period this platform is responsible for closing.

    The explicit ``OrganizationCompany.close_start_period`` wins.  When it is
    NULL the boundary is the month BEFORE the company was connected
    (``created_at``): onboarding normally happens early in the month after the
    period the client needs closed, so that period is in scope without any
    configuration.  A TenantAdmin can move it (``PUT /agent/close-start-period``).
    A realm with no company mapping has no onboarding month and therefore no
    boundary, so ``None`` is returned and no period is refused.
    """
    company = (
        db.query(OrganizationCompany)
        .filter(OrganizationCompany.realm_id == realm_id)
        .first()
    )
    return close_start_setting_for_company(company).effective_period


def period_is_before_close_start(db: Session, *, realm_id: str, period: str) -> bool:
    """True when ``period`` precedes the realm's close start boundary.

    ``YYYY-MM`` orders lexicographically, so a string compare is the calendar
    compare.  A realm without a boundary never refuses a period.
    """
    boundary = close_start_period_for_realm(db, realm_id)
    if boundary is None:
        return False
    return validate_close_period(period) < boundary


def close_start_refusal_message(period: str, boundary: str | None) -> str:
    """The user-facing refusal for a period before the close start boundary."""
    start = f" ({boundary})" if boundary else ""
    return (
        f"{period} is before this company's close start period{start}. A company admin "
        "can move the close start period in the Close Cockpit (Close start period setting)."
    )


class CloseStartPeriodError(ValueError):
    """An invalid requested close start period (bad format or a future month).

    ``user_message`` is composed here from the validated input only, so it is
    safe to show verbatim."""

    def __init__(self, user_message: str):
        self.user_message = user_message
        super().__init__(user_message)


def set_close_start_period(
    company: OrganizationCompany, period: str | None, *, today: date | None = None
) -> CloseStartSetting:
    """Set (or, with ``None``, clear back to the default) the explicit boundary.

    A future month is refused: the boundary says which past or current periods
    this platform closes, and a future one would refuse closes that are due.
    The caller authorizes (TenantAdmin), audits and commits.
    """
    if period is None or str(period).strip() == "":
        company.close_start_period = None
        return close_start_setting_for_company(company)
    try:
        value = validate_close_period(period)
    except ValueError as exc:
        raise CloseStartPeriodError("The close start period must be a month in YYYY-MM format.") from exc
    current = _month_of(datetime.combine(today or datetime.now(timezone.utc).date(), datetime.min.time()))
    if value > current:
        raise CloseStartPeriodError(
            f"The close start period cannot be in the future (latest allowed: {current})."
        )
    company.close_start_period = value
    return close_start_setting_for_company(company)


class CloseRunBeforeStartError(ValueError):
    """The requested close period precedes the realm's close start boundary."""

    def __init__(self, period: str, boundary: str | None):
        self.period = period
        self.boundary = boundary
        self.user_message = close_start_refusal_message(period, boundary)
        super().__init__(self.user_message)


def close_run_request_id(company_id: str, period: str) -> str:
    """Deterministic active-run dedupe key for one realm + clicked period."""
    return f"close:{company_id}:{validate_close_period(period)}"


def close_period_from_run(run: AgentRun) -> str | None:
    """Read the immutable period from a persisted run's structured context."""
    scope = run.permission_scope if isinstance(run.permission_scope, dict) else {}
    value = scope.get(CLOSE_PERIOD_SCOPE_KEY)
    try:
        return validate_close_period(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _close_scope(db: Session, company_id: str) -> tuple[Organization, OrganizationCompany]:
    organization = organization_for_company(db, company_id)
    if organization is None:
        raise CloseRunScopeError("Company is not mapped to an organization")
    organization_company = organization_company_for_realm(
        db,
        organization_id=organization.id,
        realm_id=company_id,
    )
    if organization_company is None:
        raise CloseRunScopeError("Company does not belong to the active organization")

    # Serialize starts for this realm on the already-unique organization-company
    # row.  This closes the concurrent double-create window without a new table or
    # migration; SQLite ignores FOR UPDATE while production Postgres enforces it.
    try:
        organization_company = (
            db.query(OrganizationCompany)
            .filter(OrganizationCompany.id == organization_company.id)
            .with_for_update()
            .one()
        )
    except NoResultFound as exc:
        raise CloseRunScopeError(
            "Company mapping disappeared before close preparation could start"
        ) from exc
    return organization, organization_company


def _find_or_create_holder(
    db: Session,
    *,
    company_id: str,
    principal: User,
    organization: Organization,
    organization_company: OrganizationCompany,
) -> AgentObjective:
    objective = (
        db.query(AgentObjective)
        .filter(
            AgentObjective.company_id == company_id,
            AgentObjective.objective_type == CLOSE_OBJECTIVE_TYPE,
            or_(
                AgentObjective.organization_id == organization.id,
                AgentObjective.organization_id.is_(None),
            ),
            or_(
                AgentObjective.organization_company_id == organization_company.id,
                AgentObjective.organization_company_id.is_(None),
            ),
        )
        .order_by(AgentObjective.id.asc())
        .first()
    )
    if objective is None:
        objective = AgentObjective(
            organization_id=organization.id,
            organization_company_id=organization_company.id,
            company_id=company_id,
            created_by_user_id=principal.id,
            name="Close orchestration (on-demand)",
            objective_type=CLOSE_OBJECTIVE_TYPE,
            instructions=None,
            schedule=None,
            enabled=False,
            next_run_at=None,
            permission_scope={
                "writeback": "proposal_only",
                "organization_id": organization.id,
                "organization_company_id": organization_company.id,
                "realm_id": organization_company.realm_id,
            },
        )
        db.add(objective)
        db.flush()
    else:
        # Adopt legacy null-scoped holders into the already-authorized tenant;
        # never overwrite a conflicting non-null tenant binding.
        if objective.organization_id is None:
            objective.organization_id = organization.id
        if objective.organization_company_id is None:
            objective.organization_company_id = organization_company.id
    return objective


def start_close_run(
    db: Session,
    *,
    company_id: str,
    period: str,
    principal: User,
) -> CloseRunStartResult:
    """Persist one visible active run per realm+period, then enqueue it.

    A duplicate while the run is active returns the same ``run_id`` and does not
    publish a second job.  A prior terminal attempt is immutable, so a later
    retry creates a new run with the same deterministic request key.
    """
    selected_period = validate_close_period(period)
    organization, organization_company = _close_scope(db, company_id)
    # Refuse a period before the close start boundary up front (the worker would
    # only skip it), so the cockpit can show why and where to change it.
    boundary = close_start_setting_for_company(organization_company).effective_period
    if boundary is not None and selected_period < boundary:
        db.commit()  # release the realm serialization row lock
        raise CloseRunBeforeStartError(selected_period, boundary)
    objective = _find_or_create_holder(
        db,
        company_id=company_id,
        principal=principal,
        organization=organization,
        organization_company=organization_company,
    )
    request_id = close_run_request_id(company_id, selected_period)

    active = (
        db.query(AgentRun)
        .filter(
            AgentRun.company_id == company_id,
            AgentRun.organization_id == organization.id,
            AgentRun.organization_company_id == organization_company.id,
            AgentRun.objective_id == objective.id,
            AgentRun.request_id == request_id,
            AgentRun.status.in_(CLOSE_ACTIVE_RUN_STATUSES),
        )
        .order_by(AgentRun.id.desc())
        .first()
    )
    if active is not None:
        objective_id = objective.id
        run_id = active.id
        active_status = active.status
        db.commit()  # release the realm serialization row lock before returning
        return CloseRunStartResult(
            objective_id=objective_id,
            run_id=run_id,
            company_id=company_id,
            period=selected_period,
            status=active_status,
            reused=True,
        )

    permission_scope = dict(objective.permission_scope or {})
    permission_scope.update(
        {
            "writeback": "proposal_only",
            "organization_id": organization.id,
            "organization_company_id": organization_company.id,
            "realm_id": company_id,
            CLOSE_PERIOD_SCOPE_KEY: selected_period,
        }
    )
    run = AgentRun(
        organization_id=organization.id,
        organization_company_id=organization_company.id,
        company_id=company_id,
        user_id=principal.id,
        objective_id=objective.id,
        trigger_type="manual",
        status="queued",
        model_provider="system",
        model_name=None,
        prompt_version=CLOSE_RUN_PROMPT_VERSION,
        input_summary=f"Prepare month-end close for {selected_period}",
        permission_scope=permission_scope,
        request_id=request_id,
    )
    db.add(run)
    db.flush()
    objective_id = objective.id
    run_id = run.id

    # Load-bearing ordering: the worker opens a fresh session and adopts this PK.
    db.commit()

    from ..tasks.agent import run_close_objective_now

    try:
        run_close_objective_now.delay(objective_id, run_id, selected_period)
    except Exception as exc:
        # queued -> failed is a valid terminal transition.  Persist a safe,
        # actionable failure; raw broker details stay in redacted logs only.
        run.status = "failed"
        run.error = "Close preparation could not be queued. Retry the start action."
        run.output_summary = run.error
        run.finished_at = datetime.utcnow()
        db.commit()
        logger.warning(
            "[close_run_start] enqueue failed (run_id=%s, realm=%s, period=%s): %s",
            run_id,
            company_id,
            selected_period,
            redact_to_string(str(exc), max_length=500),
        )
        raise CloseRunEnqueueError(
            CloseRunStartResult(
                objective_id=objective_id,
                run_id=run_id,
                company_id=company_id,
                period=selected_period,
                status="failed",
                reused=False,
                retryable=True,
            )
        ) from exc

    logger.info(
        "[close_run_start] queued close run (objective_id=%s, run_id=%s, realm=%s, period=%s)",
        objective_id,
        run_id,
        company_id,
        selected_period,
    )
    return CloseRunStartResult(
        objective_id=objective_id,
        run_id=run_id,
        company_id=company_id,
        period=selected_period,
        status="queued",
        reused=False,
    )


@dataclass(frozen=True)
class VendorAccrualOutcome:
    missing: int
    drafted: int
    unresolved: int


def _draft_once(db: Session, *, realm_id: str, action: ProposedAction) -> bool:
    """Add ``action`` unless a proposal with the same fingerprint exists (redelivery-safe)."""
    fingerprint = compute_proposal_fingerprint(action.action_type, action.payload)
    existing = (
        db.query(ProposedAction.id)
        .filter(
            ProposedAction.company_id == realm_id,
            ProposedAction.inputs_fingerprint == fingerprint,
        )
        .first()
    )
    if existing is not None:
        return False
    action.inputs_fingerprint = fingerprint
    db.add(action)
    return True


def draft_missing_vendor_accruals(
    db: Session,
    *,
    realm_id: str,
    period: str,
    agent_run_id: int | None,
    hard_locked: bool,
) -> VendorAccrualOutcome:
    """Preserve the Calendar's recurring-vendor accrual review, idempotently.

    This is the deterministic legacy detector (monthly ``VendorProfile`` with no
    matching ``QBOTransaction`` in the selected month), not the distinct
    schedule-entry detector used by ``MissingAccrualCheck``.  It drafts review
    proposals only; it never approves or posts a journal entry.  Fingerprint
    lookup plus the close-task uniqueness constraint make redelivery safe.

    A drafted accrual debits the vendor's historical expense account and
    credits the company's Accrued Liabilities account, both as resolved account
    ids (``qbo_actions.je_lines``). When either account cannot be resolved no
    journal entry is drafted — an unpostable or liability-to-liability draft
    would recognize no expense — and a review-only ``REVIEW_CHECK`` finding
    names the vendor and the reason instead.

    Shared by the close-run starter and the beat's month-end prep task.
    """
    year, month = (int(part) for part in period.split("-"))
    period_start = date(year, month, 1)
    period_end = date(year, month, calendar_module.monthrange(year, month)[1])
    vendors = (
        db.query(VendorProfile)
        .filter(
            VendorProfile.realm_id == realm_id,
            VendorProfile.expected_frequency == "MONTHLY",
        )
        .all()
    )
    billed_names = {
        row[0]
        for row in (
            db.query(QBOTransaction.vendor_name)
            .filter(
                QBOTransaction.realm_id == realm_id,
                QBOTransaction.date >= period_start,
                QBOTransaction.date <= period_end,
                QBOTransaction.vendor_name.isnot(None),
            )
            .all()
        )
    }
    missing = [vendor for vendor in vendors if vendor.vendor_name not in billed_names]
    if hard_locked:
        return VendorAccrualOutcome(missing=len(missing), drafted=0, unresolved=0)

    # One proposal authority per business fact: when an ACCRUAL schedule already
    # represents the vendor for this period, ``MissingAccrualCheck`` owns the
    # ScheduleEntry-backed proposal.  The vendor detector still contributes to
    # the Calendar count, but does not draft a second semantic JE.
    scheduled_vendor_names = {
        row[0]
        for row in (
            db.query(AccountingSchedule.vendor_name)
            .join(ScheduleEntry, ScheduleEntry.schedule_id == AccountingSchedule.id)
            .filter(
                AccountingSchedule.realm_id == realm_id,
                AccountingSchedule.schedule_type == "ACCRUAL",
                AccountingSchedule.vendor_name.isnot(None),
                ScheduleEntry.realm_id == realm_id,
                ScheduleEntry.period == period,
            )
            .all()
        )
    }

    review_task = (
        db.query(CloseTask)
        .filter(
            CloseTask.realm_id == realm_id,
            CloseTask.period == period,
            CloseTask.task_type == "REVIEW_ACCRUALS",
        )
        .order_by(CloseTask.id.asc())
        .first()
    )
    liability = resolve_accrued_liability_account(db, realm_id)
    drafted = 0
    unresolved = 0
    for vendor in missing:
        if vendor.vendor_name in scheduled_vendor_names:
            continue
        amount = Decimal(str(vendor.avg_monthly_spend or 0)).quantize(Decimal("0.01"))
        if amount <= 0:
            continue
        expense = resolve_vendor_expense_account(db, realm_id, vendor)
        reason = accrual_unresolved_reason(expense, liability)
        if reason is not None:
            unresolved += 1
            finding = unresolved_accrual_finding(
                vendor_name=vendor.vendor_name, period=period, amount=amount, reason=reason
            )
            _draft_once(
                db,
                realm_id=realm_id,
                action=ProposedAction(
                    company_id=realm_id,
                    action_type="REVIEW_CHECK",
                    payload=finding,
                    reasoning=finding["message"],
                    confidence_score=0.75,
                    status="PENDING",
                    source="system",
                    category="review_check",
                    close_task_id=review_task.id if review_task is not None else None,
                    agent_run_id=agent_run_id,
                ),
            )
            continue
        payload = {
            "date": period_end.isoformat(),
            "memo": je_memo(f"Accrued Expense – {vendor.vendor_name} – {period}"),
            "lines": accrual_journal_lines(
                vendor_name=vendor.vendor_name, amount=amount, expense=expense, liability=liability
            ),
            "source_module": "accounting",
            "entry_type": "ACCRUAL",
            "vendor_name": vendor.vendor_name,
        }
        if _draft_once(
            db,
            realm_id=realm_id,
            action=ProposedAction(
                company_id=realm_id,
                action_type="JOURNAL_ENTRY",
                payload=payload,
                reasoning=(
                    f"No {vendor.vendor_name} transaction found in {period}. "
                    f"Expected ~${amount:,.2f}/month based on historical average."
                ),
                confidence_score=0.75,
                status="PENDING",
                source="system",
                category="journal_entry",
                close_task_id=review_task.id if review_task is not None else None,
                agent_run_id=agent_run_id,
            ),
        ):
            drafted += 1
    return VendorAccrualOutcome(missing=len(missing), drafted=drafted, unresolved=unresolved)


def prepare_close_period(
    db: Session,
    *,
    realm_id: str,
    period: str,
    agent_run_id: int | None = None,
) -> dict[str, Any]:
    """Persist visible close-prep state and observed counts for ``period``.

    Missing accruals retain the Calendar's recurring-vendor detector.  A vendor
    already represented by an ACCRUAL ScheduleEntry is routed exclusively to the
    registered ``REVIEW_ACCRUALS`` check; only otherwise-unrepresented vendors
    get the legacy-shaped PENDING review proposal.  Nothing here posts, approves,
    closes, or locks anything.
    """
    selected_period = validate_close_period(period)
    # Hard-lock authority is evaluated before even loading/creating the selected
    # calendar row.  The helper applies the lock-as-of cascade and fails closed
    # when FORCE-RLS scope or storage is indeterminate.  A locked period is a
    # total prep no-op: no timestamps/counts, checklist linkage, or proposals.
    if period_is_hard_locked(db, realm_id=realm_id, period=selected_period):
        return {
            "period": selected_period,
            "schedules_processed": 0,
            "adjusting_entries": 0,
            "missing_accruals": 0,
            "missing_accrual_proposals": 0,
            "missing_accrual_unresolved": 0,
            "hard_locked": True,
            "skipped": True,
            "reason": "period_locked",
        }
    calendar = (
        db.query(FinancialCalendar)
        .filter(
            FinancialCalendar.realm_id == realm_id,
            FinancialCalendar.period == selected_period,
        )
        .first()
    )
    if calendar is None:
        calendar = FinancialCalendar(
            realm_id=realm_id,
            period=selected_period,
            status="MONTH_END_IN_PROGRESS",
            locked=False,
        )
        db.add(calendar)

    # An explicit prep rerun may reopen a legacy observational soft-CLOSED row;
    # only the separate human hard lock is immutable authority.
    calendar.status = "MONTH_END_IN_PROGRESS"
    calendar.month_end_triggered_at = datetime.utcnow()
    db.commit()

    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.realm_id == realm_id,
            ScheduleEntry.period == selected_period,
        )
        .all()
    )
    calendar.schedules_processed = len({entry.schedule_id for entry in entries})
    calendar.adjusting_entries_count = len(
        {entry.proposed_action_id for entry in entries if entry.proposed_action_id}
    )

    accruals = draft_missing_vendor_accruals(
        db,
        realm_id=realm_id,
        period=selected_period,
        agent_run_id=agent_run_id,
        hard_locked=False,
    )
    calendar.missing_accruals_count = accruals.missing
    calendar.notes = {
        "schedules_processed": calendar.schedules_processed,
        "adjusting_entries": calendar.adjusting_entries_count,
        "missing_accruals": accruals.missing,
        "missing_accrual_proposals": accruals.drafted,
        "missing_accrual_unresolved": accruals.unresolved,
        "run_status": "running",
    }
    db.commit()
    return {
        "period": selected_period,
        "schedules_processed": calendar.schedules_processed,
        "adjusting_entries": calendar.adjusting_entries_count,
        "missing_accruals": accruals.missing,
        "missing_accrual_proposals": accruals.drafted,
        "missing_accrual_unresolved": accruals.unresolved,
        "hard_locked": False,
    }


def complete_close_period_prep(
    db: Session,
    *,
    realm_id: str,
    period: str,
    run_summary: dict[str, Any],
) -> None:
    """Mark prep observation complete without changing hard-lock authority."""
    selected_period = validate_close_period(period)
    if period_is_hard_locked(db, realm_id=realm_id, period=selected_period):
        return
    calendar = (
        db.query(FinancialCalendar)
        .filter(
            FinancialCalendar.realm_id == realm_id,
            FinancialCalendar.period == selected_period,
        )
        .first()
    )
    if calendar is None:
        return
    calendar.status = "MONTH_END_IN_PROGRESS"
    calendar.month_end_completed_at = datetime.utcnow()
    notes = dict(calendar.notes or {})
    notes.update(
        {
            "run_status": str(run_summary.get("status") or "succeeded"),
            "walked": int(run_summary.get("walked") or 0),
            "proposed": int(run_summary.get("proposed") or 0),
        }
    )
    calendar.notes = notes
    db.commit()


def fail_close_period_prep(
    db: Session,
    *,
    realm_id: str,
    period: str,
    error: Exception,
) -> None:
    """Best-effort visible failure note; never unlock or auto-close the period."""
    try:
        db.rollback()
        selected_period = validate_close_period(period)
        if period_is_hard_locked(db, realm_id=realm_id, period=selected_period):
            return
        calendar = (
            db.query(FinancialCalendar)
            .filter(
                FinancialCalendar.realm_id == realm_id,
                FinancialCalendar.period == selected_period,
            )
            .first()
        )
        if calendar is None:
            return
        notes = dict(calendar.notes or {})
        notes.update(
            {
                "run_status": "failed",
                "error": "Close preparation failed. Retry the start action.",
            }
        )
        calendar.notes = notes
        db.commit()
    except Exception as note_exc:  # failure surfacing must not mask the root error
        db.rollback()
        logger.warning(
            "[close_run_start] close prep failed or its calendar note could not be persisted "
            "(realm=%s, period=%s): %s",
            realm_id,
            period,
            redact_to_string(f"root={error}; note={note_exc}", max_length=500),
        )

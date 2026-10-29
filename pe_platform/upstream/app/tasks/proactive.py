"""
Celery tasks for proactive AI intelligence.

Runs nightly across all connected companies to detect anomalies,
generate insights, create proactive action proposals, run watchdog
checks, predictive cash forecasting, and send morning briefing emails.
"""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional  # noqa: F401

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)


def _company_scope(db, company_id: str):
    from ..services.tenant_companies import organization_company_for_realm, organization_for_company

    organization = organization_for_company(db, company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=company_id)
        if organization
        else None
    )
    return organization, organization_company


def _nightly_period() -> str:
    """The closed-month period the nightly run analyzes (the month that just
    ended): ``YYYY-MM`` for last month. One helper so the orchestrator and the
    per-realm task derive the SAME period (a fanned-out child must analyze the
    same month the orchestrator intended)."""
    today = date.today()
    last_month = date(today.year, today.month, 1) - timedelta(days=1)
    return f"{last_month.year}-{last_month.month:02d}"


def _retry_countdown(retries: int) -> int:
    """Capped exponential backoff + jitter for a task retry (AP-CEL-4): space
    retries out so an overloaded DB/QBO/LLM downstream can recover instead of
    being hammered at a constant cadence, and de-synchronize many tasks retrying
    at once. 60s base, doubling, capped at 600s, +/- up to 30s of jitter."""
    import random

    base = min(60 * (2 ** retries), 600)
    return max(1, base + random.randint(-30, 30))


@celery_app.task(bind=True, max_retries=2)
def run_nightly_intelligence(self) -> dict:
    """
    Nightly proactive intelligence ORCHESTRATOR (AP-CEL-9 fan-out).

    Enumerates every connected company and dispatches ONE
    ``run_realm_nightly_intelligence`` child task per realm via ``.delay`` —
    instead of running every realm serially inside this one long task. Fan-out
    keeps each child message well under the Redis broker visibility_timeout (the
    old serial loop over all tenants could run past it and be redelivered +
    executed concurrently), gives each realm an independent ack / retry, and lets
    one slow/failing tenant not block the rest. The children route to the
    dedicated "nightly" queue (app/celery_app.py task_routes) so they can't
    starve the default fan-out queue.
    """
    db = SessionLocal()
    try:
        from ..models import QBOToken  # noqa: F401

        # CROSS-TENANT SWEEP: this orchestration query spans ALL connected
        # companies, so it runs under bypass; each per-realm CHILD task below
        # re-scopes to its own realm (company_id) so no per-tenant work runs
        # unscoped.
        with tenant_scope(bypass=True):
            realm_ids = [
                token.realm_id
                for token in db.query(QBOToken)
                .filter(QBOToken.access_token.isnot(None))
                .all()  # CROSS-TENANT-OK: nightly all-tenants enumeration; each realm re-scoped inside its own fanned-out child task
                if token.realm_id
            ]
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
    finally:
        db.close()

    if not realm_ids:
        logger.info("No connected companies found, skipping nightly intelligence.")
        return {"status": "skipped", "reason": "no_companies"}

    period = _nightly_period()
    dispatched = 0
    for realm_id in realm_ids:
        # One short child per realm — independent ack/retry, routed to "nightly".
        run_realm_nightly_intelligence.delay(realm_id, period=period)
        dispatched += 1

    logger.info("[run_nightly_intelligence] fanned out dispatched=%s", dispatched)
    return {
        "status": "dispatched",
        "date": str(date.today()),
        "period": period,
        "companies_dispatched": dispatched,
    }


@celery_app.task(bind=True, max_retries=2, name="app.tasks.proactive.run_realm_nightly_intelligence")
def run_realm_nightly_intelligence(self, realm_id: str, period: str | None = None) -> dict:
    """
    Per-realm nightly proactive intelligence unit (fanned out by
    ``run_nightly_intelligence``). Runs the full pipeline for ONE company:
    1. Analysis pipeline (anomaly detection + KPI computation)
    2. Legacy ALERT_ESCALATION proposal sweep (retirement; creation removed)
    3. Watchdog alert checks (covenant breaches, negative cash, etc.)
    4. Predictive 13-week cash forecast
    5. Executive briefing generation
    6. Morning briefing email delivery

    IDEMPOTENT-OK: safe to re-run under acks_late at-least-once delivery — every
    write path in ``_run_company_intelligence`` is dedupe/upsert-guarded
    (ProposedAction existence checks + ScheduleEntry.proposed_action_id
    back-references, create_watchdog_alert_if_new's open-alert dedupe, the
    per-(check, period) review-check dedupe, and the upsert-by-(company, date)
    ExecutiveBriefing) so a redelivery produces no duplicate proposals/alerts.
    The morning-email send is bounded to at most one extra send per day by the
    same-day briefing lookup.
    """
    from ..models import QBOCompanyInfo  # noqa: F401

    if period is None:
        period = _nightly_period()

    db = SessionLocal()
    try:
        # Resolve the display name under this realm's own scope; the heavy
        # per-company pipeline re-pins RLS to the realm inside the helper.
        with tenant_scope_for_realm(db, realm_id):
            company_info = db.query(QBOCompanyInfo).filter_by(realm_id=realm_id).first()
        company_name = company_info.company_name if company_info else realm_id

        # AC-A2 (plan 20260716-five-domain-remediation): the effective period is
        # resolved PER-REALM — the orchestrator's global _nightly_period() cannot
        # know this realm's close state. A soft-CLOSED month (the 2026-06
        # auto-close cohort) advances to the realm's first open period. A
        # calendar read error propagates into this task's retry path (fail-loud:
        # never analyze a possibly-closed month).
        from ..orchestration.pipeline import resolve_open_period

        period = resolve_open_period(db, realm_id, period)

        logger.info(f"Running nightly intelligence for {company_name} ({realm_id})")
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-7): every LLM
        # call under the nightly engine records task_type="watchdog" on its
        # ledger row (the briefing generator re-binds "briefing" inside).
        from ..services.llm_task_context import bind_llm_task_context

        with bind_llm_task_context("watchdog", realm_id=realm_id):
            return _run_company_intelligence(db, realm_id, company_name, period)

    except Exception as exc:
        db.rollback()
        logger.error(
            "Nightly intelligence failed for %s: %s",
            realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# ALERT_ESCALATION retirement (pilot fix 2026-07-01)
# ─────────────────────────────────────────────────────────────────────────────
# The nightly run used to mirror every critical InsightAnomaly into a PENDING
# ALERT_ESCALATION ProposedAction. ALERT_ESCALATION was never in
# the executor's EXECUTABLE_ACTION_TYPES (app/qbo_actions/executor.py), so those
# rows could only ever be dismissed — pure write-approval-queue pollution.
# Creation is REMOVED: critical anomalies surface through the Alerts Center
# (InsightAnomaly via /insights/alerts), the executive-briefing key_risks, and
# the WatchdogAlert producers — never through the write queue.

def sweep_alert_escalation_proposals(db, company_id: Optional[str] = None) -> int:
    """Retire legacy non-executable ALERT_ESCALATION ProposedActions.

    Marks every PENDING ALERT_ESCALATION row REJECTED with the retirement
    reasoning. IDEMPOTENT-OK: only PENDING rows match and a swept row is no
    longer PENDING, so a re-run (nightly per-realm or a maintenance invocation)
    is a no-op. Scoped to ``company_id`` when given (the nightly per-realm caller,
    already under that realm's RLS scope); unscoped for a maintenance sweep.
    Returns the number of rows retired.
    """
    from ..models import ProposedAction

    retirement_note = "non-executable escalation retired 2026-07-01"
    q = db.query(ProposedAction).filter(
        ProposedAction.action_type == "ALERT_ESCALATION",
        ProposedAction.status == "PENDING",
    )
    if company_id is not None:
        q = q.filter(ProposedAction.company_id == company_id)
    rows = q.all()
    for row in rows:
        row.status = "REJECTED"
        row.reasoning = retirement_note
        row.reviewed_by = "system:alert_escalation_retirement"
        row.reviewed_at = datetime.utcnow()
        row.execution_result = {"reviewer_comment": retirement_note}
    if rows:
        db.commit()
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# AI GL Scraping
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Proactive: missing recurring accruals (budget-gated single-draft, proposal-first)
# ─────────────────────────────────────────────────────────────────────────────

# Token-bounded thresholds (module constants; tune here — config.py is out of scope).
MISSING_ACCRUAL_MATERIALITY_FLOOR = Decimal("100")   # per-tenant amount floor
MAX_ACCRUAL_PROPOSALS_PER_RUN = 10                   # bound LLM drafts per company/period

# Tenant-visible signal for the hard-budget block on proactive drafting (the
# block itself is a ratified control — the alert only explains WHY suggestions
# stopped; it never weakens or bypasses the gate).
PROACTIVE_BUDGET_PAUSED_ALERT_TYPE = "PROACTIVE_BUDGET_PAUSED"


def _budget_month() -> str:
    """The CURRENT calendar month (``YYYY-MM``) — the tenant-budget window
    (cost_attribution.current_month_window is month-to-date), deliberately NOT
    ``_nightly_period`` (the analyzed close month). The budget-paused alert is
    fingerprinted on THIS month so exactly one alert exists per tenant per
    budget period, and a new month (budget reset) re-alerts if still blocked."""
    today = date.today()
    return f"{today.year}-{today.month:02d}"


def _surface_proactive_budget_block(db, *, company_id: str, reason: Optional[str] = None) -> bool:
    """Make the hard-budget block on proactive drafting TENANT-VISIBLE.

    Without this, the nightly run silently proposes 0 items at hard budget and
    the tenant has no idea why suggestions stopped. Goes through the SAME
    ``create_watchdog_alert_if_new`` helper every nightly watchdog producer
    uses (open-alert dedupe + acknowledged-fingerprint suppression) — the
    fingerprint reduces to (tenant, alert_type, budget month), so the daily
    nightly run creates at most ONE alert per tenant per budget period.
    Best-effort: surfacing failure is logged and swallowed — it must never
    mask the block verdict or fail the nightly task. Returns True if created.
    """
    try:
        created = create_watchdog_alert_if_new(
            db,
            company_id=company_id,
            alert_type=PROACTIVE_BUDGET_PAUSED_ALERT_TYPE,
            period=_budget_month(),
            severity="warning",
            title="AI suggestions paused — budget status unavailable" if reason == "budget_lookup_unavailable" else "AI suggestions paused — monthly budget reached",
            description=("Budget status could not be verified. Model drafting is paused until budget lookup succeeds; deterministic checks continue." if reason == "budget_lookup_unavailable" else
                "Proactive AI drafting is paused for this workspace because the "
                "monthly AI budget has been reached. Nightly suggestions resume "
                "automatically when the budget resets at the start of next month, "
                "or sooner if an admin raises the monthly AI budget in billing "
                "settings. Nothing is lost: detected items are re-evaluated on the "
                "next nightly run once budget is available."
            ),
            context={"budget_reason": reason} if reason else None,
        )
        if created:
            db.commit()
        return created
    except Exception as e:  # surfacing must never mask the block or fail the task
        db.rollback()
        logger.warning(
            "Failed to surface proactive budget-paused alert for %s: %s",
            company_id,
            redact_to_string(str(e), max_length=300),
        )
        return False


def _detect_missing_accruals(db, realm_id: str, period: str):
    """Deterministic (ZERO LLM): ACCRUAL ScheduleEntries for ``period`` that are
    unposted AND not already proposed. ScheduleEntry IS the expected entry — its
    ``status`` + ``proposed_action_id`` give 'unposted' + dedupe directly, so no
    GL join is needed and a re-run skips already-proposed entries."""
    from ..models import AccountingSchedule, ScheduleEntry

    return (
        db.query(ScheduleEntry)
        .join(AccountingSchedule, AccountingSchedule.id == ScheduleEntry.schedule_id)
        .filter(
            ScheduleEntry.realm_id == realm_id,
            ScheduleEntry.period == period,
            ScheduleEntry.status != "POSTED",
            ScheduleEntry.proposed_action_id.is_(None),
            AccountingSchedule.schedule_type == "ACCRUAL",
        )
        .all()
    )


def _draft_accrual_proposal(service, company_name: str, entry, period: str) -> dict:
    """The single LLM drafting call per material item — drafts a rationale/memo for
    the proposed adjusting entry. The QBO write only happens later, on human
    approval (proposal-first); this makes no mutating call."""
    result = service.predict_text_result(
        system_prompt=(
            "You draft a proposed adjusting journal entry for a missing recurring "
            "accrual. Be concise: a short rationale and a GL memo."
        ),
        user_prompt=(
            f"Company: {company_name}. Period: {period}. Accrual amount: {entry.amount}. "
            "Draft a one-paragraph rationale and a GL memo for the adjusting entry."
        ),
        temperature=0.2,
        model=None,
        json_mode=False,
        allow_fallback=True,
    )
    text = getattr(result, "text", None) or getattr(result, "content", None) or ""
    return {"rationale": str(text)} if text else {}


def _propose_missing_accruals(db, company_id: str, company_name: str, period: str, llm=None) -> int:
    """Phase-4 proactive suggestion: surface missing recurring accruals as
    approve/dismiss ``ProposedAction`` rows.

    Cheap deterministic detector -> materiality gate -> closed-period gate (#15-C1)
    -> proactive spend-cap gate -> exactly ONE LLM draft per material item ->
    PENDING ``ProposedAction(source='ai_proactive')``. Proposal-first: NO GL write,
    no auto-execute (the human still approves every write). Token-bounded: ZERO LLM
    on no-trigger; capped per run; dedupe via ``ScheduleEntry.proposed_action_id``
    so a re-run is a no-op. Returns the count proposed.
    """
    from ..models import ProposedAction
    from ..services.cost_attribution import enforce_tenant_budget
    from ..services.llm import get_llm_service
    from ..services.task_autonomy import period_is_closed

    missing = _detect_missing_accruals(db, company_id, period)
    if not missing:
        return 0  # ZERO LLM on no-trigger

    # Closed-period guard (#15-C1): never suggest an entry into a locked period.
    if period_is_closed(db, realm_id=company_id, period=period):
        return 0

    # Proactive spend-cap (GAP-2): a HARD block stops LLM drafting. A 'downgrade'
    # (soft budget, >=90%) is handled cheaply inside predict_text_result's lane
    # bias, so it still drafts — no alert for downgrade; ONLY the hard block is
    # tenant-surfaced below.
    org, org_company = _company_scope(db, company_id)
    budget = enforce_tenant_budget(
        db,
        organization_id=org.id if org else None,
        organization_company_id=org_company.id if org_company else None,
    )
    if budget.get("action") in ("block", "unavailable"):
        # Ratified control, unchanged: return 0 proposals. Added signal only —
        # a deduped WatchdogAlert so the tenant sees WHY suggestions stopped.
        _surface_proactive_budget_block(db, company_id=company_id, reason=budget.get("reason"))
        return 0

    service = llm if llm is not None else get_llm_service("analysis")
    proposed = 0
    for entry in missing:
        if proposed >= MAX_ACCRUAL_PROPOSALS_PER_RUN:
            break  # token-bounded cap per company/period (also AP-CEL-9 bound)
        if entry.amount is None or Decimal(str(entry.amount)) < MISSING_ACCRUAL_MATERIALITY_FLOOR:
            continue  # below materiality -> no draft, no proposal
        draft = _draft_accrual_proposal(service, company_name, entry, period)
        rationale = draft.get("rationale") or f"Recurring accrual for {period} appears unposted."  # SA-OK: dict access, not a DB query
        action = ProposedAction(
            company_id=company_id,
            action_type="JOURNAL_ENTRY",
            payload={
                "schedule_id": entry.schedule_id,
                "schedule_entry_id": entry.id,
                "period": period,
                "amount": float(entry.amount),
                "draft": draft,
            },
            reasoning=rationale,
            confidence_score=0.85,
            status="PENDING",
            source="ai_proactive",
            category="journal_entry",
            priority="normal",
        )
        db.add(action)
        db.flush()  # obtain action.id for the dedupe back-reference
        entry.proposed_action_id = action.id  # IDEMPOTENT-OK: marks this entry proposed; a re-run's detector skips it
        proposed += 1
    db.commit()
    return proposed


# ─────────────────────────────────────────────────────────────────────────────
# Shared analytical-review checks (Phase C, C3) — the SAME registry the Phase B
# close driver invokes, run here in the nightly proactive loop so the two callers
# share one check library (SYNTHESIS line 53). Additive: the inline detectors that
# a registered check SUPERSEDES are routed through the check (no double-fire); the
# rest keep running. run_check is proposal-first — it drafts a PENDING
# ProposedAction via the injected `propose` below and NEVER writes the GL.
# ─────────────────────────────────────────────────────────────────────────────

MAX_REVIEW_CHECK_PROPOSALS_PER_RUN = 25  # bound LLM drafts per company/period (AP-CEL-9 bound)


def _run_shared_review_checks(db, company_id: str, company_name: str, period: str, qbo_client=None, llm=None) -> int:
    """Run every registered analytical-review Check for one company/period.

    Iterates ``CHECK_REGISTRY`` (populated on import of ``app.services.review_checks``)
    and runs each through the SHARED ``run_check`` — the identical gate sequence the
    close driver uses (scan → could_not_verify → clean → closed-period → materiality
    → one-LLM → propose). The nightly ``propose`` injected here writes a PENDING
    ``ProposedAction(source='ai_proactive')`` with a per-(check, period) dedupe so a
    re-run is a no-op. PROPOSAL-FIRST: no GL write. Returns the count proposed.

    HARD-BUDGET GATE (mirrors ``_propose_missing_accruals`` GAP-2): a hard tenant-
    budget block (>=100% month-to-date) stops the LLM-spending work in this nightly
    sweep. EVERY registered check spends on the SAME single path — ``run_check``'s
    one Stage-7 LLM draft on a material HIT (protocol.py:396); the per-check
    ``scan`` (Stage 1) is a ZERO-LLM deterministic detector. So a hard block gates
    that ONE spend path: the free scans still run (they cost nothing and preserve
    the nightly close-readiness signal) while the per-check LLM draft + propose is
    skipped, and a single deduped ``PROACTIVE_BUDGET_PAUSED`` WatchdogAlert is
    surfaced (the SAME signal the accrual path uses). A soft budget (>=90%
    'downgrade') is NOT hard-skipped here — this Celery path never consumes the
    downgrade verdict (the interactive lane-downgrade bias lives inside
    ``predict_text_result``, not here), so a soft budget runs ``run_check`` normally.
    """
    from ..models import AuditLog, ProposedAction
    from ..services.cost_attribution import enforce_tenant_budget
    from ..services.review_checks import (
        CHECK_REGISTRY,
        CheckContext,
        Finding,
        check_runs_under,
        get_effective_aggressiveness,
        get_effective_materiality,
        get_tier,
        run_check,
    )
    from ..services.review_checks.protocol import PROGRAMMING_ERRORS

    # Resolve the company's review_aggressiveness ONCE per (realm, period) run —
    # the per-check skip decision below reads this static value, not the DB per
    # check. Fail-soft to "balanced" (never raises) so a policy-read issue can
    # never skip a check by accident (get_effective_aggressiveness contract).
    aggressiveness = get_effective_aggressiveness(db, realm_id=company_id)

    # Proactive spend-cap (mirror of ``_propose_missing_accruals``): resolve the
    # tenant's monthly hard-budget verdict ONCE for this sweep. On a HARD block
    # (>=100% MTD) the ratified control stops the LLM draft on every check — the
    # scans below still run (ZERO-LLM detectors), only run_check's Stage-7 draft +
    # propose is skipped. Surface the SAME deduped WatchdogAlert the accrual path
    # uses so the tenant sees ONE coherent "AI paused" signal (never a second
    # alert type). Lookup failures pause model drafting while deterministic
    # scans continue and expose the unavailable-budget reason.
    org, org_company = _company_scope(db, company_id)
    budget = enforce_tenant_budget(
        db,
        organization_id=org.id if org else None,
        organization_company_id=org_company.id if org_company else None,
    )
    budget_blocked = budget.get("action") in ("block", "unavailable")
    if budget_blocked:
        _surface_proactive_budget_block(db, company_id=company_id, reason=budget.get("reason"))

    proposed = 0

    def _propose(*, ctx, finding: "Finding", reasoning: str) -> int | None:
        # The nightly injected ProposedAction writer (differs from the close
        # driver's only by dedupe key + source). Per-(check, period) dedupe so a
        # re-run does not re-propose the same finding.
        existing = (
            db.query(ProposedAction)
            .filter(
                ProposedAction.company_id == company_id,
                ProposedAction.action_type == "REVIEW_CHECK",
                ProposedAction.status == "PENDING",
                ProposedAction.payload["check_id"].astext == finding.check_id,
                ProposedAction.payload["period"].astext == ctx.period,
            )
            .first()
        )
        if existing is not None:
            return None  # IDEMPOTENT-OK: per-(check_id, period) dedupe; a re-run skips an existing PENDING proposal
        action = ProposedAction(
            company_id=company_id,
            action_type="REVIEW_CHECK",
            payload={
                "check_id": finding.check_id,
                "period": ctx.period,
                "severity": finding.severity.value,
                "message": finding.message,
                "finding": finding.payload,
            },
            reasoning=reasoning or finding.message,
            confidence_score=0.85,
            status="PENDING",
            source="ai_proactive",
            category="review_check",
            priority="normal",
        )
        db.add(action)
        db.flush()  # obtain action.id
        nonlocal proposed
        proposed += 1
        return action.id

    for check_id, check in CHECK_REGISTRY.items():  # SA-OK: bounded-loop in-memory registry dict iteration, not a DB query
        if proposed >= MAX_REVIEW_CHECK_PROPOSALS_PER_RUN:
            break  # token-bounded cap per company/period (AP-CEL-9 bound)

        # AGGRESSIVENESS SKIP (Phase E AC-2) — HONEST whole-check skip BEFORE any
        # scan / materiality / LLM. A check whose tier is below the company's
        # review_aggressiveness is skipped at the selection point: a POLICY_SKIPPED
        # AuditLog row is written (NEVER a clean/NO_HIT result — a skipped check has
        # NOT proven the books clean) and we `continue`. ZERO LLM, no scan.
        tier = get_tier(check_id)
        if not check_runs_under(aggressiveness, tier):
            db.add(
                AuditLog(
                    realm_id=company_id,
                    action="UPDATE",
                    entity_type="ReviewCheck",
                    entity_id=check_id,
                    user_id="system:proactive_review",
                    details={
                        "event": "review_check_policy_skipped",
                        "check_id": check_id,
                        "tier": tier.value,
                        "aggressiveness": aggressiveness,
                        "period": period,
                        "reason": "tier below company review_aggressiveness",
                    },
                )
            )
            continue  # skipped by policy — no scan, no LLM, no proposal

        materiality = get_effective_materiality(db, check_id, realm_id=company_id)
        if materiality is None:
            continue  # registry inconsistency — skip rather than crash the nightly run
        ctx = CheckContext(
            db=db,
            realm_id=company_id,
            period=period,
            propose=_propose,
            materiality=materiality,
            qbo_client=qbo_client,  # nightly HAS a live client → checks that need it can evaluate
            company_name=company_name,
            llm=llm,  # injectable LLM seam (None → run_check falls back to get_llm_service)
        )
        try:
            if budget_blocked:
                # HARD budget block: run only the ZERO-LLM deterministic detector
                # (preserves the nightly close-readiness signal) and SKIP run_check's
                # Stage-7 LLM draft + Stage-8 propose (the spend). Mirrors
                # ``_propose_missing_accruals``: the detector runs; drafting is
                # skipped. The Finding is discarded (no propose, no GL write).
                check.scan(ctx)
            else:
                run_check(db, check, ctx)
        except PROGRAMMING_ERRORS as e:
            # OUR defect, not a provider failure. The sweep continues, but this is
            # logged with a trace at ERROR so a code bug cannot read as a routine
            # degraded check.
            logger.error(
                "Shared review check %s raised a code defect for %s: %s",
                check_id,
                company_name,
                redact_to_string(str(e), max_length=300),
                exc_info=True,
            )
        except Exception as e:  # one bad check must not kill the nightly run
            logger.warning(
                "Shared review check %s failed for %s: %s",
                check_id,
                company_name,
                redact_to_string(str(e), max_length=300),
            )
    db.commit()
    return proposed


def _scrape_gl_for_new_schedules(db, company_id: str, company_name: str, period: str):
    """
    Search QBOTransaction for debits > $500 that route to known BS asset accounts.
    If not already tracked in AccountingSchedule, use detect_with_ai to
    create a draft schedule and propse it in the AI Action inbox.
    """
    from ..models import QBOTransaction, AccountingSchedule, ScheduleCreditMapping, ProposedAction, WatchdogAlert
    from ..services.accounting.detector import detect_with_ai
    from datetime import date, timedelta
    from ..config import settings

    # 1. Which accounts are configured for schedules?
    mappings = db.query(ScheduleCreditMapping).filter(ScheduleCreditMapping.realm_id == company_id).all()
    target_account_ids = {m.qbo_account_id: m.schedule_type for m in mappings}

    date_cutoff = date.today() - timedelta(days=60)

    # Pull recent large enough txns
    txns = db.query(QBOTransaction).filter(
        QBOTransaction.realm_id == company_id,
        QBOTransaction.date >= date_cutoff,
        QBOTransaction.amount >= 500
    ).all()

    if not txns:
        return

    # Check which txns are already tracked
    existing_txn_ids = {
        s.qbo_txn_id for s in db.query(AccountingSchedule.qbo_txn_id).filter(
            AccountingSchedule.realm_id == company_id,
            AccountingSchedule.qbo_txn_id.isnot(None)
        ).all()
    }

    # AP-CEL-9 token bound: cap the number of detect_with_ai (LLM/detector) calls
    # per realm per run so one high-volume tenant can't balloon the nightly run.
    # When the cap is reached we LOG and stop the scrape — never silently truncate.
    detector_call_budget = settings.nightly_scrape_max_detector_calls
    detector_calls = 0

    for txn in txns:
        if txn.qbo_id in existing_txn_ids:
            continue

        # Optimization: Only consider lines mapped to known target accounts
        if target_account_ids and txn.account_ref not in target_account_ids:
            continue

        if detector_calls >= detector_call_budget:
            logger.warning(
                "GL scrape detector cap (%s) reached for %s (%s); stopping scrape "
                "this run — remaining candidate transactions will be re-evaluated "
                "next nightly run.",
                detector_call_budget,
                company_name,
                company_id,
            )
            break

        detector_calls += 1
        res = detect_with_ai(
            description=txn.description or "",
            vendor_name=txn.vendor_name or "",
            amount=float(txn.amount),
            txn_date=txn.date
        )
        
        # If detector logic confirms it (via rules or LLM)
        if res.schedule_type and res.confidence >= 0.65:
            schedule = AccountingSchedule(
                realm_id=company_id,
                schedule_type=res.schedule_type,
                name=res.suggested_name or f"Detected {res.schedule_type}",
                description=txn.description,
                vendor_name=txn.vendor_name,
                qbo_txn_id=txn.qbo_id,
                qbo_account_id=txn.account_ref,
                original_amount=txn.amount,
                remaining_amount=txn.amount,
                start_date=res.suggested_start_date or txn.date,
                useful_life_months=res.suggested_useful_life_months or 12,
                asset_class=res.suggested_asset_class,
                status="DETECTED",
                detection_confidence=res.confidence,
                ai_reasoning=res.reasoning
            )
            db.add(schedule)
            db.flush()
            
            action = ProposedAction(
                company_id=company_id,
                action_type="SCHEDULE_DETECTION",
                payload={
                    "schedule_id": schedule.id,
                    "schedule_type": res.schedule_type,
                    "qbo_txn_id": txn.qbo_id,
                    "vendor_name": txn.vendor_name,
                    "amount": float(txn.amount),
                    "suggested_life": res.suggested_useful_life_months
                },
                reasoning=f"Detected new {res.schedule_type}: {res.reasoning}",
                confidence_score=res.confidence,
                status="PENDING",
                source="ai_proactive",
                category="journal_entry",
                priority="medium"
            )
            db.add(action)
            
            # Also create a WatchdogAlert to surface this in the Dashboard Action Center
            from decimal import Decimal
            alert = WatchdogAlert(
                company_id=company_id,
                alert_type="NEW_SCHEDULE_DETECTION",
                severity="warning",
                title=f"New {res.schedule_type} Detected",
                description=f"Detected a new {res.schedule_type} for ${float(txn.amount):,.2f} from {txn.vendor_name or 'a vendor'}. Please provide additional details.",
                metric_value=Decimal(str(txn.amount)),
                context={"schedule_id": schedule.id, "qbo_txn_id": txn.qbo_id, "vendor": txn.vendor_name},
                period=period
            )
            db.add(alert)
            
            existing_txn_ids.add(txn.qbo_id)

    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog Alert Checks
# ─────────────────────────────────────────────────────────────────────────────

def create_watchdog_alert_if_new(db, *, company_id: str, alert_type: str, period: Optional[str], **kwargs) -> bool:
    """Create a WatchdogAlert unless it would duplicate or resurrect one.

    Suppressed when:
      a) an OPEN (unacknowledged) alert of the same type already exists for the
         company (pre-existing dedupe behavior), or
      b) the user acknowledged the same underlying condition recently
         (fingerprint match within DISMISSAL_STICKY_DAYS) and the condition has
         not materially worsened (see alert_fingerprints.is_materially_worse).

    Returns True if an alert was created.
    """
    from ..models import WatchdogAlert
    from ..services.alert_fingerprints import acknowledged_watchdog_blocks, watchdog_fingerprint

    existing_open = (
        db.query(WatchdogAlert)
        .filter(
            WatchdogAlert.company_id == company_id,
            WatchdogAlert.alert_type == alert_type,
            WatchdogAlert.acknowledged == False,  # noqa: E712
        )
        .first()
    )
    if existing_open:
        return False

    fingerprint = watchdog_fingerprint(
        company_id,
        alert_type,
        period,
        related_entity_type=kwargs.get("related_entity_type"),
        related_entity_id=kwargs.get("related_entity_id"),
        context=kwargs.get("context"),
    )
    metric_value = kwargs.get("metric_value")
    if acknowledged_watchdog_blocks(
        db,
        company_id=company_id,
        fingerprint=fingerprint,
        new_severity=kwargs.get("severity"),
        new_metric_value=float(metric_value) if metric_value is not None else None,
    ):
        logger.info(f"Watchdog alert {alert_type} suppressed for {company_id}: dismissed condition unchanged")
        return False

    db.add(WatchdogAlert(
        company_id=company_id,
        alert_type=alert_type,
        period=period,
        fingerprint=fingerprint,
        **kwargs,
    ))
    return True


def _run_watchdog_checks(db, company_id: str, company_name: str, period: str, qbo_client=None) -> int:
    """
    Run all watchdog checks and create WatchdogAlert records for new conditions.
    Returns count of new alerts created.
    """
    from ..models import WatchdogAlert, DebtCovenant, InsightKPI, InsightAnomaly  # noqa: F401

    today = date.today()  # noqa: F841
    new_alerts = 0

    def _create_alert_if_new(alert_type: str, **kwargs):
        """Create a WatchdogAlert unless it duplicates an open alert or
        resurrects a recently acknowledged, unchanged condition."""
        nonlocal new_alerts

        if create_watchdog_alert_if_new(
            db, company_id=company_id, alert_type=alert_type, period=period, **kwargs
        ):
            new_alerts += 1
            logger.info(f"Watchdog alert: {alert_type} for {company_name}")

    # Pre-fetch all KPIs for this period
    all_kpis = db.query(InsightKPI).filter(
        InsightKPI.company_id == company_id,
        InsightKPI.period == period,
    ).all()
    kpis_by_name = {k.name.lower(): k for k in all_kpis}
    
    def _find_kpi(pattern: str):
        pat = pattern.lower()
        for name, k in kpis_by_name.items():
            if pat in name:
                return k
            if "%" in pat:
                segments = [s for s in pat.split('%') if s]
                match = True
                curr_idx = 0
                for s in segments:
                    idx = name.find(s, curr_idx)
                    if idx == -1:
                        match = False
                        break
                    curr_idx = idx + len(s)
                if match:
                    return k
        return None

    # ── Check 1: Covenant Breaches ──────────────────────────────────────
    # DE-DUP (AC-2): DSCR + DEBT_EBITDA covenants get their COVENANT_BREACH from the two-lens
    # monthly snapshot task (app/tasks/covenant_snapshot.py — the correct TTM math on a
    # transition), NOT from a raw single-period KPI match here. This watchdog keeps ONLY the
    # non-two-lens metric types (MIN_CASH / LEVERAGE / CURRENT_RATIO / CUSTOM) so the two paths
    # never double-fire a COVENANT_BREACH for the same covenant+period.
    try:
        from ..services.covenant_compute import SUPPORTED_METRICS

        covenants = (
            db.query(DebtCovenant)
            .filter(DebtCovenant.company_id == company_id, DebtCovenant.active == True)  # noqa: E712
            .all()
        )

        for cov in covenants:
            # Skip the two-lens metrics — owned by the snapshot task (de-dup).
            if str(cov.metric_type or "").upper() in SUPPORTED_METRICS:
                continue
            # Look for a matching KPI value
            kpi = _find_kpi(cov.metric_type)

            if kpi and kpi.value is not None:
                threshold = float(cov.threshold)
                actual = float(kpi.value)
                breached = False

                if cov.breach_direction == "BELOW" and actual < threshold:
                    breached = True
                elif cov.breach_direction == "ABOVE" and actual > threshold:
                    breached = True

                if breached:
                    _create_alert_if_new(
                        "COVENANT_BREACH",
                        severity="critical",
                        title=f"Covenant Breach: {cov.name}",
                        description=f"{cov.loan_name} — {cov.name}: actual {actual:.2f} vs threshold {threshold:.2f}",
                        metric_value=Decimal(str(actual)),
                        threshold_value=cov.threshold,
                        metric_unit="score",  # a covenant ratio (DSCR/leverage/current-ratio), not a $ amount
                        related_entity_type="covenant",
                        related_entity_id=cov.id,
                        context={"loan_name": cov.loan_name, "metric_type": cov.metric_type},
                    )
    except Exception as e:
        logger.warning(f"Covenant check failed for {company_name}: {e}")

    # ── Check 2: Negative Cash Balance ──────────────────────────────────
    try:
        cash_kpi = _find_kpi("cash")

        if cash_kpi and cash_kpi.value is not None and float(cash_kpi.value) < 0:
            _create_alert_if_new(
                "NEGATIVE_CASH",
                severity="critical",
                title="Negative Cash Balance",
                description=f"Cash balance is ${float(cash_kpi.value):,.0f}. Immediate attention required.",
                metric_value=Decimal(str(cash_kpi.value)),
                threshold_value=Decimal("0"),
                metric_unit="currency",  # a $ cash balance
            )
    except Exception as e:
        logger.warning(f"Cash check failed for {company_name}: {e}")

    # ── Check 3: Low Runway ─────────────────────────────────────────────
    try:
        # Use revenue and expenses to estimate burn rate
        rev_kpi = _find_kpi("revenue")
        exp_kpi = _find_kpi("expense")

        if cash_kpi and rev_kpi and exp_kpi:
            cash_val = float(cash_kpi.value or 0)
            rev_val = float(rev_kpi.value or 0)
            exp_val = float(exp_kpi.value or 0)
            monthly_burn = exp_val - rev_val

            if monthly_burn > 0 and cash_val > 0:
                runway_months = cash_val / monthly_burn
                if runway_months < 3:
                    _create_alert_if_new(
                        "LOW_RUNWAY",
                        severity="warning" if runway_months >= 1.5 else "critical",
                        title=f"Low Cash Runway: {runway_months:.1f} months",
                        description=f"At current burn rate (${monthly_burn:,.0f}/mo), cash will be depleted in ~{runway_months:.1f} months.",
                        metric_value=Decimal(str(round(runway_months, 1))),
                        threshold_value=Decimal("3"),
                        metric_unit="score",  # runway in months — a bare number, not a $ amount
                        context={"monthly_burn": round(monthly_burn, 2), "cash_balance": round(cash_val, 2)},
                    )
    except Exception as e:
        logger.warning(f"Runway check failed for {company_name}: {e}")

    # ── Check 4: Transaction Alerts (Large & Duplicate) ─────────────────
    # (Removed AC-2: revenue-drop + AR-overdue-spike are NOT cash-condition watchdog checks —
    # revenue trend and AR aging live in their own surfaces; the watchdog is scoped to CASH +
    # covenant + runway + transaction-scan.)
    from ..orchestration.alerts_engine import scan_recent_transactions_for_alerts
    new_txn_alerts = scan_recent_transactions_for_alerts(db, company_id, company_name, period)
    new_alerts += new_txn_alerts

    # ── Check 5: Budget vs Actual — Revenue / EBITDA trailing budget ─────
    # Sources REVENUE_TRAILS_BUDGET / EBITDA_TRAILS_BUDGET from the REAL budget-vs-actual engine
    # (app/routers/budget.compute_revenue_ebitda_variance — COGS-inclusive, base-scenario-scoped),
    # replacing the retired "Deal LBO Budget" annual-run-rate BudgetLine hack (AC-3).
    new_alerts += _check_budget_vs_actual_trailing(db, company_id, period)

    db.commit()
    return new_alerts


def _check_budget_vs_actual_trailing(db, company_id: str, period: str) -> int:
    """Fire REVENUE_TRAILS_BUDGET / EBITDA_TRAILS_BUDGET when the period's real budget-vs-actual
    variance trails budget by more than the threshold. metric_value is the variance % (negative
    when trailing); metric_unit="percent". Sourced from the SAME math the /budget/vs-actuals
    page shows, so an alert reconciles to what the user sees (AC-3)."""
    from ..routers.budget import compute_revenue_ebitda_variance

    trailing_threshold_pct = -10.0  # trailing by more than 10% of budget
    new_alerts = 0
    try:
        year_str, month_str = period.split("-")
        budget_year = int(year_str)
        month_idx = int(month_str) - 1
    except (ValueError, IndexError):
        return 0
    from ..budget.constants import MONTHS
    if not (0 <= month_idx < len(MONTHS)):
        return 0
    month_key = MONTHS[month_idx]

    variance = compute_revenue_ebitda_variance(db, company_id, budget_year, month_key)
    if variance is None:
        return 0  # no base-scenario budget for the year, or QBO actuals unavailable — surface nothing

    def _emit(kind: str, label: str, lens) -> int:
        var_pct = lens.get("variance_pct")
        if var_pct is None or var_pct >= trailing_threshold_pct:
            return 0
        if _create_alert_if_new_standalone(
            db, company_id=company_id, period=period,
            alert_type=kind,
            severity="warning",
            title=f"{label} Trails Budget {abs(var_pct):.1f}%",
            description=(
                f"{label} actual (${lens['actual']:,.0f}) is trailing budget "
                f"(${lens['budget']:,.0f}) by {abs(var_pct):.1f}% "
                f"(${abs(lens['variance']):,.0f}) for {period}."
            ),
            metric_value=Decimal(str(var_pct)),
            metric_unit="percent",  # a variance % — NOT a $ amount
            context={"budget": lens["budget"], "actual": lens["actual"], "variance": lens["variance"]},
        ):
            return 1
        return 0

    new_alerts += _emit("REVENUE_TRAILS_BUDGET", "Revenue", variance["revenue"])
    new_alerts += _emit("EBITDA_TRAILS_BUDGET", "EBITDA", variance["ebitda"])
    return new_alerts


def _create_alert_if_new_standalone(db, *, company_id: str, period: str, alert_type: str, **kwargs) -> bool:
    """Thin wrapper over ``create_watchdog_alert_if_new`` for producers outside the closure in
    ``_run_watchdog_checks`` (dedupe + acknowledged-condition suppression preserved)."""
    return create_watchdog_alert_if_new(
        db, company_id=company_id, alert_type=alert_type, period=period, **kwargs
    )

# check_deal_variance REMOVED (AC-3): the "Deal LBO Budget" annual-run-rate variance (an
# InsightKPI ×12 annualization vs a summed BudgetLine annual figure) was replaced by
# _check_budget_vs_actual_trailing above, which sources REVENUE_TRAILS_BUDGET /
# EBITDA_TRAILS_BUDGET from the REAL COGS-inclusive budget-vs-actual math
# (app/routers/budget.compute_revenue_ebitda_variance), scenario-scoped to base.

def check_budget_variance_and_propose_updates(db, company_id, company_name, period, qbo_client):
    try:
        if qbo_client:
            from decimal import Decimal
            from ..models import BudgetLine, ProposedAction
            from ..analysis.vendor_spend import vendor_spend_monthly
            
            # 1. Fetch current budget lines for the year
            current_year = int(period.split("-")[0])
            budget_lines = db.query(BudgetLine).filter(
                BudgetLine.company_id == company_id,
                BudgetLine.budget_year == current_year,
                BudgetLine.vendor_name.isnot(None) # Only track vendor sublines
            ).all()

            if budget_lines:
                # 2. Get actual vendor spend for the period (last month)
                spend_data = vendor_spend_monthly(qbo_client, months=1)
                month_key = sorted(spend_data.get("months_analyzed", []))[-1] if spend_data.get("months_analyzed") else None
                
                if month_key:
                    # Convert 'YYYY-MM' back to 'jan', 'feb' etc to query the budget dict
                    from datetime import datetime
                    period_month_str = datetime.strptime(month_key, "%Y-%m").strftime("%b").lower()
                    actual_vendor_totals = {v["vendor"]: v["monthly"].get(month_key, 0) for v in spend_data.get("vendors", [])}

                    # 3. Compare actual to budget
                    for bl in budget_lines:
                        budgeted_amt = float(bl.monthly_amounts.get(period_month_str, 0) or 0)
                        if budgeted_amt > 0:
                            actual_amt = actual_vendor_totals.get(bl.vendor_name, 0)
                            variance = actual_amt - budgeted_amt
                            variance_pct = (variance / budgeted_amt) * 100

                            if variance_pct > 10 and variance >= 1000:
                                # Over budget > 10% AND material (>$1000)
                                alert_desc = f"Spent ${actual_amt:,.0f} on {bl.vendor_name} vs budgeted ${budgeted_amt:,.0f}."
                                # NOTE: this previously called the _create_alert_if_new
                                # closure (scoped inside _run_watchdog_checks) and
                                # silently NameError'd inside the try/except.
                                create_watchdog_alert_if_new(
                                    db,
                                    company_id=company_id,
                                    alert_type="BUDGET_OVERRUN",
                                    period=period,
                                    severity="warning" if variance_pct < 20 else "critical",
                                    title=f"Budget Overrun: {bl.vendor_name}",
                                    description=alert_desc,
                                    metric_value=Decimal(str(variance)),
                                    context={"vendor": bl.vendor_name, "expected": budgeted_amt, "actual": actual_amt}
                                )

                                # Generate PENDING Budget Update action to re-forecast automatically
                                # Check if a proposal already exists to avoid dupes
                                existing_action = db.query(ProposedAction).filter(
                                    ProposedAction.company_id == company_id,
                                    ProposedAction.action_type == "BUDGET_UPDATE",
                                    ProposedAction.status == "PENDING",
                                    ProposedAction.payload["vendor_name"].astext == bl.vendor_name,
                                    ProposedAction.payload["period"].astext == period
                                ).first()

                                if not existing_action:
                                    # Suggest bumping the budget for future months based on this new higher run-rate
                                    new_monthly_amounts = dict(bl.monthly_amounts)
                                    for future_month in ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]:
                                        if float(new_monthly_amounts.get(future_month, 0) or 0) == budgeted_amt:
                                            new_monthly_amounts[future_month] = round(actual_amt, 2)

                                    action = ProposedAction(
                                        company_id=company_id,
                                        action_type="BUDGET_UPDATE",
                                        payload={
                                            "budget_line_id": bl.id,
                                            "vendor_name": bl.vendor_name,
                                            "period": period,
                                            "proposed_amounts": new_monthly_amounts
                                        },
                                        reasoning=f"Automatic re-forecast: {bl.vendor_name} run-rate increased from ${budgeted_amt:,.0f}/mo to ${actual_amt:,.0f}/mo.",
                                        confidence_score=0.85,
                                        status="PENDING",
                                        source="ai_proactive",
                                        category="budget",
                                        priority="warning" if variance_pct < 20 else "critical"
                                    )
                                    db.add(action)

    except Exception as e:
        logger.warning(f"Budget variance check failed for {company_name}: {e}")

    db.commit()

from ..celery_app import celery_app  # noqa: E402

@celery_app.task(name="app.tasks.proactive.trigger_budget_reforecast")
def trigger_budget_reforecast(company_id: str, company_name: str, period: str):
    from ..db import SessionLocal
    from ..qbo_client import get_qbo_client_from_db
    
    db = SessionLocal()
    try:
        qbo_client = get_qbo_client_from_db(db, company_id)
        if qbo_client:
            check_budget_variance_and_propose_updates(db, company_id, company_name, period, qbo_client)
    except Exception as e:
        logger.error(f"Failed to run budget reforecast trigger: {e}")
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Predictive Cash Flow
# ─────────────────────────────────────────────────────────────────────────────

def _run_predictive_cash(db, company_id: str, qbo_client) -> Optional[Dict]:
    """
    Run predictive 13-week cash flow and create watchdog alerts if negative weeks found.
    """
    from ..analysis.predictive_cash import predict_13_week_cash
    from ..models import WatchdogAlert  # noqa: F401

    result = predict_13_week_cash(qbo_client, db=db, realm_id=company_id)

    # If any week shows negative cash, create a watchdog alert
    negative_weeks = result.get("summary", {}).get("negative_cash_weeks", [])
    if negative_weeks:
        lowest = result["summary"]["lowest_balance"]
        lowest_week = result["summary"]["lowest_balance_week"]

        today = date.today()
        created = create_watchdog_alert_if_new(
            db,
            company_id=company_id,
            alert_type="NEGATIVE_CASH_FORECAST",
            period=f"{today.year}-{today.month:02d}",
            severity="warning",
            title=f"Cash Forecast: Negative Balance in Week {negative_weeks[0]}",
            description=(
                f"Predictive cash forecast shows negative balance in "
                f"{len(negative_weeks)} week(s). Lowest point: ${lowest:,.0f} in week {lowest_week}."
            ),
            metric_value=Decimal(str(lowest)),
            threshold_value=Decimal("0"),
            context={
                "negative_weeks": negative_weeks,
                "runway_weeks": result["summary"].get("runway_weeks"),
            },
        )
        if created:
            db.commit()

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Executive Briefing
# ─────────────────────────────────────────────────────────────────────────────

def _generate_executive_briefing(db, company_id: str, company_name: str, period: str):
    """
    Generate a pre-computed executive briefing for the company.
    Aggregates KPIs, anomalies, and pending actions into a narrative summary.
    """
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-7): briefing LLM
    # calls record task_type="briefing" on the usage ledger — re-binding over
    # the nightly engine's ambient "watchdog"; a close-walk caller keeps its
    # "close" binding (so the close budget still governs its spend).
    from ..services.llm_task_context import bind_llm_task_context, current_llm_task_context

    ctx = current_llm_task_context()
    if ctx is None or ctx.task_type in ("watchdog", "other"):
        with bind_llm_task_context("briefing", realm_id=company_id):
            return _generate_executive_briefing_inner(db, company_id, company_name, period)
    return _generate_executive_briefing_inner(db, company_id, company_name, period)


def _generate_executive_briefing_inner(db, company_id: str, company_name: str, period: str):
    from ..models import (
        ExecutiveBriefing, InsightKPI, InsightAnomaly, ProposedAction, WatchdogAlert
    )

    today = date.today()
    organization, organization_company = _company_scope(db, company_id)

    # AC-A3 (plan 20260716-five-domain-remediation): the briefing applies the
    # SAME soft-close floor as the Alerts Center — otherwise the briefing
    # re-leaks the counts/key-risks the alerts view now hides. A floor read
    # error propagates: the briefing stage fails LOUD (recorded as a failed
    # stage) rather than briefing on closed-period rows.
    from ..services.task_autonomy import latest_locked_period

    anomaly_floor = latest_locked_period(db, realm_id=company_id, hard_only=False)

    # Gather financial position data
    kpis_rows = (
        db.query(InsightKPI)
        .filter(InsightKPI.company_id == company_id, InsightKPI.period == period)
        .all()
    )
    financial_position = {
        row.name: {
            "value": float(row.value or 0),
            "unit": row.unit,
            "delta": float(row.delta) if row.delta else None,
        }
        for row in kpis_rows
    }

    # Count pending actions and anomalies
    pending_count = (
        db.query(ProposedAction)
        .filter(ProposedAction.company_id == company_id, ProposedAction.status == "PENDING")
        .count()
    )
    anomaly_count_q = db.query(InsightAnomaly).filter(
        InsightAnomaly.company_id == company_id,
        InsightAnomaly.dismissed == False,  # noqa: E712
    )
    if anomaly_floor is not None:
        # AC-A3: soft-closed periods never count (YYYY-MM lexicographic).
        anomaly_count_q = anomaly_count_q.filter(InsightAnomaly.period > anomaly_floor)
    anomaly_count = anomaly_count_q.count()

    # Count active watchdog alerts
    watchdog_count = (
        db.query(WatchdogAlert)
        .filter(
            WatchdogAlert.company_id == company_id,
            WatchdogAlert.acknowledged == False,  # noqa: E712
        )
        .count()
    )

    # Gather risk indicators
    critical_anomalies_q = db.query(InsightAnomaly).filter(
        InsightAnomaly.company_id == company_id,
        InsightAnomaly.severity.in_(["critical", "warning"]),
        InsightAnomaly.dismissed == False,  # noqa: E712
    )
    if anomaly_floor is not None:
        # AC-A3: a closed-period anomaly is never a briefing key-risk.
        critical_anomalies_q = critical_anomalies_q.filter(InsightAnomaly.period > anomaly_floor)
    critical_anomalies = critical_anomalies_q.limit(5).all()

    # Add watchdog alerts to key risks
    active_watchdogs = (
        db.query(WatchdogAlert)
        .filter(
            WatchdogAlert.company_id == company_id,
            WatchdogAlert.acknowledged == False,  # noqa: E712
        )
        .order_by(WatchdogAlert.severity.desc())
        .limit(5)
        .all()
    )

    key_risks = [
        {"type": a.anomaly_type, "description": a.description, "severity": a.severity}
        for a in critical_anomalies
    ]
    for wd in active_watchdogs:
        key_risks.append({
            "type": wd.alert_type,
            "description": wd.description,
            "severity": wd.severity,
        })

    # Build narrative summary
    revenue = financial_position.get("revenue", {}).get("value", 0)
    net_income = financial_position.get("net_income", {}).get("value", 0)
    cash = financial_position.get("cash", {}).get("value", 0)

    narrative_parts = [
        f"Executive Briefing for {company_name} — {period}:",
        f"Revenue: ${revenue:,.0f} | Net Income: ${net_income:,.0f} | Cash: ${cash:,.0f}",
    ]
    if key_risks:
        narrative_parts.append(f"Key risks: {len(key_risks)} active alerts.")
        for risk in key_risks[:3]:
            narrative_parts.append(f"  - [{risk['severity'].upper()}] {risk['description']}")
    if watchdog_count > 0:
        narrative_parts.append(f"{watchdog_count} watchdog alert(s) require attention.")
    if pending_count > 0:
        narrative_parts.append(f"{pending_count} actions awaiting approval.")

    narrative = "\n".join(narrative_parts)

    # Upsert the briefing
    existing = (
        db.query(ExecutiveBriefing)
        .filter(
            ExecutiveBriefing.company_id == company_id,
            ExecutiveBriefing.briefing_date == today,
        )
    )
    if organization:
        existing = existing.filter(
            (ExecutiveBriefing.organization_id == organization.id) | (ExecutiveBriefing.organization_id.is_(None))
        )
        if organization_company:
            existing = existing.filter(
                (ExecutiveBriefing.organization_company_id == organization_company.id)
                | (ExecutiveBriefing.organization_company_id.is_(None))
            )
    else:
        existing = existing.filter(ExecutiveBriefing.organization_id.is_(None))
    existing = existing.first()

    if existing:
        if organization:
            existing.organization_id = organization.id
            if organization_company:
                existing.organization_company_id = organization_company.id
        existing.financial_position = financial_position
        existing.key_risks = key_risks
        existing.pending_actions_count = pending_count
        existing.open_anomalies_count = anomaly_count
        existing.narrative = narrative
        existing.data_freshness_at = datetime.utcnow()
    else:
        briefing = ExecutiveBriefing(
            company_id=company_id,
            organization_id=organization.id if organization else None,
            organization_company_id=organization_company.id if organization_company else None,
            briefing_date=today,
            financial_position=financial_position,
            key_risks=key_risks,
            pending_actions_count=pending_count,
            open_anomalies_count=anomaly_count,
            narrative=narrative,
            data_freshness_at=datetime.utcnow(),
        )
        db.add(briefing)

    db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Morning Briefing Email
# ─────────────────────────────────────────────────────────────────────────────

def _send_briefing_email(db, company_id: str, company_name: str) -> dict:
    """Send the morning briefing email for the latest briefing."""
    from ..models import ExecutiveBriefing, QBOCompanyInfo  # noqa: F401
    from ..services.email_service import send_morning_briefing

    today = date.today()
    organization, organization_company = _company_scope(db, company_id)
    briefing_query = (
        db.query(ExecutiveBriefing)
        .filter(
            ExecutiveBriefing.company_id == company_id,
            ExecutiveBriefing.briefing_date == today,
        )
    )
    if organization:
        briefing_query = briefing_query.filter(
            (ExecutiveBriefing.organization_id == organization.id) | (ExecutiveBriefing.organization_id.is_(None))
        )
        if organization_company:
            briefing_query = briefing_query.filter(
                (ExecutiveBriefing.organization_company_id == organization_company.id)
                | (ExecutiveBriefing.organization_company_id.is_(None))
            )
    else:
        briefing_query = briefing_query.filter(ExecutiveBriefing.organization_id.is_(None))
    briefing = briefing_query.first()

    if not briefing:
        logger.debug(f"No briefing found for {company_id} on {today}")
        return {"status": "unavailable", "reason": "briefing_missing"}

    try:
        return send_morning_briefing(db, company_id, company_name, briefing)
    except Exception as e:
        logger.error(f"Failed to send briefing email for {company_name}: {e}")
        return {"status": "uncertain", "reason": "delivery_unavailable"}


# ─────────────────────────────────────────────────────────────────────────────
# Per-company nightly-intelligence unit (RLS tenant-scoped)
# ─────────────────────────────────────────────────────────────────────────────

def _run_company_intelligence(db, company_id: str, company_name: str, period: str) -> dict:
    """Run the full nightly intelligence pipeline for ONE company.

    Re-pins the per-transaction RLS GUC to this company's realm (the worker has
    no request middleware); no-op on SQLite.
    """
    from ..qbo_client import get_qbo_client_from_db
    from ..orchestration.pipeline import run_analysis_pipeline
    from ..services.tenant_companies import bind_tenant_tag_for_realm

    # PLAN-EXEMPT: pilot LLM-usage attribution (AC-4.1) — bind org+company+realm so every
    # lane LLM call fired inside the nightly per-company pipeline is attributed (was NULL-realm).
    with tenant_scope_for_realm(db, company_id), bind_tenant_tag_for_realm(db, company_id):
        qbo = None
        try:
            qbo = get_qbo_client_from_db(db, company_id)
        except Exception as e:
            logger.warning(f"Could not init QBO client for {company_name}: {e}")

        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B2 — INV-HONEST-SCHED (SYNTHESIS
        # GAP-B1): this wrapper previously swallowed every stage failure into logs and returned
        # {"status": "ok", "anomalies": 0} even when ALL six stages raised — a night of silent
        # non-coverage indistinguishable from a clean night. Each stage now records its failure;
        # a non-empty failure set returns status="partial" with the failed stage names and
        # raises one deduped NIGHTLY_STAGE_FAILURE watchdog alert so an operator sees it.
        failed_stages: list[str] = []

        # 1. Run the analysis pipeline (anomaly detection + KPI computation)
        try:
            pipeline_result = run_analysis_pipeline(db, company_id, period)
            if pipeline_result.get("status") in ("partial", "failed"):
                failed_stages.extend("analysis_pipeline." + name for name in pipeline_result.get("failed_stages", ["incomplete"]))
        except Exception as e:
            logger.error("Analysis pipeline failed for %s: %s", company_name, redact_to_string(e))
            failed_stages.append("analysis_pipeline")
            pipeline_result = {}

        # 2. Retire legacy non-executable ALERT_ESCALATION proposals (idempotent
        #    sweep; creation removed 2026-07-01 — critical anomalies surface via
        #    the Alerts Center + WatchdogAlerts, never the write-approval queue).
        try:
            sweep_alert_escalation_proposals(db, company_id=company_id)
        except Exception as e:
            logger.error("ALERT_ESCALATION sweep failed for %s: %s", company_name, redact_to_string(e))
            failed_stages.append("alert_escalation_sweep")

        # 2.5 Scrape General Ledger for undiscovered Prepaid/Asset schedules
        try:
            _scrape_gl_for_new_schedules(db, company_id, company_name, period)
        except Exception as e:
            logger.error("GL schedule scrape failed for %s: %s", company_name, redact_to_string(e))
            failed_stages.append("gl_schedule_scrape")

        # 2.6 Shared analytical-review checks (Phase C, C3) — runs the SAME
        # registered Check library the Phase B close driver invokes (run_check),
        # so the nightly run and the close driver share one check set. SUPERSEDE:
        # the `missing_accrual` Check (review_checks/builtin_checks.py) wraps the
        # exact `_detect_missing_accruals` detector the inline `_propose_missing_accruals`
        # used, so the inline call is REPLACED here (no double-fire). Proposal-first
        # — surfaces approve/dismiss ProposedAction(s), NEVER a GL write.
        try:
            _run_shared_review_checks(db, company_id, company_name, period, qbo)
        except Exception as e:
            logger.error("Shared review checks failed for %s: %s", company_name, redact_to_string(e))
            failed_stages.append("shared_review_checks")

        # 3. Run watchdog alert checks (now includes budget variance)
        watchdog_count = 0
        try:
            watchdog_count = _run_watchdog_checks(db, company_id, company_name, period, qbo)
        except Exception as e:
            logger.error("Watchdog failed for %s: %s", company_name, redact_to_string(e))
            failed_stages.append("watchdog_checks")

        # 4. Run predictive cash forecast (needs QBO client)
        predictive_result = None
        try:
            if qbo:
                predictive_result = _run_predictive_cash(db, company_id, qbo)
        except Exception as pred_err:
            logger.warning("Predictive cash failed for %s: %s", company_name, redact_to_string(pred_err))
            failed_stages.append("predictive_cash")

        # 5. Generate executive briefing
        try:
            _generate_executive_briefing(db, company_id, company_name, period)
        except Exception as e:
            logger.error("Briefing generation failed for %s: %s", company_name, redact_to_string(e))
            failed_stages.append("executive_briefing")

        # 6. Send morning briefing email
        briefing_delivery = {"status": "unavailable"}
        briefing_sent = False
        try:
            briefing_delivery = _send_briefing_email(db, company_id, company_name)
            briefing_sent = isinstance(briefing_delivery, dict) and briefing_delivery.get("status") in ("sent", "already_sent")
            if isinstance(briefing_delivery, dict) and briefing_delivery.get("status") in ("partial", "failed", "retryable", "uncertain", "unavailable", "sending", "ready"):
                failed_stages.append("briefing_email")
        except Exception as e:
            logger.error(
                "Email failed for %s: %s",
                company_name,
                redact_to_string(str(e), max_length=500),
            )
            failed_stages.append("briefing_email")

        if failed_stages:
            # One deduped operator-visible alert per condition (create_watchdog_alert_if_new
            # collapses repeats while an alert for the same failure set is open/undismissed).
            try:
                create_watchdog_alert_if_new(
                    db,
                    company_id=company_id,
                    alert_type="NIGHTLY_STAGE_FAILURE",
                    period=period,
                    severity="warning",
                    title="Nightly intelligence run was incomplete",
                    description=(
                        "These nightly stages failed and their coverage did NOT run: "
                        + ", ".join(failed_stages)
                        + ". Findings for this night may be incomplete — see logs."
                    ),
                    context={"failed_stages": failed_stages},
                )
                # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B2 — the helper only
                # db.add()s; the caller owns the commit (mirrors _run_watchdog_checks).
                db.commit()
            except Exception as alert_err:
                logger.error(
                    "NIGHTLY_STAGE_FAILURE alert write failed for %s: %s",
                    company_name, redact_to_string(alert_err),
                )
        return {
            "company_id": company_id,
            "company_name": company_name,
            "status": "partial" if failed_stages else "ok",
            "failed_stages": failed_stages,
            "anomalies": pipeline_result.get("total_anomalies_written", 0),
            "watchdog_alerts": watchdog_count,
            "briefing_emailed": briefing_sent,
            "briefing_delivery": briefing_delivery,
            "predictive_cash": "ok" if predictive_result else "skipped",
        }

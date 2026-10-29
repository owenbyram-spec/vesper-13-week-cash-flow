# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns app/services/review_checks/** (plan.md Appendix A); the active-plan.json surface_scope entry carries an inline comment inside the glob string so the gate matcher misses it — scope is ratified, the state string is malformed.
"""recon_cadence — risk-based balance-sheet reconciliation cadence policy.

Chunk C2C-CLOSE (plan 20260706-cfo-grade-financial-workflows, AC-2C.2 build-new;
RD-3 Topic 2 + month-end-close SKILL §2.2). The practitioner standard
(Trintech [4][5], JofA [6] — practitioner-methodology, NOT a binding mandate):
substantiate every material BS balance to independent support on a RISK-BASED
cadence — high-risk accounts (cash, high-volume, judgment-heavy, prior-period
failures; typically ~10% of accounts) MONTHLY; medium-risk QUARTERLY; low-risk
ANNUALLY, with auto-certify rules for low-risk zero-activity accounts.
Uniform-monthly-everything fails by DILUTION: reviewer attention is the scarce
resource, and spreading it uniformly gives the high-risk accounts the same
shallow pass as the trivial ones.

This module is the POLICY layer: pure risk-rating + cadence + due-in-period
functions plus the close-task generator that drives task generation
(close_tasks.py /generate). It creates HUMAN checklist tasks only — it never
posts, completes, or locks anything (the reconciliation itself stays a human
control; the agent lane keeps zero lock authority).

NAMED GAPS (refuse over fabricate):
  - Quarter/year boundaries default to the CALENDAR year. Fiscal year is
    company data (QBO CompanyInfo FiscalYearStartMonth —
    financial-modeling-correctness §8); a non-calendar-FY tenant's quarterly/
    annual cadence anchors are a follow-on once the fiscal start is plumbed in.
  - Auto-certification requires per-account ACTIVITY data the local cache does
    not reliably carry, so ``auto_certify_eligible`` returns False when
    activity is unknown — a certification is never fabricated from blindness.
  - Prior-period reconciliation FAILURES (a risk-raiser per Trintech) are not
    yet tracked per BS account; the rating below is keyword/type-based. Named,
    not silently omitted.
"""

from __future__ import annotations

import logging
from enum import Enum

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TASK_TYPE_BS_RECONCILIATION = "BS_RECONCILIATION"


class ReconRisk(str, Enum):
    HIGH = "high"  # monthly — cash-adjacent, high-volume, judgment-heavy
    MEDIUM = "medium"  # quarterly
    LOW = "low"  # annually


class ReconCadence(str, Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


# risk → cadence (Trintech [4][5]: high monthly / medium quarterly / low annual).
CADENCE_BY_RISK: dict[ReconRisk, ReconCadence] = {
    ReconRisk.HIGH: ReconCadence.MONTHLY,
    ReconRisk.MEDIUM: ReconCadence.QUARTERLY,
    ReconRisk.LOW: ReconCadence.ANNUAL,
}

# Keyword rules for the risk rating (RD-3 Topic 2(1): high-risk = cash,
# high-volume, judgment-heavy). Cash/bank accounts are ALSO owned by the bank-rec
# session flow; they still rate HIGH here so a bank account outside a rec session
# is not silently unrated.
_HIGH_RISK_KEYWORDS = (
    "cash",
    "bank",
    "checking",
    "savings",
    "undeposited",
    "accounts receivable",
    "accounts payable",
    "accrued",
    "accrual",
    "allowance",
    "reserve",
    "deferred",
    "suspense",
    "clearing",
    "intercompany",
    "payroll liab",
)
_MEDIUM_RISK_KEYWORDS = (
    "prepaid",
    "fixed asset",
    "accumulated depreciation",
    "accumulated amortization",
    "intangible",
    "deposit",
    "loan",
    "note payable",
    "debt",
)

# schedule_type → risk (judgment-heavy estimate schedules rate HIGH; mechanical
# amortization schedules rate MEDIUM — their monthly movement is deterministic).
_SCHEDULE_TYPE_RISK: dict[str, ReconRisk] = {
    "ACCRUAL": ReconRisk.HIGH,
    "DEFERRED_REVENUE": ReconRisk.HIGH,
    "PREPAID": ReconRisk.MEDIUM,
    "FIXED_ASSET": ReconRisk.MEDIUM,
    "INTANGIBLE": ReconRisk.MEDIUM,
    "DEPOSIT": ReconRisk.MEDIUM,
    "DEBT_PRINCIPAL": ReconRisk.MEDIUM,
    "GENERIC": ReconRisk.MEDIUM,
}


def rate_account_risk(account_name: str, schedule_type: str | None = None) -> ReconRisk:
    """Rate one BS account's reconciliation risk (pure; keyword/type rules).

    Precedence: an explicit judgment-heavy keyword outranks the schedule-type
    default (an "Accrued Bonus Reserve" on a GENERIC schedule is HIGH, not
    MEDIUM). Unmatched accounts rate LOW (annual floor — every account still
    gets reconciled at least yearly; nothing is exempt).
    """
    lowered = (account_name or "").lower()
    if any(keyword in lowered for keyword in _HIGH_RISK_KEYWORDS):
        return ReconRisk.HIGH
    if schedule_type and schedule_type.upper() in _SCHEDULE_TYPE_RISK:
        type_risk = _SCHEDULE_TYPE_RISK[schedule_type.upper()]
        if type_risk is ReconRisk.HIGH:
            return ReconRisk.HIGH
        if any(keyword in lowered for keyword in _MEDIUM_RISK_KEYWORDS):
            return ReconRisk.MEDIUM
        return type_risk
    if any(keyword in lowered for keyword in _MEDIUM_RISK_KEYWORDS):
        return ReconRisk.MEDIUM
    return ReconRisk.LOW


def recon_due_in_period(period: str, cadence: ReconCadence) -> bool:
    """Is a reconciliation of this cadence due in ``period`` ("YYYY-MM")?

    CALENDAR anchors (named gap: fiscal-year start is company data; the
    calendar default is documented, not silently assumed correct for all):
    monthly → every period; quarterly → calendar quarter ends (3/6/9/12);
    annual → calendar year end (12). Malformed periods return False (no task
    is generated from garbage — the checklist generator validates period
    upstream).
    """
    try:
        month = int(period.split("-", 1)[1])
    except (ValueError, IndexError, TypeError):
        return False
    if cadence is ReconCadence.MONTHLY:
        return True
    if cadence is ReconCadence.QUARTERLY:
        return month in (3, 6, 9, 12)
    return month == 12


def auto_certify_eligible(risk: ReconRisk, had_activity: bool | None) -> bool:
    """Auto-certify rule (Trintech [5]): LOW-risk + PROVABLY zero activity only.

    ``had_activity is None`` (activity unknown) → False. A certification is
    never fabricated from blindness — refuse over fabricate.
    """
    return risk is ReconRisk.LOW and had_activity is False


def reconciliation_task_defs(db: Session, realm_id: str, period: str) -> list[dict]:
    """Policy-driven BS-reconciliation task definitions due for ``period``.

    Account universe (what the local DB can substantiate without a live QBO
    call at generate time): the AR/AP control accounts (always present — the
    sub-ledger tie-out's human counterpart) plus every distinct BS account
    carried by a covering AccountingSchedule for the realm. Each account is
    risk-rated, mapped to its cadence, and emitted ONLY when due this period.
    Returns task dicts in the STANDARD_TASKS shape (the /generate endpoint
    turns them into CloseTask rows). Read-only on the DB.
    """
    accounts: dict[str, tuple[str, str | None]] = {
        # label → (label, schedule_type). AR/AP controls are structural HIGHs.
        "Accounts Receivable": ("Accounts Receivable", None),
        "Accounts Payable": ("Accounts Payable", None),
    }
    try:
        from ...models import AccountingSchedule

        rows = (
            db.query(
                AccountingSchedule.qbo_account_name,
                AccountingSchedule.schedule_type,
            )
            .filter(
                AccountingSchedule.realm_id == realm_id,
                AccountingSchedule.status.in_(["ACTIVE", "FULLY_AMORTIZED"]),
            )
            .distinct()
            .all()
        )
        for name, schedule_type in rows:
            label = str(name or "").strip()
            if label and label not in accounts:
                accounts[label] = (label, schedule_type)
    except Exception as e:  # noqa: BLE001 — generation degrades to the structural AR/AP pair, never crashes /generate
        logger.warning(
            "[recon_cadence] schedule account read failed for realm=%s: %s",
            realm_id,
            type(e).__name__,
        )

    task_defs: list[dict] = []
    sort_order = 24  # cluster with the reconciliation block (bank rec = 20)
    for label, (name, schedule_type) in sorted(accounts.items()):
        risk = rate_account_risk(name, schedule_type)
        cadence = CADENCE_BY_RISK[risk]
        if not recon_due_in_period(period, cadence):
            continue
        sort_order += 1
        task_defs.append(
            {
                "task_type": TASK_TYPE_BS_RECONCILIATION,
                "title": f"Reconcile {name}",
                "description": (
                    f"Substantiate the {name} balance to INDEPENDENT support as of period end "
                    f"(risk: {risk.value} → {cadence.value} cadence; risk-based reconciliation "
                    "policy — practitioner-methodology, Trintech/JofA per RD-3). An unexplained "
                    "difference blocks completion; never plug it."
                ),
                "sort_order": sort_order,
                "priority": "high" if risk is ReconRisk.HIGH else "normal",
            }
        )
    return task_defs


# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
def generate_reconciliation_close_tasks(
    db: Session,
    realm_id: str,
    period: str,
    *,
    due_date=None,
    skip_titles: set[str] | None = None,
) -> list[str]:
    """Create the cadence-due BS-reconciliation ``CloseTask`` rows (service layer).

    The ONE service call the /close-tasks/generate router makes for this policy
    (thin-router discipline). OWNS its transaction: adds the cadence rows and
    commits them (an additive second commit after the endpoint's checklist
    commit — the mutation lives in the service layer, AP-LAYER-02).
    ``skip_titles`` lets a template that already carries a reconciliation task
    win (no duplicate). Returns the created titles. These are HUMAN checklist
    tasks: nothing here posts, completes, or locks.
    """
    # PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
    from ...models import CloseTask

    created: list[str] = []
    skip = skip_titles or set()
    for task_def in reconciliation_task_defs(db, realm_id, period):
        if task_def["title"] in skip:
            continue
        task = CloseTask(
            realm_id=realm_id,
            period=period,
            task_type=task_def["task_type"],
            title=task_def["title"],
            description=task_def["description"],
            sort_order=task_def["sort_order"],
            is_auto_generated=True,
        )
        if hasattr(task, "priority"):
            task.priority = task_def.get("priority") or "normal"  # SA-OK: dict access on a policy task-def dict, not a DB query
        if hasattr(task, "due_date") and due_date is not None:
            task.due_date = due_date
        db.add(task)
        created.append(task_def["title"])
    if created:
        db.commit()
    return created

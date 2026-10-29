"""
lease_engine.py — the ASC 842 lease measurement engine (plan
20260702-phase-c-asc842, AC-2). Leases get their OWN engine — effective-
interest math must not contort the straight-line schedule_engine substrate —
but emit into the SAME rails (JOURNAL_ENTRY ProposedActions, the Wave-2 beat,
automation_policy, period locks, tie-out, coverage).

Measurement conventions (all money math is Decimal end-to-end; every stored
figure is quantized to cents ROUND_HALF_UP):

  * MONTHLY RATE = discount_rate_annual / 12 (the documented convention —
    a nominal-annual-compounded-monthly reading of the rate, the standard
    lease-software convention; NOT (1+r)^(1/12)−1).
  * LIABILITY at commencement = PV of the fixed payments (including stepped
    escalations) at the locked rate: arrears discounts month t by (1+i)^-t,
    advance by (1+i)^-(t-1) (the first advance payment is undiscounted).
  * ROU = liability + initial direct costs + prepaid − incentives.
  * Interest_t = beginning liability × monthly rate (arrears) or
    (beginning liability − payment) × monthly rate (advance — the payment
    reduces the balance BEFORE interest accrues).
  * OPERATING: one straight-line lease cost per month =
    (Σ payments + IDC + prepaid − incentives) ÷ term  (ASC 842-20-25-6a:
    the single lease cost allocates IDC/incentives too — with zero
    IDC/prepaid/incentives this is exactly Σ payments ÷ term);
    ROU amortization_t = expense_t − interest_t (the plug).
  * FINANCE: ROU amortization = ROU ÷ term straight-line; interest is
    presented separately; row expense = interest + amortization.
  * ENGINE-OWNED FINAL-ROW PLUGS: the final row's interest is plugged to
    payment − beginning liability (zeroing the liability to the cent), the
    final ROU amortization is plugged to the beginning ROU (zeroing the ROU
    to the cent), and the final operating expense = interest + amortization
    so every row's JE balances AND Σ(expense) equals the expense base
    exactly. Each plug is asserted within PLUG_TOLERANCE_PER_MONTH × term
    of its unplugged value — beyond that the schedule is wrong, not rounded.
  * SHORT-TERM EXEMPT leases (term ≤ 12 months, ASC 842-20-25-2 election —
    binding US GAAP) generate NO rows: no liability, no ROU, rent expensed
    as incurred outside Vesper Bot (documented).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")
# A final-row plug absorbs at most half a cent of rounding drift per month;
# anything beyond term × this bound means the schedule math is broken.
PLUG_TOLERANCE_PER_MONTH = Decimal("0.01")

CLASSIFICATION_OPERATING = "operating"
CLASSIFICATION_FINANCE = "finance"
CLASSIFICATIONS = (CLASSIFICATION_OPERATING, CLASSIFICATION_FINANCE)

TIMING_ADVANCE = "advance"
TIMING_ARREARS = "arrears"
PAYMENT_TIMINGS = (TIMING_ADVANCE, TIMING_ARREARS)

RATE_BASIS_IBR = "ibr"
RATE_BASIS_RISK_FREE = "risk_free"
RATE_BASES = (RATE_BASIS_IBR, RATE_BASIS_RISK_FREE)

# The five ASC 842-10-25-2 finance-classification criteria. ANY true answer
# forces finance classification (router-enforced); answers stored for audit.
CLASSIFICATION_CRITERIA_KEYS = (
    "transfers_ownership",
    "purchase_option_reasonably_certain",
    "term_major_part_of_life",
    "pv_substantially_all_fair_value",
    "specialized_asset",
)

SHORT_TERM_MAX_MONTHS = 12  # ASC 842 glossary: short-term lease ≤ 12 months

# The payment-only reassessments a remeasurement may book at the EXISTING
# discount rate (code → cause). Every other payment change, and every term
# change, is remeasured at the rate determined at the remeasurement date.
# Binding codification: ASC 842-10-25-8 / 25-11 (a modification not accounted
# for as a separate contract) and ASC 842-20-35-5 (lease-liability
# remeasurement); this split is this implementation's interpretation of them,
# not codification text.
RATE_UNCHANGED_REASONS = {
    "residual_value_guarantee": (
        "a change in the amount probable of being owed under a residual value guarantee"
    ),
    "contingency_resolution": "the resolution of a contingency on variable lease payments",
}


class LeaseEngineError(ValueError):
    """A lease schedule failed its own invariants — never emit the rows."""


def _cents(x: Decimal) -> Decimal:
    return x.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def monthly_rate(discount_rate_annual: Decimal) -> Decimal:
    """annual / 12, exact Decimal (the documented convention — see module
    docstring). 6% annual → 0.005 monthly, exactly."""
    return Decimal(str(discount_rate_annual)) / Decimal(12)


def payment_schedule(
    *,
    payment_amount: Decimal,
    term_months: int,
    escalation_steps: list[dict] | None,
) -> list[Decimal]:
    """The contractual payment for months 1..term (1-based), applying fixed
    stepped escalations ``[{"start_month": 13, "payment": "1100.00"}, ...]``.
    Amounts are read as exact Decimals (strings or numbers)."""
    if term_months < 1:
        raise LeaseEngineError("term_months must be >= 1")
    payments = [Decimal(str(payment_amount))] * term_months
    for step in sorted(escalation_steps or [], key=lambda s: int(s["start_month"])):
        start = int(step["start_month"])
        amount = Decimal(str(step["payment"]))
        if start < 1 or start > term_months:
            raise LeaseEngineError(
                f"escalation start_month {start} outside 1..{term_months}"
            )
        if amount < 0:
            raise LeaseEngineError("escalation payment must be >= 0")
        for t in range(start - 1, term_months):
            payments[t] = amount
    return payments


def present_value(
    payments: list[Decimal],
    discount_rate_annual: Decimal,
    payment_timing: str,
) -> Decimal:
    """PV of the payment stream at the monthly rate (= annual/12), quantized
    to cents. Arrears: Σ p_t (1+i)^-t; advance: Σ p_t (1+i)^-(t-1)."""
    if payment_timing not in PAYMENT_TIMINGS:
        raise LeaseEngineError(f"payment_timing must be one of {PAYMENT_TIMINGS}")
    i = monthly_rate(discount_rate_annual)
    v = Decimal(1) / (Decimal(1) + i)
    total = Decimal(0)
    for t, p in enumerate(payments, start=1):
        exponent = t if payment_timing == TIMING_ARREARS else t - 1
        total += Decimal(str(p)) * (v ** exponent)
    return _cents(total)


def initial_rou_amount(
    liability: Decimal,
    *,
    initial_direct_costs: Decimal = Decimal(0),
    prepaid_amount: Decimal = Decimal(0),
    incentives: Decimal = Decimal(0),
) -> Decimal:
    """ROU = liability + IDC + prepaid − incentives (ASC 842-20-30-5)."""
    rou = (
        Decimal(str(liability))
        + Decimal(str(initial_direct_costs))
        + Decimal(str(prepaid_amount))
        - Decimal(str(incentives))
    )
    if rou < 0:
        raise LeaseEngineError("initial ROU is negative — check incentives vs liability")
    return _cents(rou)


def generate_rows(
    *,
    payments: list[Decimal],
    discount_rate_annual: Decimal,
    payment_timing: str,
    classification: str,
    liability: Decimal,
    rou: Decimal,
) -> list[dict]:
    """Amortize ``liability``/``rou`` over ``payments`` (month 1..n). Returns
    row dicts (Decimal, cents): payment, interest, liability_reduction,
    ending_liability, rou_amortization, ending_rou, expense.

    Engine-owned invariants (asserted; LeaseEngineError on violation):
      * final ending_liability == 0.00 and ending_rou == 0.00 exactly, via
        the documented final-row plugs, each within
        PLUG_TOLERANCE_PER_MONTH × n of its unplugged value;
      * Σ(expense) == Σ(payments) + (rou − liability) exactly — with zero
        IDC/prepaid/incentives that is Σ(operating expense) == Σ(payments).
    """
    if classification not in CLASSIFICATIONS:
        raise LeaseEngineError(f"classification must be one of {CLASSIFICATIONS}")
    if payment_timing not in PAYMENT_TIMINGS:
        raise LeaseEngineError(f"payment_timing must be one of {PAYMENT_TIMINGS}")
    n = len(payments)
    if n < 1:
        raise LeaseEngineError("cannot generate rows for an empty payment stream")

    i = monthly_rate(discount_rate_annual)
    liability = _cents(Decimal(str(liability)))
    rou = _cents(Decimal(str(rou)))
    payments = [Decimal(str(p)) for p in payments]

    # Expense base: Σ payments + (ROU − liability). The (rou − liability)
    # spread IS (IDC + prepaid − incentives) at commencement, and carries the
    # same role for post-remeasurement regeneration.
    expense_base = sum(payments, Decimal(0)) + (rou - liability)
    sl_expense = _cents(expense_base / Decimal(n))
    fin_amort = _cents(rou / Decimal(n))
    plug_tolerance = PLUG_TOLERANCE_PER_MONTH * Decimal(n)

    rows: list[dict] = []
    beg_l, beg_r = liability, rou
    for t, payment in enumerate(payments, start=1):
        final = t == n
        if final:
            # FINAL-ROW PLUG (liability): interest = payment − beginning
            # liability, so ending liability is exactly 0.00.
            interest = _cents(payment - beg_l)
            unplugged = _cents(
                (beg_l if payment_timing == TIMING_ARREARS else beg_l - payment) * i
            )
            if (interest - unplugged).copy_abs() > plug_tolerance:
                raise LeaseEngineError(
                    f"final interest plug {interest} deviates from computed "
                    f"{unplugged} beyond tolerance {plug_tolerance}"
                )
        elif payment_timing == TIMING_ARREARS:
            interest = _cents(beg_l * i)
        else:  # advance: the payment reduces the balance before interest accrues
            interest = _cents((beg_l - payment) * i)
        reduction = _cents(payment - interest)
        end_l = _cents(beg_l - reduction)

        if classification == CLASSIFICATION_OPERATING:
            if final:
                # FINAL-ROW PLUGS (ROU + expense): amortization zeroes the
                # ROU; expense = interest + amortization keeps the row's JE
                # balanced and lands Σ(expense) on the base exactly.
                amort = beg_r
                expense = _cents(interest + amort)
                unplugged_amort = _cents(sl_expense - interest)
                if (amort - unplugged_amort).copy_abs() > plug_tolerance:
                    raise LeaseEngineError(
                        f"final ROU plug {amort} deviates from computed "
                        f"{unplugged_amort} beyond tolerance {plug_tolerance}"
                    )
            else:
                expense = sl_expense
                amort = _cents(expense - interest)
        else:  # finance
            amort = beg_r if final else fin_amort
            if final and (amort - fin_amort).copy_abs() > plug_tolerance:
                raise LeaseEngineError(
                    f"final finance ROU plug {amort} deviates from {fin_amort} "
                    f"beyond tolerance {plug_tolerance}"
                )
            expense = _cents(interest + amort)
        end_r = _cents(beg_r - amort)

        rows.append(
            {
                "month_index": t,
                "payment": _cents(payment),
                "interest": interest,
                "liability_reduction": reduction,
                "ending_liability": end_l,
                "rou_amortization": amort,
                "ending_rou": end_r,
                "expense": expense,
            }
        )
        beg_l, beg_r = end_l, end_r

    # Invariants — engine-owned, asserted every generation.
    if rows[-1]["ending_liability"] != Decimal("0.00"):
        raise LeaseEngineError(
            f"final ending_liability {rows[-1]['ending_liability']} != 0.00"
        )
    if rows[-1]["ending_rou"] != Decimal("0.00"):
        raise LeaseEngineError(f"final ending_rou {rows[-1]['ending_rou']} != 0.00")
    total_expense = sum((r["expense"] for r in rows), Decimal(0))
    if total_expense != _cents(expense_base):
        raise LeaseEngineError(
            f"Σ expense {total_expense} != expense base {_cents(expense_base)} "
            f"(Σ payments + (ROU − liability))"
        )
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Period helpers
# ─────────────────────────────────────────────────────────────────────────────


def add_months(d: date, months: int) -> date:
    """First-of-month date ``months`` after ``d``'s month."""
    y, m = d.year, d.month - 1 + months
    return date(y + m // 12, m % 12 + 1, 1)


def period_for_month(commencement_date: date, month_index: int) -> str:
    """YYYY-MM of 1-based lease month ``month_index`` (month 1 = the
    commencement month)."""
    d = add_months(commencement_date, month_index - 1)
    return f"{d.year}-{d.month:02d}"


def month_index_for_period(commencement_date: date, period: str) -> int:
    """1-based lease month for a YYYY-MM period (may fall outside 1..term)."""
    y, m = int(period[:4]), int(period[5:7])
    return (y - commencement_date.year) * 12 + (m - commencement_date.month) + 1


def lease_periods(lease) -> list[str]:
    return [period_for_month(lease.commencement_date, t) for t in range(1, lease.term_months + 1)]


def build_rows_for_lease(lease) -> list[dict]:
    """Full measurement for one lease: payment schedule → PV → ROU → rows,
    with periods attached. Short-term-exempt leases return [] (no rows —
    expense-as-incurred, documented)."""
    if lease.short_term_exempt:
        return []
    payments = payment_schedule(
        payment_amount=Decimal(str(lease.payment_amount)),
        term_months=int(lease.term_months),
        escalation_steps=lease.escalation_steps,
    )
    liability = present_value(
        payments, Decimal(str(lease.discount_rate_annual)), lease.payment_timing
    )
    rou = initial_rou_amount(
        liability,
        initial_direct_costs=Decimal(str(lease.initial_direct_costs or 0)),
        prepaid_amount=Decimal(str(lease.prepaid_amount or 0)),
        incentives=Decimal(str(lease.incentives or 0)),
    )
    rows = generate_rows(
        payments=payments,
        discount_rate_annual=Decimal(str(lease.discount_rate_annual)),
        payment_timing=lease.payment_timing,
        classification=lease.classification,
        liability=liability,
        rou=rou,
    )
    for row in rows:
        row["period"] = period_for_month(lease.commencement_date, row["month_index"])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Linked-proposal state. The generic approve/reject lanes know nothing about
# leases, so lease code reads the status of the proposals it linked and
# reconciles lazily (never trusts the row's own status alone).
# ─────────────────────────────────────────────────────────────────────────────

# A proposal in one of these states will never post: REJECTED / EXPIRED by a
# reviewer or the expiry sweep, FAILED as a confirmed QBO failure (a retryable
# failure keeps the action APPROVED and is retried under the same key).
DEAD_PROPOSAL_STATUSES = frozenset({"REJECTED", "EXPIRED", "FAILED"})
LIVE_PROPOSAL_STATUSES = frozenset({"PENDING", "APPROVED"})

COMMENCEMENT_POSTED = "posted"      # EXECUTED — the ROU asset and liability are booked
COMMENCEMENT_PENDING = "pending"    # PENDING / APPROVED — not booked yet
COMMENCEMENT_REJECTED = "rejected"  # REJECTED / EXPIRED / FAILED — will never be booked
COMMENCEMENT_MISSING = "missing"    # no commencement proposal on record


def proposal_statuses(db: Session, realm_id: str, proposal_ids) -> dict[int, str]:
    """``{proposal_id: status}`` for the given ids, realm-scoped."""
    from ..models import ProposedAction

    ids = {int(pid) for pid in proposal_ids if pid}
    if not ids:
        return {}
    return {
        pid: status
        for pid, status in db.query(ProposedAction.id, ProposedAction.status)
        .filter(ProposedAction.company_id == realm_id, ProposedAction.id.in_(ids))
        .all()
    }


def commencement_state(db: Session, lease) -> str:
    """Whether the lease's commencement JE is on the books. Only EXECUTED is
    "posted": every later lease JE (monthly cost, remeasurement, termination)
    moves balances that exist only once the commencement JE has posted."""
    pid = lease.commencement_proposed_action_id
    if not pid:
        return COMMENCEMENT_MISSING
    status = proposal_statuses(db, lease.realm_id, [pid]).get(int(pid))
    if status == "EXECUTED":
        return COMMENCEMENT_POSTED
    if status in LIVE_PROPOSAL_STATUSES:
        return COMMENCEMENT_PENDING
    if status in DEAD_PROPOSAL_STATUSES:
        return COMMENCEMENT_REJECTED
    if status is not None:
        # A status this code does not know is not proof the JE will never
        # post: treat it as not booked yet (no monthly JE, no re-draft).
        return COMMENCEMENT_PENDING
    return COMMENCEMENT_MISSING


def _lease_proposal_marker(db: Session, lease, proposal_id) -> tuple[str | None, dict]:
    """(status, vesper_bot_lease marker) of one of the lease's proposals."""
    from ..models import ProposedAction

    if not proposal_id:
        return None, {}
    proposal = (
        db.query(ProposedAction)
        .filter(ProposedAction.id == proposal_id, ProposedAction.company_id == lease.realm_id)
        .first()
    )
    if proposal is None:
        return None, {}
    return proposal.status, (proposal.payload or {}).get("vesper_bot_lease") or {}


def gross_rou_booked(db: Session, lease) -> Decimal:
    """The gross ROU asset the lease's executed JEs put on the ROU account: the
    commencement ROU plus the Δ of every EXECUTED remeasurement (each posts
    Δliability straight to the gross ROU account, while ``lease.initial_rou``
    stays the commencement measure). Read from the executed proposals' frozen
    markers — what reached the books — so a termination can unwind the gross
    and accumulated-amortization accounts to zero."""
    from ..models import ProposedAction

    total = Decimal(str(lease.initial_rou or 0))
    executed = db.query(ProposedAction.payload).filter(
        ProposedAction.company_id == lease.realm_id,
        ProposedAction.action_type == "JOURNAL_ENTRY",
        ProposedAction.status == "EXECUTED",
    )
    # ProposedAction.payload is generic JSON (no ->> operator): filter in Python.
    for (payload,) in executed:
        marker = payload.get("vesper_bot_lease") if isinstance(payload, dict) else None
        if (
            isinstance(marker, dict)
            and marker.get("kind") == "remeasurement"
            and marker.get("lease_id") == lease.id
        ):
            total += Decimal(str(marker["delta_liability"]))
    return _cents(total)


def live_termination_period(db: Session, lease) -> str | None:
    """The first period a still-live (PENDING/APPROVED) termination proposal
    covers, or None. Months from it onward are skipped when that termination
    executes, so nothing may draft a monthly JE for them meanwhile."""
    status, marker = _lease_proposal_marker(db, lease, lease.termination_proposed_action_id)
    if status not in LIVE_PROPOSAL_STATUSES:
        return None
    termination_date = marker.get("termination_date") or ""
    return termination_date[:7] or None


def live_remeasurement_period(db: Session, lease) -> str | None:
    """The effective period of a still-live (PENDING/APPROVED) remeasurement
    proposal, or None. Its execution rebuilds every row from that period on
    and refuses (after the JE has posted) if any of them is proposed or
    posted, so nothing may draft a monthly JE for those months meanwhile."""
    status, marker = _lease_proposal_marker(db, lease, lease.remeasure_proposed_action_id)
    if status not in LIVE_PROPOSAL_STATUSES:
        return None
    return marker.get("effective_period") or None


def reconcile_dead_lease_proposals(db: Session, lease) -> dict:
    """Return rows whose linked proposal will never post to DRAFT, so the month
    is proposed again (the generic reject / expiry lanes do not touch lease
    rows). Called lazily by every lease path that reads row state to decide
    something (the beat, month drafting, remeasure, terminate, re-draft):

      * a PENDING_APPROVAL row whose monthly proposal is REJECTED / EXPIRED /
        FAILED → DRAFT, proposal link cleared;
      * rows a since-rejected termination SKIPPED at draft time (terminations
        drafted before rows were skipped only on execution) → DRAFT.

    Rows linked to a proposal that cannot be found are left alone (unknown is
    not dead). Commits only when something changed."""
    from ..models import LeaseScheduleRow
    from . import audit

    reopened: list[str] = []
    linked = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
            LeaseScheduleRow.status == "PENDING_APPROVAL",
            LeaseScheduleRow.proposed_action_id.isnot(None),
        )
        .all()
    )
    statuses = proposal_statuses(db, lease.realm_id, [r.proposed_action_id for r in linked])
    for row in linked:
        if statuses.get(row.proposed_action_id) in DEAD_PROPOSAL_STATUSES:
            row.status = "DRAFT"
            row.proposed_action_id = None
            reopened.append(row.period)

    restored: list[str] = []
    if lease.status == "ACTIVE":
        status, marker = _lease_proposal_marker(db, lease, lease.termination_proposed_action_id)
        termination_period = (marker.get("termination_date") or "")[:7]
        if status in DEAD_PROPOSAL_STATUSES and termination_period:
            for row in (
                db.query(LeaseScheduleRow)
                .filter(
                    LeaseScheduleRow.lease_id == lease.id,
                    LeaseScheduleRow.realm_id == lease.realm_id,
                    LeaseScheduleRow.status == "SKIPPED",
                    LeaseScheduleRow.proposed_action_id.is_(None),
                    LeaseScheduleRow.period >= termination_period,
                )
                .all()
            ):
                row.status = "DRAFT"
                restored.append(row.period)

    if reopened or restored:
        audit.log_action(
            db=db,
            realm_id=lease.realm_id,
            action="LEASE_ROWS_RECONCILED",
            entity_type="lease_schedule",
            entity_id=str(lease.id),
            details={
                "reopened_rejected_months": sorted(reopened),
                "restored_after_rejected_termination": sorted(restored),
            },
        )
        db.commit()
    return {"reopened": sorted(reopened), "restored": sorted(restored)}


# ─────────────────────────────────────────────────────────────────────────────
# AC-3 — monthly JE drafting for the beat (the per-realm lease pass)
# ─────────────────────────────────────────────────────────────────────────────


def draft_lease_months(
    db: Session,
    lease,
    *,
    through_period: str,
    submitted_by: str = "system",
    human_preparer_user_id: int | None = None,
) -> dict:
    """Draft ONE monthly JE proposal (je_builder.build_lease_je) for EVERY
    still-DRAFT row of ``lease`` with period <= ``through_period``, oldest
    first — the catch-up that books a backdated lease's past months, a month
    whose JE was rejected, and a month a locked period or a failed run left
    behind. IDEMPOTENT: proposed / posted / skipped rows are never redrafted.

    Returns ``{"drafted": [...], "skipped": [{"period", "reason"}], "blocked"}``.
    ``blocked`` (nothing drafted) is set when the commencement JE is not booked
    (``commencement_not_posted`` / ``commencement_rejected``). Months a live
    termination covers (``termination_pending``), months a live remeasurement
    rebuilds (``remeasurement_pending``) and HARD-locked periods
    (``period_locked``) are skipped and reported, never drafted."""
    from ..models import LeaseScheduleRow
    from .accounting import je_builder
    from .task_autonomy import period_is_hard_locked

    state = commencement_state(db, lease)
    if state != COMMENCEMENT_POSTED:
        # No monthly JE against an ROU asset / liability that is not booked.
        blocked = (
            "commencement_rejected" if state == COMMENCEMENT_REJECTED else "commencement_not_posted"
        )
        return {"drafted": [], "skipped": [], "blocked": blocked}

    reconcile_dead_lease_proposals(db, lease)
    covered_from = live_termination_period(db, lease)
    rebuilt_from = live_remeasurement_period(db, lease)
    rows = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
            LeaseScheduleRow.period <= through_period,
            LeaseScheduleRow.status == "DRAFT",
            LeaseScheduleRow.proposed_action_id.is_(None),
        )
        .order_by(LeaseScheduleRow.period)
        .all()
    )
    drafted: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        if covered_from is not None and row.period >= covered_from:
            # A pending termination skips this month when it executes.
            skipped.append({"period": row.period, "reason": "termination_pending"})
            continue
        if rebuilt_from is not None and row.period >= rebuilt_from:
            # A pending remeasurement rebuilds this month when it executes;
            # it is drafted from the rebuilt row afterwards.
            skipped.append({"period": row.period, "reason": "remeasurement_pending"})
            continue
        if period_is_hard_locked(db, realm_id=lease.realm_id, period=row.period):
            skipped.append({"period": row.period, "reason": "period_locked"})
            continue
        proposal = je_builder.build_lease_je(
            lease, row, db,
            submitted_by=submitted_by,
            human_preparer_user_id=human_preparer_user_id,
        )
        if proposal is None:
            continue
        drafted.append(
            {
                "lease_id": lease.id,
                "row_id": row.id,
                "period": row.period,
                "proposal_id": proposal.id,
            }
        )
    return {"drafted": drafted, "skipped": skipped, "blocked": None}


def build_monthly_lease_proposals(
    db: Session,
    *,
    realm_id: str,
    period: str,
    skip_manual_policy: bool = False,
) -> dict:
    """Draft the monthly lease JEs for one realm through ``period``: for every
    ACTIVE non-exempt lease whose commencement JE is booked, every DRAFT row up
    to and including ``period`` gets ONE multi-line JOURNAL_ENTRY proposal
    (draft_lease_months — earlier months are caught up, not just ``period``).
    IDEMPOTENT: a row that is already proposed/posted/skipped is skipped
    (unique (lease_id, period) + the proposed_action_id guard). The CALLER
    (tasks/schedules.generate_schedule_entries_for_realm) honors and surfaces
    the FinancialCalendar lock on ``period`` before invoking this; an earlier,
    locked catch-up month is skipped and reported per row."""
    from ..models import LeaseSchedule, LeaseScheduleRow
    from .schedule_types import AUTOMATION_POLICY_MANUAL

    leases = (
        db.query(LeaseSchedule)
        .filter(
            LeaseSchedule.realm_id == realm_id,
            LeaseSchedule.status == "ACTIVE",
            LeaseSchedule.short_term_exempt.is_(False),
        )
        .all()
    )

    generated: list[dict] = []
    skip_reasons: list[dict] = []
    for lease in leases:
        policy = lease.automation_policy or "auto_propose"
        if skip_manual_policy and policy == AUTOMATION_POLICY_MANUAL:
            skip_reasons.append({"lease_id": lease.id, "reason": "manual_policy"})
            continue
        result = draft_lease_months(db, lease, through_period=period)
        if result["blocked"]:
            skip_reasons.append({"lease_id": lease.id, "reason": result["blocked"]})
            continue
        generated.extend(result["drafted"])
        skip_reasons.extend({"lease_id": lease.id, **s} for s in result["skipped"])
        if not result["drafted"] and not result["skipped"]:
            target = (
                db.query(LeaseScheduleRow.id)
                .filter(
                    LeaseScheduleRow.lease_id == lease.id,
                    LeaseScheduleRow.realm_id == realm_id,
                    LeaseScheduleRow.period == period,
                )
                .first()
            )
            skip_reasons.append({
                "lease_id": lease.id,
                "reason": "no_row_for_period" if target is None else "already_proposed",
            })
    return {
        "generated": len(generated),
        "proposals": generated,
        "skip_reasons": skip_reasons,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Post-execution hook — the lease leg of run_post_execution_schedule_hook
# ─────────────────────────────────────────────────────────────────────────────


def run_post_execution_lease_hook(db: Session, action, *, actor: str | None = None) -> None:
    """After a JOURNAL_ENTRY proposal executes, apply its ``vesper_bot_lease``
    marker. No-op for payloads without the marker. Runs INSIDE
    run_post_execution_schedule_hook's loud-failure contract — an exception
    here surfaces as a SCHEDULE_POST_HOOK_FAILED audit row + execution-result
    warning, never a silent pass.

      kind == "monthly":       mark the linked row POSTED (final row →
                               lease EXPIRED).
      kind == "commencement":  audit row (the lease went ACTIVE at draft
                               time; the JE posting is the books-side event
                               that unlocks its monthly JEs — see
                               commencement_state).
      kind == "remeasurement": apply the new terms + rebuild rows ≥ the
                               effective period (remeasured=True) — ONLY now,
                               so a rejected proposal changes nothing.
      kind == "termination":   flip TERMINATED, write the termination facts,
                               SKIP remaining unposted rows.
    """
    from ..models import LeaseSchedule, LeaseScheduleRow
    from . import audit

    marker = (action.payload or {}).get("vesper_bot_lease")
    if not marker or action.action_type != "JOURNAL_ENTRY":
        return
    kind = marker.get("kind")
    lease = (
        db.query(LeaseSchedule)
        .filter(
            LeaseSchedule.id == marker.get("lease_id"),
            LeaseSchedule.realm_id == action.company_id,
        )
        .first()
    )
    if lease is None:
        raise LeaseEngineError(
            f"vesper_bot_lease marker references missing lease {marker.get('lease_id')}"
        )

    if kind == "monthly":
        row = (
            db.query(LeaseScheduleRow)
            .filter(
                LeaseScheduleRow.proposed_action_id == action.id,
                LeaseScheduleRow.realm_id == action.company_id,
            )
            .first()
        )
        if row is None:
            raise LeaseEngineError(
                f"no LeaseScheduleRow linked to executed proposal {action.id}"
            )
        row.status = "POSTED"
        row.posted_at = datetime.utcnow()
        final_period = period_for_month(lease.commencement_date, lease.term_months)
        if row.period == final_period and lease.status == "ACTIVE":
            lease.status = "EXPIRED"
        db.commit()
        return

    if kind == "commencement":
        audit.log_action(
            db=db,
            realm_id=lease.realm_id,
            action="LEASE_COMMENCEMENT_POSTED",
            entity_type="lease_schedule",
            entity_id=str(lease.id),
            details={
                "proposal_id": action.id,
                "initial_liability": str(lease.initial_liability),
                "initial_rou": str(lease.initial_rou),
                "actor": actor,
            },
        )
        db.commit()
        return

    if kind == "remeasurement":
        _apply_remeasurement_on_execution(db, lease, marker, action, actor=actor)
        return

    if kind == "termination":
        _finalize_termination(db, lease, marker, action, actor=actor)
        return

    raise LeaseEngineError(f"unknown vesper_bot_lease kind {kind!r} on proposal {action.id}")


def _apply_remeasurement_on_execution(
    db: Session, lease, marker: dict, action, *, actor: str | None = None
) -> None:
    """Apply an executed remeasurement: update the lease terms, delete the
    DRAFT/SKIPPED rows ≥ effective, regenerate them (remeasured=True) from the
    marker's frozen measurement. Posted/locked periods are never touched —
    re-validated here (LOUD failure if a row ≥ effective posted between draft
    and approval)."""
    from ..models import LeaseScheduleRow
    from . import audit

    effective_period = marker["effective_period"]
    new_terms = marker.get("new_terms") or {}

    blockers = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
            LeaseScheduleRow.period >= effective_period,
            LeaseScheduleRow.status.in_(("POSTED", "PENDING_APPROVAL")),
        )
        .count()
    )
    if blockers:
        raise LeaseEngineError(
            f"remeasurement effective {effective_period} would restate "
            f"{blockers} posted/pending row(s) — posted periods never restate"
        )

    # Apply the term changes the marker froze at draft time.
    if "payment_amount" in new_terms:
        lease.payment_amount = Decimal(str(new_terms["payment_amount"]))
    if "escalation_steps" in new_terms:
        lease.escalation_steps = new_terms["escalation_steps"]
    if "term_months" in new_terms:
        lease.term_months = int(new_terms["term_months"])
    if "discount_rate_annual" in new_terms:  # explicit rate change only
        lease.discount_rate_annual = Decimal(str(new_terms["discount_rate_annual"]))
        lease.rate_basis = new_terms.get("rate_basis", lease.rate_basis)

    effective_index = month_index_for_period(lease.commencement_date, effective_period)
    payments = payment_schedule(
        payment_amount=Decimal(str(lease.payment_amount)),
        term_months=int(lease.term_months),
        escalation_steps=lease.escalation_steps,
    )
    remaining = payments[effective_index - 1 :]
    new_liability = Decimal(str(marker["new_liability_at_effective"]))
    new_rou = Decimal(str(marker["new_rou_at_effective"]))

    rebuilt = generate_rows(
        payments=remaining,
        discount_rate_annual=Decimal(str(lease.discount_rate_annual)),
        payment_timing=lease.payment_timing,
        classification=lease.classification,
        liability=new_liability,
        rou=new_rou,
    )

    (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
            LeaseScheduleRow.period >= effective_period,
        )
        .delete(synchronize_session=False)
    )
    for offset, row in enumerate(rebuilt):
        db.add(
            LeaseScheduleRow(
                lease_id=lease.id,
                realm_id=lease.realm_id,
                period=period_for_month(
                    lease.commencement_date, effective_index + offset
                ),
                payment=row["payment"],
                interest=row["interest"],
                liability_reduction=row["liability_reduction"],
                ending_liability=row["ending_liability"],
                rou_amortization=row["rou_amortization"],
                ending_rou=row["ending_rou"],
                expense=row["expense"],
                remeasured=True,
                status="DRAFT",
            )
        )
    lease.remeasure_proposed_action_id = None
    audit.log_action(
        db=db,
        realm_id=lease.realm_id,
        action="LEASE_REMEASURED",
        entity_type="lease_schedule",
        entity_id=str(lease.id),
        details={
            "proposal_id": action.id,
            "effective_period": effective_period,
            "delta_liability": str(marker.get("delta_liability")),
            "new_terms": new_terms,
            "actor": actor,
        },
    )
    db.commit()


def _finalize_termination(
    db: Session, lease, marker: dict, action, *, actor: str | None = None
) -> None:
    """Apply an executed termination: TERMINATED status + facts, remaining
    unposted rows (the termination month onward) SKIPPED — only now, never at
    draft time. Every earlier month was POSTED when the proposal was drafted
    (terminate_lease_service refuses otherwise). A rejected proposal leaves the
    lease ACTIVE and writes none of this (Phase-B disposal precedent)."""
    from ..models import LeaseScheduleRow
    from . import audit

    termination_period = marker["termination_date"][:7]
    rows = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
            LeaseScheduleRow.period >= termination_period,
            LeaseScheduleRow.status.in_(("DRAFT", "PENDING_APPROVAL")),
        )
        .all()
    )
    for row in rows:
        row.status = "SKIPPED"
    lease.status = "TERMINATED"
    lease.termination_date = date.fromisoformat(marker["termination_date"])
    lease.termination_settlement = Decimal(str(marker["settlement"]))
    lease.termination_gain_loss = Decimal(str(marker["gain_loss"]))
    audit.log_action(
        db=db,
        realm_id=lease.realm_id,
        action="LEASE_TERMINATED",
        entity_type="lease_schedule",
        entity_id=str(lease.id),
        details={
            "proposal_id": action.id,
            "termination_date": marker["termination_date"],
            "settlement": str(marker["settlement"]),
            "gain_loss": str(marker["gain_loss"]),
            "rows_skipped": len(rows),
            "actor": actor,
        },
    )
    db.commit()

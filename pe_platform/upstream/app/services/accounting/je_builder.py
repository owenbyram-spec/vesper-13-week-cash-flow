"""
je_builder.py — Builds schedule journal-entry drafts and proposals.

Revenue-recognition builder functions here are calculation/draft utilities.
Their presence does not establish a selected-period proposal route, recurring
posting workflow, or an execution-success hook updating recognition evidence.
Those integrations remain separately scoped work; human approval alone must not
be described as completed external posting.

Each period's amortization/depreciation entry becomes a balanced JE:
  Debit:  Expense account  (e.g. Amortization Expense, Depreciation Expense)
  Credit: Asset/Prepaid BS account (e.g. Prepaid Insurance, Accum. Depreciation)
"""
from __future__ import annotations

import logging
from ..proposal_provenance import human_provenance

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ...models import AccountingSchedule, ScheduleEntry, ProposedAction, AdjustingEntry
from ..schedule_types import DIRECTION_CREDIT_PL, SCHEDULE_TYPES
from .je_memo import je_memo

logger = logging.getLogger(__name__)

# Per-type default account names / entry types now live in the schedule-type
# descriptor registry (app/services/schedule_types.py) — the former
# _DEFAULT_EXPENSE_NAMES / _DEFAULT_CREDIT_NAMES / _ENTRY_TYPE_MAP module
# dicts collapsed onto it (plan 20260701-phase-a-generic-schedules AC-1).


def _entry_type_for(schedule_type: str | None, default: str) -> str:
    descriptor = SCHEDULE_TYPES.get(schedule_type or "")
    return descriptor.entry_type if descriptor else default


def build_je_for_entry(
    entry: ScheduleEntry,
    schedule: AccountingSchedule,
    db: Session,
    submitted_by: str = "system",
    human_preparer_user_id: int | None = None,
) -> ProposedAction | None:
    """
    Create a ProposedAction (JOURNAL_ENTRY) for a single ScheduleEntry.
    Also creates an AdjustingEntry record for traceability.
    Updates the ScheduleEntry status to PENDING_APPROVAL.

    Returns None (defense-in-depth against duplicate proposals) if the entry
    already has a linked proposal or is no longer DRAFT.
    """
    if entry.proposed_action_id is not None or entry.status != "DRAFT":
        logger.info(
            f"[JEBuilder] Skipping ScheduleEntry {entry.id}: already proposed "
            f"(proposed_action_id={entry.proposed_action_id}, status={entry.status})"
        )
        return None

    # Determine the period date (use last day of the period month)
    period = entry.period  # YYYY-MM
    year, month = int(period[:4]), int(period[5:7])
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    txn_date = date(year, month, last_day).isoformat()

    schedule_type = schedule.schedule_type
    descriptor = SCHEDULE_TYPES.get(schedule_type or "")

    # Account references — prefer what's stored on the schedule, fall back to
    # the registry's per-type default names.
    expense_account_id = schedule.expense_account_id
    expense_account_name = schedule.expense_account_name or (
        (descriptor.default_expense_account_name if descriptor else "")
        or "Amortization Expense"
    )

    credit_account_id = schedule.qbo_account_id
    credit_account_name = schedule.qbo_account_name or (
        (descriptor.default_bs_account_name if descriptor else "") or "Asset Account"
    )

    raw_amount = round(float(entry.amount), 2)
    # Sign-aware: a JE line amount must be POSITIVE — QBO's line builder emits a
    # Debit line only when debit>0 and a Credit line only when credit>0, so a
    # negative debit/credit silently drops the line and posts an unbalanced JE.
    # For a negative entry (a reversal / over-amortization correction) the
    # accounting flips relative to the type's amortization_direction.
    amount = round(abs(raw_amount), 2)
    is_reversal = raw_amount < 0

    # Direction-aware layout (registry amortization_direction):
    #   debit_pl  (legacy default): positive → DEBIT P&L / CREDIT BS
    #   credit_pl (e.g. DEFERRED_REVENUE, DEBT_PRINCIPAL): positive →
    #             DEBIT BS / CREDIT P&L (recognize INTO revenue / pay down
    #             the liability)
    # A reversal flips whichever layout the direction selects, so the XOR of
    # (is_reversal, is_credit_pl) picks the side. For debit_pl types this is
    # byte-identical to the pre-registry code (golden-locked).
    is_credit_pl = bool(descriptor and descriptor.amortization_direction == DIRECTION_CREDIT_PL)

    memo = je_memo(f"{_entry_type_for(schedule_type, 'Adjusting Entry')} – {schedule.name} – {period}")

    if is_reversal != is_credit_pl:
        # DEBIT the BS account, CREDIT the P&L account.
        expense_debit, expense_credit = 0, amount
        bs_debit, bs_credit = amount, 0
    else:
        # DEBIT the P&L account, CREDIT the BS account.
        expense_debit, expense_credit = amount, 0
        bs_debit, bs_credit = 0, amount

    je_payload: dict = {
        "date": txn_date,
        "memo": memo,
        "lines": [
            {
                "account_id": expense_account_id or "",
                "account_name": expense_account_name,
                "debit": expense_debit,
                "credit": expense_credit,
                "description": memo,
            },
            {
                "account_id": credit_account_id or "",
                "account_name": credit_account_name,
                "debit": bs_debit,
                "credit": bs_credit,
                "description": memo,
            },
        ],
        # Metadata so the actions UI can show context
        "source_module": "accounting",
        "schedule_id": schedule.id,
        "schedule_type": schedule_type,
        "schedule_name": schedule.name,
    }

    # Side-accurate names for the reversal reasoning (direction-aware; for
    # legacy debit_pl types these resolve to the exact pre-registry strings).
    debited_name = credit_account_name if bs_debit else expense_account_name
    credited_name = expense_account_name if expense_credit else credit_account_name

    if is_reversal and entry.reversal_of_entry_id is not None:
        # Wave 2 AC-6: an auto-reversing accrual — NOT an over-amortization
        # correction. Say exactly what it is so the approving human isn't misled.
        reasoning = (
            f"{schedule.name}: AUTO-REVERSING ACCRUAL for {period}. "
            f"DEBIT {debited_name} / CREDIT {credited_name} ${amount:,.2f} "
            f"to reverse the prior period's posted accrual "
            f"(entry #{entry.reversal_of_entry_id}); the actual replaces the estimate. "
            f"Schedule: ${schedule.original_amount:,.2f} over {schedule.useful_life_months or '?'} months "
            f"({schedule.depreciation_method})."
        )
    elif is_reversal:
        reasoning = (
            f"{schedule.name}: REVERSAL / over-amortization correction for {period}. "
            f"DEBIT {debited_name} / CREDIT {credited_name} ${amount:,.2f} "
            f"to restore the over-amortized balance. "
            f"Schedule: ${schedule.original_amount:,.2f} over {schedule.useful_life_months or '?'} months "
            f"({schedule.depreciation_method})."
        )
    else:
        reasoning = (
            f"{schedule.name}: {_entry_type_for(schedule_type, 'Adjusting')} entry for {period}. "
            f"Monthly amount: ${amount:,.2f}. "
            f"Schedule: ${schedule.original_amount:,.2f} over {schedule.useful_life_months or '?'} months "
            f"({schedule.depreciation_method})."
        )

    proposal = ProposedAction(
        **human_provenance(human_preparer_user_id),
        company_id=schedule.realm_id,
        action_type="JOURNAL_ENTRY",
        payload=je_payload,
        reasoning=reasoning,
        confidence_score=0.99,  # These are computed, not predicted
        status="PENDING",
    )
    db.add(proposal)
    db.flush()  # get proposal.id

    # Create traceability record
    adj_entry = AdjustingEntry(
        realm_id=schedule.realm_id,
        period=period,
        schedule_id=schedule.id,
        schedule_entry_id=entry.id,
        entry_type=_entry_type_for(schedule_type, "MANUAL"),
        proposed_action_id=proposal.id,
        status="PENDING_APPROVAL",
    )
    db.add(adj_entry)

    # Update entry status
    entry.proposed_action_id = proposal.id
    entry.status = "PENDING_APPROVAL"

    db.commit()

    logger.info(
        f"[JEBuilder] Created ProposedAction {proposal.id} for ScheduleEntry {entry.id} "
        f"(schedule={schedule.id}, period={period}, amount=${amount:.2f}, "
        f"{'REVERSAL' if is_reversal else 'NORMAL'})"
    )
    return proposal


TWO_PLACES = Decimal("0.01")


def build_disposal_je(
    schedule: AccountingSchedule,
    db: Session,
    *,
    disposal_date: date,
    cost: Decimal,
    accumulated: Decimal,
    proceeds: Decimal,
    cost_account: tuple[str, str],        # (qbo_account_id, name) — asset COST
    contra_account: tuple[str, str],      # (qbo_account_id, name) — accum-dep/amort
    proceeds_account: tuple[str, str] | None,  # (id, name) — cash/receivable; None when proceeds == 0
    gain_loss_account: tuple[str, str],   # (id, name) — Gain/Loss on Disposal P&L
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> ProposedAction:
    """
    Phase-B AC-2 (plan 20260702-phase-b-fa-lifecycle): draft the ONE
    proposal-gated multi-line disposal JE for a fixed-asset/intangible
    schedule. ALWAYS a PENDING ProposedAction requiring human approval —
    regardless of the schedule's automation_policy. (The beat's
    auto_post_deterministic pass only ever sees proposals emitted by
    generate_month_entries' own batches, so a disposal proposal can never
    enter the auto-post path; this function additionally never auto-approves.)

    Lines (all amounts positive; Decimal math, quantized to cents):
        DEBIT  accumulated depreciation/amortization   accumulated
        DEBIT  proceeds account                        proceeds   (when > 0)
        DEBIT  Loss on Disposal                        NBV − proceeds   (loss case)
        CREDIT asset cost account                      cost
        CREDIT Gain on Disposal                        proceeds − NBV   (gain case)
    where NBV = cost − accumulated and gain/loss = proceeds − NBV. Debits and
    credits balance by construction: accum + proceeds + loss = cost + gain.

    Partial-period convention (documented per the plan): NO depreciation is
    taken in the disposal month — accumulated depreciation is measured through
    the END of the month BEFORE the disposal date. This is the simplest
    defensible convention (a full-month convention on the other end would take
    a full month for one day of use); the caller enforces it by blocking
    disposal while unposted entries exist at/after the disposal month.

    The payload carries a ``peregrine_disposal`` marker; on execution the
    schedule-engine post-execution hook flips the schedule to DISPOSED and
    freezes it (_finalize_disposal).
    """
    cost = Decimal(cost).quantize(TWO_PLACES)
    accumulated = Decimal(accumulated).quantize(TWO_PLACES)
    proceeds = Decimal(proceeds).quantize(TWO_PLACES)
    nbv = (cost - accumulated).quantize(TWO_PLACES)
    gain_loss = (proceeds - nbv).quantize(TWO_PLACES)

    memo = je_memo(f"Asset disposal – {schedule.name} – {disposal_date.isoformat()}")

    lines: list[dict] = []
    if accumulated > 0:
        lines.append({
            "account_id": contra_account[0],
            "account_name": contra_account[1],
            "vendor_name": "",
            "debit": float(accumulated),
            "credit": 0,
            "description": f"Remove accumulated depreciation: {schedule.name}",
        })
    if proceeds > 0:
        if proceeds_account is None:  # defense-in-depth; the router 409s first
            raise ValueError("proceeds_account is required when proceeds > 0")
        lines.append({
            "account_id": proceeds_account[0],
            "account_name": proceeds_account[1],
            "vendor_name": "",
            "debit": float(proceeds),
            "credit": 0,
            "description": f"Disposal proceeds: {schedule.name}",
        })
    if gain_loss < 0:  # loss — DEBIT the gain/loss P&L account
        lines.append({
            "account_id": gain_loss_account[0],
            "account_name": gain_loss_account[1],
            "vendor_name": "",
            "debit": float(-gain_loss),
            "credit": 0,
            "description": f"Loss on disposal: {schedule.name}",
        })
    lines.append({
        "account_id": cost_account[0],
        "account_name": cost_account[1],
        "vendor_name": "",
        "debit": 0,
        "credit": float(cost),
        "description": f"Remove asset cost: {schedule.name}",
    })
    if gain_loss > 0:  # gain — CREDIT the gain/loss P&L account
        lines.append({
            "account_id": gain_loss_account[0],
            "account_name": gain_loss_account[1],
            "vendor_name": "",
            "debit": 0,
            "credit": float(gain_loss),
            "description": f"Gain on disposal: {schedule.name}",
        })

    total_debits = sum(Decimal(str(ln["debit"])) for ln in lines)
    total_credits = sum(Decimal(str(ln["credit"])) for ln in lines)
    if (total_debits - total_credits).copy_abs() > Decimal("0.005"):
        raise ValueError(
            f"disposal JE out of balance: debits={total_debits} credits={total_credits}"
        )

    je_payload: dict = {
        "date": disposal_date.isoformat(),
        "memo": memo,
        "lines": lines,
        "source_module": "accounting",
        "schedule_id": schedule.id,
        "schedule_type": schedule.schedule_type,
        "schedule_name": schedule.name,
        # The disposal marker the post-execution hook keys off.
        "peregrine_disposal": {
            "schedule_id": schedule.id,
            "disposal_date": disposal_date.isoformat(),
            "cost": float(cost),
            "accumulated": float(accumulated),
            "net_book_value": float(nbv),
            "proceeds": float(proceeds),
            "gain_loss": float(gain_loss),
        },
    }

    if gain_loss > 0:
        outcome = f"GAIN of ${gain_loss:,.2f}"
    elif gain_loss < 0:
        outcome = f"LOSS of ${-gain_loss:,.2f}"
    else:
        outcome = "no gain or loss"
    reasoning = (
        f"{schedule.name}: DISPOSAL on {disposal_date.isoformat()}. "
        f"Removes cost ${cost:,.2f} and accumulated depreciation ${accumulated:,.2f} "
        f"(net book value ${nbv:,.2f}); proceeds ${proceeds:,.2f} → {outcome} "
        f"booked to {gain_loss_account[1]}. No depreciation is taken in the disposal "
        f"month. On approval and posting, the schedule is marked DISPOSED and stops "
        f"generating. Submitted by {submitted_by}."
    )

    proposal = ProposedAction(
        **human_provenance(human_preparer_user_id),
        company_id=schedule.realm_id,
        action_type="JOURNAL_ENTRY",
        payload=je_payload,
        reasoning=reasoning,
        confidence_score=0.99,  # computed, not predicted
        status="PENDING",  # ALWAYS proposal-gated — never auto-posted
    )
    db.add(proposal)
    db.flush()

    logger.info(
        f"[JEBuilder] Created DISPOSAL ProposedAction {proposal.id} for schedule "
        f"{schedule.id} (cost=${cost}, accum=${accumulated}, proceeds=${proceeds}, "
        f"gain_loss=${gain_loss})"
    )
    return proposal


# ─────────────────────────────────────────────────────────────────────────────
# Phase-C ASC 842 lease JEs (plan 20260702-phase-c-asc842, AC-3/AC-4)
#
# All lease JEs share the SAME simplified-lines schema the executor's
# journal_entries.create_journal_entry consumes: lines of
# {account_id, account_name, vendor_name, debit, credit, description} with
# every amount POSITIVE on exactly one side (the executor drops non-positive
# lines, which would silently unbalance a JE). Payload carries a
# ``peregrine_lease`` marker the post-execution lease hook keys off.
#
# CASH CONVENTION (documented, AC-3): the JE books the ACCOUNTING, never the
# cash movement — the actual payment is expected to be coded to the lease
# liability account by the bank feed / AP (DR liability, CR cash). The
# monthly JE therefore CREDITS the liability with the month's interest
# accretion, so liability activity nets to −(payment − interest) = the
# scheduled liability_reduction.
# ─────────────────────────────────────────────────────────────────────────────


def _lease_line(
    account: tuple[str | None, str], signed_amount: Decimal, description: str
) -> dict | None:
    """One simplified JE line from a SIGNED amount: positive → DEBIT,
    negative → CREDIT (a one-cent negative final-row interest plug flips
    sides instead of emitting a negative amount the executor would drop).
    Returns None for a zero amount (line omitted)."""
    amount = Decimal(str(signed_amount)).quantize(TWO_PLACES)
    if amount == 0:
        return None
    return {
        "account_id": str(account[0] or ""),
        "account_name": account[1] or "",
        "vendor_name": "",
        "debit": float(amount) if amount > 0 else 0,
        "credit": float(-amount) if amount < 0 else 0,
        "description": description,
    }


def _assert_lease_lines_balanced(lines: list[dict], context: str) -> None:
    total_debits = sum(Decimal(str(ln["debit"])) for ln in lines)
    total_credits = sum(Decimal(str(ln["credit"])) for ln in lines)
    if (total_debits - total_credits).copy_abs() > Decimal("0.005"):
        raise ValueError(
            f"{context} JE out of balance: debits={total_debits} credits={total_credits}"
        )
    if not lines:
        raise ValueError(f"{context} JE has no lines")


def _lease_proposal(
    lease, db: Session, *, txn_date: str, memo: str, lines: list[dict],
    marker: dict, reasoning: str,
    human_preparer_user_id: int | None = None,
) -> ProposedAction:
    proposal = ProposedAction(
        **human_provenance(human_preparer_user_id),
        company_id=lease.realm_id,
        action_type="JOURNAL_ENTRY",
        payload={
            "date": txn_date,
            "memo": memo,
            "lines": lines,
            "source_module": "accounting",
            "lease_id": lease.id,
            "lease_name": lease.name,
            "lease_classification": lease.classification,
            "peregrine_lease": marker,
        },
        reasoning=reasoning,
        confidence_score=0.99,  # computed, not predicted
        status="PENDING",
    )
    db.add(proposal)
    db.flush()
    return proposal


def _period_end_iso(period: str) -> str:
    import calendar
    year, month = int(period[:4]), int(period[5:7])
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def build_lease_je(
    lease, row, db: Session, submitted_by: str = "system",
    human_preparer_user_id: int | None = None,
) -> ProposedAction | None:
    """AC-3: ONE multi-line JOURNAL_ENTRY ProposedAction for one lease month
    (LeaseScheduleRow). Returns None if the row is already proposed / not
    DRAFT (defense-in-depth, mirrors build_je_for_entry).

    OPERATING (single lease cost; interest embedded, ASC 842-20-25-6a):
        DR  lease expense                 expense (straight-line)
        CR  lease liability               interest (accretion — see the cash
                                          convention header)
        CR  accumulated ROU amortization  rou_amortization (= expense − interest)
    FINANCE (interest + amortization presented separately):
        DR  interest expense              interest
        DR  ROU amortization expense      rou_amortization (= ROU ÷ term SL)
        CR  lease liability               interest
        CR  accumulated ROU amortization  rou_amortization
    Signed-line helper flips a negative-cent final plug to the other side.
    Commits; row → PENDING_APPROVAL, linked to the proposal.
    """
    if row.proposed_action_id is not None or row.status != "DRAFT":
        logger.info(
            f"[JEBuilder] Skipping LeaseScheduleRow {row.id}: already proposed "
            f"(proposed_action_id={row.proposed_action_id}, status={row.status})"
        )
        return None

    period = row.period
    txn_date = _period_end_iso(period)
    interest = Decimal(str(row.interest))
    amort = Decimal(str(row.rou_amortization))
    expense = Decimal(str(row.expense))
    memo = je_memo(f"Lease {lease.classification} cost – {lease.name} – {period}")

    if lease.classification == "operating":
        raw_lines = [
            _lease_line(
                (lease.lease_expense_account_id, lease.lease_expense_account_name or "Lease Expense"),
                expense,
                f"Straight-line lease cost: {lease.name}",
            ),
            _lease_line(
                (lease.lease_liability_account_id, lease.lease_liability_account_name or "Lease Liability"),
                -interest,
                f"Lease liability interest accretion: {lease.name}",
            ),
            _lease_line(
                (lease.accum_rou_amort_account_id, lease.accum_rou_amort_account_name or "Accumulated ROU Amortization"),
                -amort,
                f"ROU amortization: {lease.name}",
            ),
        ]
    else:
        raw_lines = [
            _lease_line(
                (lease.interest_expense_account_id, lease.interest_expense_account_name or "Interest Expense"),
                interest,
                f"Lease interest expense: {lease.name}",
            ),
            _lease_line(
                (lease.rou_amort_expense_account_id, lease.rou_amort_expense_account_name or "ROU Amortization Expense"),
                amort,
                f"ROU amortization expense: {lease.name}",
            ),
            _lease_line(
                (lease.lease_liability_account_id, lease.lease_liability_account_name or "Lease Liability"),
                -interest,
                f"Lease liability interest accretion: {lease.name}",
            ),
            _lease_line(
                (lease.accum_rou_amort_account_id, lease.accum_rou_amort_account_name or "Accumulated ROU Amortization"),
                -amort,
                f"ROU amortization: {lease.name}",
            ),
        ]
    lines = [ln for ln in raw_lines if ln is not None]
    _assert_lease_lines_balanced(lines, "lease monthly")

    reasoning = (
        f"{lease.name}: ASC 842 {lease.classification} lease cost for {period}. "
        f"Interest accretion ${interest:,.2f}, ROU amortization ${amort:,.2f}, "
        f"total P&L ${expense:,.2f}. Books the accounting only — the cash payment "
        f"(${Decimal(str(row.payment)):,.2f}) is expected to be coded to the lease "
        f"liability account by the bank feed/AP. Scheduled ending liability "
        f"${Decimal(str(row.ending_liability)):,.2f}, ending ROU "
        f"${Decimal(str(row.ending_rou)):,.2f}."
    )

    proposal = _lease_proposal(
        lease, db, txn_date=txn_date, memo=memo, lines=lines,
        marker={"kind": "monthly", "lease_id": lease.id, "row_id": row.id, "period": period},
        reasoning=reasoning,
        human_preparer_user_id=human_preparer_user_id,
    )
    row.proposed_action_id = proposal.id
    row.status = "PENDING_APPROVAL"
    db.commit()
    logger.info(
        f"[JEBuilder] Created LEASE ProposedAction {proposal.id} for row {row.id} "
        f"(lease={lease.id}, period={period}, {lease.classification})"
    )
    return proposal


def build_lease_commencement_je(
    lease,
    db: Session,
    *,
    idc_account: tuple[str, str] | None,
    prepaid_account: tuple[str, str] | None,
    incentive_account: tuple[str, str] | None,
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> ProposedAction:
    """AC-4: the commencement JE — ALWAYS a PENDING proposal regardless of
    automation_policy (never auto-posted).

        DR  ROU asset            initial_rou
        CR  lease liability      initial_liability
        CR  IDC source account   initial_direct_costs   (when > 0)
        CR  prepaid source       prepaid_amount         (when > 0)
        DR  incentive source     incentives             (when > 0)

    Balanced by construction: ROU = liability + IDC + prepaid − incentives.
    The source accounts are where those amounts currently sit (an expense to
    reverse, a prepaid asset to reclass, the account the incentive landed in).
    """
    liability = Decimal(str(lease.initial_liability))
    rou = Decimal(str(lease.initial_rou))
    idc = Decimal(str(lease.initial_direct_costs or 0))
    prepaid = Decimal(str(lease.prepaid_amount or 0))
    incentives = Decimal(str(lease.incentives or 0))
    for amount, account, label in (
        (idc, idc_account, "initial_direct_costs"),
        (prepaid, prepaid_account, "prepaid_amount"),
        (incentives, incentive_account, "incentives"),
    ):
        if amount > 0 and account is None:  # defense-in-depth; the router 409s first
            raise ValueError(f"{label} > 0 requires a source account")

    txn_date = lease.commencement_date.isoformat()
    memo = je_memo(f"Lease commencement – {lease.name} – {txn_date}")
    raw_lines = [
        _lease_line(
            (lease.rou_asset_account_id, lease.rou_asset_account_name or "Right-of-Use Asset"),
            rou,
            f"Recognize ROU asset: {lease.name}",
        ),
        _lease_line(
            (lease.lease_liability_account_id, lease.lease_liability_account_name or "Lease Liability"),
            -liability,
            f"Recognize lease liability: {lease.name}",
        ),
    ]
    if idc > 0:
        raw_lines.append(_lease_line(idc_account, -idc, f"Capitalize initial direct costs: {lease.name}"))
    if prepaid > 0:
        raw_lines.append(_lease_line(prepaid_account, -prepaid, f"Reclass prepaid rent into ROU: {lease.name}"))
    if incentives > 0:
        raw_lines.append(_lease_line(incentive_account, incentives, f"Lease incentives received: {lease.name}"))
    lines = [ln for ln in raw_lines if ln is not None]
    _assert_lease_lines_balanced(lines, "lease commencement")

    reasoning = (
        f"{lease.name}: ASC 842 COMMENCEMENT on {txn_date} ({lease.classification}). "
        f"Liability ${liability:,.2f} = PV of fixed payments at the locked "
        f"{Decimal(str(lease.discount_rate_annual)) * 100:.2f}% annual rate "
        f"({lease.rate_basis}); ROU ${rou:,.2f} = liability + IDC ${idc:,.2f} "
        f"+ prepaid ${prepaid:,.2f} − incentives ${incentives:,.2f}. "
        f"ALWAYS human-gated regardless of automation policy. Submitted by {submitted_by}."
    )
    proposal = _lease_proposal(
        lease, db, txn_date=txn_date, memo=memo, lines=lines,
        marker={
            "kind": "commencement",
            "lease_id": lease.id,
            "initial_liability": float(liability),
            "initial_rou": float(rou),
        },
        reasoning=reasoning,
        human_preparer_user_id=human_preparer_user_id,
    )
    logger.info(
        f"[JEBuilder] Created LEASE COMMENCEMENT ProposedAction {proposal.id} "
        f"for lease {lease.id} (L=${liability}, ROU=${rou})"
    )
    return proposal


def build_lease_remeasurement_je(
    lease,
    db: Session,
    *,
    effective_period: str,
    delta_liability: Decimal,
    new_liability_at_effective: Decimal,
    new_rou_at_effective: Decimal,
    new_terms: dict,
    submitted_by: str,
    rate_unchanged_reason: str | None = None,
    human_preparer_user_id: int | None = None,
) -> ProposedAction:
    """AC-4: the remeasurement adjustment JE — ALWAYS gated. ROU is adjusted
    by Δliability (ASC 842-10-25-8 modification-not-separate-contract):

        ΔL > 0:  DR ROU asset ΔL / CR lease liability ΔL
        ΔL < 0:  DR lease liability |ΔL| / CR ROU asset |ΔL|

    The rows ≥ effective_period are rebuilt ONLY when this proposal EXECUTES
    (the marker freezes the measurement) — a rejected proposal changes nothing.
    The reasoning names the discount rate used: the UPDATED rate when
    ``new_terms`` carries one, otherwise the existing rate and why it was kept
    (``rate_unchanged_reason``, a lease_engine.RATE_UNCHANGED_REASONS code).
    """
    from ..lease_engine import RATE_UNCHANGED_REASONS

    delta = Decimal(str(delta_liability)).quantize(TWO_PLACES)
    if delta == 0:
        raise ValueError("remeasurement with zero liability change has no JE to book")
    txn_date = _period_end_iso(effective_period)
    memo = je_memo(f"Lease remeasurement – {lease.name} – effective {effective_period}")
    lines = [
        ln
        for ln in (
            _lease_line(
                (lease.rou_asset_account_id, lease.rou_asset_account_name or "Right-of-Use Asset"),
                delta,
                f"Remeasurement ROU adjustment: {lease.name}",
            ),
            _lease_line(
                (lease.lease_liability_account_id, lease.lease_liability_account_name or "Lease Liability"),
                -delta,
                f"Remeasured lease liability: {lease.name}",
            ),
        )
        if ln is not None
    ]
    _assert_lease_lines_balanced(lines, "lease remeasurement")

    if "discount_rate_annual" in new_terms:
        rate_text = (
            f"at the UPDATED {Decimal(str(new_terms['discount_rate_annual'])) * 100:.2f}% "
            f"discount rate ({new_terms.get('rate_basis')} basis, determined at the "
            f"remeasurement date)"
        )
    else:
        rate_text = (
            f"at the EXISTING {Decimal(str(lease.discount_rate_annual)) * 100:.2f}% "
            f"discount rate, not updated because the change results from "
            f"{RATE_UNCHANGED_REASONS.get(rate_unchanged_reason or '', 'an unspecified reassessment')}"
        )
    reasoning = (
        f"{lease.name}: ASC 842 REMEASUREMENT effective {effective_period}. "
        f"Remaining liability remeasured to ${Decimal(str(new_liability_at_effective)):,.2f} "
        f"(Δ ${delta:,.2f}) {rate_text}"
        + f"; ROU adjusted by the same Δ to ${Decimal(str(new_rou_at_effective)):,.2f}. "
        f"On approval, rows from {effective_period} forward are rebuilt "
        f"(prior/posted periods are never restated). ALWAYS human-gated. "
        f"Submitted by {submitted_by}."
    )
    proposal = _lease_proposal(
        lease, db, txn_date=txn_date, memo=memo, lines=lines,
        marker={
            "kind": "remeasurement",
            "lease_id": lease.id,
            "effective_period": effective_period,
            "delta_liability": float(delta),
            "new_liability_at_effective": float(Decimal(str(new_liability_at_effective))),
            "new_rou_at_effective": float(Decimal(str(new_rou_at_effective))),
            "new_terms": new_terms,
            "rate_unchanged_reason": rate_unchanged_reason,
        },
        reasoning=reasoning,
        human_preparer_user_id=human_preparer_user_id,
    )
    logger.info(
        f"[JEBuilder] Created LEASE REMEASUREMENT ProposedAction {proposal.id} "
        f"for lease {lease.id} (effective={effective_period}, delta=${delta})"
    )
    return proposal


def build_lease_termination_je(
    lease,
    db: Session,
    *,
    termination_date: date,
    liability_remaining: Decimal,
    rou_gross: Decimal,
    accum_rou: Decimal,
    settlement: Decimal,
    settlement_account: tuple[str, str] | None,
    gain_loss_account: tuple[str, str],
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> ProposedAction:
    """AC-4: the termination JE — ALWAYS gated. gain/loss = remaining
    liability − ROU net book value − cash settlement (positive = gain):

        DR  lease liability               liability_remaining
        DR  accumulated ROU amortization  accum_rou
        DR  Loss on lease termination     −gain_loss   (loss case)
        CR  ROU asset                     rou_gross
        CR  settlement account            settlement   (when > 0)
        CR  Gain on lease termination     gain_loss    (gain case)
    """
    liability_remaining = Decimal(str(liability_remaining)).quantize(TWO_PLACES)
    rou_gross = Decimal(str(rou_gross)).quantize(TWO_PLACES)
    accum_rou = Decimal(str(accum_rou)).quantize(TWO_PLACES)
    settlement = Decimal(str(settlement)).quantize(TWO_PLACES)
    nbv = (rou_gross - accum_rou).quantize(TWO_PLACES)
    gain_loss = (liability_remaining - nbv - settlement).quantize(TWO_PLACES)
    if settlement > 0 and settlement_account is None:  # defense-in-depth; router 409s first
        raise ValueError("settlement_account is required when settlement > 0")

    memo = je_memo(f"Lease termination – {lease.name} – {termination_date.isoformat()}")
    raw_lines = [
        _lease_line(
            (lease.lease_liability_account_id, lease.lease_liability_account_name or "Lease Liability"),
            liability_remaining,
            f"Derecognize lease liability: {lease.name}",
        ),
        _lease_line(
            (lease.accum_rou_amort_account_id, lease.accum_rou_amort_account_name or "Accumulated ROU Amortization"),
            accum_rou,
            f"Remove accumulated ROU amortization: {lease.name}",
        ),
        _lease_line(
            (lease.rou_asset_account_id, lease.rou_asset_account_name or "Right-of-Use Asset"),
            -rou_gross,
            f"Derecognize ROU asset: {lease.name}",
        ),
    ]
    if settlement > 0:
        raw_lines.append(
            _lease_line(settlement_account, -settlement, f"Termination settlement: {lease.name}")
        )
    if gain_loss != 0:
        raw_lines.append(
            _lease_line(
                gain_loss_account,
                -gain_loss,  # gain (positive) → CREDIT; loss → DEBIT
                (
                    f"Gain on lease termination: {lease.name}"
                    if gain_loss > 0
                    else f"Loss on lease termination: {lease.name}"
                ),
            )
        )
    lines = [ln for ln in raw_lines if ln is not None]
    _assert_lease_lines_balanced(lines, "lease termination")

    outcome = (
        f"GAIN of ${gain_loss:,.2f}"
        if gain_loss > 0
        else (f"LOSS of ${-gain_loss:,.2f}" if gain_loss < 0 else "no gain or loss")
    )
    reasoning = (
        f"{lease.name}: ASC 842 TERMINATION on {termination_date.isoformat()}. "
        f"Derecognizes the remaining liability ${liability_remaining:,.2f} and the "
        f"ROU asset (gross ${rou_gross:,.2f} − accumulated ${accum_rou:,.2f} = NBV "
        f"${nbv:,.2f}); settlement ${settlement:,.2f} → {outcome} booked to "
        f"{gain_loss_account[1]}. On approval and posting the lease is marked "
        f"TERMINATED and remaining months are skipped. ALWAYS human-gated. "
        f"Submitted by {submitted_by}."
    )
    proposal = _lease_proposal(
        lease, db, txn_date=termination_date.isoformat(), memo=memo, lines=lines,
        marker={
            "kind": "termination",
            "lease_id": lease.id,
            "termination_date": termination_date.isoformat(),
            "liability_remaining": float(liability_remaining),
            "rou_gross": float(rou_gross),
            "accum_rou": float(accum_rou),
            "rou_net_book_value": float(nbv),
            "settlement": float(settlement),
            "gain_loss": float(gain_loss),
        },
        reasoning=reasoning,
        human_preparer_user_id=human_preparer_user_id,
    )
    logger.info(
        f"[JEBuilder] Created LEASE TERMINATION ProposedAction {proposal.id} for "
        f"lease {lease.id} (L=${liability_remaining}, NBV=${nbv}, "
        f"settle=${settlement}, gain_loss=${gain_loss})"
    )
    return proposal


# PLAN-EXEMPT: chunk 4 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
# ─────────────────────────────────────────────────────────────────────────────
# ASC 606 revenue-recognition JEs (plan 20260714-wave3-2-asc606-engine, Chunk 4,
# AC-C4a). The revenue_engine owns every recognized-revenue NUMBER (Chunk 2); this
# seam turns an engine-produced RevenueRecognitionRow (or a modification catch-up
# delta) into ONE balanced, proposal-gated PENDING ProposedAction — the SAME rails
# the ASC 842 lease JEs ride (multi-line simplified-lines schema, signed-line
# helper, balance assertion, ALWAYS human-gated). NEVER a direct GL post.
#
# The recognition entry credits P&L revenue and debits the contract-position side:
#   LIABILITY lane (billed in advance — a deferred-revenue release):
#       DR  deferred revenue (contract liability)   period revenue
#       CR  revenue                                  period revenue
#   ASSET lane (performed ahead of billing — a contract-asset draw-down at billing):
#       DR  contract asset                           period revenue
#       CR  revenue                                  period revenue
# The lane follows the row's ENDING net contract position (contract_asset_bal > 0
# ⇒ asset lane; otherwise the liability lane), which is where the balance sheet
# presents the obligation for that period.
# ─────────────────────────────────────────────────────────────────────────────


def build_revenue_recognition_je(
    obligation, row, db: Session, submitted_by: str = "system",
    human_preparer_user_id: int | None = None,
) -> ProposedAction | None:
    """AC-C4a: ONE multi-line JOURNAL_ENTRY ProposedAction for one recognition
    period (RevenueRecognitionRow). ALWAYS a PENDING proposal — proposal-gated,
    never direct-posted. Returns None if the row is already proposed / not DRAFT
    (idempotent, mirrors build_lease_je) OR the period revenue is zero (nothing
    to recognize — e.g. a point-in-time obligation before transfer).

    Period-lock honored: a recognition into a HARD-LOCKED period is refused via
    ``assert_period_writable`` (409 ``period_locked``) — never posted.

    Debits == credits by construction (the signed-line helper + the balance
    assertion). The engine has already validated the number; this seam only
    presents it.
    """
    if row.proposed_action_id is not None or row.status != "DRAFT":
        logger.info(
            f"[JEBuilder] Skipping RevenueRecognitionRow {row.id}: already proposed "
            f"(proposed_action_id={row.proposed_action_id}, status={row.status})"
        )
        return None

    amount = Decimal(str(row.revenue_recognized_this_period))
    if amount == 0:
        logger.info(
            f"[JEBuilder] Skipping RevenueRecognitionRow {row.id}: zero recognition "
            f"for {row.period} (nothing to post)"
        )
        return None

    # Period-lock: a HARD-locked period refuses the draft (raises 409). Checked
    # AFTER the idempotency guard so an already-proposed row is a clean no-op.
    from ..task_autonomy import assert_period_writable

    assert_period_writable(db, realm_id=obligation.realm_id, period=row.period)

    revenue_account = (
        obligation.revenue_account_id,
        obligation.revenue_account_name or "Revenue",
    )
    use_asset_lane = Decimal(str(row.contract_asset_bal)) > 0
    if use_asset_lane:
        debit_account = (
            obligation.contract_asset_account_id,
            obligation.contract_asset_account_name or "Contract Asset",
        )
        lane = "contract-asset"
    else:
        debit_account = (
            obligation.deferred_revenue_account_id,
            obligation.deferred_revenue_account_name or "Deferred Revenue",
        )
        lane = "deferred-revenue (contract-liability)"

    txn_date = _period_end_iso(row.period)
    memo = je_memo(f"ASC 606 revenue recognition – {obligation.name} – {row.period}")
    raw_lines = [
        # DEBIT the contract-position side (positive → DEBIT).
        _lease_line(debit_account, amount, f"Recognize revenue ({lane}): {obligation.name}"),
        # CREDIT P&L revenue (negative → CREDIT).
        _lease_line(revenue_account, -amount, f"Revenue recognized: {obligation.name}"),
    ]
    lines = [ln for ln in raw_lines if ln is not None]
    _assert_lease_lines_balanced(lines, "revenue recognition")

    reasoning = (
        f"{obligation.name}: ASC 606 revenue recognition for {row.period}. "
        f"DEBIT {debit_account[1]} / CREDIT {revenue_account[1]} ${amount:,.2f} "
        f"(period revenue = the delta of cumulative recognized). "
        f"Cumulative recognized-to-date ${Decimal(str(row.cumulative_recognized_to_date)):,.2f} "
        f"of allocated ${Decimal(str(row.allocated_price)):,.2f} "
        f"(measure of progress {Decimal(str(row.measure_of_progress_pct)) * 100:.2f}%); "
        f"remaining ${Decimal(str(row.remaining_to_recognize)):,.2f}. "
        f"Proposal-gated — always human-approved before posting. Submitted by {submitted_by}."
    )

    proposal = ProposedAction(
        **human_provenance(human_preparer_user_id),
        company_id=obligation.realm_id,
        action_type="JOURNAL_ENTRY",
        payload={
            "date": txn_date,
            "memo": memo,
            "lines": lines,
            "source_module": "accounting",
            "obligation_id": obligation.id,
            "obligation_name": obligation.name,
            "peregrine_revenue": {
                "kind": "recognition",
                "obligation_id": obligation.id,
                "row_id": row.id,
                "period": row.period,
                "lane": "asset" if use_asset_lane else "liability",
            },
        },
        reasoning=reasoning,
        confidence_score=0.99,  # computed by the engine, not predicted
        status="PENDING",
    )
    db.add(proposal)
    db.flush()

    row.proposed_action_id = proposal.id
    row.status = "PENDING_APPROVAL"
    db.commit()

    logger.info(
        f"[JEBuilder] Created REVENUE RECOGNITION ProposedAction {proposal.id} for row "
        f"{row.id} (obligation={obligation.id}, period={row.period}, amount=${amount:.2f}, "
        f"{'asset' if use_asset_lane else 'liability'} lane)"
    )
    return proposal


def build_revenue_modification_je(
    obligation,
    db: Session,
    *,
    period: str,
    catch_up,
    new_cumulative_recognized,
    use_asset_lane: bool = False,
    submitted_by: str = "system",
    human_preparer_user_id: int | None = None,
) -> ProposedAction | None:
    """AC-C4a: the contract-modification cumulative-catch-up JE for ONE
    obligation. A modification re-allocates the remaining transaction price
    (engine ``reallocate_on_modification``); the difference between the NEW
    cumulative-recognized and the recognized-to-date is a catch-up that books in
    the modification ``period``. Posted periods are UNCHANGED — the catch-up is
    surfaced as a single entry, never a silent restatement. ALWAYS PENDING.

    ``catch_up > 0`` → DR deferred revenue / CR revenue (additional recognition);
    ``catch_up < 0`` → the signed-line helper flips it to DR revenue / CR deferred
    revenue (a downward-revision reversal). Returns None when ``catch_up == 0``
    (nothing to book). Period-lock honored (``assert_period_writable``)."""
    catch = Decimal(str(catch_up))
    if catch == 0:
        logger.info(
            f"[JEBuilder] Skipping revenue modification JE for obligation "
            f"{obligation.id}: zero catch-up for {period}"
        )
        return None

    from ..task_autonomy import assert_period_writable

    assert_period_writable(db, realm_id=obligation.realm_id, period=period)

    revenue_account = (
        obligation.revenue_account_id,
        obligation.revenue_account_name or "Revenue",
    )
    if use_asset_lane:
        contra_account = (
            obligation.contract_asset_account_id,
            obligation.contract_asset_account_name or "Contract Asset",
        )
    else:
        contra_account = (
            obligation.deferred_revenue_account_id,
            obligation.deferred_revenue_account_name or "Deferred Revenue",
        )

    txn_date = _period_end_iso(period)
    memo = je_memo(f"ASC 606 modification catch-up – {obligation.name} – {period}")
    raw_lines = [
        # +catch → DEBIT the contract-liability/asset side; a negative catch flips it.
        _lease_line(contra_account, catch, f"Modification catch-up: {obligation.name}"),
        # −catch → CREDIT revenue; a negative catch flips it to a DEBIT (reversal).
        _lease_line(revenue_account, -catch, f"Modification catch-up revenue: {obligation.name}"),
    ]
    lines = [ln for ln in raw_lines if ln is not None]
    _assert_lease_lines_balanced(lines, "revenue modification")

    direction = "additional recognition" if catch > 0 else "downward-revision reversal"
    reasoning = (
        f"{obligation.name}: ASC 606 CONTRACT-MODIFICATION catch-up for {period} "
        f"({direction}). Catch-up ${catch:,.2f} = new cumulative recognized "
        f"${Decimal(str(new_cumulative_recognized)):,.2f} − prior recognized-to-date. "
        f"Posted periods are UNCHANGED; the re-allocation books as this single "
        f"catch-up entry, never a restatement. Proposal-gated — always human-approved. "
        f"Submitted by {submitted_by}."
    )

    proposal = ProposedAction(
        **human_provenance(human_preparer_user_id),
        company_id=obligation.realm_id,
        action_type="JOURNAL_ENTRY",
        payload={
            "date": txn_date,
            "memo": memo,
            "lines": lines,
            "source_module": "accounting",
            "obligation_id": obligation.id,
            "obligation_name": obligation.name,
            "peregrine_revenue": {
                "kind": "modification",
                "obligation_id": obligation.id,
                "period": period,
                "catch_up": float(catch),
                "lane": "asset" if use_asset_lane else "liability",
            },
        },
        reasoning=reasoning,
        confidence_score=0.99,  # computed by the engine, not predicted
        status="PENDING",
    )
    db.add(proposal)
    db.flush()

    logger.info(
        f"[JEBuilder] Created REVENUE MODIFICATION ProposedAction {proposal.id} for "
        f"obligation {obligation.id} (period={period}, catch_up=${catch:.2f})"
    )
    return proposal


def build_jes_for_period(
    realm_id: str,
    period: str,
    db: Session,
    human_preparer_user_id: int | None = None,
) -> list[ProposedAction]:
    """
    For a given realm + period, find all DRAFT ScheduleEntry rows and
    create ProposedAction JEs for each.
    """
    from ...models import ScheduleEntry, AccountingSchedule
    entries = db.query(ScheduleEntry).filter(
        ScheduleEntry.realm_id == realm_id,
        ScheduleEntry.period == period,
        ScheduleEntry.status == "DRAFT",
    ).all()

    proposals = []
    for entry in entries:
        schedule = db.query(AccountingSchedule).filter(
            AccountingSchedule.id == entry.schedule_id
        ).first()
        if not schedule or schedule.status != "ACTIVE":
            continue
        proposal = build_je_for_entry(entry, schedule, db, human_preparer_user_id=human_preparer_user_id)
        if proposal is not None:
            proposals.append(proposal)

    logger.info(f"[JEBuilder] Built {len(proposals)} JE proposals for {realm_id} period {period}")
    return proposals

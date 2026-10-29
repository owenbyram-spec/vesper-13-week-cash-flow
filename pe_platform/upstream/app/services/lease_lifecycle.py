# PLAN-EXEMPT: plan 20260706-cfo-grade-financial-workflows wave 3.2a-EXTRACT (Steven-ratified continue 2026-07-07) authorizes this NEW service module as its primary deliverable; the plan's surface_scope covers app/routers/leases.py and the sibling extraction target app/services/lease_service.py — this file is the split-by-concern lifecycle half (the lease_service.py 500-line cap forces the remeasure/terminate paths out here), behavior-preserving extraction of the post-recognition ASC-842 lease-lifecycle write paths.
"""lease_lifecycle.py — the callable, HTTP-free ASC 842 post-recognition write paths.

Behavior-preserving extraction of the two post-recognition lease-lifecycle write
paths out of ``app/routers/leases.py`` (activation lives in ``lease_service`` —
the 500-line cap forces the split; both files share ``LeaseValidationError``):

  * ``remeasure_lease_service``  — modification/reassessment: remeasure the
    remaining liability + ROU, draft the ALWAYS-GATED remeasurement JE (rows ≥
    effective rebuilt on EXECUTION only). Returns ``{before, after}``.
  * ``terminate_lease_service``  — derecognition: draft the ALWAYS-GATED
    termination JE (facts written on EXECUTION only). Returns
    ``{before, after, gain_loss}``.
  * ``return_to_draft_service``  — recovery for a lease whose commencement JE
    will never post: back to DRAFT, rows and measures cleared.
  * ``draft_months_service``     — on-demand monthly JE drafting (manual-policy
    leases, catch-up), every proposal PENDING.

Each service is PURE: ``db`` + ``company_id`` + the ORM row + plain params in;
plain data out. It raises the domain ``LeaseValidationError`` (from
``lease_service``) for its OWN state-machine / input guards — the router wraps
that back into the byte-identical ``HTTPException``. The ASC-842 measurement math
(``lease_engine`` PV/remeasurement + ``je_builder`` JE construction) is called
UNCHANGED — the amounts are bit-identical to the pre-extraction path.

``assert_period_writable`` (the shared HARD-lock guard) still raises its
``user_safe_http_exception`` from inside these services: it did so on this path
BEFORE the extraction too, so keeping the call is behavior-preserving —
re-wrapping it would CHANGE the emitted error envelope. It is called before any
mutation, exactly as before.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import LeaseSchedule, LeaseScheduleRow, ProposedAction
from . import lease_engine
from .accounting import je_builder
from .lease_service import LeaseValidationError, steps_json

TWO_PLACES = Decimal("0.01")
RATE_UNCHANGED_REASONS = tuple(lease_engine.RATE_UNCHANGED_REASONS)


def _live_proposal(
    db: Session, lease: LeaseSchedule, proposal_id: int | None
) -> ProposedAction | None:
    """A still-actionable prior proposal for this lease (PENDING/APPROVED),
    scoped to the lease's realm — the once-at-a-time gate for remeasure /
    terminate. Bit-identical to the pre-extraction router helper."""
    if not proposal_id:
        return None
    prior = (
        db.query(ProposedAction)
        .filter(
            ProposedAction.id == proposal_id,
            ProposedAction.company_id == lease.realm_id,
        )
        .first()
    )
    if prior and prior.status in ("PENDING", "APPROVED"):
        return prior
    return None


# ---------------------------------------------------------------------------
# remeasure — modification/reassessment (statement-impacting adjustment)
# ---------------------------------------------------------------------------

_RATE_SOURCE = (
    "Binding codification: ASC 842-10-25-8 / 25-11 and ASC 842-20-35-5; requiring "
    "an updated rate here is this implementation's interpretation of them."
)


def _validate_rate_decision(
    lease: LeaseSchedule,
    *,
    payment_change: bool,
    term_change: bool,
    new_rate: float | None,
    rate_unchanged_reason: str | None,
) -> None:
    """400 unless the request says which discount rate the remeasurement uses:
    an updated rate for any term change and for a payment change, or — for a
    payment-only reassessment the standard exempts — an explicit
    ``rate_unchanged_reason`` keeping the original rate. Never a silent default
    to the original rate."""
    if rate_unchanged_reason is not None:
        if rate_unchanged_reason not in RATE_UNCHANGED_REASONS:
            raise LeaseValidationError(
                400,
                f"rate_unchanged_reason must be one of {', '.join(RATE_UNCHANGED_REASONS)}.",
            )
        if new_rate is not None:
            raise LeaseValidationError(
                400,
                "rate_unchanged_reason keeps the existing rate — it cannot be combined "
                "with new_discount_rate_annual.",
            )
        if term_change:
            raise LeaseValidationError(
                400,
                "A change in the lease term is always remeasured at an updated discount "
                "rate; rate_unchanged_reason does not apply. " + _RATE_SOURCE,
            )
        if not payment_change:
            raise LeaseValidationError(
                400, "rate_unchanged_reason applies only to a change in the lease payments."
            )
        return
    if new_rate is not None:
        return
    if term_change:
        raise LeaseValidationError(
            400,
            f"A change in the lease term remeasures the liability at an UPDATED discount "
            f"rate: pass new_discount_rate_annual and new_rate_basis (the rate at the "
            f"remeasurement date; the original locked rate is "
            f"{Decimal(str(lease.discount_rate_annual)) * 100:.2f}%). " + _RATE_SOURCE,
        )
    if payment_change:
        raise LeaseValidationError(
            400,
            "A change in the lease payments (a modification not accounted for as a "
            "separate contract) remeasures the liability at an UPDATED discount rate: "
            "pass new_discount_rate_annual and new_rate_basis. To keep the original rate "
            "for a reassessment the standard exempts, pass rate_unchanged_reason "
            f"({' or '.join(RATE_UNCHANGED_REASONS)}). " + _RATE_SOURCE,
        )


def remeasure_lease_service(
    db: Session,
    company_id: str,
    lease: LeaseSchedule,
    *,
    effective_period: str,
    new_payment_amount: float | None = None,
    new_escalation_steps: list | None = None,
    new_term_months: int | None = None,
    new_discount_rate_annual: float | None = None,
    new_rate_basis: str | None = None,
    rate_unchanged_reason: str | None = None,
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> dict:
    """Draft the ALWAYS-GATED remeasurement JE (AC-4). A payment or term change
    remeasures the remaining liability at the UPDATED rate the caller supplies
    (``new_discount_rate_annual`` + ``new_rate_basis``, the rate determined at
    the remeasurement date); the ORIGINAL locked rate is kept only for a
    payment-only reassessment named by ``rate_unchanged_reason`` (see
    RATE_UNCHANGED_REASONS — this implementation's interpretation of ASC
    842-10-25-8 / 25-11 and 842-20-35-5). ROU adjusts by Δliability. Rows ≥
    effective_period are rebuilt ONLY when the proposal EXECUTES.

    ``new_escalation_steps`` is the plain-dict form
    ``[{"start_month": int, "payment": float}, ...]`` (the router unwraps the
    DTOs before calling). Returns ``{before, after}`` — the remeasured
    liability/ROU is statement-impacting.

    Raises ``LeaseValidationError`` for the state-machine + input guards
    (409/400) exactly as the pre-extraction handler did. ``assert_period_writable``
    raises its own 409 for a locked effective period.
    """
    from .task_autonomy import assert_period_writable

    if lease.realm_id != company_id:
        raise LeaseValidationError(404, "Lease not found")
    if lease.status != "ACTIVE":
        raise LeaseValidationError(
            409, f"Lease is {lease.status}; only an ACTIVE lease can be remeasured."
        )
    if lease.short_term_exempt:
        raise LeaseValidationError(409, "Short-term-exempt leases carry no liability to remeasure.")
    if not any(
        v is not None
        for v in (
            new_payment_amount,
            new_escalation_steps,
            new_term_months,
            new_discount_rate_annual,
        )
    ):
        raise LeaseValidationError(400, "Nothing to remeasure — pass at least one changed term.")
    if (new_discount_rate_annual is None) != (new_rate_basis is None):
        raise LeaseValidationError(
            400,
            "A rate change must be explicit AND carry its basis: pass "
            "new_discount_rate_annual and new_rate_basis together.",
        )
    _validate_rate_decision(
        lease,
        payment_change=new_payment_amount is not None or new_escalation_steps is not None,
        term_change=new_term_months is not None and int(new_term_months) != int(lease.term_months),
        new_rate=new_discount_rate_annual,
        rate_unchanged_reason=rate_unchanged_reason,
    )

    if len(effective_period) != 7 or effective_period[4] != "-":
        raise LeaseValidationError(400, "effective_period must be YYYY-MM")

    prior = _live_proposal(db, lease, lease.remeasure_proposed_action_id)
    if prior:
        raise LeaseValidationError(
            409,
            f"A remeasurement proposal (#{prior.id}, {prior.status}) is already awaiting "
            f"review for this lease. Approve or reject it first.",
        )
    pending_termination = _live_proposal(db, lease, lease.termination_proposed_action_id)
    if pending_termination:
        raise LeaseValidationError(
            409,
            f"A termination proposal (#{pending_termination.id}, {pending_termination.status}) "
            f"is awaiting review for this lease. Approve or reject it first.",
        )
    require_commencement_posted(db, lease, "remeasure")
    # A month whose JE was rejected / expired / failed is DRAFT again, not
    # "awaiting approval" — it must not block the remeasure.
    lease_engine.reconcile_dead_lease_proposals(db, lease)

    assert_period_writable(db, realm_id=lease.realm_id, period=effective_period)

    effective_index = lease_engine.month_index_for_period(
        lease.commencement_date, effective_period
    )
    new_term = int(new_term_months or lease.term_months)
    if effective_index < 1 or effective_index > new_term:
        raise LeaseValidationError(
            400,
            f"effective_period {effective_period} is outside the lease term "
            f"(months 1..{new_term} from {lease.commencement_date.isoformat()}).",
        )

    # Posted/locked periods never restate: any decided row at/after effective blocks.
    decided = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
            LeaseScheduleRow.period >= effective_period,
            LeaseScheduleRow.status.in_(("POSTED", "PENDING_APPROVAL")),
        )
        .order_by(LeaseScheduleRow.period)
        .all()
    )
    if decided:
        raise LeaseValidationError(
            409,
            f"{len(decided)} row(s) at/after {effective_period} are posted or awaiting "
            f"approval (first: {decided[0].period}, {decided[0].status}). Posted periods "
            f"are never restated — choose a later effective_period or resolve the "
            f"pending proposals first.",
        )

    # Scheduled balances entering the effective month.
    prior_row = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
            LeaseScheduleRow.period < effective_period,
        )
        .order_by(LeaseScheduleRow.period.desc())
        .first()
    )
    old_liability = Decimal(str(prior_row.ending_liability)) if prior_row else Decimal(str(lease.initial_liability or 0))
    old_rou = Decimal(str(prior_row.ending_rou)) if prior_row else Decimal(str(lease.initial_rou or 0))

    # The remeasured remaining payment stream (original 1-based indexing).
    new_terms: dict = {}
    payment_amount = Decimal(str(lease.payment_amount))
    escalation_steps = lease.escalation_steps
    if new_payment_amount is not None:
        # Flat payment from effective forward; months BEFORE effective keep
        # their original contractual amounts (encoded as a step map).
        new_flat = Decimal(str(new_payment_amount)).quantize(TWO_PLACES)
        if new_flat < 0:
            raise LeaseValidationError(400, "new_payment_amount must be >= 0")
        old_payments = lease_engine.payment_schedule(
            payment_amount=payment_amount,
            term_months=max(new_term, lease.term_months),
            escalation_steps=escalation_steps,
        )
        new_payments = [
            old_payments[t - 1] if t < effective_index else new_flat
            for t in range(1, new_term + 1)
        ]
        payment_amount = new_payments[0]
        steps = [
            {"start_month": t, "payment": str(new_payments[t - 1])}
            for t in range(2, new_term + 1)
            if new_payments[t - 1] != new_payments[t - 2]
        ]
        escalation_steps = steps or None
        new_terms["payment_amount"] = str(payment_amount)
        new_terms["escalation_steps"] = escalation_steps
    if new_escalation_steps is not None:
        escalation_steps = steps_json(new_escalation_steps, new_term)
        new_terms["escalation_steps"] = escalation_steps
    if new_term_months is not None:
        if new_term_months < effective_index:
            raise LeaseValidationError(
                400,
                f"new_term_months {new_term_months} ends before the effective month "
                f"{effective_index} — terminate the lease instead.",
            )
        new_terms["term_months"] = new_term

    rate = Decimal(str(lease.discount_rate_annual))
    if new_discount_rate_annual is not None:
        if not (0 < new_discount_rate_annual < 1):
            raise LeaseValidationError(400, "new_discount_rate_annual must be in (0, 1)")
        rate = Decimal(str(new_discount_rate_annual))
        new_terms["discount_rate_annual"] = str(rate)
        new_terms["rate_basis"] = new_rate_basis

    try:
        all_payments = lease_engine.payment_schedule(
            payment_amount=payment_amount,
            term_months=new_term,
            escalation_steps=escalation_steps,
        )
        remaining_payments = all_payments[effective_index - 1 :]
        new_liability = lease_engine.present_value(
            remaining_payments, rate, lease.payment_timing
        )
    except lease_engine.LeaseEngineError as exc:
        raise LeaseValidationError(400, str(exc)) from exc

    delta = (new_liability - old_liability).quantize(TWO_PLACES)
    if delta == 0:
        raise LeaseValidationError(
            400,
            "The remeasured liability equals the scheduled balance to the cent — "
            "there is no adjustment to book.",
        )
    new_rou = (old_rou + delta).quantize(TWO_PLACES)
    if new_rou < 0:
        raise LeaseValidationError(
            409,
            f"The liability decrease (${-delta:,.2f}) exceeds the ROU carrying amount "
            f"(${old_rou:,.2f}); the excess is a P&L gain under ASC 842-10-25-13, which "
            f"this version does not automate. Book the remeasurement manually.",
        )

    proposal = je_builder.build_lease_remeasurement_je(
        lease,
        db,
        effective_period=effective_period,
        delta_liability=delta,
        new_liability_at_effective=new_liability,
        new_rou_at_effective=new_rou,
        new_terms=new_terms,
        rate_unchanged_reason=rate_unchanged_reason,
        submitted_by=submitted_by,
        human_preparer_user_id=human_preparer_user_id,
    )
    lease.remeasure_proposed_action_id = proposal.id
    db.commit()

    return {
        "before": {"liability": float(old_liability), "rou": float(old_rou)},
        "after": {"liability": float(new_liability), "rou": float(new_rou)},
        "lease_id": lease.id,
        "proposal_id": proposal.id,
        "proposal_status": "PENDING",  # ALWAYS human-gated
        "effective_period": effective_period,
        "old_liability": float(old_liability),
        "new_liability": float(new_liability),
        "delta_liability": float(delta),
        "old_rou": float(old_rou),
        "new_rou": float(new_rou),
        "rate_used": float(rate),
        "rate_changed": new_discount_rate_annual is not None,
        "rate_unchanged_reason": rate_unchanged_reason,
        "note": (
            "Rows from the effective period forward are rebuilt when this proposal "
            "is approved and executed; a rejected proposal changes nothing."
        ),
    }


# ---------------------------------------------------------------------------
# terminate — derecognition (statement-impacting gain/loss)
# ---------------------------------------------------------------------------


def terminate_lease_service(
    db: Session,
    company_id: str,
    lease: LeaseSchedule,
    *,
    termination_date: date,
    settlement_amount: float = 0,
    settlement_account_id: str | None = None,
    settlement_account_name: str | None = None,
    gain_loss_account_id: str | None = None,
    gain_loss_account_name: str | None = None,
    skip_draft_rows: bool = False,
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> dict:
    """Draft the ALWAYS-GATED termination JE (AC-4). gain/loss = remaining
    liability − ROU net book value − settlement. The lease flips TERMINATED
    (and facts are written) ONLY when the proposal EXECUTES.

    Returns ``{before, after, gain_loss}`` — the derecognition gain/loss is
    statement-impacting.

    Raises ``LeaseValidationError`` for the state-machine + input guards
    (409/400) exactly as the pre-extraction handler did. ``assert_period_writable``
    raises its own 409 for a locked termination period.
    """
    from .schedule_engine import _resolve_account_id
    from .task_autonomy import assert_period_writable

    if lease.realm_id != company_id:
        raise LeaseValidationError(404, "Lease not found")
    if lease.status == "TERMINATED":
        raise LeaseValidationError(409, "This lease is already terminated.")
    if lease.status != "ACTIVE":
        raise LeaseValidationError(
            409, f"Lease is {lease.status}; only an ACTIVE lease can be terminated."
        )
    if lease.short_term_exempt:
        raise LeaseValidationError(
            409,
            "Short-term-exempt leases carry no liability/ROU — no termination JE is "
            "needed; mark the lease terminated by editing it once the term lapses.",
        )
    if settlement_amount < 0:
        raise LeaseValidationError(400, "settlement_amount must be >= 0")

    prior = _live_proposal(db, lease, lease.termination_proposed_action_id)
    if prior:
        raise LeaseValidationError(
            409,
            f"A termination proposal (#{prior.id}, {prior.status}) is already awaiting "
            f"review for this lease. Approve or reject it first.",
        )
    pending_remeasure = _live_proposal(db, lease, lease.remeasure_proposed_action_id)
    if pending_remeasure:
        raise LeaseValidationError(
            409,
            f"A remeasurement proposal (#{pending_remeasure.id}, {pending_remeasure.status}) "
            f"is awaiting review for this lease. Approve or reject it first.",
        )
    require_commencement_posted(db, lease, "terminate")
    # A month whose JE was rejected / expired / failed is DRAFT again (and is
    # covered by skip_draft_rows), not "awaiting approval".
    lease_engine.reconcile_dead_lease_proposals(db, lease)

    termination_period = termination_date.strftime("%Y-%m")
    assert_period_writable(db, realm_id=lease.realm_id, period=termination_period)

    rows = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
        )
        .order_by(LeaseScheduleRow.period)
        .all()
    )
    # The JE derecognizes the SCHEDULED balances entering the termination
    # month; they equal the booked balances only once every earlier month
    # has posted. An unbooked earlier month is refused, never assumed.
    unbooked = [r for r in rows if r.period < termination_period and r.status != "POSTED"]
    if unbooked:
        raise LeaseValidationError(
            409,
            f"{len(unbooked)} lease month(s) before {termination_period} are not booked "
            f"yet (periods: {', '.join(r.period for r in unbooked[:6])}"
            f"{', …' if len(unbooked) > 6 else ''}). The termination derecognizes the "
            f"balances entering {termination_period}, which match the books only once "
            f"every earlier month has posted — draft them (POST /leases/{lease.id}/"
            f"draft-months), approve and post them, then terminate.",
        )
    unposted = [
        r for r in rows
        if r.period >= termination_period and r.status in ("DRAFT", "PENDING_APPROVAL")
    ]
    pending = [r for r in unposted if r.status == "PENDING_APPROVAL"]
    drafts = [r for r in unposted if r.status == "DRAFT"]
    if pending:
        raise LeaseValidationError(
            409,
            f"{len(pending)} lease month(s) at/after {termination_period} are awaiting "
            f"approval (periods: {', '.join(sorted(r.period for r in pending)[:6])}). "
            f"Approve or reject those proposals before terminating.",
        )
    if drafts and not skip_draft_rows:
        raise LeaseValidationError(
            409,
            f"{len(drafts)} draft lease month(s) remain at/after {termination_period}. "
            f"Re-send with skip_draft_rows=true to skip them when the termination "
            f"executes (no lease cost is booked for the termination month onward), or "
            f"resolve them first.",
        )
    settlement = Decimal(str(settlement_amount)).quantize(TWO_PLACES)
    if settlement > 0 and not settlement_account_id:
        raise LeaseValidationError(
            409,
            "settlement_account_id is required when settlement_amount > 0 — the "
            "account the exit payment was (or will be) paid from.",
        )

    gl_id = gain_loss_account_id
    gl_name = gain_loss_account_name or "Gain/Loss on Lease Termination"
    if not gl_id:
        gl_id = _resolve_account_id(db, lease.realm_id, gl_name)
    if not gl_id:
        raise LeaseValidationError(
            409,
            f"Could not resolve a '{gl_name}' account in this company's chart of "
            f"accounts. Create it in QuickBooks (an Other Income/Expense account) or "
            f"pass gain_loss_account_id explicitly.",
        )

    # All 409 guards passed. The drafts stay DRAFT: they are SKIPPED only when
    # the termination EXECUTES (_finalize_termination), so a rejected
    # termination leaves every month bookable. While this proposal is live the
    # beat and month drafting skip the months it covers
    # (lease_engine.live_termination_period).

    # Balances entering the termination month = the last scheduled row before it.
    before = [r for r in rows if r.period < termination_period]
    liability_remaining = (
        Decimal(str(before[-1].ending_liability)) if before else Decimal(str(lease.initial_liability or 0))
    )
    rou_net = (
        Decimal(str(before[-1].ending_rou)) if before else Decimal(str(lease.initial_rou or 0))
    )
    # Gross = commencement ROU + every executed remeasurement Δ (the
    # remeasurement JE posts Δ to the gross ROU account), so both the gross
    # and the accumulated-amortization accounts unwind to zero.
    rou_gross = lease_engine.gross_rou_booked(db, lease)
    accum_rou = (rou_gross - rou_net).quantize(TWO_PLACES)

    proposal = je_builder.build_lease_termination_je(
        lease,
        db,
        termination_date=termination_date,
        liability_remaining=liability_remaining,
        rou_gross=rou_gross,
        accum_rou=accum_rou,
        settlement=settlement,
        settlement_account=(
            (str(settlement_account_id), settlement_account_name or "")
            if settlement > 0
            else None
        ),
        gain_loss_account=(str(gl_id), gl_name),
        submitted_by=submitted_by,
        human_preparer_user_id=human_preparer_user_id,
    )
    lease.termination_proposed_action_id = proposal.id
    db.commit()

    marker = proposal.payload["peregrine_lease"]
    return {
        "before": {
            "status": lease.status,
            "liability_remaining": marker["liability_remaining"],
            "rou_net_book_value": marker["rou_net_book_value"],
        },
        "after": {"status": "ACTIVE (TERMINATED on execution)"},
        "gain_loss": marker["gain_loss"],
        "lease_id": lease.id,
        "proposal_id": proposal.id,
        "proposal_status": "PENDING",  # ALWAYS human-gated
        "termination_date": termination_date.isoformat(),
        "liability_remaining": marker["liability_remaining"],
        "rou_net_book_value": marker["rou_net_book_value"],
        "settlement": marker["settlement"],
        # The DRAFT months at/after the termination month that execution skips.
        "draft_rows_skipped": len(drafts),
        "note": (
            "The lease is marked TERMINATED (and remaining months skipped) only when "
            "this proposal is approved and executed; a rejected proposal changes nothing."
        ),
    }


# ---------------------------------------------------------------------------
# return to DRAFT — recovery when the commencement JE will never post
# ---------------------------------------------------------------------------


def require_commencement_posted(db: Session, lease: LeaseSchedule, action: str) -> None:
    """409 unless the lease's commencement JE has EXECUTED: a remeasurement or
    termination moves ROU / liability balances that exist only once the
    commencement JE is on the books."""
    state = lease_engine.commencement_state(db, lease)
    if state == lease_engine.COMMENCEMENT_POSTED:
        return
    pid = lease.commencement_proposed_action_id
    if state == lease_engine.COMMENCEMENT_PENDING:
        detail = (
            f"The commencement JE (proposal #{pid}) has not posted yet — approve it "
            f"before you {action} this lease (the ROU asset and liability are not on "
            f"the books until it executes)."
        )
    else:
        cause = (
            f"proposal #{pid} was rejected, expired or failed"
            if pid
            else "no commencement proposal is on record"
        )
        detail = (
            f"The commencement JE for this lease was never booked ({cause}). Return "
            f"the lease to DRAFT (POST /leases/{lease.id}/return-to-draft), correct it "
            f"and activate it again."
        )
    raise LeaseValidationError(409, detail)


def return_to_draft_service(
    db: Session,
    company_id: str,
    lease: LeaseSchedule,
    *,
    actor: str,
) -> dict:
    """Return an ACTIVE lease whose commencement JE will never post (REJECTED /
    EXPIRED / FAILED, or none on record) to DRAFT: its persisted rows and
    commencement measures are cleared so the terms can be corrected and the
    lease activated again (which drafts a fresh commencement JE).

    Refused (409) while the commencement JE is still live or once it posted,
    when any lease month was posted (those JEs are in the books and must be
    reversed there first — nothing booked is silently discarded), and while
    another lease proposal is still awaiting review."""
    from . import audit

    if lease.realm_id != company_id:
        raise LeaseValidationError(404, "Lease not found")
    if lease.status != "ACTIVE":
        raise LeaseValidationError(
            409,
            f"Lease is {lease.status}; only an ACTIVE lease whose commencement JE was "
            f"never booked can return to DRAFT.",
        )
    if lease.short_term_exempt:
        raise LeaseValidationError(
            409, "Short-term-exempt leases have no commencement JE to re-draft."
        )

    pid = lease.commencement_proposed_action_id
    state = lease_engine.commencement_state(db, lease)
    if state == lease_engine.COMMENCEMENT_POSTED:
        raise LeaseValidationError(
            409,
            f"The commencement JE (proposal #{pid}) is posted — a booked lease never "
            f"returns to DRAFT. Use remeasure or terminate.",
        )
    if state == lease_engine.COMMENCEMENT_PENDING:
        raise LeaseValidationError(
            409,
            f"The commencement JE (proposal #{pid}) is still awaiting review. Reject it "
            f"first if the lease terms are wrong.",
        )

    lease_engine.reconcile_dead_lease_proposals(db, lease)
    rows = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
        )
        .order_by(LeaseScheduleRow.period)
        .all()
    )
    posted = [r for r in rows if r.status == "POSTED"]
    if posted:
        raise LeaseValidationError(
            409,
            f"{len(posted)} lease month(s) were posted against this lease although its "
            f"commencement JE never was (first: {posted[0].period}). Reverse those JEs "
            f"in QuickBooks before re-drafting — posted periods are never discarded here.",
        )
    pending = [r for r in rows if r.status == "PENDING_APPROVAL"]
    if pending:
        raise LeaseValidationError(
            409,
            f"{len(pending)} lease month(s) are awaiting approval (first: "
            f"{pending[0].period}). Reject those proposals before re-drafting.",
        )
    for label, prior_id in (
        ("remeasurement", lease.remeasure_proposed_action_id),
        ("termination", lease.termination_proposed_action_id),
    ):
        prior = _live_proposal(db, lease, prior_id)
        if prior:
            raise LeaseValidationError(
                409,
                f"A {label} proposal (#{prior.id}, {prior.status}) is still awaiting "
                f"review. Reject it before re-drafting.",
            )

    commencement_status = (
        lease_engine.proposal_statuses(db, lease.realm_id, [pid]).get(int(pid)) if pid else None
    )
    for r in rows:
        db.delete(r)
    lease.status = "DRAFT"
    lease.initial_liability = None
    lease.initial_rou = None
    lease.commencement_proposed_action_id = None
    audit.log_action(
        db=db,
        realm_id=lease.realm_id,
        action="LEASE_RETURNED_TO_DRAFT",
        entity_type="lease_schedule",
        entity_id=str(lease.id),
        details={
            "commencement_proposal_id": pid,
            "commencement_status": commencement_status,
            "rows_cleared": len(rows),
            "actor": actor,
        },
    )
    db.commit()

    return {
        "before": {
            "status": "ACTIVE",
            "commencement_proposal_id": pid,
            "commencement_status": commencement_status,
            "rows": len(rows),
        },
        "after": {"status": "DRAFT", "rows": 0},
        "lease_id": lease.id,
        "status": "DRAFT",
        "rows_cleared": len(rows),
        "note": (
            "Nothing was booked for this lease. Its terms are editable again; activate "
            "it to measure it and draft a new commencement JE."
        ),
    }


# ---------------------------------------------------------------------------
# draft months — the human-triggered monthly JE drafting
# ---------------------------------------------------------------------------


def _last_closed_period(today: date | None = None) -> str:
    today = today or date.today()
    first = date(today.year, today.month, 1)
    prior = date(first.year - 1, 12, 1) if first.month == 1 else date(first.year, first.month - 1, 1)
    return f"{prior.year}-{prior.month:02d}"


def draft_months_service(
    db: Session,
    company_id: str,
    lease: LeaseSchedule,
    *,
    through_period: str | None = None,
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> dict:
    """Draft the lease's monthly JE proposals for every still-DRAFT month up to
    ``through_period`` (default: the last closed month) — the on-demand path
    for a ``manual``-policy lease (the beat skips those) and for catching up a
    lease by hand. Every proposal stays PENDING for a human (this path never
    auto-posts), whatever the lease's automation policy.

    Refused (409) unless the lease is ACTIVE, capitalized and its commencement
    JE is booked; a future month (400) is not incurred yet. Months a pending
    termination covers and HARD-locked months are skipped and reported."""
    if lease.realm_id != company_id:
        raise LeaseValidationError(404, "Lease not found")
    if lease.status != "ACTIVE":
        raise LeaseValidationError(
            409, f"Lease is {lease.status}; only an ACTIVE lease has months to draft."
        )
    if lease.short_term_exempt:
        raise LeaseValidationError(
            409, "Short-term-exempt leases have no schedule — rent is expensed as incurred."
        )
    through = through_period or _last_closed_period()
    if len(through) != 7 or through[4] != "-" or not (through[:4] + through[5:]).isdigit():
        raise LeaseValidationError(400, "through_period must be YYYY-MM")
    current = date.today().strftime("%Y-%m")
    if through > current:
        raise LeaseValidationError(
            400,
            f"through_period {through} is in the future — a month's lease cost is "
            f"drafted once the month has begun (latest allowed: {current}).",
        )
    require_commencement_posted(db, lease, "draft monthly JEs for")

    result = lease_engine.draft_lease_months(
        db,
        lease,
        through_period=through,
        submitted_by=submitted_by,
        human_preparer_user_id=human_preparer_user_id,
    )
    return {
        "lease_id": lease.id,
        "through_period": through,
        "drafted": [
            {"period": d["period"], "proposal_id": d["proposal_id"]} for d in result["drafted"]
        ],
        "skipped": result["skipped"],
        "proposal_status": "PENDING",  # never auto-posted from this path
        "note": (
            "Each drafted month is a PENDING journal-entry proposal awaiting approval; "
            "months already proposed, posted or skipped are left alone."
        ),
    }

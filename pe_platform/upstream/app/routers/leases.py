"""
leases.py — REST API for ASC 842 lease schedules (plan 20260702-phase-c-asc842,
Wave 1 AC-4/AC-5).

Endpoints (all tenant-guarded exactly like accounting_schedules: mount-level
global_tenant_guard defense-in-depth + the LOAD-BEARING in-handler
check_tenant_access / check_entity_tenant_access re-assert):

  POST /leases                      Create (DRAFT — terms editable, nothing recognized)
  GET  /leases                      List (filterable by status/classification)
  GET  /leases/disclosures          WADR, weighted remaining term, maturity ladder, class split
  GET  /leases/tie-out              Liability + ROU NBV per account group vs QBO (balance_check shape)
  GET  /leases/{id}                 Detail + amortization rows
  PUT  /leases/{id}                 DRAFT: edit terms · ACTIVE: automation_policy only
  POST /leases/{id}/activate        Measure (PV/ROU), persist rows, draft the ALWAYS-GATED commencement JE
  POST /leases/{id}/return-to-draft ACTIVE → DRAFT when the commencement JE was rejected/expired/failed
  POST /leases/{id}/draft-months    Draft every still-DRAFT month up to a period (manual policy / catch-up)
  POST /leases/{id}/remeasure       Draft the ALWAYS-GATED remeasurement JE (rows rebuilt on EXECUTION)
  POST /leases/{id}/terminate       Draft the ALWAYS-GATED termination JE (facts written on EXECUTION)

The three lifecycle JEs are proposal-gated REGARDLESS of automation_policy —
only plain unremeasured monthly rows are ever auto-post eligible (the beat's
predicate in app/tasks/schedules.py).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional

# PLAN-EXEMPT: chunk C2 of ratified plan 20260714-wave2-7b-lease-extraction (marker RATIFICATION-2026-07-14.md)
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..auth import get_current_user_from_token as get_current_user
from ..db import get_db
from ..dependencies import check_entity_tenant_access, check_tenant_access
from ..models import CompanyAgreement, LeaseSchedule, LeaseScheduleRow, User
from ..rate_limit import limiter
from ..services import agreement_extractor, lease_engine, lease_lifecycle, lease_service
from ..services.errors import safe_error_detail, user_safe_http_exception
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leases", tags=["Leases"])

AutomationPolicy = Literal["manual", "auto_propose", "auto_post_deterministic"]
Classification = Literal["operating", "finance"]
PaymentTiming = Literal["advance", "arrears"]
RateBasis = Literal["ibr", "risk_free"]

TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class EscalationStep(BaseModel):
    start_month: int          # 1-based lease month the new payment starts
    payment: float            # the fixed payment from that month forward


class ClassificationCriteria(BaseModel):
    """The five ASC 842-10-25-2 finance criteria — stored answers. ANY true
    answer requires classification == finance (enforced on create/update)."""
    transfers_ownership: bool = False
    purchase_option_reasonably_certain: bool = False
    term_major_part_of_life: bool = False
    pv_substantially_all_fair_value: bool = False
    specialized_asset: bool = False


class LeaseCreate(BaseModel):
    realm_id: str
    name: str
    description: Optional[str] = None
    classification: Classification
    classification_criteria: Optional[ClassificationCriteria] = None
    commencement_date: date
    term_months: int
    payment_amount: float
    payment_timing: PaymentTiming = "arrears"
    escalation_steps: Optional[List[EscalationStep]] = None
    discount_rate_annual: float          # e.g. 0.06 for 6%
    rate_basis: RateBasis = "ibr"
    initial_direct_costs: float = 0
    incentives: float = 0
    prepaid_amount: float = 0
    short_term_exempt: bool = False
    automation_policy: AutomationPolicy = "auto_propose"
    # Accounts (required at ACTIVATE, not create — the wizard sets them last)
    rou_asset_account_id: Optional[str] = None
    rou_asset_account_name: Optional[str] = None
    accum_rou_amort_account_id: Optional[str] = None
    accum_rou_amort_account_name: Optional[str] = None
    lease_liability_account_id: Optional[str] = None
    lease_liability_account_name: Optional[str] = None
    interest_expense_account_id: Optional[str] = None
    interest_expense_account_name: Optional[str] = None
    lease_expense_account_id: Optional[str] = None
    lease_expense_account_name: Optional[str] = None
    rou_amort_expense_account_id: Optional[str] = None
    rou_amort_expense_account_name: Optional[str] = None


class LeaseUpdate(BaseModel):
    # DRAFT-only term edits (the rate is locked at inception = activation;
    # while DRAFT it may still be corrected).
    name: Optional[str] = None
    description: Optional[str] = None
    classification: Optional[Classification] = None
    classification_criteria: Optional[ClassificationCriteria] = None
    commencement_date: Optional[date] = None
    term_months: Optional[int] = None
    payment_amount: Optional[float] = None
    payment_timing: Optional[PaymentTiming] = None
    escalation_steps: Optional[List[EscalationStep]] = None
    discount_rate_annual: Optional[float] = None
    rate_basis: Optional[RateBasis] = None
    initial_direct_costs: Optional[float] = None
    incentives: Optional[float] = None
    prepaid_amount: Optional[float] = None
    short_term_exempt: Optional[bool] = None
    # Editable at ANY status:
    automation_policy: Optional[AutomationPolicy] = None
    rou_asset_account_id: Optional[str] = None
    rou_asset_account_name: Optional[str] = None
    accum_rou_amort_account_id: Optional[str] = None
    accum_rou_amort_account_name: Optional[str] = None
    lease_liability_account_id: Optional[str] = None
    lease_liability_account_name: Optional[str] = None
    interest_expense_account_id: Optional[str] = None
    interest_expense_account_name: Optional[str] = None
    lease_expense_account_id: Optional[str] = None
    lease_expense_account_name: Optional[str] = None
    rou_amort_expense_account_id: Optional[str] = None
    rou_amort_expense_account_name: Optional[str] = None


class ActivateRequest(BaseModel):
    # Source accounts for the commencement JE's IDC / prepaid / incentive
    # lines — each REQUIRED (409) when its amount on the lease is > 0.
    idc_source_account_id: Optional[str] = None
    idc_source_account_name: Optional[str] = None
    prepaid_source_account_id: Optional[str] = None
    prepaid_source_account_name: Optional[str] = None
    incentive_source_account_id: Optional[str] = None
    incentive_source_account_name: Optional[str] = None


class RemeasureRequest(BaseModel):
    effective_period: str                          # YYYY-MM, first rebuilt month
    new_payment_amount: Optional[float] = None     # flat payment from effective forward
    new_escalation_steps: Optional[List[EscalationStep]] = None  # replaces the step map (original 1-based months)
    new_term_months: Optional[int] = None
    # A payment or term change is remeasured at the UPDATED rate (with its
    # basis). The existing rate is kept only for a payment-only reassessment
    # named by rate_unchanged_reason — this implementation's interpretation of
    # ASC 842-10-25-8 / 25-11 and 842-20-35-5 (binding codification).
    new_discount_rate_annual: Optional[float] = None
    new_rate_basis: Optional[RateBasis] = None
    rate_unchanged_reason: Optional[Literal["residual_value_guarantee", "contingency_resolution"]] = None


class TerminateRequest(BaseModel):
    termination_date: date
    settlement_amount: float = 0                   # cash paid to exit (>= 0)
    settlement_account_id: Optional[str] = None    # required when settlement > 0
    settlement_account_name: Optional[str] = None
    gain_loss_account_id: Optional[str] = None
    gain_loss_account_name: Optional[str] = None
    skip_draft_rows: bool = False                  # skip (not delete) DRAFT months ≥ termination on EXECUTION


class DraftMonthsRequest(BaseModel):
    # Draft every still-DRAFT month up to and including this one (YYYY-MM);
    # omitted = the last closed month.
    through_period: Optional[str] = None


class LeaseRowOut(BaseModel):
    id: int
    period: str
    payment: float
    interest: float
    liability_reduction: float
    ending_liability: float
    rou_amortization: float
    ending_rou: float
    expense: float
    remeasured: bool
    status: str
    proposed_action_id: Optional[int]
    posted_at: Optional[datetime]

    # Pydantic v2 key (the v1 ``orm_mode`` is ignored, which made from_orm —
    # and so GET /leases/{id} — fail).
    model_config = ConfigDict(from_attributes=True)


class LeaseOut(BaseModel):
    id: int
    realm_id: str
    name: str
    description: Optional[str]
    classification: str
    classification_criteria: Optional[dict]
    commencement_date: date
    term_months: int
    payment_amount: float
    payment_frequency: str
    payment_timing: str
    escalation_steps: Optional[list]
    discount_rate_annual: float
    rate_basis: str
    initial_direct_costs: float
    incentives: float
    prepaid_amount: float
    short_term_exempt: bool
    initial_liability: Optional[float]
    initial_rou: Optional[float]
    rou_asset_account_id: Optional[str]
    rou_asset_account_name: Optional[str]
    accum_rou_amort_account_id: Optional[str]
    accum_rou_amort_account_name: Optional[str]
    lease_liability_account_id: Optional[str]
    lease_liability_account_name: Optional[str]
    interest_expense_account_id: Optional[str]
    interest_expense_account_name: Optional[str]
    lease_expense_account_id: Optional[str]
    lease_expense_account_name: Optional[str]
    rou_amort_expense_account_id: Optional[str]
    rou_amort_expense_account_name: Optional[str]
    automation_policy: str
    status: str
    commencement_proposed_action_id: Optional[int]
    remeasure_proposed_action_id: Optional[int]
    termination_proposed_action_id: Optional[int]
    termination_date: Optional[date]
    termination_settlement: Optional[float]
    termination_gain_loss: Optional[float]
    created_at: datetime
    # Status of each linked lifecycle proposal (PENDING / APPROVED / EXECUTED /
    # REJECTED / EXPIRED / FAILED), filled by GET /leases and GET /leases/{id}:
    # a lease is booked only once its commencement proposal EXECUTED.
    commencement_status: Optional[str] = None
    remeasure_status: Optional[str] = None
    termination_status: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
# The former _steps_json / _validate_classification / _validate_terms router
# adapters were removed here: POST /leases now calls lease_service.create_lease_service,
# which owns the ASC-842 term/classification/escalation validation directly and raises
# LeaseValidationError (the endpoint maps it to HTTPException). The service-level
# steps_json / validate_classification / validate_terms remain the one canonical home.


def _with_proposal_statuses(db: Session, realm_id: str, leases: list) -> list[LeaseOut]:
    """LeaseOut rows carrying the status of each linked lifecycle proposal
    (one realm-scoped lookup for the whole list)."""
    statuses = lease_engine.proposal_statuses(
        db,
        realm_id,
        [
            pid
            for lease in leases
            for pid in (
                lease.commencement_proposed_action_id,
                lease.remeasure_proposed_action_id,
                lease.termination_proposed_action_id,
            )
        ],
    )
    return [
        LeaseOut.model_validate(lease).model_copy(
            update={
                "commencement_status": statuses.get(lease.commencement_proposed_action_id),
                "remeasure_status": statuses.get(lease.remeasure_proposed_action_id),
                "termination_status": statuses.get(lease.termination_proposed_action_id),
            }
        )
        for lease in leases
    ]


def _get_lease(db: Session, user: User, lease_id: int, role: str) -> LeaseSchedule:
    """The row-resolves-tenant gate (§3.2 multi-tenancy-app-layer) — the
    load-bearing in-handler re-assert for every {id} route."""
    return check_entity_tenant_access(
        role,
        db=db,
        user=user,
        model=LeaseSchedule,
        entity_id=lease_id,
        tenant_attr="realm_id",
        not_found_detail="Lease not found",
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
@router.post("", response_model=LeaseOut)
def create_lease(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + the in-body check_tenant_access("EDITOR", company_id=body.realm_id); the hook's AUTH_DEP_RE omits this alias
    body: LeaseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # The EDITOR tenant gate stays in the router (byte-identical): it validates the
    # request-supplied realm against the user's memberships. The lease build + ASC-842
    # validation live in create_lease_service (shared with the create_lease agent tool);
    # the service stamps realm_id from this trusted realm, never a payload value.
    check_tenant_access("EDITOR", company_id=body.realm_id, db=db, user=user)
    try:
        return lease_service.create_lease_service(
            db, body.realm_id, body.dict(), created_by=user.email
        )
    except lease_service.LeaseValidationError as e:
        raise HTTPException(e.status_code, e.detail) from e


# PLAN-EXEMPT: chunk C2 of ratified plan 20260714-wave2-7b-lease-extraction (marker RATIFICATION-2026-07-14.md)
class LeaseFromAgreementRequest(BaseModel):
    """User-REVIEWED lease terms — the confirm-endpoint body (mirror of the loan
    LoanTrancheFromAgreementRequest). The human confirms these DEFAULTS from the
    extraction draft; the mapper turns them into a create_lease_service body.
    NEVER carries realm_id — the realm flows from the EDITOR-authorized source
    agreement, never from the request body (Control-5: server scope wins)."""
    agreement_id: int
    name: str | None = None
    description: str | None = None
    classification: Classification | None = None
    classification_criteria: ClassificationCriteria | None = None
    commencement_date: date | None = None
    term_months: int | None = None
    payment_amount: float | None = None          # the LEASE-COMPONENT payment (net of non-lease)
    payment_timing: PaymentTiming | None = None
    escalation_steps: list[EscalationStep] | None = None
    discount_rate_annual: float | None = None    # annual DECIMAL in (0,1); a percent-form is rejected downstream
    rate_basis: RateBasis | None = None
    initial_direct_costs: float | None = None
    incentives: float | None = None
    prepaid_amount: float | None = None
    short_term_exempt: bool | None = None
    non_lease_components: list[dict] | None = None  # reviewer-facing evidence (E2); NOT mapped to a create field


@router.post("/confirm-from-agreement", response_model=LeaseOut)  # sensitive: "confirm" agreement→lease write path
@limiter.limit("10/minute")  # LLM-extraction → balance-sheet write is a real cost/abuse vector; deliberate human confirm
def confirm_lease_from_agreement(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + check_entity_tenant_access("EDITOR", CompanyAgreement, ...); the hook's AUTH_DEP_RE omits this alias
    request: Request,  # required so the SlowAPI limiter engages on this route (AP-OBS-4)
    body: LeaseFromAgreementRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Human-in-the-loop confirm: create a DRAFT LeaseSchedule from REVIEWED lease terms.
    This is the ONLY path that turns LLM-extracted lease terms into a lease on the books —
    extraction NEVER writes a lease (agentic-writeback-safety: the model proposes, the human
    confirms). Same-principal authorization: the realm flows from the EDITOR-authorized source
    CompanyAgreement, never from the request body. create_lease_service's validate_terms /
    validate_classification back-stop any mis-extraction (a percent-form rate is REJECTED, a
    TRUE ASC-842 criterion forces finance) — server-side, regardless of what was confirmed."""
    # 1. Authorize the source agreement against the SAME principal (EDITOR). Tenant
    #    identity is session-derived — the lease is created for the agreement's realm.
    agreement = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=user,
        model=CompanyAgreement,
        entity_id=body.agreement_id,
        not_found_detail="Agreement not found",
    )

    # 2. Map REVIEWED terms → a create_lease_service body; a missing required basis → 400
    #    (no fabricated basis). realm_id is NEVER read from the body.
    reviewed = body.model_dump(
        include={
            "name", "description", "classification", "classification_criteria",
            "commencement_date", "term_months", "payment_amount", "payment_timing",
            "escalation_steps", "discount_rate_annual", "rate_basis",
            "initial_direct_costs", "incentives", "prepaid_amount", "short_term_exempt",
            "non_lease_components",
        }
    )
    try:
        data = agreement_extractor.lease_terms_to_create_fields(reviewed)
    except ValueError as e:
        bad_terms = user_safe_http_exception(
            status_code=400,
            error_code="invalid_lease_terms",
            user_message=str(e),
            log_context="lease_confirm_invalid_terms",
        )
        raise bad_terms from e

    # 3. The ONLY agreement→lease write — server-side ASC-842 validation back-stops the
    #    confirmed draft (validate_terms rejects a percent-form rate; validate_classification
    #    forces finance on any TRUE criterion). realm_id stamped from the trusted agreement realm.
    try:
        return lease_service.create_lease_service(
            db, agreement.company_id, data, created_by=user.email
        )
    except lease_service.LeaseValidationError as e:
        invalid = user_safe_http_exception(
            status_code=e.status_code,
            error_code="invalid_lease_terms",
            user_message=e.detail,
            log_context="lease_confirm_validation",
        )
        raise invalid from e


@router.get("", response_model=List[LeaseOut])
def list_leases(
    realm_id: str = Query(...),
    status: Optional[str] = Query(None),
    classification: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=user)
    q = db.query(LeaseSchedule).filter(LeaseSchedule.realm_id == realm_id)
    if status:
        q = q.filter(LeaseSchedule.status == status.upper())
    if classification:
        q = q.filter(LeaseSchedule.classification == classification.lower())
    return _with_proposal_statuses(db, realm_id, q.order_by(LeaseSchedule.id).all())


# ---------------------------------------------------------------------------
# AC-5 — disclosures + tie-out (declared BEFORE /{lease_id})
# ---------------------------------------------------------------------------


@router.get("/disclosures")
def lease_disclosures(
    realm_id: str = Query(...),
    as_of: Optional[str] = Query(None, description="YYYY-MM; default = the last closed month"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """ASC 842 disclosure data (AC-5): liability-weighted average discount
    rate, liability-weighted average remaining term, the Y1..Y5 + thereafter
    undiscounted maturity ladder, and the operating/finance split. Measured
    from the persisted rows as of the END of ``as_of`` (scheduled balances).
    Short-term-exempt leases carry no liability/ROU and are reported as a
    count only (expense-as-incurred)."""
    from ..tasks.schedules import period_for_date

    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=user)
    as_of = as_of or period_for_date()
    if len(as_of) != 7 or as_of[4] != "-":
        raise HTTPException(400, "as_of must be YYYY-MM")

    leases = (
        db.query(LeaseSchedule)
        .filter(
            LeaseSchedule.realm_id == realm_id,
            LeaseSchedule.status.in_(("ACTIVE", "EXPIRED")),
        )
        .all()
    )

    ladder = [Decimal(0)] * 5
    thereafter = Decimal(0)
    weighted_rate_num = Decimal(0)
    weighted_term_num = Decimal(0)
    total_liability = Decimal(0)
    split = {
        "operating": {"count": 0, "liability": Decimal(0), "rou_nbv": Decimal(0)},
        "finance": {"count": 0, "liability": Decimal(0), "rou_nbv": Decimal(0)},
    }
    exempt_count = 0
    per_lease = []

    for lease in leases:
        if lease.short_term_exempt:
            exempt_count += 1
            continue
        rows = (
            db.query(LeaseScheduleRow)
            .filter(
                LeaseScheduleRow.lease_id == lease.id,
                LeaseScheduleRow.realm_id == realm_id,
            )
            .order_by(LeaseScheduleRow.period)
            .all()
        )
        if not rows:
            continue
        at_or_before = [r for r in rows if r.period <= as_of]
        if at_or_before:
            liability = Decimal(str(at_or_before[-1].ending_liability))
            rou_nbv = Decimal(str(at_or_before[-1].ending_rou))
        else:
            # as_of precedes the first lease month — commencement measures.
            liability = Decimal(str(lease.initial_liability or 0))
            rou_nbv = Decimal(str(lease.initial_rou or 0))
        remaining = [r for r in rows if r.period > as_of]
        remaining_months = len(remaining)
        if liability <= 0 and not remaining:
            continue

        rate = Decimal(str(lease.discount_rate_annual))
        weighted_rate_num += liability * rate
        weighted_term_num += liability * Decimal(remaining_months)
        total_liability += liability
        bucket = split[lease.classification]
        bucket["count"] += 1
        bucket["liability"] += liability
        bucket["rou_nbv"] += rou_nbv

        for r in remaining:
            months_out = (
                (int(r.period[:4]) - int(as_of[:4])) * 12
                + int(r.period[5:7]) - int(as_of[5:7])
            )
            year_idx = (months_out - 1) // 12
            if year_idx < 5:
                ladder[year_idx] += Decimal(str(r.payment))
            else:
                thereafter += Decimal(str(r.payment))

        per_lease.append({
            "lease_id": lease.id,
            "name": lease.name,
            "classification": lease.classification,
            "discount_rate_annual": float(rate),
            "rate_basis": lease.rate_basis,
            "remaining_liability": float(liability),
            "rou_net_book_value": float(rou_nbv),
            "remaining_term_months": remaining_months,
        })

    wadr = float((weighted_rate_num / total_liability).quantize(Decimal("0.000001"))) if total_liability > 0 else None
    wart = float((weighted_term_num / total_liability).quantize(Decimal("0.01"))) if total_liability > 0 else None
    undiscounted = sum(ladder, Decimal(0)) + thereafter

    return {
        "realm_id": realm_id,
        "as_of": as_of,
        "weighted_average_discount_rate": wadr,           # liability-weighted, annual decimal
        "weighted_average_remaining_term_months": wart,   # liability-weighted
        "total_remaining_liability": float(total_liability),
        "total_undiscounted_payments": float(undiscounted),
        "imputed_interest": float(undiscounted - total_liability),
        "maturity_ladder": {
            "year_1": float(ladder[0]),
            "year_2": float(ladder[1]),
            "year_3": float(ladder[2]),
            "year_4": float(ladder[3]),
            "year_5": float(ladder[4]),
            "thereafter": float(thereafter),
        },
        "classification_split": {
            k: {
                "count": v["count"],
                "liability": float(v["liability"]),
                "rou_net_book_value": float(v["rou_nbv"]),
            }
            for k, v in split.items()
        },
        "short_term_exempt_count": exempt_count,  # expense-as-incurred; no liability/ROU
        "leases": per_lease,
    }


def _scheduled_balances_at(db: Session, realm_id: str, as_of: str) -> list[dict]:
    """Per-ACTIVE/EXPIRED-lease scheduled (liability, ROU NBV) at as_of."""
    out = []
    leases = (
        db.query(LeaseSchedule)
        .filter(
            LeaseSchedule.realm_id == realm_id,
            LeaseSchedule.status.in_(("ACTIVE", "EXPIRED")),
            LeaseSchedule.short_term_exempt.is_(False),
        )
        .all()
    )
    for lease in leases:
        rows = (
            db.query(LeaseScheduleRow)
            .filter(
                LeaseScheduleRow.lease_id == lease.id,
                LeaseScheduleRow.realm_id == realm_id,
                LeaseScheduleRow.period <= as_of,
            )
            .order_by(LeaseScheduleRow.period)
            .all()
        )
        if rows:
            liability = Decimal(str(rows[-1].ending_liability))
            rou_nbv = Decimal(str(rows[-1].ending_rou))
        else:
            first = (
                db.query(LeaseScheduleRow)
                .filter(
                    LeaseScheduleRow.lease_id == lease.id,
                    LeaseScheduleRow.realm_id == realm_id,
                )
                .order_by(LeaseScheduleRow.period)
                .first()
            )
            if first is None or first.period <= as_of:
                continue
            liability = Decimal(str(lease.initial_liability or 0))
            rou_nbv = Decimal(str(lease.initial_rou or 0))
        out.append({"lease": lease, "liability": liability, "rou_nbv": rou_nbv})
    return out


@router.get("/tie-out")
def lease_tie_out(
    realm_id: str = Query(...),
    as_of: Optional[str] = Query(None, description="YYYY-MM; default = the last closed month"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """AC-5 tie-out: scheduled lease liability + ROU NBV per QBO account
    group vs the QBO balance sheet, in the schedule-grid balance_check shape
    ({schedule_total, qbo_balance, delta, in_balance, matched_accounts,
    as_of_date, check_type}; $1 tolerance)."""
    from ..qbo_client import get_qbo_client_from_db
    from ..tasks.schedules import period_for_date

    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=user)
    as_of = as_of or period_for_date()
    if len(as_of) != 7 or as_of[4] != "-":
        raise HTTPException(400, "as_of must be YYYY-MM")

    balances = _scheduled_balances_at(db, realm_id, as_of)

    # Group by account: liability by the liability account; ROU NBV by the
    # (ROU asset, accum) pair keyed on the ROU asset account.
    liability_groups: dict[str, dict] = {}
    rou_groups: dict[str, dict] = {}
    for item in balances:
        lease = item["lease"]
        l_key = str(lease.lease_liability_account_id or "")
        lg = liability_groups.setdefault(
            l_key,
            {
                "account_id": l_key or None,
                "account_name": lease.lease_liability_account_name or "",
                "schedule_total": Decimal(0),
                "lease_count": 0,
            },
        )
        lg["schedule_total"] += item["liability"]
        lg["lease_count"] += 1
        r_key = str(lease.rou_asset_account_id or "")
        rg = rou_groups.setdefault(
            r_key,
            {
                "account_id": r_key or None,
                "account_name": lease.rou_asset_account_name or "",
                "accum_account_id": str(lease.accum_rou_amort_account_id or "") or None,
                "accum_account_name": lease.accum_rou_amort_account_name or "",
                "schedule_total": Decimal(0),
                "lease_count": 0,
            },
        )
        rg["schedule_total"] += item["rou_nbv"]
        rg["lease_count"] += 1

    try:
        import calendar as _calendar

        client = get_qbo_client_from_db(db, realm_id)
        year, month = int(as_of[:4]), int(as_of[5:7])
        end_day = _calendar.monthrange(year, month)[1]
        bs_end_date = f"{year}-{month:02d}-{end_day:02d}"
        report = client.get_report(
            "BalanceSheet",
            params={
                "accounting_method": "Accrual",
                "start_date": f"{year}-01-01",
                "end_date": bs_end_date,
                "summarize_column_by": "Month",
            },
        )
        bs_columns = report.get("Columns", {}).get("Column", [])
        target_col_idx = max(len(bs_columns) - 1, 1)
        target_label = f"{_calendar.month_abbr[month]} {year}"
        for i, bs_col in enumerate(bs_columns):
            if target_label.lower() in (bs_col.get("ColTitle", "") or "").lower():
                target_col_idx = i
                break
        bs_rows = report.get("Rows", {}).get("Row", [])

        def _balance_for(account_id: Optional[str], account_name: str) -> tuple[float, list]:
            """Recursive BS walk summing the balance of ONE account matched
            by id (preferred) or exact name (Header/Summary parent totals
            handled like the schedule-grid walk)."""
            total = 0.0
            matched: list = []
            want_name = (account_name or "").lower()

            def _match(name: str, acct_id: str) -> bool:
                if account_id and str(acct_id) == str(account_id):
                    return True
                return bool(want_name) and name == want_name

            def _walk(rows_list):
                nonlocal total
                for row in rows_list:
                    header_cols = row.get("Header", {}).get("ColData", [])
                    if len(header_cols) >= 1:
                        h_name = (header_cols[0].get("value", "") or "").lower()
                        h_id = header_cols[0].get("id", "")
                        if _match(h_name, h_id):
                            summary_cols = row.get("Summary", {}).get("ColData", [])
                            if len(summary_cols) > target_col_idx:
                                try:
                                    bal = float(summary_cols[target_col_idx].get("value", "0") or "0")
                                    total += bal
                                    matched.append({"name": header_cols[0].get("value", ""), "balance": bal})
                                except (ValueError, TypeError):
                                    pass
                            continue
                    col_data = row.get("ColData", [])
                    if len(col_data) > target_col_idx:
                        acct_name = (col_data[0].get("value", "") or "").lower()
                        acct_id = col_data[0].get("id", "")
                        if _match(acct_name, acct_id):
                            try:
                                bal = float(col_data[target_col_idx].get("value", "0") or "0")
                                total += bal
                                matched.append({"name": col_data[0].get("value", ""), "balance": bal})
                            except (ValueError, TypeError):
                                pass
                    sub_rows = row.get("Rows", {}).get("Row", [])
                    if sub_rows:
                        _walk(sub_rows)

            _walk(bs_rows)
            return round(total, 2), matched

        liability_checks = []
        for key in sorted(liability_groups):
            g = liability_groups[key]
            qbo_balance, matched = _balance_for(g["account_id"], g["account_name"])
            schedule_total = float(g["schedule_total"].quantize(TWO_PLACES))
            # A liability's QBO BS balance is positive-normal; compare magnitudes.
            delta = round(schedule_total - abs(qbo_balance), 2)
            liability_checks.append({
                "account_id": g["account_id"],
                "account_name": g["account_name"],
                "lease_count": g["lease_count"],
                "schedule_total": schedule_total,
                "qbo_balance": qbo_balance,
                "delta": delta,
                "in_balance": abs(delta) <= 1.00,
                "matched_accounts": matched,
                "check_type": "lease_liability",
            })

        rou_checks = []
        for key in sorted(rou_groups):
            g = rou_groups[key]
            rou_balance, rou_matched = _balance_for(g["account_id"], g["account_name"])
            accum_balance, accum_matched = _balance_for(g["accum_account_id"], g["accum_account_name"])
            # NBV = gross ROU + (negative) accumulated contra, signed sum.
            qbo_nbv = round(rou_balance + accum_balance, 2)
            schedule_total = float(g["schedule_total"].quantize(TWO_PLACES))
            delta = round(schedule_total - qbo_nbv, 2)
            rou_checks.append({
                "account_id": g["account_id"],
                "account_name": g["account_name"],
                "accum_account_id": g["accum_account_id"],
                "accum_account_name": g["accum_account_name"],
                "lease_count": g["lease_count"],
                "schedule_total": schedule_total,
                "qbo_balance": qbo_nbv,
                "delta": delta,
                "in_balance": abs(delta) <= 1.00,
                "matched_accounts": rou_matched + accum_matched,
                "check_type": "rou_net_book_value",
            })

        return {
            "realm_id": realm_id,
            "as_of_date": bs_end_date,
            "liability_groups": liability_checks,
            "rou_groups": rou_checks,
            "in_balance": all(c["in_balance"] for c in liability_checks + rou_checks),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(
            "Lease tie-out failed for realm %s: %s",
            realm_id,
            redact_to_string(str(e), max_length=500),
            exc_info=True,
        )
        return {
            "realm_id": realm_id,
            "as_of_date": as_of,
            "liability_groups": [],
            "rou_groups": [],
            "in_balance": False,
            "error": safe_error_detail(
                error_code="lease_tie_out_unavailable",
                user_message="The QuickBooks lease balance check could not be completed.",
                exc=e,
                retryable=True,
                log_context="lease_tie_out_unavailable",
            ),
            "data_quality": {
                "is_partial": True,
                "unavailable_sections": ["lease_tie_out_unavailable"],
            },
        }


# ---------------------------------------------------------------------------
# Detail / update
# ---------------------------------------------------------------------------


@router.get("/{lease_id}")
def get_lease(
    lease_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    lease = _get_lease(db, user, lease_id, "VIEWER")
    rows = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
        )
        .order_by(LeaseScheduleRow.period)
        .all()
    )
    return {
        "lease": _with_proposal_statuses(db, lease.realm_id, [lease])[0],
        "rows": [LeaseRowOut.model_validate(r) for r in rows],
    }


@router.put("/{lease_id}", response_model=LeaseOut)
def update_lease(
    lease_id: int,
    body: LeaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """DRAFT: terms freely editable (nothing is recognized yet). ACTIVE:
    only automation_policy and account ids/names — the measurement basis is
    LOCKED at activation; term/payment/rate changes go through the gated
    remeasure endpoint, never a PUT (rows are immutable outside it)."""
    lease = _get_lease(db, user, lease_id, "EDITOR")
    data = body.dict(exclude_unset=True)
    try:
        return lease_service.update_lease_service(db, lease.realm_id, lease, data)
    except lease_service.LeaseValidationError as e:
        raise HTTPException(e.status_code, e.detail) from e


# ---------------------------------------------------------------------------
# AC-4 — lifecycle (ALL human-gated)
# ---------------------------------------------------------------------------


@router.post("/{lease_id}/activate")
def activate_lease(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + _get_lease's check_entity_tenant_access; the hook's AUTH_DEP_RE omits this alias
    lease_id: int,
    body: ActivateRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Measure the lease at the LOCKED rate, persist the immutable
    amortization rows, and draft the commencement JE — ALWAYS proposal-gated
    regardless of automation_policy. Short-term-exempt leases activate with
    NO rows and NO JE (expense-as-incurred, documented).

    Thin caller over ``lease_service.activate_lease_service`` — the ASC-842
    PV/ROU recognition + row persistence + JE drafting live in the service; the
    domain error maps back to the byte-identical HTTPException.

    409 teaching guards: not DRAFT · locked commencement period · missing
    accounts · missing IDC/prepaid/incentive source accounts."""
    body = body or ActivateRequest()
    lease = _get_lease(db, user, lease_id, "EDITOR")
    try:
        r = lease_service.activate_lease_service(
            db,
            lease.realm_id,
            lease,
            idc_source_account_id=body.idc_source_account_id,
            idc_source_account_name=body.idc_source_account_name,
            prepaid_source_account_id=body.prepaid_source_account_id,
            prepaid_source_account_name=body.prepaid_source_account_name,
            incentive_source_account_id=body.incentive_source_account_id,
            incentive_source_account_name=body.incentive_source_account_name,
            submitted_by=user.email,
            human_preparer_user_id=user.id,
        )
    except lease_service.LeaseValidationError as e:
        raise HTTPException(e.status_code, e.detail) from e

    if r["short_term_exempt"]:
        return {
            "lease_id": r["lease_id"],
            "status": "ACTIVE",
            "short_term_exempt": True,
            "rows_generated": 0,
            "proposal_id": None,
            "note": r["note"],
        }
    return {
        "lease_id": r["lease_id"],
        "status": "ACTIVE",
        "short_term_exempt": False,
        "initial_liability": r["initial_liability"],
        "initial_rou": r["initial_rou"],
        "rows_generated": r["rows_generated"],
        "first_period": r["first_period"],
        "final_period": r["final_period"],
        "proposal_id": r["proposal_id"],
        "proposal_status": "PENDING",  # ALWAYS human-gated
    }


@router.post("/{lease_id}/return-to-draft")
def return_lease_to_draft(
    lease_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Recovery for an ACTIVE lease whose commencement JE will never post
    (rejected / expired / failed): back to DRAFT with its rows and measures
    cleared, so the terms can be corrected and the lease activated again.

    409 teaching guards: not ACTIVE · commencement still pending or posted ·
    a lease month already posted · a lease proposal still awaiting review."""
    lease = _get_lease(db, user, lease_id, "EDITOR")
    try:
        return lease_lifecycle.return_to_draft_service(
            db, lease.realm_id, lease, actor=user.email
        )
    except lease_service.LeaseValidationError as e:
        raise HTTPException(e.status_code, e.detail) from e


@router.post("/{lease_id}/draft-months")
def draft_months(
    lease_id: int,
    body: DraftMonthsRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Draft the monthly lease JE proposals for every still-DRAFT month up to
    ``through_period`` (default: the last closed month) — how a manual-policy
    lease is booked (the monthly beat skips it) and how past months are caught
    up by hand. Every drafted proposal is PENDING for a human, whatever the
    automation policy.

    409 teaching guards: not ACTIVE · short-term exempt · commencement JE not
    booked. 400: malformed or future through_period."""
    body = body or DraftMonthsRequest()
    lease = _get_lease(db, user, lease_id, "EDITOR")
    try:
        return lease_lifecycle.draft_months_service(
            db,
            lease.realm_id,
            lease,
            through_period=body.through_period,
            submitted_by=user.email,
            human_preparer_user_id=user.id,
        )
    except lease_service.LeaseValidationError as e:
        raise HTTPException(e.status_code, e.detail) from e


@router.post("/{lease_id}/remeasure")
def remeasure_lease(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + _get_lease's check_entity_tenant_access; the hook's AUTH_DEP_RE omits this alias
    lease_id: int,
    body: RemeasureRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Draft the ALWAYS-GATED remeasurement JE (AC-4). A payment or term
    change is remeasured at the UPDATED rate the request supplies (+ basis);
    the existing rate is kept only for a payment-only reassessment named by
    rate_unchanged_reason. ROU adjusts by Δliability. Rows ≥
    effective_period are rebuilt ONLY when the proposal EXECUTES — a rejected
    proposal changes nothing, and posted/locked periods are NEVER restated.

    Thin caller over ``lease_lifecycle.remeasure_lease_service`` — the ASC-842
    liability/ROU remeasurement math + JE drafting live in the service; the DTO
    escalation steps are unwrapped to plain dicts; the domain error maps back
    to the byte-identical HTTPException.

    409 teaching guards: not ACTIVE · exempt · commencement JE not booked ·
    locked effective period · posted/pending rows at/after effective ·
    remeasure or termination already pending · negative resulting ROU.
    400: no changed term · rate without basis · payment/term change without
    an updated rate (or an exempt rate_unchanged_reason)."""
    lease = _get_lease(db, user, lease_id, "EDITOR")
    steps = (
        [{"start_month": s.start_month, "payment": s.payment} for s in body.new_escalation_steps]
        if body.new_escalation_steps is not None
        else None
    )
    try:
        r = lease_lifecycle.remeasure_lease_service(
            db,
            lease.realm_id,
            lease,
            effective_period=body.effective_period,
            new_payment_amount=body.new_payment_amount,
            new_escalation_steps=steps,
            new_term_months=body.new_term_months,
            new_discount_rate_annual=body.new_discount_rate_annual,
            new_rate_basis=body.new_rate_basis,
            rate_unchanged_reason=body.rate_unchanged_reason,
            submitted_by=user.email,
            human_preparer_user_id=user.id,
        )
    except lease_service.LeaseValidationError as e:
        raise HTTPException(e.status_code, e.detail) from e

    return {
        "lease_id": r["lease_id"],
        "proposal_id": r["proposal_id"],
        "proposal_status": "PENDING",  # ALWAYS human-gated
        "effective_period": r["effective_period"],
        "old_liability": r["old_liability"],
        "new_liability": r["new_liability"],
        "delta_liability": r["delta_liability"],
        "old_rou": r["old_rou"],
        "new_rou": r["new_rou"],
        "rate_used": r["rate_used"],
        "rate_changed": r["rate_changed"],
        "rate_unchanged_reason": r["rate_unchanged_reason"],
        "note": r["note"],
    }


@router.post("/{lease_id}/terminate")
def terminate_lease(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + _get_lease's check_entity_tenant_access; the hook's AUTH_DEP_RE omits this alias
    lease_id: int,
    body: TerminateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Draft the ALWAYS-GATED termination JE (AC-4). gain/loss = remaining
    liability − ROU net book value − settlement. The lease flips TERMINATED
    (and facts are written) ONLY when the proposal EXECUTES.

    Thin caller over ``lease_lifecycle.terminate_lease_service`` — the ASC-842
    derecognition math (gain/loss, accum-ROU, draft-month guard) + JE drafting live in
    the service; the domain error maps back to the byte-identical HTTPException.

    409 teaching guards: not ACTIVE · locked period · unposted rows at/after
    the termination month (PENDING always blocks; DRAFT blocks unless
    skip_draft_rows) · termination already pending · missing settlement or
    gain/loss account."""
    lease = _get_lease(db, user, lease_id, "EDITOR")
    try:
        r = lease_lifecycle.terminate_lease_service(
            db,
            lease.realm_id,
            lease,
            termination_date=body.termination_date,
            settlement_amount=body.settlement_amount,
            settlement_account_id=body.settlement_account_id,
            settlement_account_name=body.settlement_account_name,
            gain_loss_account_id=body.gain_loss_account_id,
            gain_loss_account_name=body.gain_loss_account_name,
            skip_draft_rows=body.skip_draft_rows,
            submitted_by=user.email,
            human_preparer_user_id=user.id,
        )
    except lease_service.LeaseValidationError as e:
        raise HTTPException(e.status_code, e.detail) from e

    return {
        "lease_id": r["lease_id"],
        "proposal_id": r["proposal_id"],
        "proposal_status": "PENDING",  # ALWAYS human-gated
        "termination_date": r["termination_date"],
        "liability_remaining": r["liability_remaining"],
        "rou_net_book_value": r["rou_net_book_value"],
        "settlement": r["settlement"],
        "gain_loss": r["gain_loss"],
        "draft_rows_skipped": r["draft_rows_skipped"],
        "note": r["note"],
    }

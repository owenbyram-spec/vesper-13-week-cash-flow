# PLAN-EXEMPT: chunk 3 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
"""revenue_contracts.py — REST API for ASC 606 revenue contracts (plan
20260714-wave3-2-asc606-engine, Chunk 3 / AC-4).

Endpoints (all tenant-guarded exactly like leases: mount-level
global_tenant_guard defense-in-depth + the LOAD-BEARING in-handler
check_tenant_access / check_entity_tenant_access re-assert):

  POST /revenue-contracts                 Create (DRAFT — terms editable, nothing recognized)
  GET  /revenue-contracts                 List (by realm)
  GET  /revenue-contracts/{id}            Detail + obligations + recognition rows
  POST /revenue-contracts/{id}/activate   Measure-at-activate: allocate, persist immutable rows, → ACTIVE
  POST /revenue-contracts/{id}/modify     Re-allocate (posted periods unchanged; catch-up surfaced) → MODIFIED

The router is THIN: it parses a request DTO, runs the tenant guard, calls ONE
service function, maps to a response_model, and translates the domain
``RevenueContractError`` to the one error envelope. All revenue math lives in
``revenue_engine`` (Chunk 2); all lifecycle logic in ``revenue_contracts``
service (this wave). NO agent tool lives here (that is Chunk 5).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..auth import get_current_user_from_token as get_current_user
from ..db import get_db
from ..dependencies import check_entity_tenant_access, check_tenant_access
from ..models import PerformanceObligation, RevenueContract, RevenueRecognitionRow, User
from ..services import revenue_contracts
from ..services.errors import user_safe_http_exception

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/revenue-contracts", tags=["Revenue Contracts"])

RecognitionMethod = Literal["over_time", "point_in_time"]
OverTimeMeasure = Literal["cost_to_cost", "output"]


# ---------------------------------------------------------------------------
# Request DTOs
# ---------------------------------------------------------------------------


class PerformanceObligationCreate(BaseModel):
    obligation_index: int
    name: str
    description: str | None = None
    ssp: float                                   # REQUIRED human input (> 0); rejected if None/≤0
    recognition_method: RecognitionMethod
    over_time_measure: OverTimeMeasure | None = None
    total_estimated_cost: float | None = None    # REQUIRED for cost_to_cost over-time
    recognition_criteria: dict | None = None      # human-answered ASC 606-10-25-27 booleans
    deferred_revenue_account_id: str | None = None
    deferred_revenue_account_name: str | None = None
    contract_asset_account_id: str | None = None
    contract_asset_account_name: str | None = None
    revenue_account_id: str | None = None
    revenue_account_name: str | None = None


class RevenueContractCreate(BaseModel):
    realm_id: str
    name: str
    description: str | None = None
    customer_name: str | None = None
    contract_date: date
    transaction_price: float
    performance_obligations: list[PerformanceObligationCreate]


class ProgressObservationIn(BaseModel):
    period: str                                   # YYYY-MM
    cumulative_cost: float | None = None          # cost_to_cost input measure
    measure_pct: float | None = None              # output measure (0..1)
    transferred: bool | None = None               # point_in_time transfer event


class ObligationProgressIn(BaseModel):
    obligation_index: int
    observations: list[ProgressObservationIn]


class ActivateRevenueContractRequest(BaseModel):
    progress: list[ObligationProgressIn]


class ObligationRevisionIn(BaseModel):
    obligation_index: int
    ssp: float
    current_measure_pct: float


class ModifyRevenueContractRequest(BaseModel):
    new_transaction_price: float
    revisions: list[ObligationRevisionIn]


# ---------------------------------------------------------------------------
# Response DTOs (Pydantic v2 ORM bridge — ConfigDict(from_attributes=True))
# ---------------------------------------------------------------------------


class RevenueContractOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    realm_id: str
    organization_id: int
    name: str
    description: str | None
    customer_name: str | None
    contract_date: date
    transaction_price: float
    status: str
    activated_at: datetime | None
    created_by: str | None
    created_at: datetime | None


class ObligationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    contract_id: int
    realm_id: str
    obligation_index: int
    name: str
    description: str | None
    ssp: float | None
    allocated_price: float | None
    recognition_method: str
    over_time_measure: str | None
    total_estimated_cost: float | None
    recognition_criteria: dict | None
    deferred_revenue_account_id: str | None
    revenue_account_id: str | None


class RevenueRecognitionRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    obligation_id: int
    realm_id: str
    period: str
    allocated_price: float
    measure_of_progress_pct: float
    revenue_recognized_this_period: float
    cumulative_recognized_to_date: float
    remaining_to_recognize: float
    contract_liability_bal: float
    contract_asset_bal: float
    modified: bool
    status: str


class RevenueContractDetailOut(BaseModel):
    contract: RevenueContractOut
    obligations: list[ObligationOut]
    rows: list[RevenueRecognitionRowOut]


class AllocationOut(BaseModel):
    obligation_id: int
    obligation_index: int
    allocated_price: float
    rows_generated: int


class ActivateResultOut(BaseModel):
    contract_id: int
    status: str
    transaction_price: float
    rows_generated: int
    allocations: list[AllocationOut]


class ModifyAllocationOut(BaseModel):
    obligation_id: int
    obligation_index: int
    new_allocated: float
    new_cumulative_recognized: float
    catch_up: float


class ModifyResultOut(BaseModel):
    contract_id: int
    status: str
    new_transaction_price: float
    total_catch_up: float
    allocations: list[ModifyAllocationOut]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_contract(db: Session, user: User, contract_id: int, role: str) -> RevenueContract:
    """The row-resolves-tenant gate — the load-bearing in-handler re-assert for
    every {id} route (§3.2 multi-tenancy-app-layer)."""
    return check_entity_tenant_access(
        role,
        db=db,
        user=user,
        model=RevenueContract,
        entity_id=contract_id,
        tenant_attr="realm_id",
        not_found_detail="Revenue contract not found",
    )


def _map_error(exc: revenue_contracts.RevenueContractError):
    return user_safe_http_exception(
        status_code=exc.status_code,
        error_code="invalid_revenue_contract",
        user_message=exc.detail,
        log_context="revenue_contract_lifecycle",
    )


# ---------------------------------------------------------------------------
# CRUD + lifecycle
# ---------------------------------------------------------------------------


@router.post("", response_model=RevenueContractOut)
def create_revenue_contract(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + the in-body check_tenant_access("EDITOR", company_id=body.realm_id); AUTH_DEP_RE omits this alias
    body: RevenueContractCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # The EDITOR tenant gate validates the request-supplied realm against the
    # user's memberships. The service stamps realm_id from THIS trusted realm,
    # never a payload value (Control-5: server scope wins).
    check_tenant_access("EDITOR", company_id=body.realm_id, db=db, user=user)
    try:
        return revenue_contracts.create_revenue_contract_service(
            db, body.realm_id, body.model_dump(), created_by=user.email
        )
    except revenue_contracts.RevenueContractError as e:
        raise _map_error(e) from e


@router.get("", response_model=list[RevenueContractOut])
def list_revenue_contracts(
    realm_id: str = Query(...),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    check_tenant_access("VIEWER", company_id=realm_id, db=db, user=user)
    q = db.query(RevenueContract).filter(RevenueContract.realm_id == realm_id)
    if status:
        q = q.filter(RevenueContract.status == status.upper())
    return q.order_by(RevenueContract.id).all()


@router.get("/{contract_id}", response_model=RevenueContractDetailOut)
def get_revenue_contract(
    contract_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    contract = _get_contract(db, user, contract_id, "VIEWER")
    obligations = (
        db.query(PerformanceObligation)
        .filter(
            PerformanceObligation.contract_id == contract.id,
            PerformanceObligation.realm_id == contract.realm_id,
        )
        .order_by(PerformanceObligation.obligation_index)
        .all()
    )
    obligation_ids = [o.id for o in obligations]
    rows = (
        db.query(RevenueRecognitionRow)
        .filter(
            RevenueRecognitionRow.realm_id == contract.realm_id,
            RevenueRecognitionRow.obligation_id.in_(obligation_ids),
        )
        .order_by(RevenueRecognitionRow.obligation_id, RevenueRecognitionRow.period)
        .all()
        if obligation_ids
        else []
    )
    return RevenueContractDetailOut(
        contract=RevenueContractOut.model_validate(contract),
        obligations=[ObligationOut.model_validate(o) for o in obligations],
        rows=[RevenueRecognitionRowOut.model_validate(r) for r in rows],
    )


@router.post("/{contract_id}/activate", response_model=ActivateResultOut)
def activate_revenue_contract(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + _get_contract's check_entity_tenant_access("EDITOR", ...); AUTH_DEP_RE omits this alias
    contract_id: int,
    body: ActivateRevenueContractRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Measure-at-activate: allocate the transaction price across obligations by
    relative SSP, generate the immutable recognition rows from the confirmed
    progress observations, and flip the contract ACTIVE. A None/invalid SSP or a
    completion % outside [0, 1] is rejected (400) and NO rows are persisted."""
    contract = _get_contract(db, user, contract_id, "EDITOR")
    progress_by_index = {
        p.obligation_index: [o.model_dump(exclude_none=True) for o in p.observations]
        for p in body.progress
    }
    try:
        return revenue_contracts.activate_revenue_contract_service(
            db, contract.realm_id, contract, progress_by_index=progress_by_index, submitted_by=user.email
        )
    except revenue_contracts.RevenueContractError as e:
        raise _map_error(e) from e


@router.post("/{contract_id}/modify", response_model=ModifyResultOut)
def modify_revenue_contract(  # AUTH-OK: authed via user=Depends(get_current_user)=get_current_user_from_token (the file-wide auth edge) + _get_contract's check_entity_tenant_access("EDITOR", ...); AUTH_DEP_RE omits this alias
    contract_id: int,
    body: ModifyRevenueContractRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-allocate the (revised) transaction price across obligations by revised
    SSP. POSTED recognition periods are never restated — the cumulative catch-up
    surfaces as a delta; the contract flips MODIFIED."""
    contract = _get_contract(db, user, contract_id, "EDITOR")
    revisions = [r.model_dump() for r in body.revisions]
    try:
        return revenue_contracts.modify_revenue_contract_service(
            db,
            contract.realm_id,
            contract,
            new_transaction_price=body.new_transaction_price,
            revisions=revisions,
            submitted_by=user.email,
        )
    except revenue_contracts.RevenueContractError as e:
        raise _map_error(e) from e

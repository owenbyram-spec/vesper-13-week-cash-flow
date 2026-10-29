# PLAN-EXEMPT: chunk 3 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
"""revenue_contracts.py — the ASC 606 RevenueContract lifecycle service.

The callable, HTTP-free create → validate → activate → modify path for revenue
contracts, mirroring ``lease_service`` (ASC 842). The service is PURE (db +
company_id + explicit params in; the refreshed ORM row / a plain result dict
out) and raises a domain error (``RevenueContractError``), NEVER
``HTTPException`` — the router wraps it and maps the status/detail back.

The contract (engine-owns-math / human-confirms-judgment, decision 2):

  * The ENGINE (``revenue_engine``) owns every recognized-revenue NUMBER —
    relative-SSP allocation and over-time / point-in-time recognition. This
    service PERSISTS what the engine returns; it invents no figure.

  * The HUMAN supplies every JUDGMENT the engine consumes but never invents:
    the standalone selling price (SSP), the progress measure, and the ASC 606
    over-time criteria answers. Each is a REQUIRED input validated server-side —
    a fabricated/None SSP or completion % is REJECTED (400), never defaulted
    (the LOCKED-lease-rate precedent, ``lease_service.py:115``).

Measure-at-activate (the 842 pattern): nothing is recognized at create. DRAFT
stores confirmed terms; ACTIVATE runs the engine allocation + row generation and
persists the IMMUTABLE recognition rows. A contract modification re-allocates the
remaining transaction price; POSTED periods are never restated — the cumulative
catch-up surfaces as a delta.

E3/E7 double-recognition (decision 5): a QBO deferred-revenue account may be
recognized by ONLY ONE RevenueContract. The create path REFUSES a genuine
same-source double (an account already exclusively owned by a DIFFERENT
contract); activation CLAIMS exclusive ownership. A legitimately shared /
unclaimed Deferred Revenue account is NOT falsely refused
(``ScheduleAccountCoverage.deferred_rev_recognition_conflict``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from ..models import (
    PerformanceObligation,
    RevenueContract,
    RevenueRecognitionRow,
    ScheduleAccountCoverage,
)
from . import revenue_engine
from .tenant_companies import organization_for_company

TWO_PLACES = Decimal("0.01")


class RevenueContractError(ValueError):
    """A domain validation / state failure on the revenue-contract lifecycle.
    Carries the HTTP ``status_code`` + ``detail`` the router re-raises as the
    error envelope — the service itself stays FastAPI-free (the
    ``LeaseValidationError`` precedent)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _money(value) -> Decimal:
    """Exact cents Decimal from a str/number/Decimal (never binary float)."""
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# validate — SSP / method / measure / criteria are REQUIRED human inputs.
# Rejects (400) a None/invalid SSP, an unknown recognition_method/measure, or a
# missing over-time criteria set. The completion % is validated by the engine at
# activation (recognize_over_time rejects out-of-[0,1]); this function owns the
# static per-obligation structure. Reuses the engine's rejection for SSP.
# ---------------------------------------------------------------------------


def validate_revenue_contract(obligations: list[dict]) -> None:
    """Validate the confirmed per-obligation inputs BEFORE any persistence.
    Raises ``RevenueContractError(400)`` on the first failure — no default is
    ever substituted for a missing SSP / criterion (refuse-over-fabricate)."""
    if not obligations:
        raise RevenueContractError(400, "a revenue contract requires at least one performance obligation")

    # SA-OK: bounded-loop obligations-per-contract — the in-loop `.get(` matches across
    # this module are dict/param reads (od / rev / by_index / progress_by_index), NOT DB
    # queries; every per-obligation DB read lives in a helper (_obligations_for /
    # _claim_account_ownership / _recognized_to_date / deferred_rev_recognition_conflict)
    # and is bounded by the small obligation count of one contract, never a table scan.
    seen_indexes: set[int] = set()
    for od in obligations:
        idx = od.get("obligation_index")
        if idx is None:
            raise RevenueContractError(400, "each performance obligation needs an obligation_index")
        if idx in seen_indexes:
            raise RevenueContractError(400, f"duplicate obligation_index {idx}")
        seen_indexes.add(idx)

        method = od.get("recognition_method")
        if method not in PerformanceObligation.RECOGNITION_METHODS:
            raise RevenueContractError(
                400,
                f"recognition_method must be one of "
                f"{PerformanceObligation.RECOGNITION_METHODS}, got {method!r}",
            )

        # SSP is a REQUIRED human input (> 0) — reuse the engine's rejection so
        # the refuse-over-fabricate gate is the SAME code the allocation runs.
        try:
            revenue_engine._require_positive(od.get("ssp"), f"ssp (obligation {idx})")
        except revenue_engine.RevenueEngineError as exc:
            raise RevenueContractError(400, str(exc)) from exc

        if method == PerformanceObligation.RECOGNITION_METHODS[0]:  # "over_time"
            measure = od.get("over_time_measure")
            if measure not in PerformanceObligation.OVER_TIME_MEASURES:
                raise RevenueContractError(
                    400,
                    f"over_time obligation {idx} needs over_time_measure ∈ "
                    f"{PerformanceObligation.OVER_TIME_MEASURES}, got {measure!r}",
                )
            criteria = od.get("recognition_criteria")
            if not isinstance(criteria, dict):
                raise RevenueContractError(
                    400,
                    f"over_time obligation {idx} needs the ASC 606-10-25-27 criteria "
                    f"answers (recognition_criteria) — the engine never infers them from text",
                )
            missing = [k for k in revenue_engine.OVER_TIME_CRITERIA_KEYS if k not in criteria]
            if missing:
                raise RevenueContractError(
                    400,
                    f"over_time obligation {idx} is missing criteria answers: {', '.join(missing)}",
                )
            # Binding GAAP (606-10-25-27): over-time requires at least one criterion TRUE.
            if not any(bool(criteria.get(k)) for k in revenue_engine.OVER_TIME_CRITERIA_KEYS):
                raise RevenueContractError(
                    400,
                    f"over_time obligation {idx}: no ASC 606-10-25-27 criterion is met — "
                    f"this obligation is satisfied at a POINT IN TIME, not over time",
                )
            if measure == PerformanceObligation.OVER_TIME_MEASURES[0]:  # "cost_to_cost"
                try:
                    revenue_engine._require_positive(
                        od.get("total_estimated_cost"), f"total_estimated_cost (obligation {idx})"
                    )
                except revenue_engine.RevenueEngineError as exc:
                    raise RevenueContractError(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# create — persist a DRAFT contract + its obligations. Nothing is recognized.
# ---------------------------------------------------------------------------


def create_revenue_contract_service(
    db: Session,
    company_id: str,
    data: dict,
    *,
    created_by: str | None = None,
) -> RevenueContract:
    """Create a DRAFT RevenueContract + its PerformanceObligations and persist.

    ``company_id`` is the TRUSTED realm (the router validated it via
    ``check_tenant_access("EDITOR", ...)``); ``realm_id`` is stamped from THIS
    value on the contract AND every obligation, NEVER from ``data["realm_id"]``
    (Control-5: a body-supplied realm is ignored — server scope wins).

    Refuses (409) a genuine E3/E7 same-source double-recognition: an obligation's
    ``deferred_revenue_account_id`` already exclusively owned by a DIFFERENT
    contract on this realm. A legitimately shared / unclaimed account is allowed.
    """
    obligations_data = data.get("performance_obligations") or []
    validate_revenue_contract(obligations_data)

    org = organization_for_company(db, company_id)
    if org is None:
        raise RevenueContractError(404, "No organization is mapped to this company")

    # E3/E7 guard FIRST (before any write): a fresh contract can never already own
    # an account, so requesting_contract_id=None ⇒ any existing owner is a conflict.
    for od in obligations_data:
        dr = od.get("deferred_revenue_account_id")
        if dr and ScheduleAccountCoverage.deferred_rev_recognition_conflict(
            db, company_id, str(dr), requesting_contract_id=None
        ):
            raise RevenueContractError(
                409,
                f"Deferred-revenue account {dr} is already exclusively recognized by "
                f"another revenue contract on this company — a second contract would "
                f"double-recognize the same source (ASC 606; E3/E7 partition).",
            )

    contract = RevenueContract(
        realm_id=company_id,  # TRUSTED server scope — NEVER data["realm_id"]
        organization_id=org.id,
        name=data["name"],
        description=data.get("description"),
        customer_name=data.get("customer_name"),
        contract_date=data["contract_date"],
        transaction_price=_money(data["transaction_price"]),
        status="DRAFT",
        created_by=created_by,
    )
    db.add(contract)
    db.flush()  # assign contract.id for the obligation FKs

    for od in obligations_data:
        tec = od.get("total_estimated_cost")
        db.add(
            PerformanceObligation(
                contract_id=contract.id,
                realm_id=company_id,  # denormalized TRUSTED realm (RLS key)
                obligation_index=od["obligation_index"],
                name=od["name"],
                description=od.get("description"),
                ssp=_money(od["ssp"]),
                recognition_method=od["recognition_method"],
                over_time_measure=od.get("over_time_measure"),
                total_estimated_cost=_money(tec) if tec is not None else None,
                recognition_criteria=od.get("recognition_criteria"),
                deferred_revenue_account_id=od.get("deferred_revenue_account_id"),
                deferred_revenue_account_name=od.get("deferred_revenue_account_name"),
                contract_asset_account_id=od.get("contract_asset_account_id"),
                contract_asset_account_name=od.get("contract_asset_account_name"),
                revenue_account_id=od.get("revenue_account_id"),
                revenue_account_name=od.get("revenue_account_name"),
            )
        )
    db.commit()
    db.refresh(contract)
    return contract


# ---------------------------------------------------------------------------
# activate — DRAFT → ACTIVE: engine allocation + immutable recognition rows.
# ---------------------------------------------------------------------------


def _obligations_for(db: Session, contract: RevenueContract, company_id: str) -> list[PerformanceObligation]:
    return (
        db.query(PerformanceObligation)
        .filter(
            PerformanceObligation.contract_id == contract.id,
            PerformanceObligation.realm_id == company_id,
        )
        .order_by(PerformanceObligation.obligation_index)
        .all()
    )


def _claim_account_ownership(db: Session, company_id: str, obligation, contract_id: int) -> None:
    """Set ``ScheduleAccountCoverage.owning_contract_id`` for the obligation's
    deferred-revenue account so no AccountingSchedule / other contract can
    double-recognize it (E3/E7). Upsert on (realm_id, qbo_account_id)."""
    dr = obligation.deferred_revenue_account_id
    if not dr:
        return
    row = (
        db.query(ScheduleAccountCoverage)
        .filter(
            ScheduleAccountCoverage.realm_id == company_id,
            ScheduleAccountCoverage.qbo_account_id == str(dr),
        )
        .first()
    )
    if row is None:
        db.add(
            ScheduleAccountCoverage(
                realm_id=company_id,
                qbo_account_id=str(dr),
                qbo_account_name=obligation.deferred_revenue_account_name,
                owning_contract_id=contract_id,
            )
        )
    else:
        row.owning_contract_id = contract_id


def activate_revenue_contract_service(
    db: Session,
    company_id: str,
    contract: RevenueContract,
    *,
    progress_by_index: dict,
    submitted_by: str,
) -> dict:
    """Measure-at-activate: run the engine SSP allocation, generate the immutable
    recognition rows from the confirmed progress observations, persist them, claim
    the E3/E7 account ownership, and flip the contract ACTIVE.

    ``progress_by_index`` maps ``obligation_index`` → the ordered progress
    observations for ``revenue_engine.generate_rows`` (each obs carries
    ``cumulative_cost`` for cost_to_cost, ``measure_pct`` for output, or
    ``transferred`` for point_in_time). A missing observation set, or a
    completion % outside [0, 1], is REJECTED (400) and NO rows are persisted.

    Raises ``RevenueContractError`` for the state-machine (404/409) and any
    engine measurement failure (400). Rows are generated fully in-memory FIRST,
    so a failure persists nothing (no partial recognition).
    """
    if contract.realm_id != company_id:
        raise RevenueContractError(404, "Revenue contract not found")
    if contract.status != "DRAFT":
        raise RevenueContractError(
            409, f"Contract is {contract.status}; only a DRAFT contract can be activated."
        )

    obligations = _obligations_for(db, contract, company_id)
    if not obligations:
        raise RevenueContractError(409, "Contract has no performance obligations to activate.")

    # E3/E7: refuse if any deferred-rev account is already owned by another contract.
    for ob in obligations:
        dr = ob.deferred_revenue_account_id
        if dr and ScheduleAccountCoverage.deferred_rev_recognition_conflict(
            db, company_id, str(dr), requesting_contract_id=contract.id
        ):
            raise RevenueContractError(
                409,
                f"Deferred-revenue account {dr} is already exclusively recognized by "
                f"another revenue contract — cannot activate (E3/E7 partition).",
            )

    # Engine allocation (Σ allocated ≡ transaction_price, last-obligation plug).
    try:
        allocated = revenue_engine.allocate(contract.transaction_price, [ob.ssp for ob in obligations])
    except revenue_engine.RevenueEngineError as exc:
        raise RevenueContractError(400, str(exc)) from exc

    # Pass 1 — generate ALL rows in memory (no DB mutation); a failure raises here,
    # so nothing is persisted (transactional "400 → no rows").
    planned: list[tuple] = []
    for ob, alloc in zip(obligations, allocated):
        obs = progress_by_index.get(ob.obligation_index)
        if obs is None:
            obs = progress_by_index.get(str(ob.obligation_index))
        if not obs:
            raise RevenueContractError(
                400,
                f"obligation {ob.obligation_index} needs at least one progress observation "
                f"to recognize at activation (the human supplies the measure of progress).",
            )
        try:
            rows = revenue_engine.generate_rows(
                allocated_price=alloc,
                recognition_method=ob.recognition_method,
                progress_observations=obs,
                over_time_measure=ob.over_time_measure,
                total_estimated_cost=ob.total_estimated_cost,
            )
        except revenue_engine.RevenueEngineError as exc:
            raise RevenueContractError(400, str(exc)) from exc
        planned.append((ob, alloc, rows))

    # Pass 2 — persist (allocations, immutable rows, account ownership).
    summary = []
    total_rows = 0
    for ob, alloc, rows in planned:
        ob.allocated_price = alloc
        for r in rows:
            db.add(
                RevenueRecognitionRow(
                    obligation_id=ob.id,
                    realm_id=company_id,
                    period=r["period"],
                    allocated_price=r["allocated_price"],
                    measure_of_progress_pct=r["measure_of_progress_pct"],
                    revenue_recognized_this_period=r["revenue_recognized_this_period"],
                    cumulative_recognized_to_date=r["cumulative_recognized_to_date"],
                    remaining_to_recognize=r["remaining_to_recognize"],
                    contract_liability_bal=r["contract_liability_bal"],
                    contract_asset_bal=r["contract_asset_bal"],
                    status="DRAFT",
                )
            )
        _claim_account_ownership(db, company_id, ob, contract.id)
        total_rows += len(rows)
        summary.append(
            {
                "obligation_id": ob.id,
                "obligation_index": ob.obligation_index,
                "allocated_price": float(alloc),
                "rows_generated": len(rows),
            }
        )

    contract.status = "ACTIVE"
    contract.activated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(contract)

    return {
        "contract_id": contract.id,
        "status": "ACTIVE",
        "transaction_price": float(contract.transaction_price),
        "rows_generated": total_rows,
        "allocations": summary,
    }


# ---------------------------------------------------------------------------
# modify — re-allocate the remaining transaction price; posted periods UNCHANGED.
# ---------------------------------------------------------------------------


def _recognized_to_date(db: Session, company_id: str, obligation_id: int) -> Decimal:
    """The recognized-to-date for an obligation = the cumulative recognized of its
    latest POSTED period (posted periods are immutable — this is a fixed input to
    the re-allocation, never rewritten). Zero when nothing is posted yet."""
    row = (
        db.query(RevenueRecognitionRow)
        .filter(
            RevenueRecognitionRow.obligation_id == obligation_id,
            RevenueRecognitionRow.realm_id == company_id,
            RevenueRecognitionRow.status == "POSTED",
        )
        .order_by(RevenueRecognitionRow.period.desc())
        .first()
    )
    if row is None:
        return Decimal("0.00")
    return _money(row.cumulative_recognized_to_date)


def modify_revenue_contract_service(
    db: Session,
    company_id: str,
    contract: RevenueContract,
    *,
    new_transaction_price,
    revisions: list[dict],
    submitted_by: str,
) -> dict:
    """Re-allocate ``new_transaction_price`` across the obligations by revised SSP
    (the engine's ``reallocate_on_modification``), update each obligation's
    ``allocated_price``, surface the cumulative catch-up, and flip the contract
    MODIFIED. POSTED recognition rows are NEVER touched — ``recognized_to_date``
    is read from them as a fixed input; the catch-up books in the modification
    period (surfaced here, not silently restated).

    ``revisions``: ``[{"obligation_index", "ssp", "current_measure_pct"}]`` — the
    human's revised SSP + current progress per obligation.
    """
    if contract.realm_id != company_id:
        raise RevenueContractError(404, "Revenue contract not found")
    if contract.status not in ("ACTIVE", "MODIFIED"):
        raise RevenueContractError(
            409, f"Contract is {contract.status}; only an ACTIVE contract can be modified."
        )

    obligations = _obligations_for(db, contract, company_id)
    if not obligations:
        raise RevenueContractError(409, "Contract has no performance obligations to modify.")

    by_index = {}
    for rev in revisions:
        idx = rev.get("obligation_index")
        if idx is None:
            raise RevenueContractError(400, "each revision needs an obligation_index")
        by_index[idx] = rev

    engine_obligations: list[dict] = []
    for ob in obligations:
        rev = by_index.get(ob.obligation_index)
        if rev is None:
            raise RevenueContractError(
                400, f"modification is missing a revision for obligation {ob.obligation_index}"
            )
        engine_obligations.append(
            {
                "ssp": rev.get("ssp"),
                "current_measure_pct": rev.get("current_measure_pct"),
                "recognized_to_date": _recognized_to_date(db, company_id, ob.id),
            }
        )

    try:
        result = revenue_engine.reallocate_on_modification(
            new_transaction_price=new_transaction_price,
            obligations=engine_obligations,
        )
    except revenue_engine.RevenueEngineError as exc:
        raise RevenueContractError(400, str(exc)) from exc

    allocations = []
    for ob, alloc in zip(obligations, result["allocations"]):
        ob.allocated_price = alloc["new_allocated"]
        allocations.append(
            {
                "obligation_id": ob.id,
                "obligation_index": ob.obligation_index,
                "new_allocated": float(alloc["new_allocated"]),
                "new_cumulative_recognized": float(alloc["new_cumulative_recognized"]),
                "catch_up": float(alloc["catch_up"]),
            }
        )

    contract.transaction_price = _money(result["new_transaction_price"])
    contract.status = "MODIFIED"
    db.commit()
    db.refresh(contract)

    return {
        "contract_id": contract.id,
        "status": "MODIFIED",
        "new_transaction_price": float(result["new_transaction_price"]),
        "total_catch_up": float(result["total_catch_up"]),
        "allocations": allocations,
    }

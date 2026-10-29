# PLAN-EXEMPT: plan 20260706-cfo-grade-financial-workflows wave 3.1b-EXTRACT (Steven-ratified extract-now 2026-07-07) authorizes this NEW service file as its primary deliverable; the plan's surface_scope covers app/routers/leases.py but the sibling extraction target app/services/lease_service.py was not yet globbed — behavior-preserving extraction of the leases-router update path.
"""lease_service.py — the callable, HTTP-free ASC 842 lease UPDATE path.

Behavior-preserving extraction of the pre-activation lease field-update logic
+ its ASC 842-10-25-2 classification / term validation out of
``app/routers/leases.py`` so an agent tool (row 11 update_lease) can call it
directly. The service is PURE (db + company_id + explicit params in; the
refreshed ORM row out) and raises a domain error (``LeaseValidationError``),
never ``HTTPException`` — the router wraps it and maps the status/detail back.

The three ASC-842 helpers travel WITH the update path so the validation is not
left behind in the router (their logic is bit-identical to the pre-extraction
form — the DRAFT-only guard, the any-true-criterion → finance rule, the term /
short-term / rate bounds, and the escalation-step normalization all preserved).
"""
from __future__ import annotations

# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..models import LeaseSchedule, LeaseScheduleRow
from . import lease_engine
from .accounting import je_builder

TWO_PLACES = Decimal("0.01")

# The DRAFT-only measurement-term surface (locked at activation). Editing any of
# these on a non-DRAFT lease is rejected — remeasure is the gated path instead.
TERM_FIELDS = frozenset(
    {
        "name", "description", "classification", "classification_criteria",
        "commencement_date", "term_months", "payment_amount", "payment_timing",
        "escalation_steps", "discount_rate_annual", "rate_basis",
        "initial_direct_costs", "incentives", "prepaid_amount", "short_term_exempt",
    }
)


class LeaseValidationError(ValueError):
    """A domain validation failure on the lease update/create path. Carries the
    HTTP ``status_code`` + ``detail`` the router re-raises byte-identically as
    ``HTTPException`` — the service itself stays FastAPI-free."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# ASC 842 validation helpers (pure — operate on plain data, raise the domain
# error). Moved from routers/leases.py; logic bit-identical.
# ---------------------------------------------------------------------------


def steps_json(steps: list | None, term_months: int) -> list | None:
    """Validate + normalize escalation steps to the stored JSON shape
    (payments as STRINGS so the engine reads exact Decimals). ``steps`` is a
    list of ``{"start_month": int, "payment": float}`` plain dicts."""
    if not steps:
        return None
    # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
    ordered = sorted(steps, key=lambda s: s["start_month"])
    seen: set[int] = set()
    normalized: list[dict] = []
    for s in ordered:
        start_month = s["start_month"]
        # Coerce the payment string→exact Decimal (str-first, never float-first, so a
        # currency value keeps exact cents). A number or a numeric string is accepted;
        # a non-numeric payment is a caller error surfaced as a clean 400.
        try:
            payment = Decimal(str(s["payment"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise LeaseValidationError(400, "escalation payment must be a number") from exc
        if start_month < 2 or start_month > term_months:
            raise LeaseValidationError(
                400,
                f"escalation start_month {start_month} must be within 2..{term_months} "
                f"(month 1 is the base payment_amount).",
            )
        if start_month in seen:
            raise LeaseValidationError(400, f"duplicate escalation start_month {start_month}")
        if payment < 0:
            raise LeaseValidationError(400, "escalation payment must be >= 0")
        seen.add(start_month)
        normalized.append({"start_month": start_month, "payment": str(payment.quantize(TWO_PLACES))})
    return normalized


def validate_classification(classification: str, criteria: dict | None) -> None:
    """Any TRUE ASC 842-10-25-2 criterion forces finance classification.
    ``criteria`` is the plain-dict form of the five stored answers."""
    if criteria is None:
        return
    truthy = [k for k, v in criteria.items() if v]
    if truthy and classification != "finance":
        raise LeaseValidationError(
            400,
            f"Classification must be FINANCE: criteria {', '.join(truthy)} are met "
            f"(ASC 842-10-25-2 — any one criterion makes the lease a finance lease).",
        )


def validate_short_term_exemption(short_term_exempt: bool, criteria: dict | None) -> None:
    """The ASC 842 Master Glossary "short-term lease" (binding codification)
    also requires that the lease carry no purchase option the lessee is
    reasonably certain to exercise — the 12-month term bound alone is not
    enough. A lease whose stored criteria answer that option TRUE cannot take
    the election (it would leave a lease that must be capitalized off the
    balance sheet)."""
    if short_term_exempt and criteria and criteria.get("purchase_option_reasonably_certain"):
        raise LeaseValidationError(
            400,
            "short_term_exempt is not available: the lessee is reasonably certain to "
            "exercise a purchase option (ASC 842 Master Glossary 'short-term lease' — "
            "binding US GAAP). Capitalize the lease instead.",
        )


def validate_terms(
    *, term_months: int, payment_amount: float, discount_rate_annual: float,
    short_term_exempt: bool, initial_direct_costs: float, incentives: float,
    prepaid_amount: float,
) -> None:
    if term_months < 1:
        raise LeaseValidationError(400, "term_months must be >= 1")
    if payment_amount < 0:
        raise LeaseValidationError(400, "payment_amount must be >= 0")
    if discount_rate_annual <= 0 or discount_rate_annual >= 1:
        raise LeaseValidationError(
            400,
            "discount_rate_annual must be a decimal rate in (0, 1) — e.g. 0.06 for 6%.",
        )
    if short_term_exempt and term_months > lease_engine.SHORT_TERM_MAX_MONTHS:
        raise LeaseValidationError(
            400,
            f"short_term_exempt requires term_months <= {lease_engine.SHORT_TERM_MAX_MONTHS} "
            f"(ASC 842 short-term lease definition — binding US GAAP).",
        )
    for label, v in (
        ("initial_direct_costs", initial_direct_costs),
        ("incentives", incentives),
        ("prepaid_amount", prepaid_amount),
    ):
        if v < 0:
            raise LeaseValidationError(400, f"{label} must be >= 0")


# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
# ---------------------------------------------------------------------------
# The create path (extracted from routers/leases.py POST /leases so the router AND
# the create_lease agent tool call ONE service). Behavior-preserving for the router.
# ---------------------------------------------------------------------------


def create_lease_service(
    db: Session,
    company_id: str,
    data: dict,
    *,
    created_by: str | None = None,
) -> LeaseSchedule:
    """Create a DRAFT ASC-842 lease and persist it.

    Args:
        db: the live session (tenant scope already pinned by the caller).
        company_id: the TRUSTED realm the lease belongs to — the router passes the
            realm it validated via ``check_tenant_access("EDITOR", ...)``; the agent
            handler passes the server-pinned ``ContextPacket`` realm. The lease's
            ``realm_id`` is stamped from THIS value, NEVER from ``data["realm_id"]``
            (Control-5 second wall: a tool-supplied realm is ignored — server scope
            wins).
        data: the create payload (``LeaseCreate.dict()`` from the router, or the
            model-supplied params from the agent handler) — nested
            ``classification_criteria`` / ``escalation_steps`` are plain dict/list.
        created_by: the actor recorded on the row (router: the user's email; agent
            handler: the approver email or "AI Assistant").

    Returns the refreshed ORM row. Raises ``LeaseValidationError`` for the ASC-842
    term / classification failures (400) — including a percent-form
    ``discount_rate_annual`` (a rate given as "5"/"6.0" is REJECTED, never coerced to
    0.05; guessing 5% vs 500% is refuse-over-fabricate). Nothing is recognized at
    create — the engine owns all PV/ROU measurement at ACTIVATE; this stores DRAFT
    terms only.
    """
    validate_terms(
        term_months=data["term_months"],
        payment_amount=data["payment_amount"],
        discount_rate_annual=data["discount_rate_annual"],
        short_term_exempt=data.get("short_term_exempt", False),
        initial_direct_costs=data.get("initial_direct_costs", 0) or 0,
        incentives=data.get("incentives", 0) or 0,
        prepaid_amount=data.get("prepaid_amount", 0) or 0,
    )
    criteria = data.get("classification_criteria")
    validate_classification(data["classification"], criteria)
    validate_short_term_exemption(data.get("short_term_exempt", False), criteria)

    lease = LeaseSchedule(
        realm_id=company_id,  # TRUSTED server scope — NEVER data["realm_id"]
        name=data["name"],
        description=data.get("description"),
        classification=data["classification"],
        classification_criteria=criteria if criteria else None,
        commencement_date=data["commencement_date"],
        term_months=data["term_months"],
        payment_amount=Decimal(str(data["payment_amount"])).quantize(TWO_PLACES),
        payment_frequency="monthly",
        payment_timing=data.get("payment_timing", "arrears"),
        escalation_steps=steps_json(data.get("escalation_steps"), data["term_months"]),
        discount_rate_annual=Decimal(str(data["discount_rate_annual"])),
        rate_basis=data.get("rate_basis", "ibr"),
        initial_direct_costs=Decimal(str(data.get("initial_direct_costs", 0) or 0)).quantize(TWO_PLACES),
        incentives=Decimal(str(data.get("incentives", 0) or 0)).quantize(TWO_PLACES),
        prepaid_amount=Decimal(str(data.get("prepaid_amount", 0) or 0)).quantize(TWO_PLACES),
        short_term_exempt=data.get("short_term_exempt", False),
        automation_policy=data.get("automation_policy", "auto_propose"),
        status="DRAFT",
        created_by=created_by,
        rou_asset_account_id=data.get("rou_asset_account_id"),
        rou_asset_account_name=data.get("rou_asset_account_name"),
        accum_rou_amort_account_id=data.get("accum_rou_amort_account_id"),
        accum_rou_amort_account_name=data.get("accum_rou_amort_account_name"),
        lease_liability_account_id=data.get("lease_liability_account_id"),
        lease_liability_account_name=data.get("lease_liability_account_name"),
        interest_expense_account_id=data.get("interest_expense_account_id"),
        interest_expense_account_name=data.get("interest_expense_account_name"),
        lease_expense_account_id=data.get("lease_expense_account_id"),
        lease_expense_account_name=data.get("lease_expense_account_name"),
        rou_amort_expense_account_id=data.get("rou_amort_expense_account_id"),
        rou_amort_expense_account_name=data.get("rou_amort_expense_account_name"),
    )
    db.add(lease)
    db.commit()
    db.refresh(lease)
    return lease


# ---------------------------------------------------------------------------
# The update path
# ---------------------------------------------------------------------------


def update_lease_service(
    db: Session,
    company_id: str,
    lease: LeaseSchedule,
    data: dict[str, object],
) -> LeaseSchedule:
    """Apply a validated field update to a lease and persist it.

    Args:
        db: the live session (tenant scope already pinned by the caller).
        company_id: the realm the lease must belong to — re-asserted here as a
            defense-in-depth tenant check (the row was resolved through
            ``check_entity_tenant_access`` upstream).
        lease: the tenant-resolved ``LeaseSchedule`` ORM row.
        data: the ``exclude_unset`` update payload — nested criteria/steps are
            already plain dict/list (Pydantic ``.dict(exclude_unset=True)``).

    Returns the refreshed ORM row. Raises ``LeaseValidationError`` for the
    DRAFT-only guard (409) and the ASC-842 validation failures (400).

    DRAFT: terms freely editable (nothing is recognized yet). ACTIVE: only
    automation_policy and account ids/names — the measurement basis is LOCKED
    at activation; term/payment/rate changes go through the gated remeasure
    endpoint, never a plain update (rows are immutable outside it).
    """
    # Defense-in-depth tenant re-assert: the row must belong to the claimed realm.
    if lease.realm_id != company_id:
        raise LeaseValidationError(404, "Lease not found")

    if lease.status != "DRAFT":
        illegal = sorted(set(data) & TERM_FIELDS)
        if illegal:
            raise LeaseValidationError(
                409,
                f"Lease is {lease.status}; measurement terms are locked at activation "
                f"({', '.join(illegal)}). Use POST /leases/{{id}}/remeasure for a "
                f"gated modification — posted periods are never restated.",
            )

    merged = {
        "term_months": data.get("term_months", lease.term_months),
        "payment_amount": data.get("payment_amount", float(lease.payment_amount)),
        "discount_rate_annual": data.get("discount_rate_annual", float(lease.discount_rate_annual)),
        "short_term_exempt": data.get("short_term_exempt", lease.short_term_exempt),
        "initial_direct_costs": data.get("initial_direct_costs", float(lease.initial_direct_costs or 0)),
        "incentives": data.get("incentives", float(lease.incentives or 0)),
        "prepaid_amount": data.get("prepaid_amount", float(lease.prepaid_amount or 0)),
    }
    if lease.status == "DRAFT":
        validate_terms(**merged)
        criteria = data["classification_criteria"] if "classification_criteria" in data else (
            dict(lease.classification_criteria) if lease.classification_criteria else None
        )
        validate_classification(data.get("classification", lease.classification), criteria)
        validate_short_term_exemption(merged["short_term_exempt"], criteria)

    for field, value in data.items():
        if field == "escalation_steps":
            lease.escalation_steps = steps_json(value, merged["term_months"])
        elif field == "classification_criteria":
            lease.classification_criteria = value if value else None
        elif field in ("payment_amount", "initial_direct_costs", "incentives", "prepaid_amount"):
            setattr(lease, field, Decimal(str(value)).quantize(TWO_PLACES))
        elif field == "discount_rate_annual":
            lease.discount_rate_annual = Decimal(str(value))
        else:
            setattr(lease, field, value)
    db.commit()
    db.refresh(lease)
    return lease


# ---------------------------------------------------------------------------
# activate — DRAFT → ACTIVE (statement-impacting recognition)
# ---------------------------------------------------------------------------


def activate_lease_service(
    db: Session,
    company_id: str,
    lease: LeaseSchedule,
    *,
    idc_source_account_id: str | None = None,
    idc_source_account_name: str | None = None,
    prepaid_source_account_id: str | None = None,
    prepaid_source_account_name: str | None = None,
    incentive_source_account_id: str | None = None,
    incentive_source_account_name: str | None = None,
    submitted_by: str,
    human_preparer_user_id: int | None = None,
) -> dict:
    """Measure the lease at the LOCKED rate, persist the immutable amortization
    rows, and draft the commencement JE — ALWAYS proposal-gated regardless of
    automation_policy. Short-term-exempt leases activate with NO rows and NO JE
    (expense-as-incurred, documented).

    Returns ``{before, after, schedule}`` for the proposal payload — the
    ROU/liability recognition is statement-impacting, so before (nothing
    recognized) / after (recognized measures) are surfaced. Raises
    ``LeaseValidationError`` for the state-machine + account guards (409) and a
    lease-engine measurement failure (400). ``assert_period_writable`` raises its
    own 409 for a locked commencement period (unchanged collaborator — it raised
    the same HTTPException on this path before the extraction).
    """
    from .task_autonomy import assert_period_writable

    if lease.realm_id != company_id:
        raise LeaseValidationError(404, "Lease not found")
    if lease.status != "DRAFT":
        raise LeaseValidationError(
            409, f"Lease is {lease.status}; only a DRAFT lease can be activated."
        )

    if lease.short_term_exempt:
        # Re-checked here: a DRAFT saved before the purchase-option condition
        # was enforced must not activate off-balance-sheet.
        validate_short_term_exemption(True, lease.classification_criteria)
        lease.status = "ACTIVE"
        db.commit()
        return {
            "before": {"status": "DRAFT", "initial_liability": None, "initial_rou": None},
            "after": {"status": "ACTIVE", "initial_liability": None, "initial_rou": None},
            "schedule": {"rows_generated": 0},
            "lease_id": lease.id,
            "status": "ACTIVE",
            "short_term_exempt": True,
            "rows_generated": 0,
            "proposal_id": None,
            "note": (
                "Short-term lease (ASC 842-20-25-2 election): no liability, no ROU, "
                "no schedule rows — rent is expensed as incurred."
            ),
        }

    # Required accounts for the classification's JEs.
    missing = []
    if not lease.rou_asset_account_id:
        missing.append("rou_asset_account_id")
    if not lease.accum_rou_amort_account_id:
        missing.append("accum_rou_amort_account_id")
    if not lease.lease_liability_account_id:
        missing.append("lease_liability_account_id")
    if lease.classification == "operating":
        if not lease.lease_expense_account_id:
            missing.append("lease_expense_account_id")
    else:
        if not lease.interest_expense_account_id:
            missing.append("interest_expense_account_id")
        if not lease.rou_amort_expense_account_id:
            missing.append("rou_amort_expense_account_id")
    if missing:
        raise LeaseValidationError(
            409,
            f"Set these QBO accounts before activating ({lease.classification} lease): "
            f"{', '.join(missing)}.",
        )

    commencement_period = f"{lease.commencement_date.year}-{lease.commencement_date.month:02d}"
    assert_period_writable(db, realm_id=lease.realm_id, period=commencement_period)

    for amount, account_id, label in (
        (Decimal(str(lease.initial_direct_costs or 0)), idc_source_account_id, "idc_source_account_id"),
        (Decimal(str(lease.prepaid_amount or 0)), prepaid_source_account_id, "prepaid_source_account_id"),
        (Decimal(str(lease.incentives or 0)), incentive_source_account_id, "incentive_source_account_id"),
    ):
        if amount > 0 and not account_id:
            raise LeaseValidationError(
                409,
                f"{label} is required — the commencement JE must move that amount out "
                f"of the account it currently sits in.",
            )

    try:
        rows = lease_engine.build_rows_for_lease(lease)
    except lease_engine.LeaseEngineError as exc:
        raise LeaseValidationError(400, str(exc)) from exc

    # The exact commencement measures (computed directly, not derived from row 1).
    payments = lease_engine.payment_schedule(
        payment_amount=Decimal(str(lease.payment_amount)),
        term_months=int(lease.term_months),
        escalation_steps=lease.escalation_steps,
    )
    liability = lease_engine.present_value(
        payments, Decimal(str(lease.discount_rate_annual)), lease.payment_timing
    )
    rou = lease_engine.initial_rou_amount(
        liability,
        initial_direct_costs=Decimal(str(lease.initial_direct_costs or 0)),
        prepaid_amount=Decimal(str(lease.prepaid_amount or 0)),
        incentives=Decimal(str(lease.incentives or 0)),
    )
    lease.initial_liability = liability
    lease.initial_rou = rou

    for row in rows:
        db.add(
            LeaseScheduleRow(
                lease_id=lease.id,
                realm_id=lease.realm_id,
                period=row["period"],
                payment=row["payment"],
                interest=row["interest"],
                liability_reduction=row["liability_reduction"],
                ending_liability=row["ending_liability"],
                rou_amortization=row["rou_amortization"],
                ending_rou=row["ending_rou"],
                expense=row["expense"],
                remeasured=False,
                status="DRAFT",
            )
        )

    proposal = je_builder.build_lease_commencement_je(
        lease,
        db,
        idc_account=(
            (str(idc_source_account_id), idc_source_account_name or "")
            if idc_source_account_id
            else None
        ),
        prepaid_account=(
            (str(prepaid_source_account_id), prepaid_source_account_name or "")
            if prepaid_source_account_id
            else None
        ),
        incentive_account=(
            (str(incentive_source_account_id), incentive_source_account_name or "")
            if incentive_source_account_id
            else None
        ),
        submitted_by=submitted_by,
        human_preparer_user_id=human_preparer_user_id,
    )
    lease.commencement_proposed_action_id = proposal.id
    lease.status = "ACTIVE"
    db.commit()

    return {
        "before": {"status": "DRAFT", "initial_liability": None, "initial_rou": None},
        "after": {
            "status": "ACTIVE",
            "initial_liability": float(liability),
            "initial_rou": float(rou),
        },
        "schedule": {
            "rows_generated": len(rows),
            "first_period": rows[0]["period"],
            "final_period": rows[-1]["period"],
        },
        "lease_id": lease.id,
        "status": "ACTIVE",
        "short_term_exempt": False,
        "initial_liability": float(liability),
        "initial_rou": float(rou),
        "rows_generated": len(rows),
        "first_period": rows[0]["period"],
        "final_period": rows[-1]["period"],
        "proposal_id": proposal.id,
        "proposal_status": "PENDING",  # ALWAYS human-gated
    }

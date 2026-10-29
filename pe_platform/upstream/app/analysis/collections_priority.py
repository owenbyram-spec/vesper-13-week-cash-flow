# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C4 / AC-6 (marker
# RATIFICATION-2026-07-13.md on disk; dispatched session lacks active-plan state).
"""Pure, deterministic AR collections prioritization (plan decision 13, AC-6).

PURE by contract: no DB, no HTTP, no clock — inputs are the PRE-FETCHED analytic
outputs (the runner in ``app/tasks/agent.py`` owns fetching + failure isolation):

- ``open_invoices``: rows in the receivables_payables ``/open-invoices`` AR shape
  (``{id-as-invoice_id, doc_number, customer, balance, days_overdue, due_date}``).
- ``aging``: the ``app.analysis.ar_aging.ar_aging`` output (per-customer balances
  + ``total_ar``), or None when that analytic raised.
- ``lateness_by_customer``: ``{customer_name: median payment lateness days}``.
  HONEST LABEL (AP-FIN-09): this is payment LATENESS — median(payment_date −
  due_date) days past due — NOT a Days-Sales-Outstanding ratio. It is named
  lateness everywhere in this module.
- ``haircuts``: the ``derive_collection_haircuts`` output (per-aging-bucket
  historical collection rates; a thin-sample bucket is None), or None.

Honest degradation: an analytic that raised (None input) or hit its no-data
floor (empty lateness map; all-None haircut buckets) contributes NOTHING to any
score and is NAMED in ``missing_signals`` — a missing signal is never replaced
by a fabricated score component or a made-up default rate.
"""
from __future__ import annotations

from .payment_behavior import AGING_BUCKET_FLOORS

# ── Documented scoring weights (sum = 100; every coefficient named, AP-FIN-08) ──
# Why the invoice is chased: how far past due it is dominates; how much money is
# at stake comes second; behavioral/recovery signals refine the ordering.
WEIGHT_OVERDUE_SEVERITY = 40.0  # aging-bucket severity of THIS invoice (1-30 → 31-60 → 61-90 → 91+)
WEIGHT_BALANCE_MATERIALITY = 25.0  # invoice balance as a share of total overdue AR in the candidate set
WEIGHT_CUSTOMER_LATENESS = 15.0  # customer's historical median payment lateness (days past due)
WEIGHT_RECOVERY_RISK = 10.0  # 1 − historical collection rate for the invoice's aging bucket
WEIGHT_CUSTOMER_EXPOSURE = 10.0  # customer's share of total AR (aging-report concentration)

# Lateness tiers (days past due → fraction of WEIGHT_CUSTOMER_LATENESS).
# Bucketized so scores are hand-computable golden numbers, not curve-fit floats.
LATENESS_TIER_MILD_DAYS = 15  # pays up to ~2 weeks late → 1/3 weight
LATENESS_TIER_SLOW_DAYS = 30  # pays up to a month late → 2/3 weight; beyond → full weight

# Aging-bucket severity fractions of WEIGHT_OVERDUE_SEVERITY, keyed by the
# shared AGING_BUCKET_FLOORS = (0, 31, 61, 91) ladder (floor 0 = 1-30 days).
OVERDUE_SEVERITY_FRACTIONS = {0: 0.25, 31: 0.5, 61: 0.75, 91: 1.0}

BUCKET_LABELS = {0: "1-30", 31: "31-60", 61: "61-90", 91: "91+"}


def _as_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bucket_floor(days_overdue: int) -> int:
    floor = 0
    for candidate in AGING_BUCKET_FLOORS:
        if days_overdue >= candidate:
            floor = candidate
    return floor


def _lateness_fraction(lateness_days: "int | None") -> float:
    if lateness_days is None or lateness_days <= 0:
        return 0.0
    if lateness_days <= LATENESS_TIER_MILD_DAYS:
        return 1.0 / 3.0
    if lateness_days <= LATENESS_TIER_SLOW_DAYS:
        return 2.0 / 3.0
    return 1.0


def _recovery_rate(haircuts: "dict | None", floor: int) -> "float | None":
    """Historical collection rate for an aging bucket; None = no evidence (never fabricated)."""
    if not isinstance(haircuts, dict):
        return None
    buckets = haircuts.get("buckets") or {}
    rate = buckets.get(floor, buckets.get(str(floor)))  # SA-OK: dict access on an in-memory analytic output — no DB session in this pure module
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None
    return float(rate)


def _haircuts_have_evidence(haircuts: "dict | None") -> bool:
    if not isinstance(haircuts, dict):
        return False
    buckets = haircuts.get("buckets") or {}
    return any(
        isinstance(rate, (int, float)) and not isinstance(rate, bool)
        for rate in buckets.values()
    )


def prioritize_collections(
    open_invoices: "list | None",
    aging: "dict | None",
    lateness_by_customer: "dict | None",
    haircuts: "dict | None",
) -> dict:
    """Deterministic prioritized chase list over overdue open AR invoices.

    Returns ``{as_of, candidate_count, total_overdue_balance, chase_list,
    missing_signals}`` where ``chase_list`` is ordered by (score desc, balance
    desc, invoice_id asc) and every entry carries its full ``score_components``
    breakdown plus a human rationale naming only the signals that were present.
    """
    missing_signals: list = []

    if open_invoices is None:
        missing_signals.append("open_invoices")
        open_invoices = []
    aging_available = isinstance(aging, dict)
    if not aging_available:
        missing_signals.append("ar_aging")
    lateness_available = bool(lateness_by_customer)
    if not lateness_available:
        missing_signals.append("payment_lateness")
    haircuts_available = _haircuts_have_evidence(haircuts)
    if not haircuts_available:
        missing_signals.append("collection_haircuts")

    # Candidates: overdue with money actually outstanding. Never chase a current
    # invoice; never chase a zero/credit balance.
    candidates = [
        inv
        for inv in open_invoices
        if int(inv.get("days_overdue") or 0) > 0 and _as_float(inv.get("balance")) > 0.0  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module
    ]
    total_overdue = sum(_as_float(inv.get("balance")) for inv in candidates)  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module

    total_ar = _as_float(aging.get("total_ar")) if aging_available else 0.0
    exposure_by_customer: dict = {}
    if aging_available:
        for row in aging.get("invoices") or []:
            name = str(row.get("customer") or "")  # SA-OK: dict access on in-memory aging rows — no DB session in this pure module
            exposure_by_customer[name] = exposure_by_customer.get(name, 0.0) + _as_float(row.get("balance"))  # SA-OK: dict access on in-memory aging rows — no DB session in this pure module

    chase_list: list = []
    for inv in candidates:
        balance = _as_float(inv.get("balance"))  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module
        days_overdue = int(inv.get("days_overdue") or 0)  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module
        customer = str(inv.get("customer") or "")  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module
        floor = _bucket_floor(days_overdue)

        overdue_component = round(OVERDUE_SEVERITY_FRACTIONS[floor] * WEIGHT_OVERDUE_SEVERITY, 2)
        # total_overdue > 0 whenever candidates exist (each candidate balance > 0);
        # the guard is honesty against a fabricated 0-denominator, not a fallback.
        overdue_share = (balance / total_overdue) if total_overdue > 0 else 0.0
        balance_component = round(overdue_share * WEIGHT_BALANCE_MATERIALITY, 2)

        lateness_days = None
        if lateness_available and customer in lateness_by_customer:
            raw_lateness = lateness_by_customer[customer]
            if isinstance(raw_lateness, (int, float)) and not isinstance(raw_lateness, bool):
                lateness_days = int(raw_lateness)
        lateness_component = round(_lateness_fraction(lateness_days) * WEIGHT_CUSTOMER_LATENESS, 2)

        recovery_rate = _recovery_rate(haircuts, floor) if haircuts_available else None
        recovery_component = (
            round((1.0 - recovery_rate) * WEIGHT_RECOVERY_RISK, 2) if recovery_rate is not None else 0.0
        )

        customer_ar = exposure_by_customer.get(customer)  # SA-OK: dict access on an in-memory exposure map — no DB session in this pure module
        exposure_component = 0.0
        if aging_available and customer_ar is not None and total_ar > 0:
            exposure_component = round((customer_ar / total_ar) * WEIGHT_CUSTOMER_EXPOSURE, 2)

        score_components = {
            "overdue_severity": overdue_component,
            "balance_materiality": balance_component,
            "customer_lateness": lateness_component,
            "recovery_risk": recovery_component,
            "customer_exposure": exposure_component,
        }
        score = round(sum(score_components.values()), 2)

        rationale_parts = [
            f"{days_overdue} day(s) overdue ({BUCKET_LABELS[floor]} bucket)",
            f"${balance:,.2f} outstanding ({overdue_share:.0%} of overdue AR)",
        ]
        if lateness_days is not None:
            if lateness_days > 0:
                rationale_parts.append(f"customer historically pays a median {lateness_days} day(s) late")
            else:
                rationale_parts.append("customer historically pays on time or early")
        if recovery_rate is not None:
            rationale_parts.append(
                f"historical collection rate for the {BUCKET_LABELS[floor]} bucket is {recovery_rate:.0%}"
            )
        if aging_available and customer_ar is not None and total_ar > 0:
            rationale_parts.append(f"customer holds {customer_ar / total_ar:.0%} of total AR")

        chase_list.append(
            {
                "invoice_id": str(inv.get("id") or inv.get("invoice_id") or ""),  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module
                "doc_number": str(inv.get("doc_number") or ""),  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module
                "customer": customer,
                "balance": round(balance, 2),
                "days_overdue": days_overdue,
                "due_date": str(inv.get("due_date") or ""),  # SA-OK: dict access on in-memory invoice rows — no DB session in this pure module
                "score": score,
                "score_components": score_components,
                "rationale": "; ".join(rationale_parts) + ".",
            }
        )

    chase_list.sort(key=lambda entry: (-entry["score"], -entry["balance"], entry["invoice_id"]))

    return {
        "as_of": aging.get("as_of") if aging_available else None,
        "candidate_count": len(chase_list),
        "total_overdue_balance": round(total_overdue, 2),
        "chase_list": chase_list,
        "missing_signals": missing_signals,
    }

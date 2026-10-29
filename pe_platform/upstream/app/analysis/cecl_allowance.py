# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk A (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json not synced into this worktree).
"""
app/analysis/cecl_allowance.py
──────────────────────────────
Deterministic CECL (ASC 326) allowance-for-credit-losses estimator — decisions 2/3/4
of plan 20260713-wave2-5-cecl-allowance, DRAFTED-FOR-CPA-REVIEW.

PURE computation module: per-bucket loss rates come from
``derive_collection_haircuts`` (reused, extended IN PLACE with the additive
``sample_truncated`` key — never forked), bucket balances come from ``ar_aging``,
and the live allowance balance comes from ONE QBO Account-entity read
(``read_allowance_balance`` — the single non-pure helper in this module; the
computation itself takes data in, data out).

THE SIGN CONVENTION (E1, the crit-eval's blocking fix): QBO reports a
credit-balance contra-asset as a NEGATIVE ``CurrentBalance``. The engine pins
``current_allowance = -CurrentBalance`` so an existing $5,000 credit allowance
(CurrentBalance -5000) against an $8,000 required allowance drafts a $3,000
adjustment — never the $13,000 a raw read would compute. A DEBIT-balance
(positive-CurrentBalance) allowance is SURFACED as an anomaly finding, never
silently treated as a release. A zero/absent balance on a successfully-read
account is a legitimate 0. A FAILED account read REFUSES (``CeclReadError``) —
a fabricated $0 would draft a full-required-allowance JE.

Partial evidence (D2, CPA-gated): any NONZERO-balance bucket with no estimated
rate (<MIN_BUCKET_INVOICES mature invoices) → the estimate refuses to draft,
naming each bucket; the read output still shows everything computable.
"""
from __future__ import annotations

import logging
import os
from typing import TypedDict

logger = logging.getLogger(__name__)


# ── Named constants (AP-FIN-08) ───────────────────────────────────────────────

# Below this |Δ| (required − current allowance, dollars) the honest answer is
# "no adjustment needed" — a sub-$50 allowance true-up is bookkeeping noise.
# Env-tunable via CECL_DE_MINIMIS_DELTA, read at CALL time (decision 4).
# Boundary semantics (pinned): |Δ| < the threshold refuses (49.99 refuses);
# |Δ| >= the threshold drafts (50.00 drafts).
CECL_DE_MINIMIS_DELTA = 50.00

# The v1 method label carried on every estimate (decision 3).
CECL_METHOD = "aging_bucket_historical_loss_rate"

# ar_aging bucket name → derive_collection_haircuts floor. "current" (not-yet-due)
# takes the floor-0 (0-30 days-past-due) rate as a DOCUMENTED PROXY — no separate
# not-yet-due rate is derivable from the payment-history walk (decision 3).
AGING_BUCKET_TO_RATE_FLOOR: dict[str, int] = {
    "current": 0,
    "1-30": 0,
    "31-60": 31,
    "61-90": 61,
    "90+": 91,
}
AGING_BUCKET_ORDER: tuple[str, ...] = ("current", "1-30", "31-60", "61-90", "90+")

# Every limitation of the method, named in the evidence block (decision 3 + the two
# E2-added disclosures). This text ships verbatim on every estimate/proposal.
CECL_DISCLOSED_LIMITS: tuple[str, ...] = (
    "Right-censoring / conservatism: a still-open invoice past the maturity window "
    "counts as uncollected evidence even though some of those invoices will "
    "eventually pay — the derived loss rates are biased toward OVERSTATING losses.",
    "Cumulative-not-marginal semantics: each bucket's rate is losses among ALL "
    "invoices that ever REACHED that bucket, not the marginal roll-rate from one "
    "bucket to the next. Marginal roll-rates are named future work.",
    "DENOMINATOR MISMATCH: rates are losses per ORIGINAL invoice dollar, but they "
    "are applied to the REMAINING-OPEN dollars in today's aging. Worked example: "
    "invoices totaling $100,000 reached the 91+ bucket and $80,000 was ultimately "
    "collected — a 20% loss rate per original dollar. If the $20,000 still open "
    "today is mostly the never-paying tail, its true expected loss approaches 100 "
    "cents on the dollar, but the engine books 20% x $20,000 = $4,000 — up to a 5x "
    "understatement. A rebasis needs open-balance-at-bucket-entry data QuickBooks "
    "does not provide; disclosure is the v1 remedy.",
    "Dollar-weighting: rates are dollar-weighted, so a few large invoices dominate "
    "the derived rates.",
    "Payment-page truncation is RATE CORRUPTION, not just a thin sample: a paid "
    "invoice whose payment fell off a truncated (MAXRESULTS-full) Payment page "
    "counts as paid-at-due, inflating early-bucket collection rates. The "
    "sample_truncated flag covers BOTH the Invoice and Payment pages and degrades "
    "the basis label.",
    "'Current' (not-yet-due) balances take the floor-0 (0-30 days-past-due) "
    "collection rate as a documented proxy.",
    "Forward-looking conditions: the estimate assumes historical collection "
    "conditions persist — the ASU 2025-05 current-conditions practical-expedient "
    "framing for current-period estimates; no macroeconomic forecast adjustment is "
    "modeled. Methodology is DRAFTED-FOR-CPA-REVIEW (requires_cpa_review stays "
    "true).",
)


def _de_minimis_delta() -> float:
    """The de-minimis |Δ| threshold, env-tunable at CALL time (decision 4)."""
    raw = os.getenv("CECL_DE_MINIMIS_DELTA")
    if raw is None or not raw.strip():
        return CECL_DE_MINIMIS_DELTA
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "[CECL] CECL_DE_MINIMIS_DELTA env value is not numeric; using the %.2f default.",
            CECL_DE_MINIMIS_DELTA,
        )
        return CECL_DE_MINIMIS_DELTA


class CeclReadError(RuntimeError):
    """A FAILED allowance-account read — the engine REFUSES, never defaults to $0.

    A fabricated $0 current allowance would draft a full-required-allowance JE
    (decision 4). Callers surface this as a refusal, never swallow it into zero.
    """


class AllowanceBalance(TypedDict):
    """The sign-pinned live allowance balance (one QBO Account-entity read)."""

    account_id: str
    qbo_current_balance: float   # the raw QBO CurrentBalance (credit balance = NEGATIVE)
    current_allowance: float     # = -CurrentBalance (THE SIGN PIN, E1)
    debit_balance_anomaly: str | None


class BucketEvidence(TypedDict):
    """One aging bucket's rate/balance/expected-loss row in the evidence block."""

    bucket: str
    rate_floor: int
    balance: float
    collection_rate: float | None
    loss_rate: float | None
    invoice_count: int
    expected_loss: float | None


class CeclEstimate(TypedDict):
    """The full typed computation-evidence structure (decision 4) chunk B consumes."""

    method: str
    as_of: str
    basis: str
    sample_truncated: bool
    window_months: int
    maturity_days: int
    min_bucket_invoices: int
    bucket_rows: list[BucketEvidence]
    total_ar: float
    required_allowance: float
    required_allowance_partial: bool
    required_allowance_floored: bool
    unestimated_buckets: list[str]
    allowance_account_id: str | None
    qbo_current_balance: float | None
    current_allowance: float | None
    delta: float | None
    direction: str | None           # "increase" | "release" | "no_adjustment"
    de_minimis_delta: float
    de_minimis: bool
    anomalies: list[str]
    draft_refusals: list[str]
    can_draft: bool
    disclosed_limits: list[str]


def read_allowance_balance(client, allowance_account_id: str) -> AllowanceBalance:
    """Read the live allowance balance from the QBO Account entity (ONE call).

    Applies THE SIGN PIN: ``current_allowance = -CurrentBalance`` (QBO reports a
    credit-balance contra-asset negative). A debit balance (positive
    CurrentBalance) is surfaced as an anomaly finding. An absent CurrentBalance on
    a successfully-read account is a legitimate 0. ANY read failure raises
    ``CeclReadError`` — never a defaulted 0.
    """
    account_id = str(allowance_account_id)
    try:
        resp = client.get_transaction("account", account_id)
    except Exception as e:
        raise CeclReadError(f"Allowance account {account_id} could not be read from QuickBooks — refusing to assume a $0 balance.") from e
    account = (resp or {}).get("Account") or {}
    if not account:
        raise CeclReadError(
            f"QuickBooks returned no Account entity for allowance account {account_id} "
            "— refusing to assume a $0 balance."
        )
    raw_balance = account.get("CurrentBalance")
    if raw_balance is None:
        qbo_balance = 0.0  # a zero/absent balance on a READ account is a legitimate 0
    else:
        try:
            qbo_balance = float(raw_balance)
        except (TypeError, ValueError) as e:
            raise CeclReadError(f"Allowance account {account_id} returned a non-numeric CurrentBalance — refusing to assume a $0 balance.") from e
    qbo_balance = round(qbo_balance, 2)
    anomaly: str | None = None
    if qbo_balance > 0:
        anomaly = (
            f"Allowance account {account_id} carries a DEBIT balance "
            f"(CurrentBalance {qbo_balance:.2f}). A contra-asset allowance normally "
            "carries a CREDIT (negative CurrentBalance in QBO) — this usually means "
            "write-offs exceeded the allowance or the mapping points at the wrong "
            "account. Surfaced for review; NOT treated as a release."
        )
    return {
        "account_id": account_id,
        "qbo_current_balance": qbo_balance,
        "current_allowance": round(-qbo_balance, 2),
        "debit_balance_anomaly": anomaly,
    }


def compute_cecl_allowance(
    *,
    haircuts: dict,
    aging: dict,
    allowance: AllowanceBalance | None,
    de_minimis_delta: float | None = None,
) -> CeclEstimate:
    """The pure CECL estimate: bucket loss rates × aging balances → required
    allowance → Δ vs the live allowance (decisions 2/3/4).

    ``haircuts`` is the ``derive_collection_haircuts`` return (with the additive
    ``sample_truncated`` key); ``aging`` is the ``ar_aging`` return; ``allowance``
    is a ``read_allowance_balance`` result, or None when the allowance account is
    UNMAPPED (the dry-run read half still shows the required-allowance estimate;
    Δ and drafting refuse until mapped — decision 5's unmapped branch).
    """
    rate_buckets = haircuts.get("buckets") or {}
    invoice_counts = haircuts.get("invoice_counts") or {}
    sample_truncated = bool(haircuts.get("sample_truncated"))
    basis = str(haircuts.get("basis") or "insufficient_history")
    if sample_truncated:
        basis = f"{basis}_truncated_sample"  # honest degrade (decision 3)

    balances = aging.get("buckets") or {}

    bucket_rows: list[BucketEvidence] = []
    unestimated: list[str] = []
    expected_total = 0.0
    for bucket in AGING_BUCKET_ORDER:  # SA-OK: bounded-loop over the 5 aging buckets — in-memory dict access, no DB query
        floor = AGING_BUCKET_TO_RATE_FLOOR[bucket]
        balance = round(float(balances.get(bucket) or 0.0), 2)
        collection_rate = rate_buckets.get(floor)
        loss_rate = round(1.0 - float(collection_rate), 4) if collection_rate is not None else None
        count = int(invoice_counts.get(floor) or 0)
        expected: float | None
        if balance == 0.0:
            expected = 0.0  # a zero bucket contributes nothing, rate or no rate
        elif loss_rate is None:
            expected = None  # nonzero balance, no estimable rate → D2 refusal below
            unestimated.append(bucket)
        else:
            expected = round(balance * loss_rate, 2)
            expected_total += expected
        bucket_rows.append(
            {
                "bucket": bucket,
                "rate_floor": floor,
                "balance": balance,
                "collection_rate": collection_rate,
                "loss_rate": loss_rate,
                "invoice_count": count,
                "expected_loss": expected,
            }
        )

    required = round(expected_total, 2)
    floored = False
    if required < 0.0:
        # Credit memos can net a bucket (and thus the sum) negative; a NEGATIVE
        # required allowance is not bookable — floor at 0 and say so.
        required = 0.0
        floored = True
    partial = bool(unestimated)

    anomalies: list[str] = []
    draft_refusals: list[str] = []
    for bucket in unestimated:  # SA-OK: bounded-loop over at most 5 bucket names — no DB query
        draft_refusals.append(f"insufficient history: {bucket}")
    if floored:
        anomalies.append(
            "Credit balances in the aging netted the expected-loss sum negative; the "
            "required allowance is floored at $0.00."
        )

    threshold = de_minimis_delta if de_minimis_delta is not None else _de_minimis_delta()

    allowance_account_id: str | None = None
    qbo_current_balance: float | None = None
    current_allowance: float | None = None
    delta: float | None = None
    direction: str | None = None
    de_minimis = False

    if allowance is None:
        draft_refusals.append(
            "allowance_unmapped: no allowance account is mapped for this company — "
            "the required-allowance ESTIMATE above stands, but Δ and drafting are "
            "unavailable until the CECL account mapping is configured."
        )
    else:
        allowance_account_id = allowance["account_id"]
        qbo_current_balance = allowance["qbo_current_balance"]
        current_allowance = allowance["current_allowance"]
        if allowance["debit_balance_anomaly"]:
            anomalies.append(allowance["debit_balance_anomaly"])
        if not partial:
            delta = round(required - current_allowance, 2)
            if abs(delta) < threshold:
                de_minimis = True
                direction = "no_adjustment"
                draft_refusals.append(
                    f"de_minimis: |Δ| {abs(delta):.2f} is below the "
                    f"{threshold:.2f} de-minimis threshold — no adjustment needed."
                )
            elif delta > 0:
                direction = "increase"
            else:
                direction = "release"  # Δ<0 IS the release direction (decision 5)

    can_draft = bool(not draft_refusals and delta is not None)

    logger.info(
        "[CECL] estimate basis=%s truncated=%s required=%.2f partial=%s delta=%s "
        "direction=%s refusals=%d",
        basis, sample_truncated, required, partial,
        f"{delta:.2f}" if delta is not None else "n/a", direction, len(draft_refusals),
    )

    return {
        "method": CECL_METHOD,
        "as_of": str(aging.get("as_of") or ""),
        "basis": basis,
        "sample_truncated": sample_truncated,
        "window_months": int(haircuts.get("window_months") or 0),
        "maturity_days": int(haircuts.get("maturity_days") or 0),
        "min_bucket_invoices": int(haircuts.get("min_bucket_invoices") or 0),
        "bucket_rows": bucket_rows,
        "total_ar": round(float(aging.get("total_ar") or 0.0), 2),
        "required_allowance": required,
        "required_allowance_partial": partial,
        "required_allowance_floored": floored,
        "unestimated_buckets": unestimated,
        "allowance_account_id": allowance_account_id,
        "qbo_current_balance": qbo_current_balance,
        "current_allowance": current_allowance,
        "delta": delta,
        "direction": direction,
        "de_minimis_delta": threshold,
        "de_minimis": de_minimis,
        "anomalies": anomalies,
        "draft_refusals": draft_refusals,
        "can_draft": can_draft,
        "disclosed_limits": list(CECL_DISCLOSED_LIMITS),
    }


def build_je_lines(
    estimate: CeclEstimate,
    *,
    allowance_account_id: str,
    bad_debt_account_id: str,
) -> list[dict[str, str | float]]:
    """The simplified-shape JE lines for a draftable estimate (decision 4).

    Increase (Δ>0): debit Bad Debt Expense / credit the Allowance. Release
    (Δ<0): debit the Allowance / credit Bad Debt Expense. Float + round-2dp at
    this JE seam; balanced by construction. Raises ValueError on a non-draftable
    estimate — a refused estimate must never silently become a JE.
    """
    delta = estimate["delta"]
    if not estimate["can_draft"] or delta is None:
        reasons = "; ".join(estimate["draft_refusals"]) or "delta unavailable"
        raise ValueError(f"CECL estimate is not draftable — {reasons}")
    amount = round(abs(delta), 2)
    if delta > 0:
        debit_account, credit_account = str(bad_debt_account_id), str(allowance_account_id)
        description = (
            "CECL allowance increase — aging-bucket historical loss-rate estimate "
            "(drafted for CPA review)"
        )
    else:
        debit_account, credit_account = str(allowance_account_id), str(bad_debt_account_id)
        description = (
            "CECL allowance release — aging-bucket historical loss-rate estimate "
            "(drafted for CPA review)"
        )
    return [
        {"account_id": debit_account, "debit": amount, "credit": 0.0, "description": description},
        {"account_id": credit_account, "debit": 0.0, "credit": amount, "description": description},
    ]

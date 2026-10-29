# PLAN-EXEMPT: chunk 0 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
"""
revenue_engine.py — the ASC 606 revenue-recognition measurement engine
(plan 20260714-wave3-2-asc606-engine, RK-7). Revenue contracts get their OWN
engine — relative-SSP allocation and over-time cost-to-cost recognition must
not contort the single straight-line DEFERRED_REVENUE schedule_engine
substrate — but emit into the SAME rails (JOURNAL_ENTRY ProposedActions,
period locks, tie-out), the ASC 842 lease_engine precedent.

THE CONTRACT (engine-owns-math / human-confirms-judgment):

  * The ENGINE owns every recognized-revenue NUMBER: relative-SSP allocation,
    the cost-to-cost / output measure of progress, the cumulative-catch-up
    delta on a contract modification, and the contract-asset / contract-
    liability roll-forward. Every stored figure is Decimal, quantized to cents
    ROUND_HALF_UP, and asserted against a Σ-invariant (Σ allocated ≡ the
    transaction price) before any row is emitted.

  * The HUMAN (or the LLM, then human-confirmed) owns every JUDGMENT the
    engine CONSUMES but NEVER invents: the standalone selling price (SSP)
    estimate, the SSP estimation method, the measure-of-progress %, and the
    ASC 606 step criteria below. These are REQUIRED inputs validated
    server-side — a fabricated SSP or completion % is REJECTED, never
    defaulted (the LOCKED-lease-rate precedent, lease_service.py:115-119).

THE CRITERIA ARE HUMAN-ANSWERED BOOLEANS — THE ENGINE NEVER SELF-EVALUATES
FROM CONTRACT TEXT (decision 3, E6). The tuples below are FIXED key sets: a
human answers each key True/False (or selects one method), the engine READS
the answer and applies the binding-GAAP rule. The engine does NOT read the
contract prose and infer a criterion — inferring "control transfers over time"
from a paragraph of legal text is exactly the judgment ASC 606 reserves for a
preparer + CPA (every criterion topic is requires_cpa_review). This mirrors
the ASC 842 ``CLASSIFICATION_CRITERIA_KEYS`` pattern (lease_engine.py:66-74):
the human answers, the engine never guesses.

GROUNDING (decision 3, E5 — §3 binding discipline): the criteria below are
BINDING US GAAP (ASC 606-10-25-27 over-time; 606-10-32-34 SSP methods), NOT a
proposal. They are encoded here as human-answered KEYS — they are not
corpus-grounded at sub-paragraph granularity (fasb.org 403s non-browser
clients; refuse-over-fabricate), so the ``asc_knowledge`` corpus records for
these steps (ids 91-115) stay ``binding_status="binding"`` + requires_cpa_
review, and NO record is tagged ``"proposed-not-binding"`` (that value is
reserved for NPRMs). The sub-paragraph citations are NOT fabricated here; the
human/CPA confirms each criterion against authoritative ASC text.

*** Chunk 2 (plan 20260714-wave3-2, C2) fills the ENGINE BODY — SSP
    allocation, over-time recognition, the modification catch-up delta, and
    the both-sign roll-forward, with hand-computed golden tests. This module
    currently exposes ONLY the criteria-key constants below; no allocation, no
    recognition, no measurement math lives here in Chunk 0. ***
"""
from __future__ import annotations

# PLAN-EXEMPT: chunk 2 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

# The engine NEVER self-evaluates a criterion from contract text — it READS a
# human's boolean answer for each key below. This flag is the machine-checkable
# statement of that contract (asserted in tests/test_revenue_criteria.py); it
# stays False for the life of the engine.
ENGINE_SELF_EVALUATES_CRITERIA_FROM_TEXT = False

# ── Step 5 — over-time vs point-in-time (ASC 606-10-25-27, binding US GAAP) ───
# The THREE over-time criteria. ANY one answered True → the performance
# obligation is satisfied OVER TIME (a measure of progress applies); all three
# False → POINT IN TIME (control transfers at an instant). Human-answered per
# obligation; the engine reads the answers, never infers them from text.
OVER_TIME_CRITERIA_KEYS = (
    "customer_simultaneously_receives_and_consumes",           # 606-10-25-27(a)
    "performance_creates_or_enhances_asset_customer_controls",  # 606-10-25-27(b)
    "no_alternative_use_and_enforceable_right_to_payment",     # 606-10-25-27(c)
)

# For an over-time obligation, the MEASURE OF PROGRESS (ASC 606-10-25-33): an
# input method (e.g. cost-to-cost) or an output method. Human-selected per
# obligation; the engine consumes the % it produces, never picks the method
# from contract text.
PROGRESS_MEASURE_METHODS = (
    "input",   # e.g. cost-to-cost (606-10-55-20)
    "output",  # e.g. units delivered / milestones (606-10-55-17)
)

# ── Step 4 — allocate the transaction price (ASC 606-10-32-31..34) ───────────
# When SSP is not directly observable it is ESTIMATED by one of the three
# permitted methods (606-10-32-34). Human-selected; the engine consumes the SSP
# figure, never estimates it from text.
SSP_METHOD_ADJUSTED_MARKET = "adjusted_market_assessment"          # 606-10-32-34(a)
SSP_METHOD_EXPECTED_COST_PLUS_MARGIN = "expected_cost_plus_margin"  # 606-10-32-34(b)
SSP_METHOD_RESIDUAL = "residual"                                    # 606-10-32-34(c) — GATED

SSP_METHODS = (
    SSP_METHOD_ADJUSTED_MARKET,
    SSP_METHOD_EXPECTED_COST_PLUS_MARGIN,
    SSP_METHOD_RESIDUAL,
)

# The residual approach (606-10-32-34(c)) is PERMITTED ONLY in limited
# circumstances: the SSP is highly variable (the same good/service is sold to
# different customers over a broad range) OR uncertain (a price has not been
# established and the item has not previously been sold standalone). Encoded as
# a human-answered eligibility gate — Chunk 2's engine refuses a residual SSP
# method unless one of these is answered True.
RESIDUAL_ELIGIBILITY_KEYS = (
    "selling_price_highly_variable",                # 606-10-32-34(c)(1)
    "selling_price_uncertain_not_yet_established",   # 606-10-32-34(c)(2)
)

# PLAN-EXEMPT: chunk 2 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
# ── Recognition method / measure vocabularies (mirror the C1 model constants) ─
# PerformanceObligation.RECOGNITION_METHODS / OVER_TIME_MEASURES (models.py:4775).
RECOGNITION_OVER_TIME = "over_time"
RECOGNITION_POINT_IN_TIME = "point_in_time"
RECOGNITION_METHODS = (RECOGNITION_OVER_TIME, RECOGNITION_POINT_IN_TIME)

OVER_TIME_MEASURE_COST_TO_COST = "cost_to_cost"  # input method (606-10-55-20)
OVER_TIME_MEASURE_OUTPUT = "output"              # output method (606-10-55-17)
OVER_TIME_MEASURES = (OVER_TIME_MEASURE_COST_TO_COST, OVER_TIME_MEASURE_OUTPUT)

# ─────────────────────────────────────────────────────────────────────────────
# Money primitives — every stored figure is Decimal, cents, ROUND_HALF_UP.
# The measure-of-progress ratio (PoC / output %) carries 6 dp (models.py:4866
# Numeric(9,6)); the money is derived from the FULL-precision ratio, then
# quantized once, so PoC display-rounding never drifts the recognized dollars.
# ─────────────────────────────────────────────────────────────────────────────

TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")
# An allocation / final-recognition plug absorbs at most half a cent of rounding
# drift per split element; anything beyond N × this bound means the math is
# broken, not rounded (the lease_engine PLUG_TOLERANCE_PER_MONTH precedent).
PLUG_TOLERANCE_PER_ELEMENT = Decimal("0.01")


class RevenueEngineError(ValueError):
    """A revenue computation failed its own invariants — never emit the number.

    Raised for BOTH a broken engine invariant (Σ allocated ≢ transaction price)
    AND a missing/invalid REQUIRED HUMAN INPUT (a None/≤0 SSP or completion %).
    The engine refuses over fabricating — it never defaults a judgment input
    (the lease_service.validate_terms precedent, lease_service.py:115-119)."""


def _dec(x) -> Decimal:
    """Exact Decimal from a str/number/Decimal — never via binary float."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _cents(x: Decimal) -> Decimal:
    return x.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _six(x: Decimal) -> Decimal:
    return x.quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def _require_positive(value, name: str) -> Decimal:
    """A REQUIRED human input: reject None (never defaulted) and reject ≤ 0.
    This is the refuse-over-fabricate gate — a fabricated SSP / completion %
    is the finance cardinal sin (a plausible wrong number that ships)."""
    if value is None:
        raise RevenueEngineError(
            f"{name} is a required human input and is missing — the engine "
            f"never fabricates it (refuse-over-fabricate)."
        )
    d = _dec(value)
    if d <= 0:
        raise RevenueEngineError(f"{name} must be > 0, got {d}")
    return d


def _require_nonneg(value, name: str) -> Decimal:
    if value is None:
        raise RevenueEngineError(
            f"{name} is a required human input and is missing — the engine "
            f"never fabricates it (refuse-over-fabricate)."
        )
    d = _dec(value)
    if d < 0:
        raise RevenueEngineError(f"{name} must be >= 0, got {d}")
    return d


# PLAN-EXEMPT: engine fix under ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
def _require_measure(value, name: str) -> Decimal:
    """A REQUIRED human input: a measure-of-progress % in [0, 1]. Reject None
    (never defaulted) and reject out-of-range — the SAME refuse-over-fabricate
    gate on EVERY path that consumes a completion % (cost-to-cost, output, and
    the modification catch-up), so a None/invalid % can never slip through as a
    low-level decimal error instead of a clean engine refusal."""
    if value is None:
        raise RevenueEngineError(
            f"{name} is a required human input and is missing — the engine "
            f"never fabricates it (refuse-over-fabricate)."
        )
    d = _dec(value)
    if d < 0 or d > 1:
        raise RevenueEngineError(f"{name} must be in [0, 1], got {d}")
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — relative-SSP allocation (ASC 606-10-32-31..34).
# ─────────────────────────────────────────────────────────────────────────────


# PLAN-EXEMPT: engine fix under ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
def _largest_remainder(
    tp_cents: Decimal, parts: list[Decimal], total_ssp: Decimal
) -> list[Decimal]:
    """Allocate ``tp_cents`` (whole cents) across ``parts`` by relative SSP with
    the LARGEST-REMAINDER method: floor each exact share to a cent, then hand the
    leftover cents (TP − Σfloors) one-by-one to the obligations with the largest
    fractional remainders (ties broken by index, ascending). This guarantees
    every allocation is NON-NEGATIVE and Σ ≡ TP to the cent — unlike a
    round-then-plug last obligation, whose plug goes NEGATIVE when the first N−1
    round up past TP (the 0.02-over-{1,1,1,1} defect)."""
    floors: list[Decimal] = []
    remainders: list[Decimal] = []
    for s in parts:
        exact = tp_cents * s / total_ssp  # exact relative share, in cents
        floor = exact.to_integral_value(rounding=ROUND_DOWN)
        floors.append(floor)
        remainders.append(exact - floor)
    residual = int(tp_cents - sum(floors, Decimal(0)))  # leftover whole cents, ≥ 0
    order = sorted(range(len(parts)), key=lambda i: (-remainders[i], i))
    for k in range(residual):
        floors[order[k]] += 1
    return [(c / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP) for c in floors]


def allocate(transaction_price, ssps: list) -> list[Decimal]:
    """Allocate ``transaction_price`` across obligations by relative SSP
    (``allocated_i ≈ TP × s_i / Σ s_j``, to the cent) via the LARGEST-REMAINDER
    method so EVERY allocation is NON-NEGATIVE AND ``Σ allocated ≡ TP`` — both
    asserted (``RevenueEngineError`` on violation). Each SSP is a REQUIRED human
    input (> 0, never defaulted).

    Golden: TP $120,000 over {$100k, $20k, $30k} → $80,000.00 / $16,000.00 /
    $24,000.00, Σ = $120,000.00. Indivisible: $0.02 over {1,1,1,1} → two cents of
    $0.01 and two of $0.00 (never a negative plug), Σ = $0.02."""
    tp = _cents(_require_nonneg(transaction_price, "transaction_price"))
    if not ssps:
        raise RevenueEngineError("cannot allocate across zero obligations")
    parts = [_require_positive(s, f"ssp[{i}]") for i, s in enumerate(ssps)]
    total_ssp = sum(parts, Decimal(0))
    if total_ssp <= 0:
        raise RevenueEngineError("Σ SSP must be > 0 to allocate")

    tp_cents = (tp * 100).to_integral_value(rounding=ROUND_HALF_UP)
    allocated = _largest_remainder(tp_cents, parts, total_ssp)

    # Every allocation is NON-NEGATIVE — RAISED, never silently returned. The
    # largest-remainder method cannot emit a negative element; this asserts it
    # (the guard the old round-then-plug last obligation lacked).
    for i, a in enumerate(allocated):
        if a < 0:
            raise RevenueEngineError(
                f"allocation[{i}] = {a} is negative — allocation math is broken"
            )
    total_alloc = sum(allocated, Decimal(0))
    if total_alloc != tp:
        raise RevenueEngineError(
            f"Σ allocated {total_alloc} != transaction price {tp} (Σ-invariant)"
        )
    return allocated


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — over-time / point-in-time recognition.
# Recognized revenue = allocated × measure-of-progress; PERIOD revenue is the
# DELTA of the cumulative measure, NEVER period-% × price (the E3 discriminator).
# ─────────────────────────────────────────────────────────────────────────────


def measure_cost_to_cost(cumulative_cost, total_estimated_cost) -> Decimal:
    """Percentage-of-completion (input method, cost-to-cost) =
    ``cumulative_cost / total_estimated_cost``, FULL precision, CAPPED at 1.0
    (a cost overrun never recognizes > allocated). ``total_estimated_cost`` is
    a REQUIRED human input (> 0, never defaulted)."""
    cc = _require_nonneg(cumulative_cost, "cumulative_cost")
    tec = _require_positive(total_estimated_cost, "total_estimated_cost")
    poc = cc / tec
    if poc > Decimal(1):
        poc = Decimal(1)  # overrun cap — never recognize more than allocated
    return poc


def recognize_over_time(
    *, allocated_price, poc, prior_cumulative_recognized=Decimal("0")
) -> dict:
    """One over-time recognition period from a supplied progress measure ``poc``
    (0..1, a REQUIRED human-derived input). Returns the D3.3 row fields:
    ``cumulative_recognized = allocated × poc`` (cents), and the PERIOD revenue
    = the delta vs ``prior_cumulative_recognized``.

    ``negative_delta`` is surfaced (True) when the period delta is < 0 (a
    downward estimate revision / mod-loss event) — it is FLAGGED, never
    silently netted into a positive number."""
    # PLAN-EXEMPT: engine fix under ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
    ap = _require_positive(allocated_price, "allocated_price")
    # The output-method completion % is a REQUIRED human input — reject None /
    # out-of-range through the SAME gate as the cost-to-cost path (a None must
    # not slip through as a raw decimal error instead of a clean refusal).
    p = _require_measure(poc, "measure of progress")
    prior = _cents(_dec(prior_cumulative_recognized))
    cumulative = _cents(ap * p)
    delta = _cents(cumulative - prior)
    remaining = _cents(ap - cumulative)
    return {
        "measure_of_progress_pct": _six(p),
        "cumulative_recognized_to_date": cumulative,
        "revenue_recognized_this_period": delta,
        "remaining_to_recognize": remaining,
        "negative_delta": delta < 0,
    }


def recognize_cost_to_cost(
    *, allocated_price, cumulative_cost, total_estimated_cost,
    prior_cumulative_recognized=Decimal("0"),
) -> dict:
    """Convenience: cost-to-cost measure → over-time recognition period.

    THE E3 DISCRIMINATING GOLDEN: allocated $80,000, total_estimated_cost
    $64,000, cumulative_cost $32,000 → PoC 32000/64000 = 0.500000 →
    cumulative_recognized = 80,000 × 0.50 = **$40,000.00** (≠ the $32,000 cost
    incurred — the number that kills the recognized-=-cost bug); with a prior
    cumulative of $18,000 the period revenue = 40,000 − 18,000 = $22,000.00."""
    poc = measure_cost_to_cost(cumulative_cost, total_estimated_cost)
    return recognize_over_time(
        allocated_price=allocated_price,
        poc=poc,
        prior_cumulative_recognized=prior_cumulative_recognized,
    )


def recognize_point_in_time(*, allocated_price, transferred: bool) -> dict:
    """A point-in-time obligation (all over-time criteria answered False)
    recognizes 100% of its allocated price AT the transfer event and nothing
    before — no schedule. Before transfer everything is a contract liability."""
    ap = _require_positive(allocated_price, "allocated_price")
    if transferred:
        return {
            "measure_of_progress_pct": _six(Decimal(1)),
            "cumulative_recognized_to_date": _cents(ap),
            "revenue_recognized_this_period": _cents(ap),
            "remaining_to_recognize": Decimal("0.00"),
            "negative_delta": False,
        }
    return {
        "measure_of_progress_pct": _six(Decimal(0)),
        "cumulative_recognized_to_date": Decimal("0.00"),
        "revenue_recognized_this_period": Decimal("0.00"),
        "remaining_to_recognize": _cents(ap),
        "negative_delta": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# The E2 BOTH-SIGN contract-position roll-forward. The two formulas DIFFER by
# side — the asset side is NOT the liability sign (a single formula seeds an
# inverted golden). Both are pure period-step primitives.
# ─────────────────────────────────────────────────────────────────────────────


def roll_forward_liability(*, beginning, billings, recognition) -> Decimal:
    """Contract LIABILITY (deferred revenue — billed in advance):
    ``beginning + billings − recognition = ending``. Recognition DECREASES the
    liability (it debits deferred revenue, credits revenue)."""
    beg = _cents(_dec(beginning))
    bill = _require_nonneg(billings, "billings")
    rec = _require_nonneg(recognition, "recognition")
    return _cents(beg + bill - rec)


def roll_forward_asset(*, beginning, recognition, billing_reclass) -> Decimal:
    """Contract ASSET (unbilled revenue — performed before billing):
    ``beginning + recognition − billing_reclass = ending``. Recognition
    INCREASES the asset (debit contract asset, credit revenue); a later billing
    reclasses contract-asset → receivable, DECREASING it. This is the INVERSE
    of the liability sign — do not collapse the two into one formula."""
    beg = _cents(_dec(beginning))
    rec = _require_nonneg(recognition, "recognition")
    reclass = _require_nonneg(billing_reclass, "billing_reclass")
    return _cents(beg + rec - reclass)


# ─────────────────────────────────────────────────────────────────────────────
# The E4 contract-modification re-allocation (rec 94, requires_cpa_review).
# Re-allocate the NEW transaction price across obligations by revised SSP;
# posted periods are UNCHANGED (recognized_to_date is fixed); the cumulative
# catch-up delta is SURFACED on the current period, never a silent restatement.
# ─────────────────────────────────────────────────────────────────────────────


def reallocate_on_modification(*, new_transaction_price, obligations: list) -> dict:
    """``obligations``: ``[{"ssp", "current_measure_pct", "recognized_to_date"}]``.

    Re-allocates ``new_transaction_price`` by relative revised SSP (reusing
    ``allocate`` → same plug + Σ-invariant), then for each obligation the NEW
    cumulative-recognized = new_allocated × current PoC and the catch-up =
    new_cumulative − recognized_to_date. Posted periods (``recognized_to_date``)
    are inputs, never rewritten; the catch-up books in the modification period.

    Golden: new TP $132,000 over revised SSPs {A $72k, B $48k}; A 50% complete
    (recognized $30,000), B 25% complete (recognized $10,000). New allocated A
    $79,200.00 / B $52,800.00 (Σ ≡ $132,000). New cumulative A 79,200×0.50 =
    $39,600.00, B 52,800×0.25 = $13,200.00. Catch-up A $9,600.00, B $3,200.00;
    total catch-up $12,800.00."""
    ntp = _require_positive(new_transaction_price, "new_transaction_price")
    if not obligations:
        raise RevenueEngineError("cannot re-allocate across zero obligations")
    ssps = [_require_positive(o["ssp"], "ssp") for o in obligations]
    new_alloc = allocate(ntp, ssps)

    # PLAN-EXEMPT: engine fix under ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
    results: list[dict] = []
    total_catch = Decimal(0)
    for o, alloc in zip(obligations, new_alloc):
        # A None/invalid completion % is a REQUIRED human input — refuse, never
        # default (the same gate as the recognition paths); ``.get`` so a missing
        # key is the missing-input refusal, not a raw KeyError.
        poc = _require_measure(o.get("current_measure_pct"), "current_measure_pct")  # SA-OK: dict .get on an in-memory obligation dict, not a DB query — no N+1
        # Re-guard: the allocation feeding the catch-up must be non-negative.
        # ``allocate`` (largest-remainder) already guarantees it; asserted here so
        # a negative can never propagate into a cumulative-catch-up delta.
        if alloc < 0:
            raise RevenueEngineError(
                f"reallocated {alloc} is negative — allocation math is broken"
            )
        recognized = _cents(_require_nonneg(o["recognized_to_date"], "recognized_to_date"))
        new_cum = _cents(alloc * poc)
        catch = _cents(new_cum - recognized)
        results.append(
            {
                "new_allocated": alloc,
                "new_cumulative_recognized": new_cum,
                "catch_up": catch,
            }
        )
        total_catch += catch
    return {
        "new_transaction_price": _cents(ntp),
        "allocations": results,
        "total_catch_up": _cents(total_catch),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Recognition-row generation — the immutable D3.3 rows with engine-owned
# invariants asserted every generation (the lease_engine.generate_rows pattern,
# lease_engine.py:260-272).
# ─────────────────────────────────────────────────────────────────────────────


def generate_rows(
    *,
    allocated_price,
    recognition_method: str,
    progress_observations: list,
    over_time_measure: str | None = None,
    total_estimated_cost=None,
    billings_by_period: dict | None = None,
    allow_negative_delta: bool = False,
) -> list[dict]:
    """Emit the immutable per-period ``RevenueRecognitionRow``-shaped dicts for
    one obligation (the D3.3 columns). ``progress_observations`` is an ordered
    list of ``{"period": "YYYY-MM", ...}``:

      * over_time / cost_to_cost → each carries ``"cumulative_cost"``;
      * over_time / output       → each carries ``"measure_pct"`` (0..1);
      * point_in_time            → one obs carrying ``"transferred": True``.

    Contract-position balances: ``billings_by_period`` (cumulative-billed is its
    running Σ) drives the E2 net position — liability = max(billed − recognized,
    0), asset = max(recognized − billed, 0). Default (None) = billed-in-advance
    in full at inception → a pure contract-liability (deferred-revenue) release.

    Engine-owned invariants asserted (``RevenueEngineError`` on violation):
    cumulative monotone non-decreasing (absent ``allow_negative_delta``); on a
    COMPLETE obligation (final PoC = 1.0) Σ recognized ≡ allocated to the cent
    (final-period plug) and remaining → 0.00."""
    ap = _require_positive(allocated_price, "allocated_price")
    if recognition_method not in RECOGNITION_METHODS:
        raise RevenueEngineError(
            f"recognition_method must be one of {RECOGNITION_METHODS}"
        )
    if not progress_observations:
        raise RevenueEngineError("cannot generate rows for zero observations")

    obs = sorted(progress_observations, key=lambda ob: ob["period"])
    tolerance = PLUG_TOLERANCE_PER_ELEMENT * Decimal(len(obs))
    # PLAN-EXEMPT: engine fix under ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
    rows: list[dict] = []
    prior_cumulative = Decimal("0.00")

    for idx, o in enumerate(obs):
        period = o["period"]
        final = idx == len(obs) - 1

        if recognition_method == RECOGNITION_POINT_IN_TIME:
            transferred = bool(o["transferred"]) if "transferred" in o else False
            r = recognize_point_in_time(allocated_price=ap, transferred=transferred)
        elif over_time_measure == OVER_TIME_MEASURE_COST_TO_COST:
            r = recognize_cost_to_cost(
                allocated_price=ap,
                cumulative_cost=o["cumulative_cost"],
                total_estimated_cost=total_estimated_cost,
                prior_cumulative_recognized=prior_cumulative,
            )
        elif over_time_measure == OVER_TIME_MEASURE_OUTPUT:
            r = recognize_over_time(
                allocated_price=ap,
                poc=o["measure_pct"],
                prior_cumulative_recognized=prior_cumulative,
            )
        else:
            raise RevenueEngineError(
                f"over_time_measure must be one of {OVER_TIME_MEASURES} for an "
                f"over-time obligation"
            )

        if r["negative_delta"] and not allow_negative_delta:
            raise RevenueEngineError(
                f"period {period}: cumulative recognized decreased "
                f"({r['revenue_recognized_this_period']}) — a downward revision "
                f"is a contract-mod/loss event, never silently netted"
            )

        cumulative = r["cumulative_recognized_to_date"]
        remaining = r["remaining_to_recognize"]
        poc = r["measure_of_progress_pct"]
        delta = r["revenue_recognized_this_period"]

        # COMPLETE-obligation final plug: when the last period reaches 100%,
        # land Σ recognized on allocated exactly (remaining → 0.00).
        if final and poc >= Decimal(1):
            plug_delta = _cents(ap - prior_cumulative)
            if (plug_delta - delta).copy_abs() > tolerance:
                raise RevenueEngineError(
                    f"final recognition plug {plug_delta} deviates from {delta} "
                    f"beyond tolerance {tolerance}"
                )
            cumulative, delta, remaining = _cents(ap), plug_delta, Decimal("0.00")

        # E2 net contract position from cumulative billings vs cumulative
        # recognized (composes roll_forward_liability / roll_forward_asset).
        # PLAN-EXEMPT: engine fix under ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
        if billings_by_period is None:
            cumulative_billed = _cents(ap)  # billed in full at inception
        else:
            # Cumulative billed-to-date = Σ of ALL billings through the current
            # period (every bp <= period), NOT only a billing that coincides with
            # an observation. Dropping an ADVANCE billing (billed before the first
            # progress observation) would leave cumulative_billed at 0 and invert
            # a contract LIABILITY (deferred revenue) into a phantom contract ASSET.
            billed_to_date = Decimal("0")
            for bp, amt in billings_by_period.items():
                if bp <= period:
                    billed_to_date += _require_nonneg(amt, f"billings[{bp}]")
            cumulative_billed = _cents(billed_to_date)
        net = _cents(cumulative_billed - cumulative)
        contract_liability_bal = net if net > 0 else Decimal("0.00")
        contract_asset_bal = _cents(-net) if net < 0 else Decimal("0.00")

        rows.append(
            {
                "period": period,
                "allocated_price": _cents(ap),
                "measure_of_progress_pct": poc,
                "revenue_recognized_this_period": delta,
                "cumulative_recognized_to_date": cumulative,
                "remaining_to_recognize": remaining,
                "contract_liability_bal": contract_liability_bal,
                "contract_asset_bal": contract_asset_bal,
            }
        )
        prior_cumulative = cumulative

    # Invariants — engine-owned, asserted every generation.
    total_recognized = sum(
        (row["revenue_recognized_this_period"] for row in rows), Decimal(0)
    )
    if total_recognized != rows[-1]["cumulative_recognized_to_date"]:
        raise RevenueEngineError(
            f"Σ period revenue {total_recognized} != final cumulative "
            f"{rows[-1]['cumulative_recognized_to_date']}"
        )
    final_poc = rows[-1]["measure_of_progress_pct"]
    if final_poc >= Decimal(1):
        if total_recognized != _cents(ap):
            raise RevenueEngineError(
                f"complete obligation: Σ recognized {total_recognized} != "
                f"allocated {_cents(ap)} (Σ-invariant)"
            )
        if rows[-1]["remaining_to_recognize"] != Decimal("0.00"):
            raise RevenueEngineError(
                f"complete obligation: final remaining "
                f"{rows[-1]['remaining_to_recognize']} != 0.00"
            )
    return rows

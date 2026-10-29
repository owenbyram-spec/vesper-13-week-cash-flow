"""
app/lbo/waterfall_solve.py
──────────────────────────
Iterative cash-flow ⇄ debt waterfall solver for the LBO 3-statement model.

The LBO has a CIRCULAR reference: cash sweep pays down debt from free cash flow,
FCF depends on net income, net income depends on interest, and interest depends
on the (post-sweep) debt balance. A single sequential pass cannot resolve it —
the prior forecast engine ran one pass with the sweep NEVER applied.

`solve_waterfall` resolves the circularity by FIXED-POINT ITERATION: hold the
operating model (EBIT, D&A, capex, working-capital change, tax rate, distribution
policy) fixed, then repeatedly recompute the debt schedule (which applies the cash
sweep) and the resulting FCF until each year's ending debt balance is stable to a
dollar tolerance.

The iteration starts at ``fcf = 0`` (no sweep — today's state) and each pass the
sweep can only pay down MORE debt, so balances are non-increasing and interest
non-increasing (WS-1). The NOL carryforward (below) couples taxes across years,
so FCF is no longer strictly monotone pass-to-pass — the tax-rate (< 1) damping
keeps the map a contraction in practice, and the convergence check is unchanged:
per-year ending balances (and, in ledger mode, distributions) must stabilise. A
non-converging (constructed pathological) input fails LOUD via
:class:`WaterfallConvergenceError` — a non-converged LBO number is a fabricated
number and is never returned.

THE TAX LINE (AC-C4 legs 2/3/6 — plan 20260716-five-domain-remediation):

- **NOL carryforward (leg 2, always on):** a year with NEGATIVE taxable income
  accrues a net-operating-loss balance instead of a (nonsensical) negative cash
  tax; subsequent POSITIVE taxable income is first offset by the accumulated
  NOL, and only the remainder is taxed. Cash taxes are never negative. The
  §382-limitation / 80%-of-taxable-income cap on post-2017 federal NOLs is NOT
  modeled — a disclosed simplification (full offset), per-deal tax advice owns
  the caps.
- **§197 excess deduction (leg 3, election-gated):** when the deal elects
  tax-goodwill amortization (``extra_tax_deductions_by_year`` — the per-year
  IRC §197 tax amortization MINUS book goodwill amortization, signed), the
  CASH-TAX base is reduced by that book-tax difference. Book D&A is untouched.
- **OID amortization (leg 6, ``noncash_interest_by_year``):** financing-fee
  (ASC 835-30 contra-debt) amortization is BOOK interest expense — it reduces
  book EBT and net income and is added back in CFO as non-cash — but carries
  NO tax shield (the C3 grid articulation convention, a disclosed conservative
  simplification), so the cash-tax base excludes it and the swept FCF is
  unchanged by it.

Cash-tax base per year: ``EBIT − schedule interest (cash + PIK) − §197 excess
deduction``, then the NOL offset, then the rate. Book EBT per year:
``EBIT − schedule interest − OID amortization``.

The cash waterfall is SEQUENCED (WS-3): excess cash funds the senior, covenant-
driven cash sweep FIRST (inside ``compute_debt_schedule``), and the interim equity
distribution is then taken from what REMAINS — the same excess is never spent
twice.

This module is PURE (no DB, no Pydantic) so it is unit-testable with conftest-free
golden-number tests and reusable by any caller. With no ``sweep_terms`` and no
revolver tranche it calls ``compute_debt_schedule`` exactly as before (WS-5 parity
path — byte-identical numbers). Phase 2A (RD-1 rows 5-6): passing per-deal
``sweep_terms`` (sweep_pct / step-downs / min-cash floor) or a revolver tranche
activates the cash-ledger schedule, and the solver additionally feeds each
iterate's interim distributions back into the ledger and converges on the
distribution vector as part of the same fixed point.
"""
from typing import TypedDict

from .debt_schedule import compute_debt_schedule

# A debt tranche is the opaque assumption payload that compute_debt_schedule reads
# (funded_amount / interest_rate / amort_years / pik_flag / cash_sweep_flag / …);
# the solver never inspects it — it forwards it unchanged (WS-5). The debt-schedule
# result is likewise the opaque dict that module returns.
DebtTranche = dict[str, object]
DebtScheduleResult = dict[str, object]


class OperatingYear(TypedDict):
    """One year of the FIXED pre-debt operating model (does not change as debt iterates)."""

    ebit: float
    total_da: float
    capex: float
    delta_nwc: float
    tax_rate: float
    distribution_pct: float


class WaterfallSolution(TypedDict):
    """The converged fixed point returned by :func:`solve_waterfall`."""

    debt_schedule: DebtScheduleResult
    fcf_by_year: list[float]
    interest_by_year: list[float]
    distributions: list[float]
    net_income_by_year: list[float]
    # The tax detail (AC-C4 legs 2/3/6): book EBT (net of OID), the cash-tax
    # base, NOL usage/balance, and the cash taxes the engine's P&L presents.
    ebt_by_year: list[float]
    taxable_income_by_year: list[float]
    taxes_by_year: list[float]
    nol_used_by_year: list[float]
    nol_balance_by_year: list[float]
    iterations: int
    converged: bool


class WaterfallConvergenceError(RuntimeError):
    """Raised when the fixed-point iteration does not converge within max_iter.

    A non-converged LBO schedule is a fabricated number an underwriter would act
    on — the solver fails loud rather than returning a plausible-but-wrong value.
    """


def _derive_statement_lines(
    operating_by_year: list[OperatingYear],
    schedule: list[dict],
    num_years: int,
    noncash_interest_by_year: list[float] | None,
    extra_tax_deductions_by_year: list[float] | None,
) -> dict[str, list[float]]:
    """The SEQUENTIAL P&L → FCF walk off one debt schedule (WS-2 + AC-C4 legs 2/3/6).

    Per year, in order (the NOL balance threads forward, so this is one walk,
    never a per-year map):

      book EBT   = EBIT − schedule interest (cash + PIK) − OID amortization
      tax base   = EBIT − schedule interest − §197 excess deduction
                   (the OID carries NO tax shield — the C3 grid convention)
      NOL        : tax base < 0 → accrue the loss, tax 0;
                   tax base ≥ 0 → offset with the accumulated NOL first
      cash taxes = (tax base − NOL used) × rate     (never negative)
      NI (book)  = book EBT − cash taxes            (cash-tax presentation —
                   no deferred-tax asset/liability is booked; disclosed)
      CFO        = NI + D&A + PIK + OID − ΔNWC      (PIK and OID are non-cash)
      swept FCF  = CFO + CFI (= −capex) — scheduled amortization is NOT
                   pre-subtracted (compute_debt_schedule subtracts it itself:
                   ``excess = fcf − total_amortization``, debt_schedule.py).

    With zero OID, zero §197 delta, and no loss year this reproduces the prior
    per-year arithmetic to the cent (the WS-4 parity gate).
    """
    oid = noncash_interest_by_year or []
    extra = extra_tax_deductions_by_year or []
    nol_balance = 0.0
    out: dict[str, list[float]] = {
        "interest": [], "ebt": [], "taxable": [], "taxes": [],
        "nol_used": [], "nol_balance": [], "net_income": [], "fcf": [],
    }
    for yr in range(num_years):
        ds_yr = schedule[yr]
        interest_cash = float(ds_yr["total_interest_cash"])
        interest_pik = float(ds_yr["total_interest_pik"])
        total_interest = round(interest_cash + interest_pik, 2)

        op = operating_by_year[yr]
        ebit = float(op["ebit"])
        tax_rate = float(op["tax_rate"])
        oid_k = round(float(oid[yr]), 2) if yr < len(oid) else 0.0
        extra_k = round(float(extra[yr]), 2) if yr < len(extra) else 0.0

        ebt = round(ebit - total_interest - oid_k, 2)
        taxable = round(ebit - total_interest - extra_k, 2)
        if taxable < 0:
            nol_balance = round(nol_balance - taxable, 2)  # accrue the loss
            nol_used = 0.0
            taxes = 0.0
        else:
            nol_used = round(min(nol_balance, taxable), 2)
            nol_balance = round(nol_balance - nol_used, 2)
            taxes = round((taxable - nol_used) * tax_rate, 2)
        net_income = round(ebt - taxes, 2)

        da = float(op["total_da"])
        delta_nwc = float(op["delta_nwc"])
        capex = float(op["capex"])
        cfo = round(net_income + da + interest_pik + oid_k - delta_nwc, 2)
        cfi = round(-capex, 2)

        out["interest"].append(total_interest)
        out["ebt"].append(ebt)
        out["taxable"].append(taxable)
        out["taxes"].append(taxes)
        out["nol_used"].append(nol_used)
        out["nol_balance"].append(nol_balance)
        out["net_income"].append(net_income)
        out["fcf"].append(round(cfo + cfi, 2))
    return out


def _distribution_for_year(
    operating_year: OperatingYear,
    fcf: float,
    amort_incl_sweep: float,
) -> float:
    """Interim equity distribution for one year, from the POST-sweep residual (WS-3).

    The cash sweep is senior (covenant-driven) and runs first inside
    ``compute_debt_schedule``. The distribution is then a fraction of what REMAINS:
    ``max(cfo + cfi − amort − sweep, 0) × distribution_pct``. ``total_amortization``
    in the schedule already INCLUDES the swept amount (debt_schedule.py:123), so the
    post-sweep residual is exactly ``fcf − total_amortization`` — the same excess is
    never spent twice. When no tranche sweeps, ``sweep == 0`` so this reduces to the
    pre-existing ``max(cfo + cfi − amort, 0) × pct`` (WS-4 parity).
    """
    residual = round(fcf - amort_incl_sweep, 2)
    pct = float(operating_year["distribution_pct"])
    return round(pct * max(residual, 0.0), 2)


def solve_waterfall(
    operating_by_year: list[OperatingYear],
    debt_tranches: list[DebtTranche],
    num_years: int,
    *,
    tol: float = 0.01,
    max_iter: int = 50,
    sweep_terms: DebtTranche | None = None,
    noncash_interest_by_year: list[float] | None = None,
    extra_tax_deductions_by_year: list[float] | None = None,
    period_fraction: list[float] | None = None,  # W3d: per-year run-rate fraction (stub year<1); None => byte-parity
) -> WaterfallSolution:
    """Iteratively solve the LBO cash-flow ⇄ debt circularity to a fixed point.

    Args:
        operating_by_year: the FIXED pre-debt operating model, one dict per year,
            each carrying ``ebit``, ``total_da``, ``capex``, ``delta_nwc``,
            ``tax_rate``, and ``distribution_pct`` (the operating line does not
            change as the debt schedule iterates).
        debt_tranches: tranche definitions, passed UNCHANGED to
            ``compute_debt_schedule`` each iteration.
        num_years: number of projection years.
        tol: per-year ending-balance convergence tolerance, in dollars.
        max_iter: hard cap on iterations; exceeding it raises (fail-loud).
        sweep_terms: None → the pre-existing memoryless flat sweep (parity path).
            A per-deal terms dict (sweep_pct / step-downs / min_cash_floor /
            opening_cash — see debt_schedule.compute_debt_schedule) activates the
            CASH-LEDGER schedule; the solver then also feeds each iterate's interim
            distributions back into the ledger (``cash_outflows_by_year``) and takes
            the distribution from the ledger's ``distributable_surplus`` (never
            below the min-cash floor), converging on balances AND distributions.
        noncash_interest_by_year: per-year BOOK-ONLY non-cash interest (the OID /
            financing-fee amortization, ASC 835-30) — reduces book EBT and NI, is
            added back in CFO, carries NO tax shield (module docstring). None / all
            zeros → the pre-existing arithmetic exactly.
        extra_tax_deductions_by_year: per-year signed reduction of the CASH-TAX
            base only (the §197 tax-goodwill excess deduction when elected). Never
            touches book EBT/NI presentation lines other than through taxes.
            None → no book-tax difference.

    Returns:
        The converged :class:`WaterfallSolution` (debt schedule, FCF, interest,
        distributions, net income, iteration count). A non-converged run raises
        before returning.

    Raises:
        WaterfallConvergenceError: if the ending balance does not stabilise within
            ``max_iter`` iterations.
    """
    # Ledger mode: sweep terms provided, or a revolver tranche present (the revolver
    # needs the cash ledger to know what to draw/repay).
    ledger_mode = sweep_terms is not None or any(
        bool(t.get("revolver_flag")) for t in debt_tranches  # SA-OK: bounded-loop dict.get on in-memory tranche dicts, not a DB query
    )
    prev_dist: list[float] = [0.0] * num_years

    # Start with no sweep (today's state): fcf = 0 → no excess → no paydown (WS-1).
    # Seed prev_ending from this fcf=0 pass so a NO-circularity (sweep-off) deal —
    # whose ending balances are FCF-independent — converges on the FIRST loop
    # iteration (the FCF derived from the fcf=0 schedule reproduces the same
    # balances), matching the prior one-pass exactly (WS-4 parity gate).
    fcf_by_year = [0.0] * num_years
    seed = compute_debt_schedule(
        debt_tranches,
        num_years,
        fcf_by_year,
        sweep_terms=sweep_terms,
        cash_outflows_by_year=prev_dist if ledger_mode else None,
        period_fraction=period_fraction,
    )
    prev_ending: list[float] | None = [
        float(seed["schedule"][yr]["total_ending"]) for yr in range(num_years)
    ]
    # Derive the FCF the fcf=0 schedule implies, to feed the first iteration
    # (the same sequential walk the loop uses — NOL threading included).
    fcf_by_year = list(
        _derive_statement_lines(
            operating_by_year, seed["schedule"], num_years,
            noncash_interest_by_year, extra_tax_deductions_by_year,
        )["fcf"]
    )

    debt_result: DebtScheduleResult = {}
    lines: dict[str, list[float]] = {}

    for iteration in range(1, max_iter + 1):
        # Debt schedule WITH the current FCF → applies the cash sweep. In ledger mode
        # the PREVIOUS iterate's distributions flow back in as cash outflows so the
        # running cash balance is real (the distribution is part of the fixed point).
        debt_result = compute_debt_schedule(
            debt_tranches,
            num_years,
            fcf_by_year,
            sweep_terms=sweep_terms,
            cash_outflows_by_year=prev_dist if ledger_mode else None,
            period_fraction=period_fraction,
        )
        schedule = debt_result["schedule"]

        # Interest → taxes (NOL-threaded) → NI → FCF off the just-computed
        # (post-sweep) balances. ``schedule`` is the in-memory
        # compute_debt_schedule result (a list of plain dicts), never a DB row —
        # _derive_statement_lines indexes it directly (no DB, no N+1).
        lines = _derive_statement_lines(
            operating_by_year, schedule, num_years,
            noncash_interest_by_year, extra_tax_deductions_by_year,
        )
        next_fcf = lines["fcf"]
        ending = [float(schedule[yr]["total_ending"]) for yr in range(num_years)]

        # This iterate's distributions. Ledger mode: the schedule's post-sweep
        # ``distributable_surplus`` already respects the min-cash floor — the
        # distribution can never sweep the company below its operating floor.
        # Legacy mode: the pre-existing post-sweep-residual formula, unchanged.
        if ledger_mode:
            dist_now = [
                round(
                    float(operating_by_year[yr]["distribution_pct"])
                    * float(schedule[yr].get("distributable_surplus", 0.0)),  # SA-OK: dict.get on the in-memory schedule row, not a DB query
                    2,
                )
                for yr in range(num_years)
            ]
        else:
            dist_now = [
                _distribution_for_year(
                    operating_by_year[yr],
                    next_fcf[yr],
                    float(schedule[yr]["total_amortization"]),
                )
                for yr in range(num_years)
            ]

        # Convergence on the per-year ending balance (AC-CONVERGENCE) — and, in
        # ledger mode, on the distribution vector too (it feeds the cash ledger,
        # so a still-moving distribution is a still-moving fixed point).
        dist_stable = (not ledger_mode) or all(
            abs(dist_now[yr] - prev_dist[yr]) < tol for yr in range(num_years)
        )
        if (
            prev_ending is not None
            and dist_stable
            and all(abs(ending[yr] - prev_ending[yr]) < tol for yr in range(num_years))
        ):
            return {
                "debt_schedule": debt_result,
                "fcf_by_year": next_fcf,
                "interest_by_year": lines["interest"],
                "distributions": dist_now,
                "net_income_by_year": lines["net_income"],
                "ebt_by_year": lines["ebt"],
                "taxable_income_by_year": lines["taxable"],
                "taxes_by_year": lines["taxes"],
                "nol_used_by_year": lines["nol_used"],
                "nol_balance_by_year": lines["nol_balance"],
                "iterations": iteration,
                "converged": True,
            }

        prev_ending = ending
        prev_dist = dist_now
        fcf_by_year = next_fcf

    raise WaterfallConvergenceError(
        f"waterfall did not converge within {max_iter} iterations "
        f"(per-year ending-balance tolerance ${tol})"
    )

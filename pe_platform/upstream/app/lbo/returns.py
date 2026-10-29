"""
app/lbo/returns.py
──────────────────
Canonical return-metric helpers shared by the LBO exit waterfall
(`app/lbo/exit_waterfall.py`) and the FP&A exit-planning module
(`app/fpa/exit_planning.py`). This is the SINGLE source of truth for IRR so the
two call-sites cannot drift (one canonical financial implementation).

IRR is solved as a TRUE internal rate of return over the period-indexed cash
flow vector — NOT the closed-form ``MOIC ** (1 / hold) - 1`` shortcut. That
shortcut is valid ONLY when there are zero interim cash flows; the moment a
deal has an interim distribution / dividend recap / partial exit, the timing of
the flows matters and the shortcut is arithmetically wrong (FIND-MONEY-002).

The solver finds the real roots of the NPV polynomial in ``x = 1 / (1 + r)`` —
the same method ``numpy_financial.irr`` uses — which is robust to interim flows
and needs no extra dependency (numpy is already a project dependency).

Two solvers live here, and they answer DIFFERENT questions:

* :func:`irr` — the PERIOD-indexed IRR: flows are exactly one uniform period
  (annual) apart. Correct when the model has no real calendar dates.
* :func:`xirr` — the DATED XIRR (Decision D4): flows carry real calendar dates
  and the discounting is over ACTUAL day-counts (Actual/365). This is what an
  underwriter means by "the deal IRR" — anchored on the expected transaction
  close date, day-count-accurate, so a mid-year close or an off-cycle exit is
  priced correctly (a period-count IRR silently mis-times both). ``numpy_financial``
  ships no dated solver (and is not a project dependency), so ``xirr`` rolls its
  own bracketed root-find over ``XNPV(r) = 0`` using ``scipy.optimize.brentq``
  (scipy is already a pinned project dependency).
"""
from __future__ import annotations

import datetime

import numpy as np
from scipy.optimize import brentq


class IRRError(ValueError):
    """No economically-meaningful IRR exists for the given cash flows.

    Raised when the NPV polynomial has no real root with ``r > -1`` — e.g. a
    flow vector that never changes sign. Callers MUST surface this (raise /
    typed error) and never fall back to the ``MOIC ** (1 / hold) - 1`` shortcut,
    which would silently re-introduce FIND-MONEY-002.
    """


def irr(cashflows: list[float]) -> float:
    """True periodic IRR over uniform-period cash flows.

    Returns the rate ``r`` such that ``sum(cf_t / (1 + r) ** t) == 0`` for
    ``t = 0 .. N`` (uniform periods — annual in the LBO/FP&A models). The flow
    at ``t = 0`` is the entry outflow (negative); later indices are the interim
    and terminal inflows.

    Args:
        cashflows: Period-indexed flows, ``cashflows[0]`` at ``t = 0``.

    Returns:
        The internal rate of return as a decimal (``0.15`` == 15%).

    Raises:
        IRRError: when the flows do not change sign, or the NPV polynomial has
            no real root with ``r > -1``.
    """
    cfs = [float(c) for c in cashflows]
    if len(cfs) < 2:
        raise IRRError("IRR requires at least two cash flows (t0 outflow + a later flow)")

    # NPV(r) = 0 has a root with r > -1 only if the flows change sign. An
    # all-positive or all-negative vector (e.g. a total wipeout with no terminal
    # inflow) has no IRR — surface it, never fabricate a rate.
    if all(c >= 0 for c in cfs) or all(c <= 0 for c in cfs):
        raise IRRError("IRR undefined: cash flows do not change sign")

    # NPV(r) = Σ_t cf_t · x^t  with  x = 1 / (1 + r).  numpy.roots wants the
    # highest-degree coefficient first, so the coefficient vector is the flows
    # reversed: [cf_N, cf_{N-1}, ..., cf_0].
    roots = np.roots(cfs[::-1])

    candidates: list[float] = []
    for z in roots:
        # Discard complex roots (relative imaginary tolerance for numerical noise).
        if abs(z.imag) > 1e-7 * (1.0 + abs(z.real)):  # pragma: no mutate  # equivalent: relative complex-root discard tolerance — killing a mutated threshold/operator needs an np.roots root whose |imag| lands in a measure-zero band (Wave-2 A4 mutation triage)
            continue
        x = z.real
        if x <= 0:  # x = 1/(1+r) must be positive, i.e. r > -1  # pragma: no mutate  # equivalent: x<=0 vs x<0 — a numpy x==0 root yields inf (not ZeroDivisionError) and never wins sort(key=abs); x==0-only vectors are pre-caught by the sign-change guard (Wave-2 A4)
            continue
        candidates.append(1.0 / x - 1.0)

    if not candidates:
        raise IRRError("IRR has no real root with r > -1 for the given cash flows")

    # With a single sign change (entry outflow + non-negative later flows) there
    # is exactly one such root. For pathological multi-sign-change vectors IRR is
    # genuinely ambiguous; return the rate nearest zero as the deterministic
    # principal IRR.
    candidates.sort(key=abs)
    return candidates[0]


def xirr(dated_flows: list[tuple[datetime.date, float]]) -> float:
    """True DATED internal rate of return (XIRR) over real-calendar cash flows.

    Solves ``XNPV(r) = Σ cf_i / (1 + r) ** ((d_i − d_0) / 365) == 0`` for ``r``,
    where ``d_0`` is the earliest flow date and every exponent is the ACTUAL number
    of days from ``d_0`` (Actual/365 day-count). This is the day-count-accurate
    analogue of :func:`irr`: it is the correct IRR when the flows land on real
    calendar dates (an expected transaction close date + real period-end dates)
    rather than uniform annual periods (Decision D4). A deal whose close date is
    mid-fiscal-year, or whose exit is off-cycle, has a hold that is NOT an integer
    number of years — the period-count :func:`irr` mis-times those flows; ``xirr``
    prices them on the real day-count.

    Args:
        dated_flows: ``(date, amount)`` pairs — the entry outflow (negative) plus
            the interim/terminal inflows, in any order (``d_0`` is taken as the
            minimum date). Amounts are money, not per-period.

    Returns:
        The internal rate of return as an annualised decimal (``0.15`` == 15%).

    Raises:
        IRRError: fewer than two flows, flows that never change sign, or no root
            with ``r > -1`` — the SAME typed-error contract as :func:`irr`, so a
            caller surfaces it and NEVER falls back to a ``MOIC ** (1/hold) − 1``
            shortcut (FIND-MONEY-002 / AP-FIN-02).
    """
    flows = [(d, float(cf)) for d, cf in dated_flows]
    if len(flows) < 2:
        raise IRRError("XIRR requires at least two dated cash flows (t0 outflow + a later flow)")

    amounts = [cf for _, cf in flows]
    # A dated NPV has a root with r > -1 only if the flows change sign — identical
    # reasoning to the period IRR. An all-one-sign vector (e.g. a total wipeout with
    # no terminal inflow) has no XIRR; surface it, never fabricate a rate.
    if all(cf >= 0 for cf in amounts) or all(cf <= 0 for cf in amounts):
        raise IRRError("XIRR undefined: cash flows do not change sign")

    d0 = min(d for d, _ in flows)
    # (year-fraction, amount) — Actual/365 day-count from the earliest date.
    terms = [(((d - d0).days) / 365.0, cf) for d, cf in flows]

    def _xnpv(rate: float) -> float:
        return sum(cf / (1.0 + rate) ** t for t, cf in terms)

    # Bracket a sign change of XNPV in r ∈ (−1, ∞). As r → −1⁺ the latest-dated flow
    # (the exit inflow) dominates → +∞; as r → ∞ only the t=0 entry outflow survives
    # → negative. So a widening upper bound eventually flips the sign for the
    # standard single-sign-change deal; a pathological vector that never brackets
    # raises IRRError rather than returning a fabricated rate.
    lo = -1.0 + 1e-9
    f_lo = _xnpv(lo)
    hi = 1.0
    f_hi = _xnpv(hi)
    tries = 0
    while f_lo * f_hi > 0.0 and tries < 256:
        hi *= 1.5
        f_hi = _xnpv(hi)
        tries += 1
    if f_lo * f_hi > 0.0:
        raise IRRError("XIRR has no real root with r > -1 for the given cash flows")

    return float(brentq(_xnpv, lo, hi, xtol=1e-12, maxiter=256))

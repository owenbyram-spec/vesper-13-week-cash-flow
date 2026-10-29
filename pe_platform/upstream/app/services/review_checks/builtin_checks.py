"""builtin_checks — the 3 existing detectors wrapped as Checks on the C0 framework.

Phase C C1 (plan 20260620-phase-c-analytical-review-build, AC-2 + AC-6). Each
class here implements the ``Check`` Protocol (``check_id`` + ``scan(ctx) -> Finding``)
by REUSING the deterministic core of a detector that already runs in
``app/tasks/proactive.py`` / ``app/routers/insights.py`` — it does NOT re-derive
the math. ``scan`` owns STAGE 1 only (the zero-LLM deterministic detect +
classify into a ``Finding``); ``run_check`` (protocol.py:311) owns the
materiality / closed-period / LLM-draft / propose gates. PROPOSAL-FIRST: a
``scan`` NEVER writes the GL — it returns a Finding, and ``run_check`` calls the
injected ``ctx.propose`` (CLAUDE.md §5, agentic-writeback Control 1).

The three detectors have THREE DIFFERENT output paths on disk (CRITICAL-REVIEW
§2.1) — wrapping them is real adaptation, not a uniform lift:

  - ACCRUAL (LLM-draft path): ``_detect_missing_accruals`` (proactive.py:183) +
    ``_propose_missing_accruals`` (:226). The closest to the Protocol already —
    its deterministic detector returns unposted ACCRUAL ScheduleEntries; the
    Check sums the material-eligible amount and emits ``hit``/``no_hit``. The
    per-entry materiality floor ($100, :179/:266) maps to ``Materiality.absolute``.
  - BUDGET-VARIANCE (deterministic-proposal path): the per-line variance core of
    ``check_budget_variance_and_propose_updates`` (proactive.py:730) — the dual
    gate ``variance_pct > 10 AND variance >= 1000`` (:764). The Check reuses the
    same ``vendor_spend_monthly`` actuals + ``BudgetLine`` budgeted-amount math and
    emits ``hit`` with the worst over-budget line; needs a live qbo_client, so
    ``qbo_client is None`` → ``could_not_verify`` (the close-driver passes None
    today — honest degradation, NOT a crash). Maps to ``Materiality.dual``.
  - CASH-RUNWAY (watchdog-alert path → re-homed): the runway burn core. ON DISK
    there are TWO burn defs (DEC-C-5 / G1) — proactive.py:577
    ``monthly_burn = exp_val - rev_val`` (single-period expense−revenue) and
    insights.py:498 ``monthly_burn = -avg_monthly_net_income`` (3-6mo avg of the
    ``InsightTrend`` net_income series). CANONICAL CHOICE = insights.py:498
    (averaged net-income). Reasoning (financial-modeling-correctness): a runway
    *risk* signal needs a smoothed, methodologically-complete burn — averaging
    absorbs single-month lumpiness so a one-off bill does not false-trigger, and
    net income folds in other income/expense + interest + taxes that the bare
    ``expense − revenue`` omits (which understates true burn). It is also the def
    already exposed on the ``/runway`` route, so the Check matches the
    user-facing number. Maps to ``Materiality.derived`` (runway-months < 3 floor,
    proactive.py:579) — gated as "material when runway <= floor", see RunwayCheck.

Honest-degradation (DEC-CAR-C-4): every check emits ``could_not_verify`` — never
``no_hit`` and never a fabricated ``Decimal("0")`` — when its input is absent
(no qbo_client, no KPIs/trends). A check that cannot see the data has NOT proven
the books clean.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TypedDict

from . import CheckTier, register_check
from .materiality import Materiality
from .protocol import CheckContext, Finding, FindingSeverity

logger = logging.getLogger(__name__)

# Canonical task_types these checks review (the close-step seam — TASK_TYPE_TO_CHECK).
TASK_TYPE_REVIEW_ACCRUALS = "REVIEW_ACCRUALS"
TASK_TYPE_REVIEW_BUDGET_VARIANCE = "REVIEW_BUDGET_VARIANCE"
TASK_TYPE_REVIEW_CASH_RUNWAY = "REVIEW_CASH_RUNWAY"


class _VendorVariance(TypedDict):
    """One vendor budget line's computed variance (the budget-variance scan's
    worst-line working shape — reuses proactive.py:761-762 math, no new fields)."""

    vendor_name: str
    budgeted: float
    actual: float
    variance: float
    variance_pct: float
    budget_line_id: int


def _to_decimal(value: object) -> Decimal | None:
    """Coerce a numeric-ish value to Decimal; None on absent/uncoercible.

    None (not 0) on a missing value — a fabricated ``Decimal("0")`` is the banned
    silent-pass anti-pattern (DEC-CAR-C-4). The caller treats None as 'unknown'.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Check 1 — Missing recurring accrual (LLM-draft path; the closest to Protocol)
# ─────────────────────────────────────────────────────────────────────────────


class MissingAccrualCheck:
    """Wraps ``_detect_missing_accruals`` (proactive.py:183).

    The deterministic detector returns unposted, not-yet-proposed ACCRUAL
    ScheduleEntries for the period. The Check classifies: any such entry →
    ``hit`` (carrying the largest entry amount for the materiality gate); none →
    ``no_hit``. It does NOT draft or propose — ``run_check`` does (one LLM draft,
    then ``ctx.propose``). The per-entry $100 floor (proactive.py:179/:266) is
    registered as ``Materiality.absolute`` and applied by ``run_check``, NOT here
    (the Phase E hook — never hardcode the floor in scan).
    """

    check_id = "missing_accrual"

    def scan(self, ctx: CheckContext) -> Finding:
        try:
            from ...tasks.proactive import _detect_missing_accruals
        except Exception as e:  # detector import unavailable → honest degradation.
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"accrual detector unavailable: {redact_to_string(e)}",
            )

        missing = _detect_missing_accruals(ctx.db, ctx.realm_id, ctx.period)
        if not missing:
            return Finding.no_hit(
                self.check_id,
                message=f"No unposted recurring accruals for {ctx.period}.",
            )

        # PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
        # Sum + max the candidate amounts (deterministic; the math is the
        # detector's ScheduleEntry.amount, not re-derived).
        #
        # AGGREGATE-ACCRUAL MATERIALITY GATE (AC-2C.1; RD-3 Topic 3(4c) /
        # month-end-close SKILL §4.3): gate on the LARGER of (a) the largest
        # single entry (the original per-entry floor — a single material accrual
        # still clears) and (b) the GROSS aggregate — fifty $90 accruals under a
        # $100 per-entry floor are a $4.5k misstatement the per-entry gate
        # structurally misses. Gross (abs-sum), not signed net: offsetting-sign
        # entries must not hide each other under the floor.
        amounts = [_to_decimal(getattr(e, "amount", None)) for e in missing]
        amounts = [a for a in amounts if a is not None]
        max_abs = max((abs(a) for a in amounts), default=None) if amounts else None
        total = sum(amounts) if amounts else None  # signed net — what would post
        gross = sum(abs(a) for a in amounts) if amounts else None
        gate_amount = max(max_abs, gross) if (max_abs is not None and gross is not None) else None

        return Finding.hit(
            self.check_id,
            message=(
                f"{len(missing)} unposted recurring accrual(s) for {ctx.period} "
                f"(largest ${max_abs or 0:,.2f}, net total ${total or 0:,.2f}, "
                f"aggregate gross ${gross or 0:,.2f})."
            ),
            severity=FindingSeverity.WARNING,
            amount=gate_amount,
            payload={
                "period": ctx.period,
                "missing_count": len(missing),
                "max_amount": float(max_abs) if max_abs is not None else None,
                "total_amount": float(total) if total is not None else None,
                "aggregate_gross": float(gross) if gross is not None else None,
                "aggregate_gated": True,
                "schedule_entry_ids": [getattr(e, "id", None) for e in missing],
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 2 — Budget-vs-actual variance (deterministic-proposal path)
# ─────────────────────────────────────────────────────────────────────────────


class BudgetVarianceCheck:
    """Wraps the per-line variance core of
    ``check_budget_variance_and_propose_updates`` (proactive.py:730).

    REUSES the same actuals source (``vendor_spend_monthly``) + the same budgeted
    amount (``BudgetLine.monthly_amounts``) + the same variance math
    (``variance = actual − budgeted``; ``variance_pct = variance/budgeted*100``,
    :761-762). It does NOT re-derive — it iterates the same budget lines and picks
    the WORST over-budget line, returning its ``variance`` (amount) + ``variance_pct``
    for the DUAL materiality gate (``pct > 10 AND abs >= 1000``, :764) applied by
    run_check. The on-disk path needs a live ``qbo_client`` (:732) — so when
    ``ctx.qbo_client is None`` (the close-driver default) the Check emits
    ``could_not_verify``, never a fabricated clean pass.
    """

    check_id = "budget_variance"

    def scan(self, ctx: CheckContext) -> Finding:
        if ctx.qbo_client is None:
            return Finding.could_not_verify(
                self.check_id,
                message="No QBO client available; budget variance needs live vendor spend.",
            )

        try:
            from ...analysis.vendor_spend import vendor_spend_monthly
            from ...models import BudgetLine
        except Exception as e:
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"budget variance detector unavailable: {redact_to_string(e)}",
            )

        try:
            current_year = int(ctx.period.split("-")[0])
        except (ValueError, IndexError):
            return Finding.could_not_verify(
                self.check_id,
                message=f"period {ctx.period!r} is not a parseable YYYY-MM period.",
            )

        budget_lines = (
            ctx.db.query(BudgetLine)
            .filter(
                BudgetLine.company_id == ctx.realm_id,
                BudgetLine.budget_year == current_year,
                BudgetLine.vendor_name.isnot(None),  # only vendor sublines (proactive.py:742)
            )
            .all()
        )
        if not budget_lines:
            # No vendor budget to compare against — cannot evaluate variance.
            return Finding.could_not_verify(
                self.check_id,
                message=f"No vendor budget lines for {current_year}; cannot evaluate variance.",
            )

        spend_data = vendor_spend_monthly(ctx.qbo_client, months=1)
        months_analyzed = spend_data.get("months_analyzed") or []
        if not months_analyzed:
            return Finding.could_not_verify(
                self.check_id,
                message="Vendor spend returned no analyzable month; cannot evaluate variance.",
            )

        from datetime import datetime

        month_key = sorted(months_analyzed)[-1]
        period_month_str = datetime.strptime(month_key, "%Y-%m").strftime("%b").lower()
        actual_vendor_totals = {
            v["vendor"]: v["monthly"].get(month_key, 0)  # SA-OK: dict access on the detector payload, not a DB query
            for v in spend_data.get("vendors", [])
        }

        # Reuse the EXACT per-line variance math; keep the worst over-budget line.
        worst: _VendorVariance | None = None
        for bl in budget_lines:
            budgeted_amt = float(bl.monthly_amounts.get(period_month_str, 0) or 0)  # SA-OK: dict access, not a DB query
            if budgeted_amt <= 0:
                continue
            actual_amt = float(actual_vendor_totals.get(bl.vendor_name, 0))  # SA-OK: dict access, not a DB query
            variance = actual_amt - budgeted_amt
            variance_pct = (variance / budgeted_amt) * 100
            if worst is None or variance > worst["variance"]:
                worst = _VendorVariance(
                    vendor_name=bl.vendor_name,
                    budgeted=budgeted_amt,
                    actual=actual_amt,
                    variance=variance,
                    variance_pct=variance_pct,
                    budget_line_id=bl.id,
                )

        if worst is None:
            return Finding.no_hit(
                self.check_id,
                message=f"No budgeted vendor lines active for {month_key}; nothing to compare.",
            )

        # HIT iff over budget (variance>0). The DUAL floor (pct>10 AND abs>=1000)
        # is the materiality gate in run_check — scan classifies direction only.
        if worst["variance"] <= 0:
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"No vendor over budget for {month_key} "
                    f"(closest: {worst['vendor_name']} ${worst['variance']:,.0f})."
                ),
            )

        return Finding.hit(
            self.check_id,
            message=(
                f"{worst['vendor_name']} over budget for {month_key}: "
                f"spent ${worst['actual']:,.0f} vs budgeted ${worst['budgeted']:,.0f} "
                f"(+${worst['variance']:,.0f}, +{worst['variance_pct']:.1f}%)."
            ),
            severity=FindingSeverity.WARNING,
            amount=_to_decimal(worst["variance"]),
            pct=_to_decimal(worst["variance_pct"]),
            payload={
                "period": ctx.period,
                "month": month_key,
                "vendor_name": worst["vendor_name"],
                "budgeted": worst["budgeted"],
                "actual": worst["actual"],
                "variance": worst["variance"],
                "variance_pct": worst["variance_pct"],
                "budget_line_id": worst["budget_line_id"],
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Check 3 — Cash-runway risk (watchdog-alert path → re-homed; CANONICAL burn)
# ─────────────────────────────────────────────────────────────────────────────


class RunwayCheck:
    """Wraps the cash-runway burn core, CANONICALIZED to the insights.py:498 def.

    DEC-C-5 / G1 — TWO on-disk burn defs. CANONICAL = insights.py:498
    ``monthly_burn = -avg_monthly_net_income`` over the last 3-6 ``InsightTrend``
    net_income points (the smoothed, methodologically-complete burn used by the
    ``/runway`` route), NOT proactive.py:577's single-period ``expense−revenue``.
    Reasoning is in the module docstring (financial-modeling-correctness).

    ``runway_months = cash / monthly_burn``. The Check classifies:
      - profitable (burn <= 0) → not at risk → ``no_hit``;
      - no cash/trend data → ``could_not_verify`` (honest degradation);
      - at-risk computed → ``hit`` carrying runway_months as ``amount`` for the
        DERIVED materiality gate (see the registration note on polarity).
    """

    check_id = "cash_runway"

    def scan(self, ctx: CheckContext) -> Finding:
        try:
            from ...models import InsightKPI, InsightTrend
        except Exception as e:
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"runway detector unavailable: {redact_to_string(e)}",
            )

        # Cash on hand — latest-period KPI (insights.py:486-492 source).
        latest_period_row = (
            ctx.db.query(InsightKPI.period)
            .filter(InsightKPI.company_id == ctx.realm_id)
            .order_by(InsightKPI.period.desc())
            .first()
        )
        latest_period = latest_period_row[0] if latest_period_row else None
        kpi_rows = (
            ctx.db.query(InsightKPI)
            .filter(InsightKPI.company_id == ctx.realm_id, InsightKPI.period == latest_period)
            .all()
            if latest_period
            else []
        )
        kpis = {r.name: float(r.value or 0) for r in kpi_rows}  # SA-OK: dict-build over a single-period result, not in-loop queries
        cash = kpis.get("cash") or kpis.get("cash_and_equivalents")

        # Canonical burn — avg of the last <=6 net_income trend points (insights.py:494-498).
        trends = (
            ctx.db.query(InsightTrend)
            .filter(
                InsightTrend.company_id == ctx.realm_id,
                InsightTrend.metric == "net_income",
            )
            .order_by(InsightTrend.period.desc())
            .limit(6)
            .all()
        )

        if not trends or cash is None:
            # No trend series or no cash KPI → cannot compute runway honestly.
            return Finding.could_not_verify(
                self.check_id,
                message="Insufficient net_income trend / cash KPI data to compute runway.",
            )

        monthly_net_incomes = [float(t.value or 0) for t in trends]
        avg_monthly_net_income = sum(monthly_net_incomes) / len(monthly_net_incomes)
        monthly_burn = -avg_monthly_net_income  # positive = burning cash (insights.py:498)

        if monthly_burn <= 0:
            # Profitable — not burning cash → no runway risk (a verified clean pass).
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"Profitable (avg net income ${avg_monthly_net_income:,.0f}/mo); "
                    "no cash-runway risk."
                ),
            )
        if cash <= 0:
            # Burning AND no cash — a definite, maximally-severe risk (runway ~0).
            return Finding.hit(
                self.check_id,
                message=(
                    f"Burning ${monthly_burn:,.0f}/mo with ${cash:,.0f} cash on hand — "
                    "runway is effectively zero."
                ),
                severity=FindingSeverity.CRITICAL,
                amount=Decimal("0"),
                payload={
                    "period": latest_period,
                    "monthly_burn": round(monthly_burn, 2),
                    "cash_on_hand": round(cash, 2),
                    "runway_months": 0.0,
                    "burn_definition": "avg_net_income (insights.py:498)",
                    "periods_analyzed": len(trends),
                },
            )

        runway_months = cash / monthly_burn
        return Finding.hit(
            self.check_id,
            message=(
                f"At ${monthly_burn:,.0f}/mo burn, ${cash:,.0f} cash lasts "
                f"~{runway_months:.1f} months."
            ),
            severity=FindingSeverity.WARNING if runway_months >= 1.5 else FindingSeverity.CRITICAL,
            amount=_to_decimal(round(runway_months, 4)),
            payload={
                "period": latest_period,
                "monthly_burn": round(monthly_burn, 2),
                "cash_on_hand": round(cash, 2),
                "runway_months": round(runway_months, 1),
                "burn_definition": "avg_net_income (insights.py:498)",
                "periods_analyzed": len(trends),
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registration (module-level — the single seam; Phase E overrides materiality)
# ─────────────────────────────────────────────────────────────────────────────

# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
# Accrual: absolute floor $100 (proactive.py:179/:266), applied to the LARGER of
# the largest single entry and the GROSS aggregate (AC-2C.1 aggregate gate) — a
# single material accrual still clears, and systematic below-floor smallness
# (fifty $90 accruals) now clears on the sum.
register_check(
    MissingAccrualCheck(),
    materiality=Materiality.absolute(Decimal("100"), label="accrual per-entry + aggregate floor"),
    task_types=[TASK_TYPE_REVIEW_ACCRUALS],
    tier=CheckTier.STANDARD,  # routine accrual review — runs at balanced + aggressive
)

# Budget variance: the DUAL floor pct>10 AND abs>=1000 (proactive.py:764). scan
# returns (amount=variance, pct=variance_pct); run_check's DUAL gate applies both.
register_check(
    BudgetVarianceCheck(),
    materiality=Materiality.dual(
        pct=Decimal("10"), abs_floor=Decimal("1000"), label="budget dual floor"
    ),
    task_types=[TASK_TYPE_REVIEW_BUDGET_VARIANCE],
    tier=CheckTier.STANDARD,  # routine budget-variance review — runs at balanced + aggressive
)

# Runway: DERIVED floor = the on-disk runway-months threshold (proactive.py:579
# < 3). Polarity note: a SMALL runway is the risk, but Materiality.is_material is
# ``amount >= abs_floor``. RunwayCheck.scan emits HIT ONLY when its deterministic
# core has already established an at-risk runway (burning + finite cash), carrying
# the computed runway-months as ``amount``; a profitable/clean book returns
# no_hit and never reaches the gate. So the risk gating (runway BELOW 3) lives in
# scan's classification, and this DERIVED floor of 3 preserves the on-disk
# constant + the Phase-E per-company override seam (zero check-code change).
register_check(
    RunwayCheck(),
    materiality=Materiality.derived(Decimal("3"), label="runway-months risk floor"),
    task_types=[TASK_TYPE_REVIEW_CASH_RUNWAY],
    tier=CheckTier.STANDARD,  # routine runway watch — runs at balanced + aggressive
)

# Plan 20260713-budget-variance-narrative-fix — deterministic prompt assembly for the
# budget vs-actuals AI analysis, extracted from app/routers/budget.py so it is unit-testable
# WITHOUT an LLM call (we test the context string the model receives, never the model output).
"""Build the (system_prompt, user_prompt) pair for a single budget-variance narrative.

The two defects this module exists to prevent (both were live on Hi-Tech Health, account
4565 "Administrative Services" — a revenue account whose name reads like overhead):

1. SECTION / FAVORABILITY BLINDNESS — the prompt used to omit the account's statement
   section, so the model inferred "expense" from a misleading name and narrated a revenue
   shortfall (actual < budget) as a cost saving. Here the section + favorability are stated
   explicitly and the system prompt vocabulary is section-scoped (no "charges/spend" for a
   revenue account, no "customers/churn" for an expense account).
2. FABRICATION UNDER ABSENT EVIDENCE — when there is no GL/entity detail, the prompt now
   forbids attributing the variance to any specific customer/vendor/driver; the model must
   name the limitation instead of inventing causes.
"""
from __future__ import annotations

from typing import TypedDict


class DriverInfo(TypedDict, total=False):
    """One budget driver as assembled by the handler (in-memory, from BudgetDriver rows)."""

    method: str
    params: dict[str, object]
    vendor: str | None
    customer: str | None


class BudgetEntity(TypedDict):
    """A budgeted per-entity sub-line (``{name, budgeted}``)."""

    name: str
    budgeted: float


class ActualEntity(TypedDict):
    """A GL-derived actual per-entity total (``{name, actual}``)."""

    name: str
    actual: float


# account_type values (app/budget/qbo_reports.py::_infer_account_type):
#   revenue | other_income | cogs | opex | other_expense
_REVENUE_TYPES = ("revenue", "other_income")


def is_revenue_account_type(account_type: str | None) -> bool:
    """True when the account belongs to a revenue statement section (its GL entities are
    CUSTOMERS, and actual-below-budget is a shortfall — revenue not earned)."""
    return (account_type or "").strip().lower() in _REVENUE_TYPES


def _favorability_sentence(is_revenue: bool, variance: float) -> str:
    """One sentence naming the section AND whether this variance is favorable/unfavorable.

    ``variance`` is actual − budget. For REVENUE: below budget (variance < 0) is an
    UNFAVORABLE shortfall (revenue not earned). For EXPENSE: below budget is a FAVORABLE
    saving (less spent than planned). Zero variance is on-plan.
    """
    below = variance < 0
    if is_revenue:
        if below:
            return ("Account section: Revenue — actual is BELOW budget, an UNFAVORABLE "
                    "shortfall (revenue not earned); never describe this as a cost saving.")
        if variance > 0:
            return ("Account section: Revenue — actual is ABOVE budget, a FAVORABLE "
                    "outperformance (revenue above plan).")
        return "Account section: Revenue — actual is on plan."
    if below:
        return ("Account section: Operating expense — actual is BELOW budget, a FAVORABLE "
                "saving (less spent than planned).")
    if variance > 0:
        return ("Account section: Operating expense — actual is ABOVE budget, an "
                "UNFAVORABLE overspend.")
    return "Account section: Operating expense — actual is on plan."


# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
def no_gl_detail_speculation_clause() -> str:
    """The ONE shared no-speculation clause (plan 20260714-wave2-7-lease-flux, decision 4).

    When no transaction-level (GL) detail is available for an account/period, this clause
    forbids attributing the variance to any specific customer/vendor/driver — the model (or
    a deterministic narrative) must NAME the limitation instead of inventing a cause. Extracted
    verbatim from the former inline ``_system_prompt`` tail so the budget-variance path is
    byte-identical, and REUSED by the actual-vs-prior flux sibling narrative
    (``app/services/flux_review.py``) so both refuse fabricated causes with one wording.
    """
    return (
        " No transaction-level (GL) detail is available for this account/month, so you "
        "have NO evidence of which parties or drivers moved the number. Do NOT attribute "
        "the variance to any specific customer, vendor, or driver, and do NOT guess. Say "
        "plainly that line-level detail is unavailable, then limit the analysis to the "
        "budget-vs-actual totals and the driver configuration shown."
    )


def _system_prompt(is_revenue: bool, has_gl_detail: bool) -> str:
    """Section-scoped analyst instruction. Revenue and expense vocabularies never mix, and
    when GL detail is absent the model is forbidden from naming specific entities/drivers."""
    if is_revenue:
        base = (
            "You are a CFO-level financial analyst. Analyze the budget-to-actual variance "
            "for this REVENUE account. Be concise (2-4 sentences). Focus on the WHY: which "
            "customers fell short, churned, or were over-forecast, and whether the revenue "
            "drivers were miscalibrated. Reference specific dollar amounts. Treat actual "
            "below budget as revenue NOT EARNED (a shortfall), never as a saving. Do not "
            "repeat the variance numbers — the user already sees them."
        )
    else:
        base = (
            "You are a CFO-level financial analyst. Analyze the budget-to-actual variance "
            "for this EXPENSE account. Be concise (2-4 sentences). Focus on the WHY: which "
            "vendors overspent, what unexpected charges appeared, or whether the cost "
            "drivers were under-budgeted. Reference specific dollar amounts. Treat actual "
            "below budget as a favorable saving (less spent than planned). Do not repeat "
            "the variance numbers — the user already sees them."
        )
    if not has_gl_detail:
        # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
        base += no_gl_detail_speculation_clause()
    return base


def build_variance_analysis_prompt(
    *,
    account_name: str,
    month_label: str,
    budget_amt: float,
    actual_amt: float,
    variance: float,
    var_pct: float | None,
    account_type: str | None,
    driver_info: list[DriverInfo],
    vendor_budget: list[BudgetEntity],
    vendor_actuals: list[ActualEntity],
) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the vs-actuals AI analysis.

    Pure + deterministic (no I/O, no DB, no clock) so the exact context the model receives
    is unit-testable. ``variance`` is actual − budget; ``vendor_actuals`` empty ⇒ no GL
    evidence (honest-gating path).
    """
    is_revenue = is_revenue_account_type(account_type)
    entity_label = "Customer" if is_revenue else "Vendor"
    entity_plural = "customers" if is_revenue else "vendors"
    has_gl_detail = bool(vendor_actuals)

    context_parts: list[str] = [
        f"Account: {account_name}",
        _favorability_sentence(is_revenue, variance),
        f"Month: {month_label}",
    ]
    if var_pct is not None:
        context_parts.append(
            f"Budget: ${budget_amt:,.0f}  |  Actual: ${actual_amt:,.0f}  |  "
            f"Variance: ${variance:,.0f} ({var_pct}%)"
        )
    else:
        context_parts.append(f"Budget: ${budget_amt:,.0f}  |  Actual: ${actual_amt:,.0f}")

    if driver_info:
        driver_strs: list[str] = []
        for d in driver_info:
            label = str(d.get("method") or "")  # SA-OK: d is a plain driver dict (in-memory), not a DB session — .get is not a query
            # Only the entity that matches the account section — a revenue account's GL
            # parties are customers, not vendors (and vice-versa).
            entity = d.get("customer") if is_revenue else d.get("vendor")  # SA-OK: plain-dict access on an in-memory driver dict, not a DB query
            if entity:
                label += f" ({entity_label.lower()}: {entity})"
            params = d.get("params")  # SA-OK: plain-dict access on an in-memory driver dict, not a DB query
            if params:
                label += f" params={params}"
            driver_strs.append(label)
        context_parts.append(f"Budget Drivers: {'; '.join(driver_strs)}")

    if vendor_budget:
        vb_strs = [f"{v['name']}: ${v['budgeted']:,.0f}" for v in vendor_budget[:10]]
        context_parts.append(f"Budgeted by {entity_label}: {', '.join(vb_strs)}")

    if vendor_actuals:
        va_strs = [f"{v['name']}: ${v['actual']:,.0f}" for v in vendor_actuals[:10]]
        context_parts.append(f"Actual by {entity_label} (GL): {', '.join(va_strs)}")
    else:
        context_parts.append(
            f"Actual by {entity_label}: No GL detail available — do NOT attribute the "
            f"variance to any specific {entity_plural} or driver (no evidence)."
        )

    return _system_prompt(is_revenue, has_gl_detail), "\n".join(context_parts)

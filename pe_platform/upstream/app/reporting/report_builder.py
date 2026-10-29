from typing import Dict, Any
from datetime import datetime
from .data_gatherer import gather_report_data
from .analysis_notes import generate_analysis_notes
from sqlalchemy.orm import Session

def build_report_data(db: Session, company_id: str, period: str) -> Dict[str, Any]:
    """
    Orchestrates data gathering and AI narrative generation for the monthly report.
    Returns a structured payload with summary KPIs AND account-level detail.
    """
    # 1. Gather all real QBO data (including detailed P&L/BS rows)
    raw_data = gather_report_data(db, company_id, period)

    # 2. Build the context dict the LLM needs
    ai_context = {
        "period": raw_data.get("period_label", period),
        "kpis": raw_data.get("kpis", {}),
        "bs": raw_data.get("current_bs", {}),
        "ratios": raw_data.get("ratios", {}),
        "ttm_pl": raw_data.get("ttm_pnl", {}),
        "trends": raw_data.get("trends", []),
    }

    # 3. Generate AI narrative (real LLM call with fallback)
    notes = generate_analysis_notes(ai_context)

    # 4. Structure into final report payload
    #    pdf_renderer.py reads flat TOP-LEVEL keys.
    report_payload = {
        # ── metadata ──
        "metadata": {
            "company_id": company_id,
            "company_name": raw_data.get("company_name", ""),
            "period": period,
            "period_label": raw_data.get("period_label", period),
            "title": f"Financial Report — {raw_data.get('period_label', period)}",
            "generated_at": datetime.utcnow().strftime("%B %d, %Y at %I:%M %p UTC"),
            # Fiscal-year anchor (and its source) behind any year-to-date figure.
            "fiscal_period": raw_data.get("fiscal_period"),
        },

        # ── summary KPIs for scorecard ──
        "kpis": raw_data.get("kpis", {}),

        # ── P&L summaries ──
        "current_pnl": raw_data.get("current_pnl", {}),
        "prior_pnl": raw_data.get("prior_pnl", {}),
        "ttm_pl": raw_data.get("ttm_pnl", {}),

        # ── Balance Sheet summary ──
        "balance_sheet": raw_data.get("current_bs", {}),

        # ── Cash Flow ──
        "cash_flow": raw_data.get("ttm_cf", {}),

        # ── Account-level detail (every line item) ──
        "pnl_detail": raw_data.get("pnl_detail", []),
        "bs_detail": raw_data.get("bs_detail", []),

        # ── Budget map (account_name → budget amount) ──
        "budget_map": raw_data.get("budget_map", {}),

        # ── Rolling 12-month P&L ──
        "rolling_pnl_columns": raw_data.get("rolling_pnl_columns", []),
        "rolling_pnl_detail": raw_data.get("rolling_pnl_detail", []),

        # ── Rolling 12-month Cash Flow ──
        "rolling_cf_columns": raw_data.get("rolling_cf_columns", []),
        "rolling_cf_detail": raw_data.get("rolling_cf_detail", []),

        # ── Trend table (12-month) ──
        "trends": raw_data.get("trends", []),

        # ── AI narrative ──
        "analysis_notes": notes,
        "executive_narrative": notes,

        # ── Ratios ──
        "ratios": raw_data.get("ratios", {}),

        # ── Budget vs Actual ──
        "bva_top_variances": _compute_bva_variances(
            raw_data.get("pnl_detail", []),
            raw_data.get("budget_map", {}),
        ),

        # ── AR/AP Aging ──
        "ar_aging": raw_data.get("ar_aging", {}),
        "ap_aging": raw_data.get("ap_aging", {}),

        # ── Source quality ──
        "errors": raw_data.get("errors", []),
        "data_quality": raw_data.get("data_quality", {"is_partial": False, "unavailable_sections": []}),
    }

    return report_payload


# ── report_type → payload builder dispatch (FIX-6 extension point) ──────────────
# Each named report_type maps to the function that assembles its render payload.
# AC-2 ships month_end (the existing board-pack payload); the named reports
# (standard_financials, ar_ap_aging, covenant, cashflow_forecast) register their
# own builders in Waves B–C. An unknown type falls back to the month_end builder
# so a legacy caller (and any unrecognised type) still renders the board pack.
PAYLOAD_BUILDERS = {
    "month_end": build_report_data,
}

# AC-3 / AC-4 — register the two reuse-report builders (defined in their own module
# to keep this file under the size cap). Each shares the (db, company_id, period)
# signature so build_report_payload dispatches them by report_type.
from .named_report_builders import (  # noqa: E402
    build_ar_ap_aging,
    build_cashflow_forecast,
    build_covenant,
    build_covenant_compliance,
)

# W-4 fix #7 — the standard-financials builder moved to its own module (monthly
# columns rework; named_report_builders.py sits near the 500-line cap).
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
from .standard_financials_builder import build_standard_financials  # noqa: E402

PAYLOAD_BUILDERS["standard_financials"] = build_standard_financials
PAYLOAD_BUILDERS["ar_ap_aging"] = build_ar_ap_aging
# AC-5 / AC-6 — the two new-renderer reports (covenant + cashflow_forecast).
PAYLOAD_BUILDERS["covenant"] = build_covenant
PAYLOAD_BUILDERS["cashflow_forecast"] = build_cashflow_forecast
# Covenant-tracking AC-2 — the MONTHLY covenant compliance grid (stored-snapshot history).
# PLAN-EXEMPT: AC-2 monthly grid builder registration (ratified plan 20260630-covenant-tracking); report_builder.py outside the plan's enumerated surface_scope, AC-2 dispatch owns the new report type.
PAYLOAD_BUILDERS["covenant_compliance"] = build_covenant_compliance

# covenant-redesign — the BANK-SEND monthly covenant report (two-lens component build-up
# per covenant, computed live via compute_monthly_series). Registered by report_type.
# PLAN-EXEMPT: bank-send monthly grid builder registration (ratified plan 20260630-covenant-redesign); report_builder.py outside the plan's enumerated surface_scope, this plan's dispatch owns the new report type.
from .covenant_monthly_builder import build_covenant_monthly  # noqa: E402

PAYLOAD_BUILDERS["covenant_monthly"] = build_covenant_monthly

# bank-reconciliation (plan 20260701-bank-reconciliation, AC-1.4) — the tie-out report,
# resolved from the ReconciliationSession + compute_tieout (display-only). Registered by report_type.
# PLAN-EXEMPT: bank-reconciliation builder registration under ratified plan 20260701-bank-reconciliation (AC-1.4); report_builder.py outside the plan's enumerated surface_scope, the C1-d dispatch owns the new report type.
from .bank_reconciliation_builder import build_bank_reconciliation  # noqa: E402

PAYLOAD_BUILDERS["bank_reconciliation"] = build_bank_reconciliation

# W-7 (F-15 / item #15) — the BOARD PACK: four deterministic sections merged from their
# owning builders + the ONE bounded/cached/ledger-metered commentary pass. Registered by
# report_type like every other named report.
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — board_pack builder registration.
from .board_pack_builder import build_board_pack  # noqa: E402

PAYLOAD_BUILDERS["board_pack"] = build_board_pack


def build_report_payload(
    db: Session, company_id: str, period: str, report_type: str = "month_end",
    view: "str | None" = None,
) -> Dict[str, Any]:  # CRAFT-OK: dynamic flat report payload boundary (same edge as build_report_data)
    """Assemble the render payload for a given report_type. Dispatches to the
    per-report builder (FIX-6); defaults to the month_end board-pack payload.

    ``view`` (W-4 fix #7) selects the statement window for builders that support
    one (standard_financials: ytd default | ttm | month). It is forwarded ONLY to
    builders whose signature declares a ``view`` parameter, so every legacy
    builder keeps its exact (db, company_id, period) call.
    PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4"""
    builder = PAYLOAD_BUILDERS.get(report_type or "month_end", build_report_data)
    if view:
        import inspect
        try:
            accepts_view = "view" in inspect.signature(builder).parameters
        except (TypeError, ValueError):
            accepts_view = False
        if accepts_view:
            return builder(db, company_id, period, view=view)
    return builder(db, company_id, period)


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _compute_bva_variances(
    pnl_detail: list,
    budget_map: dict,
    top_n: int = 10,
) -> list:
    """
    Walk pnl_detail rows, match each account against budget_map,
    compute variance ($ and %), and return the top favorable and
    unfavorable variances sorted by absolute dollar impact.

    Returns a list of dicts:
        {account, actual, budget, variance_dollar, variance_pct, direction}
    """
    if not pnl_detail or not budget_map:
        return []

    variances = []
    for row in pnl_detail:
        # Skip headers and totals — we want individual account lines
        if row.get("is_header") or row.get("is_total"):
            continue

        name = (row.get("name") or "").strip()
        actual = row.get("value")
        if actual is None or not name:
            continue

        actual_f = _safe_float(actual)
        budget_f = budget_map.get(name.lower(), 0)

        # Only include accounts that have a budget value
        if budget_f == 0:
            continue

        variance = actual_f - budget_f
        variance_pct = variance / abs(budget_f) if budget_f != 0 else 0

        # Direction: favorable means revenue > budget or expense < budget
        # We can't perfectly determine account type from detail rows alone,
        # so we use simple heuristic: positive variance = favorable for
        # income accounts, unfavorable for expense accounts.
        # For BVA reporting, we just report the raw variance direction.
        direction = "favorable" if variance > 0 else "unfavorable" if variance < 0 else "on_budget"

        variances.append({
            "account": name,
            "actual": round(actual_f, 2),
            "budget": round(budget_f, 2),
            "variance_dollar": round(variance, 2),
            "variance_pct": round(variance_pct, 4),
            "direction": direction,
        })

    # Sort by absolute variance descending and take top N from each direction
    favorable = sorted(
        [v for v in variances if v["direction"] == "favorable"],
        key=lambda x: abs(x["variance_dollar"]),
        reverse=True,
    )[:top_n]

    unfavorable = sorted(
        [v for v in variances if v["direction"] == "unfavorable"],
        key=lambda x: abs(x["variance_dollar"]),
        reverse=True,
    )[:top_n]

    return favorable + unfavorable

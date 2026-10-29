"""
app/reporting/section_registry.py
───────────────────────────────────
The section-driven report engine's REGISTRY: a `SectionType` describing how one
report section renders (Excel + PDF), plus the binders that pre-compute each
section's display data, plus `SECTION_REGISTRY` mapping section_id → SectionType
for the 8 month-end sections.

A report is now a *definition* (an ordered list of section specs); `render_excel`
/ `render_pdf` loop a definition's sections, look each up here, run its binder,
and call its render fn. The month-end pack is re-expressed as a definition in
`report_definitions.py` and renders content-identically (proven by the golden).

FIX-3: `SectionType.pdf_orientation` ("portrait" | "landscape") drives the PDF
loop's NextPageTemplate switch (rolling P&L + rolling CF are landscape).
FIX-4: `_bind_kpis` pre-computes the KPI re-derivations (cogs/opex/margins) so the
render fns are display-only (AP-DR-06). The binder is the ONE place a renderer is
allowed a little derivation; the section render fns never re-figure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List  # CRAFT-OK: Any = dynamic QBO payload + reportlab/openpyxl boundary

from . import excel_sections as xs
from . import excel_sections_aging as xsa
from . import excel_sections_bank_reconciliation as xsbr
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — board_pack sections.
from . import excel_sections_board as xsb
from . import excel_sections_covenant as xsc
from . import excel_sections_covenant_monthly as xscm
from . import excel_sections_forecast as xsf
from . import excel_sections_notes as xsn
from . import pdf_sections as ps
from . import pdf_sections_aging as psa
from . import pdf_sections_bank_reconciliation as psbr
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — board_pack sections.
from . import pdf_sections_board as psb
from . import pdf_sections_covenant as psc
from . import pdf_sections_covenant_monthly as pscm
from . import pdf_sections_forecast as psf
from . import pdf_sections_notes as psn
from .excel_context import ExcelContext, safe_float as _xls_safe_float
from .pdf_context import PdfContext

Payload = Dict[str, Any]  # CRAFT-OK: dynamic provider-derived QBO report payload boundary

# Render-fn signatures (kept Any-typed because they touch reportlab/openpyxl edges).
ExcelRender = Callable[[ExcelContext, Payload], None]
PdfRender = Callable[[PdfContext, List[Any], Payload], None]  # CRAFT-OK: reportlab story list
Binder = Callable[[Payload], Payload]
Condition = Callable[[Payload], bool]


@dataclass
class SectionType:
    """Describes one renderable report section. `binder` pre-computes the section's
    display data from the full report payload; `excel_render` / `pdf_render` consume
    that bound data; `condition` decides whether the section renders at all."""

    section_id: str
    title: str
    binder: Binder
    excel_render: ExcelRender
    pdf_render: PdfRender
    condition: Condition = field(default=lambda p: True)
    excel_tab_name: str = ""
    pdf_orientation: str = "portrait"  # FIX-3: "portrait" | "landscape"


# ─────────────────────────────────────────────────────────────────────────────
# Binders — pre-compute each section's display data (the only place a renderer is
# allowed a derivation; render fns are display-only thereafter).
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pnl_totals_from_detail(pnl_detail: List[Payload]) -> Dict[str, float]:
    """Extract key totals from parsed pnl_detail rows (shared by KPIs)."""
    totals: Dict[str, float] = {}
    if not pnl_detail:
        return totals
    TOTAL_MAPPINGS = {
        "total income": "revenue", "total revenue": "revenue", "total sales": "revenue",
        "total cost of goods sold": "cogs", "total cost of sales": "cogs", "total cogs": "cogs",
        "gross profit": "gross_profit",
        "total expenses": "operating_expenses", "total operating expenses": "operating_expenses",
        "net operating income": "noi",
        "total other income": "other_income",
        "total other expense": "other_expense", "total other expenses": "other_expense",
        "net other income": "net_other",
        "net income": "net_income", "net earnings": "net_income",
    }
    for row in pnl_detail:
        if not row.get("is_total"):  # SA-OK: bounded-loop N in-memory pnl_detail dict.get, not a DB query
            continue
        name = (row.get("name") or "").strip().lower()
        val = row.get("value")
        if val is None:
            continue
        key = TOTAL_MAPPINGS.get(name)
        if key and key not in totals:
            totals[key] = _xls_safe_float(val)
    if "gross_profit" not in totals and "revenue" in totals:
        totals["gross_profit"] = totals["revenue"] - totals.get("cogs", 0)
    if "ebitda" not in totals and "gross_profit" in totals:
        totals["ebitda"] = totals["gross_profit"] - totals.get("operating_expenses", 0)
    return totals


def _bind_kpis(payload: Payload) -> Payload:
    """FIX-4: pre-compute the hero/overview KPI figures (revenue/gp/cogs/opex/ebitda/
    net_income) preferring detail-table totals so they reconcile with the P&L Detail
    section. The render fns then DISPLAY these — no re-derivation in the renderer."""
    detail_totals = _extract_pnl_totals_from_detail(payload.get("pnl_detail", []))
    kpis = payload.get("kpis", {})

    def kpi(key: str) -> float:
        if key in detail_totals:
            return detail_totals[key]
        return _xls_safe_float(kpis.get(key))

    revenue = kpi("revenue")
    gp = kpi("gross_profit")
    cogs = detail_totals.get("cogs", revenue - gp)
    return {
        "revenue": revenue,
        "gross_profit": gp,
        "cogs": cogs,
        "operating_expenses": kpi("operating_expenses"),
        "ebitda": kpi("ebitda"),
        "net_income": kpi("net_income"),
        "prior_pnl": payload.get("prior_pnl", {}),
        "ttm": payload.get("ttm_pl", {}),
    }


def _bind_passthrough(*keys: str) -> Binder:
    """A binder that simply forwards a fixed set of payload keys (with safe defaults
    for the list/dict-shaped ones) to the render fn."""
    def binder(payload: Payload) -> Payload:
        out: Payload = {}
        for k in keys:
            out[k] = payload.get(k, _DEFAULTS.get(k))  # SA-OK: bounded-loop forwarding fixed key list, not a DB query
        return out
    return binder


_DEFAULTS: Dict[str, Any] = {  # CRAFT-OK: dynamic default shapes for forwarded payload keys
    "trends": [], "pnl_detail": [], "budget_map": {}, "current_pnl": {},
    "bs_detail": [], "balance_sheet": {},
    "rolling_pnl_columns": [], "rolling_pnl_detail": [],
    "rolling_cf_columns": [], "rolling_cf_detail": [],
    "bva_top_variances": [], "ar_aging": {}, "ap_aging": {},
    # W-4 fix #7 — standard_financials monthly balance-sheet grid (month_end payloads
    # carry none of these, so its sections render exactly as before).
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
    "bs_monthly_columns": [], "bs_monthly_rows": [],
}


def _bind_income_statement(payload: Payload) -> Payload:
    """AC-3 standard-financials Income Statement: bind the parsed P&L detail rows
    (FIX-6 parse seam). budget_map is intentionally empty so the shared render fn
    shows the 'Actuals'-only layout — the standard financials report is not a BVA.
    W-4 fix #7: also forward the Month-summarized grid (YTD/TTM views) so the Excel
    section renders month columns + QBO's Total column."""
    return {
        "pnl_detail": payload.get("pnl_detail", []),
        "budget_map": {},
        "current_pnl": payload.get("current_pnl", {}),
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
        "pnl_monthly_columns": payload.get("pnl_monthly_columns", []),
        "pnl_monthly_rows": payload.get("pnl_monthly_rows", []),
        "financials_view": payload.get("financials_view"),
    }


def _bind_cash_flow(payload: Payload) -> Payload:
    """AC-3 standard-financials Cash Flow: bind the parsed CashFlow detail rows
    (FIX-6 parse seam: parse_qbo_report_rows over the QBO CashFlow report).
    W-4 fix #7: also forward the Month-summarized grid (YTD/TTM views)."""
    return {
        "cf_detail": payload.get("cf_detail", []),
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
        "cf_monthly_columns": payload.get("cf_monthly_columns", []),
        "cf_monthly_rows": payload.get("cf_monthly_rows", []),
        "financials_view": payload.get("financials_view"),
    }


def _bind_covenant(payload: Payload) -> Payload:
    """AC-5 covenant: forward the covenant rows + overall status + window that
    `build_covenant` carried VERBATIM from `/covenants/status` (display-only)."""
    return {
        "covenants": payload.get("covenants", []),
        "overall_status": payload.get("overall_status", "UNKNOWN"),
        "covenant_period": payload.get("covenant_period", "—"),
    }


def _bind_covenant_grid(payload: Payload) -> Payload:  # PLAN-EXEMPT: AC-2 covenant monthly-grid section binder (ratified plan 20260630-covenant-tracking); this registry file is outside the plan's enumerated surface_scope but the AC-2 dispatch owns the new grid section.
    """AC-2 covenant monthly grid: forward the per-month axis + the per-covenant grid
    rows the builder read VERBATIM from the stored CovenantRecord snapshots (display-only)."""
    return {
        "grid_months": payload.get("grid_months", []),
        "grid_rows": payload.get("grid_rows", []),
    }


def _bind_covenant_monthly(payload: Payload) -> Payload:  # PLAN-EXEMPT: covenant monthly-grid section binder under ratified plan 20260630-covenant-redesign; this registry file is outside the plan's enumerated surface_scope but this plan's dispatch owns the new bank-send monthly grid section.
    """Bank-send monthly grid: forward the per-covenant two-lens series the builder
    carried VERBATIM from ``compute_monthly_series`` (display-only, re-figures nothing)."""
    return {"monthly_covenants": payload.get("monthly_covenants", [])}


def _bind_bank_reconciliation(payload: Payload) -> Payload:  # PLAN-EXEMPT: bank-reconciliation section binder under ratified plan 20260701-bank-reconciliation (AC-1.4); this registry file is outside the plan's enumerated surface_scope but the C1-d dispatch owns the new tie-out section.
    """Forward the tie-out block the builder carried VERBATIM from ``compute_tieout`` +
    ``outstanding_items`` (display-only, re-figures nothing). The render fns DISPLAY these
    figures; the ONE place any derivation happens is the builder's compute_tieout call."""
    return {"reconciliation": payload.get("reconciliation", {})}


def _bind_forecast(payload: Payload) -> Payload:
    """AC-6 cashflow_forecast: forward the forecast grid + runway summary that
    `build_cashflow_forecast` carried VERBATIM from `/cashflow-forecast/weekly`
    (the runway is a min-selection over the source series — display-only)."""
    return {
        "forecast": payload.get("forecast", {}),
        "runway": payload.get("runway", {}),
    }


def _bind_analysis_notes(payload: Payload) -> Payload:
    """AC-7: forward the model-generated narrative (display-only). The narrative is
    read from `analysis_notes` (the month-end key), falling back to `executive_narrative`
    so any payload that carries either renders it."""
    narrative = payload.get("analysis_notes") or payload.get("executive_narrative") or ""
    return {"narrative": narrative}


def _bind_board_commentary(payload: Payload) -> Payload:
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — board_pack sections.
    """W-7 board_pack: forward the ONE-pass commentary map + its honest status
    (display-only; the render fn shows a placeholder per missing section)."""
    return {
        "board_commentary": payload.get("board_commentary", {}),
        "board_commentary_status": payload.get("board_commentary_status", ""),
    }


def _bind_board_bva(payload: Payload) -> Payload:
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — board_pack sections.
    """W-7 board_pack: forward the month-end-computed BVA variances + the
    budget-availability flag (drives the honest empty state — the board pack
    never silently skips the section like the conditional month-end one)."""
    return {
        "bva_top_variances": payload.get("bva_top_variances", []),
        "budget_available": bool(payload.get("budget_available")),
    }


def _bind_aging(payload: Payload) -> Payload:
    """Bind AR/AP aging + the fetch-failed flag (distinguishes a QBO fetch failure
    from a genuine all-zero period — preserves the legacy empty-state messaging)."""
    unavailable = set((payload.get("data_quality", {}) or {}).get("unavailable_sections", []))
    fetch_failed = (
        "report_ar_aging_unavailable" in unavailable
        or "report_ap_aging_unavailable" in unavailable
    )
    return {
        "ar_aging": payload.get("ar_aging", {}),
        "ap_aging": payload.get("ap_aging", {}),
        "aging_fetch_failed": fetch_failed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Conditions
# ─────────────────────────────────────────────────────────────────────────────

def _cond_bva(payload: Payload) -> bool:
    return bool(payload.get("bva_top_variances"))


def _cond_analysis_notes(payload: Payload) -> bool:
    """AC-7: the commentary section renders ONLY when there IS a non-empty narrative.
    Absent/empty → the section is skipped, so a report without commentary still
    renders (the LLM call is never a hard dependency of rendering)."""
    narrative = payload.get("analysis_notes") or payload.get("executive_narrative") or ""
    return bool(str(narrative).strip())


def _cond_aging(payload: Payload) -> bool:
    """The PDF always renders the aging section (legacy behaviour); the Excel tab is
    likewise always created. So the section is unconditional in the month-end
    definition — the empty-state messaging lives inside the render fns."""
    return True


# ─────────────────────────────────────────────────────────────────────────────
# The registry — section_id → SectionType
# ─────────────────────────────────────────────────────────────────────────────

SECTION_REGISTRY: Dict[str, SectionType] = {
    "kpis": SectionType(
        section_id="kpis", title="P&L Metrics", binder=_bind_kpis,
        excel_render=xs.render_kpis, pdf_render=ps.render_kpis,
        excel_tab_name="P&L Metrics", pdf_orientation="portrait",
    ),
    "profitability_trends": SectionType(
        section_id="profitability_trends", title="Profitability Trends",
        binder=_bind_passthrough("trends"),
        excel_render=xs.render_profitability_trends, pdf_render=ps.render_profitability_trends,
        excel_tab_name="Profitability Trends", pdf_orientation="portrait",
    ),
    "pnl_detail": SectionType(
        section_id="pnl_detail", title="P&L Detail",
        binder=_bind_passthrough("pnl_detail", "budget_map", "current_pnl"),
        excel_render=xs.render_pnl_detail, pdf_render=ps.render_pnl_detail,
        excel_tab_name="P&L Detail", pdf_orientation="portrait",
    ),
    "rolling_pnl": SectionType(
        section_id="rolling_pnl", title="Rolling 12M P&L",
        binder=_bind_passthrough("rolling_pnl_columns", "rolling_pnl_detail"),
        excel_render=xs.render_rolling_pnl, pdf_render=ps.render_rolling_pnl,
        excel_tab_name="Rolling 12M P&L", pdf_orientation="landscape",
    ),
    "balance_sheet": SectionType(
        section_id="balance_sheet", title="Balance Sheet",
        # W-4 fix #7: also forward the standard_financials monthly grid keys (absent
        # on month_end payloads → the tab renders exactly as before).
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
        binder=_bind_passthrough("bs_detail", "balance_sheet",
                                 "bs_monthly_columns", "bs_monthly_rows",
                                 "financials_view"),
        excel_render=xs.render_balance_sheet, pdf_render=ps.render_balance_sheet,
        excel_tab_name="Balance Sheet", pdf_orientation="portrait",
    ),
    "rolling_cf": SectionType(
        section_id="rolling_cf", title="Rolling 12M CF",
        binder=_bind_passthrough("rolling_cf_columns", "rolling_cf_detail"),
        excel_render=xs.render_rolling_cf, pdf_render=ps.render_rolling_cf,
        excel_tab_name="Rolling 12M CF", pdf_orientation="landscape",
    ),
    "bva_variances": SectionType(
        section_id="bva_variances", title="BVA Variances",
        binder=_bind_passthrough("bva_top_variances"),
        excel_render=xsa.render_bva_variances, pdf_render=psa.render_bva_variances,
        condition=_cond_bva, excel_tab_name="BVA Variances", pdf_orientation="portrait",
    ),
    "ar_ap_aging": SectionType(
        section_id="ar_ap_aging", title="AR-AP Aging", binder=_bind_aging,
        excel_render=xsa.render_ar_ap_aging, pdf_render=psa.render_ar_ap_aging,
        condition=_cond_aging, excel_tab_name="AR-AP Aging", pdf_orientation="portrait",
    ),
    # ── AC-3 standard_financials section types (reuse the lifted detail renderers) ──
    "income_statement": SectionType(
        section_id="income_statement", title="Income Statement",
        binder=_bind_income_statement,
        # W-4 fix #7: monthly-aware Excel render (falls back to render_pnl_detail
        # when the payload carries no monthly grid). PDF layout unchanged.
        # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-4
        excel_render=xs.render_income_statement, pdf_render=ps.render_pnl_detail,
        excel_tab_name="Income Statement", pdf_orientation="portrait",
    ),
    "cash_flow": SectionType(
        section_id="cash_flow", title="Cash Flow", binder=_bind_cash_flow,
        excel_render=xs.render_cash_flow, pdf_render=ps.render_cash_flow,
        excel_tab_name="Cash Flow", pdf_orientation="portrait",
    ),
    # ── AC-5 covenant section type (NEW renderers; PASS/WARN/BREACH + detail) ──
    "covenant_compliance": SectionType(
        section_id="covenant_compliance", title="Covenant Compliance",
        binder=_bind_covenant,
        excel_render=xsc.render_covenant, pdf_render=psc.render_covenant,
        excel_tab_name="Covenant Compliance", pdf_orientation="portrait",
    ),
    # ── AC-2 covenant MONTHLY GRID section (PASS/WARN/BREACH per month, from stored rows) ──
    # PLAN-EXEMPT: AC-2 covenant monthly-grid section type (ratified plan 20260630-covenant-tracking); registry entry for the new grid renderers (registry file outside the plan's enumerated surface_scope, AC-2 dispatch owns it).
    "covenant_monthly_grid": SectionType(
        section_id="covenant_monthly_grid", title="Covenant Compliance History",
        binder=_bind_covenant_grid,
        excel_render=xsc.render_covenant_grid, pdf_render=psc.render_covenant_grid,
        excel_tab_name="Covenant History", pdf_orientation="landscape",
    ),
    # ── Bank-send MONTHLY covenant grid (two-lens component build-up from compute engine) ──
    # PLAN-EXEMPT: covenant monthly-grid section type under ratified plan 20260630-covenant-redesign; registry entry for the new bank-send monthly-grid renderers (registry file outside the plan's enumerated surface_scope, this plan's dispatch owns it).
    "covenant_monthly": SectionType(
        section_id="covenant_monthly", title="Monthly Covenant Compliance",
        binder=_bind_covenant_monthly,
        excel_render=xscm.render_covenant_monthly, pdf_render=pscm.render_covenant_monthly,
        excel_tab_name="Covenant Monthly", pdf_orientation="landscape",
    ),
    # ── bank-reconciliation tie-out section (opening → cleared → statement → difference) ──
    # PLAN-EXEMPT: bank-reconciliation section type under ratified plan 20260701-bank-reconciliation (AC-1.4); registry entry for the new tie-out renderers (registry file outside the plan's enumerated surface_scope, C1-d dispatch owns it).
    "bank_reconciliation": SectionType(
        section_id="bank_reconciliation", title="Bank Reconciliation",
        binder=_bind_bank_reconciliation,
        excel_render=xsbr.render_bank_reconciliation, pdf_render=psbr.render_bank_reconciliation,
        excel_tab_name="Bank Reconciliation", pdf_orientation="portrait",
    ),
    # ── AC-6 cashflow_forecast section types (the grid is LANDSCAPE — FIX-3) ──
    "forecast_grid": SectionType(
        section_id="forecast_grid", title="13-Week Forecast",
        binder=_bind_forecast,
        excel_render=xsf.render_forecast_grid, pdf_render=psf.render_forecast_grid,
        excel_tab_name="13-Week Forecast", pdf_orientation="landscape",
    ),
    "runway_summary": SectionType(
        section_id="runway_summary", title="Runway Summary",
        binder=_bind_forecast,
        excel_render=xsf.render_runway_summary, pdf_render=psf.render_runway_summary,
        excel_tab_name="Runway Summary", pdf_orientation="portrait",
    ),
    # ── W-7 board_pack sections: the ONE-pass commentary block + the honest-empty BVA ──
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — registry entries for the
    # board_pack's two new sections (all other board_pack sections reuse existing entries).
    "board_commentary": SectionType(
        section_id="board_commentary", title="Board Commentary",
        binder=_bind_board_commentary,
        excel_render=xsb.render_board_commentary, pdf_render=psb.render_board_commentary,
        excel_tab_name="Board Commentary", pdf_orientation="portrait",
    ),
    "board_bva": SectionType(
        section_id="board_bva", title="Budget vs. Actuals",
        binder=_bind_board_bva,
        excel_render=xsb.render_board_bva, pdf_render=psb.render_board_bva,
        excel_tab_name="BVA Variances", pdf_orientation="portrait",
    ),
    # ── AC-7 AI commentary section (OPTIONAL: condition skips it when no narrative) ──
    "analysis_notes": SectionType(
        section_id="analysis_notes", title="Management Commentary",
        binder=_bind_analysis_notes,
        excel_render=xsn.render_analysis_notes, pdf_render=psn.render_analysis_notes,
        condition=_cond_analysis_notes,
        excel_tab_name="Commentary", pdf_orientation="portrait",
    ),
}

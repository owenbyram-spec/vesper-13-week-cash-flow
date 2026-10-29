"""
app/reporting/report_definitions.py
─────────────────────────────────────
Report DEFINITIONS — each is an ordered list of section specs that the engine
loops over. A definition is data (builder-ready): a named report is a list of
section ids the registry knows how to render, not a hand-wired renderer.

`MONTH_END_REPORT_DEFINITION` re-expresses the existing month-end board pack
exactly: the same 8 sections in the same order, so `render_excel` / `render_pdf`
produce content-identical output (proven by the golden in tests/reporting).

AC-2+ adds the named reports (standard_financials, ar_ap_aging, covenant,
cashflow_forecast) to `REPORT_DEFINITIONS`; AC-1 ships month_end only.
"""
from __future__ import annotations

from typing import Any, Dict, List  # CRAFT-OK: a section spec is a small dynamic dict

SectionSpec = Dict[str, Any]  # CRAFT-OK: {"section_id": str, "title"?: str, ...}
ReportDefinition = Dict[str, Any]  # CRAFT-OK: {"name", "display_name", "sections": [...]}


MONTH_END_REPORT_DEFINITION: ReportDefinition = {
    "name": "month_end",
    "display_name": "Monthly Board Pack",
    # Layout flags (PDF only; Excel ignores them — one tab per section):
    #  - `page_break_before` (default True): start the section on a new page. KPIs +
    #    Profitability Trends share one page (trends = False); the first section never
    #    breaks (it follows the cover's break).
    #  - `page_break_after`: emit a trailing break AFTER the section. pnl_detail carries
    #    it to reproduce the legacy blank portrait page that fell between pnl_detail and
    #    the landscape rolling P&L (the old renderer broke after pnl_detail AND switched
    #    to landscape — a double break). Preserved for content-equivalence.
    # The PDF loop pairs these with each section's registry `pdf_orientation` to emit the
    # NextPageTemplate + PageBreak switch (FIX-3).
    "sections": [
        {"section_id": "kpis"},
        {"section_id": "profitability_trends", "page_break_before": False},
        {"section_id": "pnl_detail", "page_break_after": True},
        {"section_id": "rolling_pnl"},
        {"section_id": "balance_sheet"},
        {"section_id": "rolling_cf"},
        {"section_id": "bva_variances"},
        {"section_id": "ar_ap_aging"},
    ],
}


# AC-3 — Standard financials: Income Statement + Balance Sheet + Cash Flow, each
# rendered from the lifted detail section renderers over the FIX-6 parse-seam rows
# (parse_qbo_report_rows). PDF section order = the conventional statement order
# (IS → BS → CF); Excel emits one tab per section in the same order.
STANDARD_FINANCIALS_REPORT_DEFINITION: ReportDefinition = {
    "name": "standard_financials",
    "display_name": "Standard Financial Statements",
    "sections": [
        {"section_id": "income_statement"},
        {"section_id": "balance_sheet"},
        {"section_id": "cash_flow"},
        # AC-7 — AI commentary (optional: self-skips via its registry condition when
        # the payload carries no narrative, so the report still renders without it).
        {"section_id": "analysis_notes"},
    ],
}


# AC-4 — Standalone AR/AP aging. The lifted `ar_ap_aging` section renders AR + AP
# buckets (0-30/31-60/61-90/90+) + top entities + the net AR/AP summary line in one
# combined section (PDF reuses the lifted aging section; Excel reuses the lifted
# aging tab). Numbers tie to ar_aging()/ap_aging().
AR_AP_AGING_REPORT_DEFINITION: ReportDefinition = {
    "name": "ar_ap_aging",
    "display_name": "AR / AP Aging",
    "sections": [
        {"section_id": "ar_ap_aging"},
        # AC-7 — AI commentary (optional: self-skips when no narrative).
        {"section_id": "analysis_notes"},
    ],
}


# AC-5 — Lender covenant report. One combined covenant_compliance section renders the
# overall PASS/WARN/BREACH status, a per-covenant summary table, and the per-covenant
# detail (metric / threshold / current / breakdown). Numbers tie to /covenants/status.
COVENANT_REPORT_DEFINITION: ReportDefinition = {
    "name": "covenant",
    "display_name": "Loan Covenant Compliance",
    "sections": [
        {"section_id": "covenant_compliance"},
        # AC-7 — AI commentary (optional: self-skips when no narrative).
        {"section_id": "analysis_notes"},
    ],
}


# Covenant-tracking AC-2 — MONTHLY covenant compliance grid. One landscape grid section
# renders a per-covenant row × per-month column PASS/WARN/BREACH matrix read from the
# STORED CovenantRecord snapshots (the rows the Wave-3 snapshot task writes each closed
# month). Numbers tie to the stored covenant_records.
# PLAN-EXEMPT: AC-2 covenant monthly-grid report definition (ratified plan 20260630-covenant-tracking); definitions file outside the plan's enumerated surface_scope, AC-2 dispatch owns the new report type.
COVENANT_COMPLIANCE_REPORT_DEFINITION: ReportDefinition = {
    "name": "covenant_compliance",
    "display_name": "Covenant Compliance History",
    "sections": [
        {"section_id": "covenant_monthly_grid"},
        # AC-7 — AI commentary (optional: self-skips when no narrative).
        {"section_id": "analysis_notes"},
    ],
}


# covenant-redesign — Bank-send MONTHLY covenant report. One landscape grid section per
# active covenant, each the 12-month two-lens component build-up (Interest / Principal /
# Debt Service / EBITDA / run-rate / Overage / Total Debt / TTM) rendered DISPLAY-ONLY from
# the Wave-1 compute engine (compute_monthly_series). Mirrors the bank workbook exactly.
# PLAN-EXEMPT: covenant monthly-grid report definition under ratified plan 20260630-covenant-redesign; definitions file outside the plan's enumerated surface_scope, this plan's dispatch owns the new report type.
COVENANT_MONTHLY_REPORT_DEFINITION: ReportDefinition = {
    "name": "covenant_monthly",
    "display_name": "Monthly Covenant Compliance",
    "sections": [
        {"section_id": "covenant_monthly"},
        # AC-7 — AI commentary (optional: self-skips when no narrative).
        {"section_id": "analysis_notes"},
    ],
}


# bank-reconciliation (plan 20260701-bank-reconciliation, AC-1.4) — the tie-out report.
# One portrait section renders the tie-out summary (opening → cleared → statement ending →
# difference) DISPLAY-ONLY from the resolved ReconciliationSession's compute_tieout, plus the
# outstanding/uncleared item list. Numbers tie to the session tie-out (no re-derivation).
# PLAN-EXEMPT: bank-reconciliation report definition under ratified plan 20260701-bank-reconciliation (AC-1.4); definitions file outside the plan's enumerated surface_scope, this plan's C1-d dispatch owns the new report type.
BANK_RECONCILIATION_REPORT_DEFINITION: ReportDefinition = {
    "name": "bank_reconciliation",
    "display_name": "Bank Reconciliation",
    "sections": [
        {"section_id": "bank_reconciliation"},
        # AC-7 — AI commentary (optional: self-skips when no narrative).
        {"section_id": "analysis_notes"},
    ],
}


# AC-6 — 13-week cash-flow forecast. The forecast_grid section is LANDSCAPE (FIX-3 —
# the 13-week grid is WIDE); the runway_summary section is portrait. Numbers tie to
# /cashflow-forecast/weekly (the runway is a min-selection over the source series).
CASHFLOW_FORECAST_REPORT_DEFINITION: ReportDefinition = {
    "name": "cashflow_forecast",
    "display_name": "13-Week Cash Flow Forecast",
    "sections": [
        {"section_id": "forecast_grid"},
        {"section_id": "runway_summary"},
        # AC-7 — AI commentary (optional: self-skips when no narrative).
        {"section_id": "analysis_notes"},
    ],
}


# W-7 (plan 20260702-qa-remediation, F-15 / item #15) — the BOARD PACK. Four sections
# assembled 100% deterministically from EXISTING engines (covenant monthly two-lens grid,
# AR/AP per-entity aging, budget-vs-actuals, CFF weekly grid + runway) fronted by the
# board_commentary section: the ONE bounded/cached/ledger-metered LLM pass's per-section
# prose, with an honest placeholder when that pass is unavailable (never a second call).
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — the board_pack report type.
BOARD_PACK_REPORT_DEFINITION: ReportDefinition = {
    "name": "board_pack",
    "display_name": "Board Pack",
    "sections": [
        {"section_id": "board_commentary"},
        {"section_id": "covenant_monthly"},
        {"section_id": "ar_ap_aging"},
        {"section_id": "board_bva"},
        {"section_id": "forecast_grid"},
        {"section_id": "runway_summary"},
    ],
}


# report_type → definition. AC-1 ships month_end; AC-3/AC-4 add the reuse reports;
# AC-5/AC-6 add the two new-renderer reports.
REPORT_DEFINITIONS: dict[str, ReportDefinition] = {
    "month_end": MONTH_END_REPORT_DEFINITION,
    "standard_financials": STANDARD_FINANCIALS_REPORT_DEFINITION,
    "ar_ap_aging": AR_AP_AGING_REPORT_DEFINITION,
    "covenant": COVENANT_REPORT_DEFINITION,
    "covenant_compliance": COVENANT_COMPLIANCE_REPORT_DEFINITION,  # PLAN-EXEMPT: AC-2 monthly grid report (ratified plan 20260630-covenant-tracking)
    "covenant_monthly": COVENANT_MONTHLY_REPORT_DEFINITION,  # PLAN-EXEMPT: bank-send monthly grid report (ratified plan 20260630-covenant-redesign)
    "bank_reconciliation": BANK_RECONCILIATION_REPORT_DEFINITION,  # PLAN-EXEMPT: bank-reconciliation tie-out report (ratified plan 20260701-bank-reconciliation, AC-1.4)
    "cashflow_forecast": CASHFLOW_FORECAST_REPORT_DEFINITION,
    "board_pack": BOARD_PACK_REPORT_DEFINITION,  # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 (board pack)
}


def get_report_definition(report_type: str) -> ReportDefinition:
    """Resolve a report_type to its definition, defaulting to month_end so any
    legacy caller (and an unknown type) renders the board pack."""
    return REPORT_DEFINITIONS.get(report_type or "month_end", MONTH_END_REPORT_DEFINITION)


def definition_sections(definition: ReportDefinition) -> List[SectionSpec]:
    return list(definition.get("sections", []))

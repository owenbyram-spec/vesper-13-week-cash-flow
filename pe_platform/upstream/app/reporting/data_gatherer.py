import logging
import re
from typing import Dict, Any, List, Tuple
from datetime import date, timedelta
from sqlalchemy.orm import Session
from ..models import BudgetLine
from ..qbo_client import get_qbo_client_from_db, QBOClient
from ..analysis.ratios import (
    resolve_period,
    compute_period_pl_kpis,
    compute_ttm_pl_kpis,
    compute_ytd_pl_kpis,
    compute_balance_sheet_kpis,
    compute_cashflow_kpis,
    compute_ratios_with_quality,
    safe_float,
)
from ..analysis.vendor_spend import vendor_spend_summary
from ..analysis.customer_revenue import customer_revenue_summary
from ..analysis.ar_aging import ar_aging
from ..analysis.ap_aging import ap_aging
from ..services.errors import safe_error_detail
from .trend_table import build_trend_table

logger = logging.getLogger(__name__)


# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — extracted (verbatim
# semantics) from the inline budget-map block in gather_report_data so the
# board_pack builder shares the SAME deterministic budget source (AC-1).
def build_budget_map(
    db: Session, company_id: str, year: "int | None", month: "int | None"
) -> dict[str, float]:
    """account_name (lower) → that month's budget amount, from the Peregrine
    BudgetLine table (top-level lines only). The exact query + month-key logic
    the month-end gather has always used; callers own the try/except (the
    gather records a partial-data error, the board pack an honest empty state)."""
    MONTH_KEYS = ["jan", "feb", "mar", "apr", "may", "jun",
                  "jul", "aug", "sep", "oct", "nov", "dec"]
    budget_map: dict[str, float] = {}
    budget_year_val = year or date.today().year
    budget_month_key = MONTH_KEYS[(month or date.today().month) - 1] if month else MONTH_KEYS[date.today().month - 1]
    budget_lines = (
        db.query(BudgetLine)
        .filter(
            BudgetLine.company_id == company_id,
            BudgetLine.budget_year == budget_year_val,
            BudgetLine.parent_line_id == None,  # noqa: E711
        )
        .all()
    )
    for bl in budget_lines:
        amounts = bl.monthly_amounts or {}  # SA-OK: bounded-loop over the single pre-fetched budget_lines query, not per-row DB access
        val = safe_float(amounts.get(budget_month_key, 0))
        if val != 0:
            budget_map[bl.account_name.lower()] = val
    return budget_map


# ─── QBO Report Parser (Single-Column) ───────────────────────────────────────
# Walks the nested Section/Data/Summary rows from a QBO report and
# produces a flat list of {name, value, depth, is_total, is_section_header}
# dicts suitable for rendering detailed financial statement tables.

def parse_qbo_report_rows(report_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parses a raw QBO report (ProfitAndLoss, BalanceSheet, CashFlow)
    into a flat list of account-level rows preserving hierarchy.
    
    Each row: {name: str, value: float, depth: int, is_total: bool, is_header: bool}
    """
    result: List[Dict[str, Any]] = []
    root_rows = report_json.get("Rows", {}).get("Row", [])
    _walk_rows(root_rows, result, depth=0)
    return result


def _walk_rows(rows: list, result: list, depth: int):
    """Recursively walk QBO report rows."""
    for row in rows:
        row_type = (row.get("type") or "").lower()

        if row_type == "section":
            header = row.get("Header", {}).get("ColData", [])
            label = (header[0].get("value", "") if header else "").strip()

            if label:
                result.append({
                    "name": label,
                    "value": None,
                    "depth": depth,
                    "is_total": False,
                    "is_header": True,
                })

            # Recurse into child rows
            child_rows = row.get("Rows", {}).get("Row", [])
            _walk_rows(child_rows, result, depth + 1)

            # Section summary (e.g. "Total Income")
            summary = row.get("Summary", {})
            if summary:
                col_data = summary.get("ColData", [])
                if col_data:
                    sum_label = (col_data[0].get("value", "")).strip()
                    sum_val = safe_float(col_data[-1].get("value", 0))
                    result.append({
                        "name": sum_label,
                        "value": sum_val,
                        "depth": depth,
                        "is_total": True,
                        "is_header": False,
                    })

        elif row_type == "data":
            col_data = row.get("ColData", [])
            if col_data:
                label = (col_data[0].get("value", "")).strip()
                val = safe_float(col_data[-1].get("value", 0))
                if label:
                    result.append({
                        "name": label,
                        "value": val,
                        "depth": depth,
                        "is_total": False,
                        "is_header": False,
                    })


# ─── QBO Report Parser (Multi-Column — Rolling 12-Month) ─────────────────────

def parse_qbo_multicolumn_report(
    report_json: Dict[str, Any],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Parses a QBO report fetched with summarize_column_by=Month into:
      - column_headers: list of month labels (e.g. ["Oct 2024", ..., "Sep 2025"])
      - rows: list of dicts with:
          name, values (List[float] — one per month), total (float),
          depth, is_total, is_header

    The last QBO column is typically "Total" — it is extracted separately.
    """
    # ── Extract column headers ──
    columns = report_json.get("Columns", {}).get("Column", [])

    # Filter to Money columns only (skip the first which is the label column)
    money_cols = [
        c for c in columns
        if c.get("ColType") == "Money"
    ]

    # Separate month columns from the Total column
    month_cols: List[str] = []
    has_total_col = False
    for c in money_cols:
        title = (c.get("ColTitle") or "").strip()
        if title.lower() == "total":
            has_total_col = True
        else:
            month_cols.append(title)

    n_months = len(month_cols)

    # ── Walk rows ──
    result: List[Dict[str, Any]] = []
    root_rows = report_json.get("Rows", {}).get("Row", [])
    _walk_multicolumn_rows(root_rows, result, depth=0,
                           n_months=n_months, has_total=has_total_col)

    return month_cols, result


def _walk_multicolumn_rows(rows: list, result: list, depth: int,
                           n_months: int, has_total: bool):
    """Recursively walk QBO multi-column report rows."""
    for row in rows:
        row_type = (row.get("type") or "").lower()

        if row_type == "section":
            header = row.get("Header", {}).get("ColData", [])
            label = (header[0].get("value", "") if header else "").strip()

            if label:
                result.append({
                    "name": label,
                    "values": [None] * n_months,
                    "total": None,
                    "depth": depth,
                    "is_total": False,
                    "is_header": True,
                })

            child_rows = row.get("Rows", {}).get("Row", [])
            _walk_multicolumn_rows(child_rows, result, depth + 1,
                                   n_months, has_total)

            # Section summary
            summary = row.get("Summary", {})
            if summary:
                col_data = summary.get("ColData", [])
                if col_data:
                    sum_label = (col_data[0].get("value", "")).strip()
                    values, total = _extract_column_values(col_data, n_months, has_total)
                    result.append({
                        "name": sum_label,
                        "values": values,
                        "total": total,
                        "depth": depth,
                        "is_total": True,
                        "is_header": False,
                    })

        elif row_type == "data":
            col_data = row.get("ColData", [])
            if col_data:
                label = (col_data[0].get("value", "")).strip()
                if label:
                    values, total = _extract_column_values(col_data, n_months, has_total)
                    result.append({
                        "name": label,
                        "values": values,
                        "total": total,
                        "depth": depth,
                        "is_total": False,
                        "is_header": False,
                    })


def _extract_column_values(
    col_data: list, n_months: int, has_total: bool
) -> Tuple[List[float], float]:
    """
    From a ColData list (first element is the label), extract:
      - monthly values (list of floats, length n_months)
      - total value (float, from the last column if has_total)
    """
    # Skip first element (label); remaining are the numeric columns
    numeric = col_data[1:]

    monthly: List[float] = []
    for i, c in enumerate(numeric):
        if has_total and i == len(numeric) - 1:
            break  # last one is Total
        monthly.append(safe_float(c.get("value", 0)))

    # Pad if fewer columns than expected
    while len(monthly) < n_months:
        monthly.append(0.0)
    monthly = monthly[:n_months]

    total = 0.0
    if has_total and numeric:
        total = safe_float(numeric[-1].get("value", 0))
    else:
        total = sum(monthly)

    return monthly, total


# ─── Raw Report Fetcher ───────────────────────────────────────────────────────

def _fetch_raw_report(client: QBOClient, report_name: str, params: dict) -> Dict[str, Any]:
    """Fetch a raw QBO report."""
    return client.get_report(report_name, params)


# ─── Company Name Helper ──────────────────────────────────────────────────────

def _get_company_name(client: QBOClient) -> str:
    """Try to extract the company name from QBO CompanyInfo."""
    try:
        info = client.get_company_info()
        return info.get("CompanyInfo", {}).get("CompanyName", "")
    except Exception:
        return ""


# ─── Prior-Month Period Helper ────────────────────────────────────────────────

def _prior_month_period(resolved: Dict[str, str]) -> Dict[str, str]:
    """Compute the prior month's start/end dates from the current resolved period."""
    end = date.fromisoformat(resolved["start_date"]) - timedelta(days=1)
    start = date(end.year, end.month, 1)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "label": end.strftime("%b %Y"),
    }


# ─── Report Period ────────────────────────────────────────────────────────────

_REPORT_PERIOD = re.compile(r"^(\d{4})-(\d{2})$")


def parse_report_period(period: str) -> Tuple[int, int]:
    """(year, month) for a YYYY-MM report period; raises ValueError otherwise.

    A malformed period is refused rather than silently replaced by the last completed
    month: the report (and its budget columns) must describe the month asked for.
    """
    match = _REPORT_PERIOD.match(str(period or "").strip())
    if not match:
        raise ValueError(f"Report period must be YYYY-MM, got {period!r}")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"Report period month must be 01-12, got {period!r}")
    return year, month


# ─── Main Gatherer ────────────────────────────────────────────────────────────

def gather_report_data(db: Session, company_id: str, period: str) -> Dict[str, Any]:
    """
    Gathers all necessary data for the month-end financial report.
    period format: YYYY-MM (anything else raises ValueError)
    Includes both summary KPIs AND account-level detail for full statements.
    Fiscal-year windows use the company fiscal calendar, as the dashboard does.
    """
    year, month = parse_report_period(period)

    client = get_qbo_client_from_db(db, company_id)
    resolved = resolve_period(year, month, db=db, realm_id=company_id)

    # ── Parallelized QBO Data Gathering ──
    import concurrent.futures

    prior_period = _prior_month_period(resolved)
    partial_errors: List[Dict[str, Any]] = []

    def _optional_result(future, *, error_code: str, user_message: str, fallback):
        try:
            return future.result()
        except Exception as exc:
            partial_errors.append(
                safe_error_detail(
                    error_code=error_code,
                    user_message=user_message,
                    exc=exc,
                    retryable=True,
                    log_context=f"report_data_{error_code}",
                )
            )
            return fallback
    
    try:
        bs_start = f"{int(resolved['end_date'][:4])}-01-01"
    except Exception:
        bs_start = resolved["end_date"]

    def _fetch_prior_pl():
        return compute_period_pl_kpis(client, prior_period)
        
    def _fetch_customers():
        return customer_revenue_summary(client, limit=10)
        
    def _fetch_vendors():
        return vendor_spend_summary(client, limit=10)
        
    def _fetch_trends():
        return build_trend_table(client, months=12)

    def _fetch_ar(): return _safe_ar_ap(client, "ar")
    def _fetch_ap(): return _safe_ar_ap(client, "ap")

    # Fire network requests with lower concurrency to avoid QBO 429 Rate Limits
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f_company = executor.submit(_get_company_name, client)
        f_period_pl = executor.submit(compute_period_pl_kpis, client, resolved)
        f_ttm_pl = executor.submit(compute_ttm_pl_kpis, client, resolved)
        f_ytd_pl = executor.submit(compute_ytd_pl_kpis, client, resolved)
        f_prior_pl = executor.submit(_fetch_prior_pl)
        f_bs = executor.submit(compute_balance_sheet_kpis, client, resolved["end_date"])
        f_cf = executor.submit(compute_cashflow_kpis, client, resolved)
        
        f_raw_pnl = executor.submit(_fetch_raw_report, client, "ProfitAndLoss", {
            "start_date": resolved["start_date"],
            "end_date": resolved["end_date"],
            "summarize_column_by": "Total",
            "accounting_method": "Accrual",
        })
        f_raw_bs = executor.submit(_fetch_raw_report, client, "BalanceSheet", {
            "start_date": bs_start,
            "end_date": resolved["end_date"],
            "summarize_column_by": "Total",
            "accounting_method": "Accrual",
        })
        f_raw_rolling_pnl = executor.submit(_fetch_raw_report, client, "ProfitAndLoss", {
            "start_date": resolved["ttm_start"],
            "end_date": resolved["ttm_end"],
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        })
        f_raw_rolling_cf = executor.submit(_fetch_raw_report, client, "CashFlow", {
            "start_date": resolved["ttm_start"],
            "end_date": resolved["ttm_end"],
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        })
        
        f_cust = executor.submit(_fetch_customers)
        f_vend = executor.submit(_fetch_vendors)
        f_trend = executor.submit(_fetch_trends)
        f_ar = executor.submit(_fetch_ar)
        f_ap = executor.submit(_fetch_ap)
        
        # Wait for all network IO to complete
        company_name = _optional_result(
            f_company,
            error_code="report_company_name_unavailable",
            user_message="Company name could not be loaded from QuickBooks.",
            fallback="",
        )
        period_pl = f_period_pl.result()
        ttm_pl = f_ttm_pl.result()
        # YTD is not a core statement of the month-end report: a failed fetch is a
        # stated gap, not a reason to abort the whole report.
        ytd_pl = _optional_result(
            f_ytd_pl,
            error_code="report_ytd_pl_unavailable",
            user_message="Fiscal year-to-date P&L data could not be loaded.",
            fallback=None,
        )
        prior_pl = _optional_result(
            f_prior_pl,
            error_code="report_prior_pl_unavailable",
            user_message="Prior-period P&L data could not be loaded.",
            fallback={},
        )
        bs = f_bs.result()
        cf = f_cf.result()
        raw_pnl = _optional_result(
            f_raw_pnl,
            error_code="report_pnl_detail_unavailable",
            user_message="P&L statement detail could not be loaded.",
            fallback={},
        )
        raw_bs = _optional_result(
            f_raw_bs,
            error_code="report_balance_sheet_detail_unavailable",
            user_message="Balance sheet detail could not be loaded.",
            fallback={},
        )
        raw_rolling_pnl = _optional_result(
            f_raw_rolling_pnl,
            error_code="report_rolling_pnl_unavailable",
            user_message="Rolling P&L detail could not be loaded.",
            fallback={},
        )
        raw_rolling_cf = _optional_result(
            f_raw_rolling_cf,
            error_code="report_rolling_cashflow_unavailable",
            user_message="Rolling cash-flow detail could not be loaded.",
            fallback={},
        )
        customers = _optional_result(
            f_cust,
            error_code="report_customer_revenue_unavailable",
            user_message="Customer revenue detail could not be loaded.",
            fallback={},
        )
        vendors = _optional_result(
            f_vend,
            error_code="report_vendor_spend_unavailable",
            user_message="Vendor spend detail could not be loaded.",
            fallback={},
        )
        trend_table = _optional_result(
            f_trend,
            error_code="report_trends_unavailable",
            user_message="Trend table data could not be loaded.",
            fallback=[],
        )
        ar_aging_data = _optional_result(
            f_ar,
            error_code="report_ar_aging_unavailable",
            user_message="AR aging data could not be loaded.",
            fallback={"as_of": None, "buckets": {}, "total_ar": None, "invoices": []},
        )
        ap_aging_data = _optional_result(
            f_ap,
            error_code="report_ap_aging_unavailable",
            user_message="AP aging data could not be loaded.",
            fallback={"as_of": None, "buckets": {}, "total_ap": None, "bills": []},
        )

    # ── Ratios & Sync Parsing ──
    ratios, ratio_status = compute_ratios_with_quality(period_pl, bs)
    
    pnl_detail = parse_qbo_report_rows(raw_pnl) if raw_pnl else []
    bs_detail = parse_qbo_report_rows(raw_bs) if raw_bs else []
    
    rolling_pnl_columns, rolling_pnl_detail = [], []
    if raw_rolling_pnl:
        try:
            rolling_pnl_columns, rolling_pnl_detail = parse_qbo_multicolumn_report(raw_rolling_pnl)
        except Exception as e:
            partial_errors.append(
                safe_error_detail(
                    error_code="report_rolling_pnl_parse_failed",
                    user_message="Rolling P&L detail could not be parsed.",
                    exc=e,
                    retryable=False,
                    log_context="report_rolling_pnl_parse_failed",
                )
            )
            
    rolling_cf_columns, rolling_cf_detail = [], []
    if raw_rolling_cf:
        try:
            rolling_cf_columns, rolling_cf_detail = parse_qbo_multicolumn_report(raw_rolling_cf)
        except Exception as e:
            partial_errors.append(
                safe_error_detail(
                    error_code="report_rolling_cashflow_parse_failed",
                    user_message="Rolling cash-flow detail could not be parsed.",
                    exc=e,
                    retryable=False,
                    log_context="report_rolling_cashflow_parse_failed",
                )
            )

    # ── Budget from Peregrine BudgetLine table ──
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 — the inline budget-map
    # query is EXTRACTED (verbatim semantics) into build_budget_map so the board_pack
    # builder reuses the same deterministic budget source; behavior unchanged here.
    budget_map: dict[str, float] = {}  # account_name (lower) → budget amount
    try:
        budget_map = build_budget_map(db, company_id, year, month)
    except Exception as e:
        partial_errors.append(
            safe_error_detail(
                error_code="report_budget_map_unavailable",
                user_message="Budget comparison data could not be loaded.",
                exc=e,
                retryable=True,
                log_context="report_budget_map_unavailable",
            )
        )

    return {
        "company_id": company_id,
        "company_name": company_name,
        "period": period,
        "period_label": resolved["label"],
        "kpis": {
            "revenue": period_pl["revenue"],
            "gross_profit": period_pl["gross_profit"],
            "ebitda": period_pl["ebitda"],
            "net_income": period_pl["net_income"],
            "cash": bs["cash"],
            "total_assets": bs["total_assets"],
            "total_equity": bs["total_equity"],
            # None where the ratio is not meaningful (see data_quality.ratio_status).
            "gross_margin": ratios.get("gross_profit_ratio"),
            "ebitda_margin": ratios.get("ebitda_ratio"),
            "current_ratio": ratios.get("current_ratio"),
            "quick_ratio": ratios.get("quick_ratio"),
            "debt_equity_ratio": ratios.get("debt_to_equity_ratio"),
        },
        "current_pnl": period_pl,
        "prior_pnl": prior_pl,
        "current_bs": bs,
        "ttm_pnl": ttm_pl,
        "ttm_bs": bs,
        "ttm_cf": cf,
        "ytd": ytd_pl,
        # The fiscal-year anchor behind the YTD window and where it came from
        # ("qbo_company_info" or the visible "default_january" fallback).
        "fiscal_period": {
            "fy_start": resolved["fy_start"],
            "fy_end": resolved["fy_end"],
            "fy_start_month": resolved["fy_start_month"],
            "fy_start_source": resolved["fy_start_source"],
        },
        "ratios": ratios,
        "trends": trend_table,
        "customers": customers,
        "vendors": vendors,
        # ── Account-level detail for full statements ──
        "pnl_detail": pnl_detail,
        "bs_detail": bs_detail,
        # ── NEW: Budget map from Peregrine ──
        "budget_map": budget_map,
        # ── NEW: Rolling 12-month detail ──
        "rolling_pnl_columns": rolling_pnl_columns,
        "rolling_pnl_detail": rolling_pnl_detail,
        "rolling_cf_columns": rolling_cf_columns,
        "rolling_cf_detail": rolling_cf_detail,
        # ── NEW: AR/AP Aging ──
        "ar_aging": ar_aging_data,
        "ap_aging": ap_aging_data,
        "errors": partial_errors,
        "data_quality": {
            "is_partial": bool(partial_errors),
            "unavailable_sections": [err["error_code"] for err in partial_errors],
            "ratio_status": ratio_status,
        },
    }


def _safe_ar_ap(qbo_client: QBOClient, kind: str) -> Dict[str, Any]:
    """Fetch AR or AP aging; caller decides whether this is fatal or partial."""
    if kind == "ar":
        return ar_aging(qbo_client)
    return ap_aging(qbo_client)

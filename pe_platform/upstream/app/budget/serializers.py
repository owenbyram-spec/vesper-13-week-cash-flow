import json
from typing import Any, Dict, List

from ..models import BudgetDriver, BudgetLine
from .constants import MONTHS

DRIVER_METHODS = {
    "ttm_avg": "TTM Average",
    "pct_revenue": "% of Revenue",
    "fixed": "Fixed Amount",
    "yoy_growth": "YoY Growth",
    "volume": "Volume × Price",
}

REVENUE_METHODS = {"volume", "fixed", "yoy_growth"}
EXPENSE_METHODS = {"pct_revenue", "ttm_avg", "fixed"}


def _line_to_dict(line: BudgetLine, vendor_lines: List = None, customer_lines: List = None) -> Dict[str, Any]:
    amounts = line.monthly_amounts or {}
    annual = sum(amounts.get(m, 0) for m in MONTHS)
    result = {
        "id": line.id,
        "company_id": line.company_id,
        "budget_year": line.budget_year,
        "account_name": line.account_name,
        "account_type": line.account_type,
        "qbo_account_id": line.qbo_account_id,
        "monthly_amounts": {m: amounts.get(m, 0) for m in MONTHS},
        "annual_total": round(annual, 2),
        "sort_order": line.sort_order,
        "notes": getattr(line, "notes", None),
        "department": getattr(line, "department", None),
        "status": getattr(line, "status", "draft"),
        "scenario": getattr(line, "scenario", "base"),
        "driver_formula": getattr(line, "driver_formula", None),
        "parent_line_id": getattr(line, "parent_line_id", None),
        "vendor_name": getattr(line, "vendor_name", None),
        "customer_name": getattr(line, "customer_name", None),
    }
    if vendor_lines is not None:
        result["vendor_lines"] = [_line_to_dict(vl) for vl in vendor_lines]
    if customer_lines is not None:
        result["customer_lines"] = [_line_to_dict(cl) for cl in customer_lines]
    return result


def _safe_json(val: Any) -> dict:
    """Ensure a JSON field is a dict, parsing from string if needed."""
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _driver_to_dict(d: BudgetDriver) -> Dict[str, Any]:
    computed = _safe_json(d.computed_values)
    overrides = _safe_json(d.overrides)
    effective = {m: overrides.get(m, computed.get(m, 0)) for m in MONTHS}
    return {
        "id": d.id,
        "company_id": d.company_id,
        "budget_year": d.budget_year,
        "scenario": d.scenario,
        "account_name": d.account_name,
        "qbo_account_id": d.qbo_account_id,
        "driver_method": d.driver_method,
        "driver_method_label": DRIVER_METHODS.get(d.driver_method, d.driver_method),
        "driver_category": getattr(d, "driver_category", "expense") or "expense",
        "vendor_name": getattr(d, "vendor_name", None),
        "customer_name": getattr(d, "customer_name", None),
        "driver_params": _safe_json(d.driver_params),
        "computed_values": {m: computed.get(m, 0) for m in MONTHS},
        "overrides": overrides,
        "effective_values": effective,
        "annual_total": round(sum(effective.get(m, 0) for m in MONTHS), 2),
        "budget_line_id": d.budget_line_id,
        "is_dirty": getattr(d, "is_dirty", True),
    }

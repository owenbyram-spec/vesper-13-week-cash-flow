"""
app/routers/budget.py
──────────────────────
12-Month Forward Budget — Full Feature Set.

GET    /budget                  List all budget lines for a company + year
POST   /budget                  Create/upsert a budget line
PUT    /budget/{id}             Update monthly amounts / notes / department
DELETE /budget/{id}             Delete a line
GET    /budget/vs-actuals       Compare budget to QBO YTD actuals month-by-month
POST   /budget/seed-from-actuals Seed the budget from prior-year QBO P&L (+ growth %)
GET    /budget/rolling-forecast  Blend actuals (past months) + budget (future months)
GET    /budget/export-xlsx       Multi-sheet Excel workbook
POST   /budget/submit            Set status to "submitted"
POST   /budget/approve           Set status to "approved"
POST   /budget/unlock            Revert status to "draft"
"""
from __future__ import annotations

import io
import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

log = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional  # noqa: E402

from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from typing import TypedDict  # noqa: E402

from ..db import get_db  # noqa: E402
from ..models import BudgetLine, BudgetMonthLock, BudgetAuditLog, BudgetDriver, User  # noqa: E402
from ..auth import get_current_user_from_token  # noqa: E402
from ..budget.constants import MONTHS, MONTH_LABELS  # noqa: E402
from ..budget.permissions import _require_budget_access  # noqa: E402
from ..budget.period_locks import accounting_closed_month_keys, budget_locked_month_keys  # noqa: E402
from ..budget.qbo_reports import (  # noqa: E402
    _actual_entries_by_id,
    _actual_entries_by_name,
    _fetch_customer_revenue_breakdown,
    _fetch_vendor_expense_breakdown,
    _parse_pl_by_month,
    _parse_pl_by_month_with_types,
    _parse_pl_single_month,
    _parse_pl_single_month_entries,
)
from ..budget.seed_sublines import plan_sublines, unallocated_note  # noqa: E402
from ..budget.rollups import (  # noqa: E402
    _aggregate_actual_amount,
    _aggregate_budget_line_amount,
    _rollup_budget_lines,
)
from ..budget.schemas import (  # noqa: E402
    BudgetLineCreate,
    BudgetLineUpdate,
    CustomerLineCreate,
    DriverCreate,
    DriverUpdate,
    VendorLineCreate,
)
from ..budget.serializers import (  # noqa: E402
    DRIVER_METHODS,
    EXPENSE_METHODS,
    REVENUE_METHODS,
    _driver_to_dict,
    _line_to_dict,
    _safe_json,
)
# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-6B — global_tenant_guard import for the scenario-compare route decorator.
from ..dependencies import check_entity_tenant_access, check_tenant_access, global_tenant_guard, require_debug_endpoint_access  # noqa: E402
from ..qbo_client import get_qbo_client_from_db  # noqa: E402
from ..services.budget_drivers import (bulk_set_driver_method_core, update_budget_driver_service, create_budget_driver_service, compute_budget_driver_service)  # noqa: E402
from ..services.budget_reforecast import apply_budget_drivers_service, create_reforecast_service, lock_budget_write_scope, BudgetPlanError  # noqa: E402
from ..qbo_actions.qbo_query import qbo_int  # noqa: E402  # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
from ..services.errors import safe_error_detail, user_safe_http_exception  # noqa: E402
from ..services.redaction import redact_to_string  # noqa: E402

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/budget", tags=["Budget"])


# ─────────────────────────────────────────────────────────────────────────────
# Reusable budget-vs-actual math (NOT a route — shared by the watchdog)
# ─────────────────────────────────────────────────────────────────────────────

class VarianceLens(TypedDict):
    """One section's single-month budget-vs-actual: $ amounts + the two distinct variances."""
    budget: float
    actual: float
    variance: float             # actual − budget ($), distinct from the %
    variance_pct: float | None  # signed % of budget; None when budget is 0


class RevenueEbitdaVariance(TypedDict):
    """The revenue + EBITDA lenses returned by ``compute_revenue_ebitda_variance``."""
    revenue: VarianceLens
    ebitda: VarianceLens


def compute_revenue_ebitda_variance(
    db: Session, company_id: str, budget_year: int, month_key: str, *, qbo_client=None
) -> RevenueEbitdaVariance | None:
    """Base-scenario single-month Revenue & EBITDA budget-vs-actual for ``company_id``.

    The SAME COGS-inclusive math as the ``/vs-actuals`` endpoint (revenue section total,
    EBITDA = gross_profit − opex from real QBO P&L actuals), factored out so a non-HTTP
    caller (the watchdog) can source variance from the real budget-vs-actual engine instead
    of the retired LBO/BudgetLine annual-run-rate hack. Scenario-scoped to ``base`` (+ legacy
    NULL rows) — the canonical budget-router scenario filter — so a non-base plan never skews
    the variance.

    Returns ``None`` when there is no base-scenario budget for the year, or when QBO actuals
    are unavailable (never fabricates a zero-actuals variance). ``variance`` is ``actual −
    budget`` ($, distinct from the %); ``variance_pct`` is the signed % of budget (None when
    budget is 0). A trailing metric is ``variance < 0``.
    """
    month_key = month_key.lower()[:3]
    if month_key not in MONTHS:
        return None
    month_idx = MONTHS.index(month_key)

    # Base scenario only (+ legacy NULL), the canonical budget-router scenario predicate.
    all_budget_lines = (
        db.query(BudgetLine)
        .filter(
            BudgetLine.company_id == company_id,
            BudgetLine.budget_year == budget_year,
            (BudgetLine.scenario == "base") | (BudgetLine.scenario == None),  # noqa: E711
        )
        .order_by(BudgetLine.account_type, BudgetLine.sort_order, BudgetLine.account_name)
        .all()
    )
    if not all_budget_lines:
        return None
    budget_lines = [line for line in all_budget_lines if line.parent_line_id is None]
    children_by_parent: dict[int, list[BudgetLine]] = defaultdict(list)
    for line in all_budget_lines:
        if line.parent_line_id is not None:
            children_by_parent[line.parent_line_id].append(line)

    # Real QBO P&L actuals for the single month. Unavailable → None (surface, never zero-fill).
    try:
        client = qbo_client or get_qbo_client_from_db(db, company_id)
        month_num = month_idx + 1
        if month_num in (1, 3, 5, 7, 8, 10, 12):
            last_day = 31
        elif month_num in (4, 6, 9, 11):
            last_day = 30
        else:
            last_day = 29 if (budget_year % 4 == 0 and (budget_year % 100 != 0 or budget_year % 400 == 0)) else 28
        start_dt = date(budget_year, month_num, 1)
        end_dt = date(budget_year, month_num, last_day)
        report = client.get_report("ProfitAndLoss", {
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "accounting_method": "Accrual",
        })
        actual_entries = _parse_pl_single_month_entries(report, month_key=month_key)
        actuals = _actual_entries_by_name(actual_entries)
        actuals_by_id = _actual_entries_by_id(actual_entries)
        actual_names_without_id = {
            entry["account_name"] for entry in actual_entries if not entry.get("qbo_account_id")
        }
    except Exception as e:
        logger.warning(
            "[Budget] variance actuals fetch failed for %s %s: %s",
            company_id, month_key, redact_to_string(str(e), max_length=500),
        )
        return None

    section_budget: dict[str, float] = defaultdict(float)
    section_actual: dict[str, float] = defaultdict(float)
    for line in budget_lines:
        budget_amt = _aggregate_budget_line_amount(line, month_key, children_by_parent)
        actual_amt = _aggregate_actual_amount(
            line, actuals, children_by_parent,
            actuals_by_id=actuals_by_id, actual_names_without_id=actual_names_without_id,
        )
        acct_type = line.account_type or "opex"
        if acct_type not in ("revenue", "cogs", "opex", "other_income", "other_expense"):
            acct_type = "opex"
        section_budget[acct_type] += budget_amt
        section_actual[acct_type] += actual_amt

    def _lens(budget_v: float, actual_v: float) -> VarianceLens:
        budget_v = round(budget_v, 2)
        actual_v = round(actual_v, 2)
        variance = round(actual_v - budget_v, 2)
        var_pct = round((variance / budget_v * 100), 1) if budget_v else None
        return {"budget": budget_v, "actual": actual_v, "variance": variance, "variance_pct": var_pct}

    rev = _lens(section_budget["revenue"], section_actual["revenue"])
    # EBITDA = gross_profit − opex = (revenue − cogs) − opex (COGS-inclusive), matching /vs-actuals.
    ebitda_budget = section_budget["revenue"] - section_budget["cogs"] - section_budget["opex"]
    ebitda_actual = section_actual["revenue"] - section_actual["cogs"] - section_actual["opex"]
    ebitda = _lens(ebitda_budget, ebitda_actual)

    return {"revenue": rev, "ebitda": ebitda}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.get("")
def list_budget(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    department: Optional[str] = Query(None),
    scenario: str = Query("base"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    q = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    )
    # Filter by scenario — handle legacy rows where scenario may be NULL
    q = q.filter((BudgetLine.scenario == scenario) | (BudgetLine.scenario == None))  # noqa: E711
    if department:
        q = q.filter(BudgetLine.department == department)
    lines = q.order_by(BudgetLine.account_type, BudgetLine.sort_order, BudgetLine.account_name).all()

    # Separate parent lines from vendor sub-lines
    parent_lines = [l for l in lines if not getattr(l, "parent_line_id", None)]  # noqa: E741
    child_map: Dict[int, list] = {}
    for l in lines:  # noqa: E741
        pid = getattr(l, "parent_line_id", None)
        if pid:
            child_map.setdefault(pid, []).append(l)

    # Render parent totals from child rows without mutating on a read endpoint.
    # Persisted repairs belong in explicit EDITOR write paths.
    response_amount_overrides: Dict[int, Dict[str, float]] = {}
    for parent in parent_lines:
        children = child_map.get(parent.id)
        if not children:
            continue  # no sub-lines — parent's own amounts are authoritative
        aggregated = {}
        for m in MONTHS:
            aggregated[m] = round(sum(
                float((c.monthly_amounts or {}).get(m, 0)) for c in children
            ), 2)
        # Only write if actually changed to avoid unnecessary commits
        current = parent.monthly_amounts or {}
        if any(aggregated.get(m, 0) != current.get(m, 0) for m in MONTHS):
            response_amount_overrides[parent.id] = aggregated

    def _to_dict_with_vendors(l):  # noqa: E741
        children = child_map.get(l.id, [])
        vendors = [c for c in children if getattr(c, "vendor_name", None)]
        customers = [c for c in children if getattr(c, "customer_name", None)]
        result = _line_to_dict(
            l,
            vendor_lines=vendors if vendors else None,
            customer_lines=customers if customers else None,
        )
        if l.id in response_amount_overrides:
            result["monthly_amounts"] = response_amount_overrides[l.id]
            result["annual_total"] = round(sum(response_amount_overrides[l.id].values()), 2)
        return result

    revenue_lines = [_to_dict_with_vendors(l) for l in parent_lines if l.account_type == "revenue"]  # noqa: E741
    cogs_lines = [_to_dict_with_vendors(l) for l in parent_lines if l.account_type == "cogs"]  # noqa: E741
    opex_lines = [_to_dict_with_vendors(l) for l in parent_lines if l.account_type == "opex"]  # noqa: E741
    other_income_lines = [_to_dict_with_vendors(l) for l in parent_lines if l.account_type == "other_income"]  # noqa: E741
    other_expense_lines = [_to_dict_with_vendors(l) for l in parent_lines if l.account_type == "other_expense"]  # noqa: E741

    def _monthly_sum(group):
        return {m: round(sum(l["monthly_amounts"].get(m, 0) for l in group), 2) for m in MONTHS}  # noqa: E741

    rev_totals = _monthly_sum(revenue_lines)
    cogs_totals = _monthly_sum(cogs_lines)
    opex_totals = _monthly_sum(opex_lines)
    oi_totals = _monthly_sum(other_income_lines)
    oe_totals = _monthly_sum(other_expense_lines)
    gp_totals = {m: round(rev_totals[m] - cogs_totals[m], 2) for m in MONTHS}
    ebitda_totals = {m: round(gp_totals[m] - opex_totals[m], 2) for m in MONTHS}
    net_other = {m: round(oi_totals[m] - oe_totals[m], 2) for m in MONTHS}
    net_income = {m: round(ebitda_totals[m] + net_other[m], 2) for m in MONTHS}

    # Unique departments
    all_lines = lines
    depts = sorted(set(getattr(l, "department", None) or "Unassigned" for l in all_lines))  # noqa: E741

    # Overall status (use first line's status or draft)
    status = getattr(lines[0], "status", "draft") if lines else "draft"

    return {
        "budget_year": budget_year,
        "months": MONTH_LABELS,
        "revenue_lines": revenue_lines,
        "cogs_lines": cogs_lines,
        "opex_lines": opex_lines,
        "other_income_lines": other_income_lines,
        "other_expense_lines": other_expense_lines,
        "totals": {
            "revenue": rev_totals,
            "cogs": cogs_totals,
            "gross_profit": gp_totals,
            "opex": opex_totals,
            "ebitda": ebitda_totals,
            "other_income": oi_totals,
            "other_expense": oe_totals,
            "net_other": net_other,
            "net_income": net_income,
        },
        "departments": depts,
        "status": status,
    }


@router.post("")
def create_line(
    body: BudgetLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    line = BudgetLine(**body.model_dump())
    # Reject a duplicate line for the same company/year/account/scenario/parent — there is no DB
    # uniqueness constraint, and a duplicate parent double-renders in the grid + the base read.
    _dup_q = db.query(BudgetLine.id).filter(
        BudgetLine.company_id == line.company_id,
        BudgetLine.budget_year == line.budget_year,
        BudgetLine.account_name == line.account_name,
        BudgetLine.scenario == (line.scenario or "base"),
    )
    if line.parent_line_id is None:
        _dup_q = _dup_q.filter(BudgetLine.parent_line_id.is_(None))
    else:
        _dup_q = _dup_q.filter(BudgetLine.parent_line_id == line.parent_line_id)
    if line.qbo_account_id:
        _dup_q = _dup_q.filter(BudgetLine.qbo_account_id == line.qbo_account_id)
    if _dup_q.first() is not None:
        raise user_safe_http_exception(
            status_code=409,
            error_code="budget_line_duplicate",
            user_message="A budget line for this account already exists for this year and scenario.",
        )
    db.add(line)  # LAYER-OK: legacy inline-db budget router (whole-file pattern); UX dedup guard added inline, service extraction is out of scope for this label/guard change
    db.commit()
    db.refresh(line)
    return _line_to_dict(line)


@router.put("/{line_id}")
def update_line(
    line_id: int,
    body: BudgetLineUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    line = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetLine,
        entity_id=line_id,
        not_found_detail="Budget line not found",
    )

    try:
        lock_budget_write_scope(db, company_id=line.company_id)
        db.refresh(line)
        from ..budget.driver_calculation import month_values
        monthly_update = month_values({**(line.monthly_amounts or {}), **body.monthly_amounts}) if body.monthly_amounts is not None else None
    except BudgetPlanError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Manual budget locks and accounting hard locks share one write boundary.
    if body.monthly_amounts:
        locked_months = set(budget_locked_month_keys(db, company_id=line.company_id, budget_year=line.budget_year))
        old_amounts = line.monthly_amounts or {}
        for m in MONTHS:
            new_val = body.monthly_amounts.get(m)
            old_val = old_amounts.get(m, 0)
            if new_val is not None and float(new_val) != float(old_val) and m in locked_months:
                raise HTTPException(status_code=400, detail=f"Month '{m}' is locked and cannot be edited")

    # Audit trail — log changes
    update_data = body.model_dump(exclude_none=True)
    if monthly_update is not None:
        update_data["monthly_amounts"] = monthly_update
    for field, new_val in update_data.items():
        if field == "monthly_amounts" and new_val:
            old_amounts = line.monthly_amounts or {}
            for m in MONTHS:
                old_v = old_amounts.get(m, 0)
                new_v = new_val.get(m, old_v)
                if float(new_v) != float(old_v):
                    db.add(BudgetAuditLog(
                        company_id=line.company_id,
                        budget_line_id=line.id,
                        field=m,
                        old_value=str(old_v),
                        new_value=str(new_v),
                    ))
        else:
            old_v = getattr(line, field, None)
            if str(old_v) != str(new_val):
                db.add(BudgetAuditLog(
                    company_id=line.company_id,
                    budget_line_id=line.id,
                    field=field,
                    old_value=str(old_v) if old_v is not None else None,
                    new_value=str(new_val),
                ))

    for field, val in update_data.items():
        setattr(line, field, val)
    db.commit()
    db.refresh(line)
    return _line_to_dict(line)


@router.delete("/{line_id}")
def delete_line(
    line_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    line = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetLine,
        entity_id=line_id,
        not_found_detail="Budget line not found",
    )
    db.delete(line)
    db.commit()
    return {"status": "deleted"}


@router.post("/vendor-lines")
def create_vendor_line(
    body: VendorLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Create a vendor sub-line under a parent expense budget line."""
    parent = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetLine,
        entity_id=body.parent_line_id,
        not_found_detail="Parent budget line not found",
    )
    vl = BudgetLine(
        company_id=parent.company_id,
        budget_year=parent.budget_year,
        account_name=parent.account_name,
        account_type=parent.account_type,
        qbo_account_id=parent.qbo_account_id,
        scenario=getattr(parent, "scenario", "base"),
        parent_line_id=parent.id,
        vendor_name=body.vendor_name,
        monthly_amounts=body.monthly_amounts,
    )
    db.add(vl)
    db.commit()
    db.refresh(vl)
    return _line_to_dict(vl)


@router.post("/customer-lines")
def create_customer_line(
    body: CustomerLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Create a customer sub-line under a parent revenue budget line."""
    parent = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetLine,
        entity_id=body.parent_line_id,
        not_found_detail="Parent budget line not found",
    )
    cl = BudgetLine(
        company_id=parent.company_id,
        budget_year=parent.budget_year,
        account_name=parent.account_name,
        account_type=parent.account_type,
        qbo_account_id=parent.qbo_account_id,
        scenario=getattr(parent, "scenario", "base"),
        parent_line_id=parent.id,
        customer_name=body.customer_name,
        monthly_amounts=body.monthly_amounts,
    )
    db.add(cl)
    db.commit()
    db.refresh(cl)
    return _line_to_dict(cl)


@router.get("/vendor-breakdown")
def vendor_breakdown(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    account_name: str = Query(..., description="Account to break down by vendor"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Fetch vendor-level spend for a given expense account from QBO, for seeding vendor budgets."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    try:
        client = get_qbo_client_from_db(db, company_id)
        from ..analysis.vendor_spend import vendor_spend_summary
        data = vendor_spend_summary(client, limit=1000)
        vendors = data.get("vendor_breakdown", [])
        # Return all vendors (the frontend can filter) 
        return {"account_name": account_name, "vendors": [{"name": v[0], "total": v[1]} for v in vendors]}
    except Exception as e:
        return {
            "account_name": account_name,
            "vendors": [],
            "error": safe_error_detail(
                error_code="budget_vendor_breakdown_unavailable",
                user_message="Vendor spend data is temporarily unavailable.",
                exc=e,
                retryable=True,
                log_context="budget_vendor_breakdown_unavailable",
            ),
            "data_quality": {
                "is_partial": True,
                "unavailable_sections": ["budget_vendor_breakdown_unavailable"],
            },
        }


@router.get("/vs-actuals")
def budget_vs_actuals(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    month: str = Query("jan", description="Three-letter month key (jan, feb, ...)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """Single-month Budget vs Actuals P&L analysis.

    Returns P&L-grouped lines for the selected month with actual, budget,
    variance $, variance %, and a flagged indicator for |var%| > threshold.
    """
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    month_key = month.lower()[:3]
    if month_key not in MONTHS:
        from fastapi import HTTPException as HE
        raise HE(status_code=400, detail=f"Invalid month: {month}")
    month_idx = MONTHS.index(month_key)

    all_budget_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).order_by(BudgetLine.account_type, BudgetLine.sort_order, BudgetLine.account_name).all()
    budget_lines = [line for line in all_budget_lines if line.parent_line_id is None]
    children_by_parent: Dict[int, List[BudgetLine]] = defaultdict(list)
    for line in all_budget_lines:
        if line.parent_line_id is not None:
            children_by_parent[line.parent_line_id].append(line)

    # Fetch actuals from QBO P&L for the selected month only
    actuals_partial = False  # 2A: surface a failed QBO actuals fetch, not silent zeros
    try:
        client = get_qbo_client_from_db(db, company_id)
        month_num = month_idx + 1
        last_day = 28
        if month_num in (1, 3, 5, 7, 8, 10, 12):
            last_day = 31
        elif month_num in (4, 6, 9, 11):
            last_day = 30
        elif month_num == 2:
            last_day = 29 if (budget_year % 4 == 0 and (budget_year % 100 != 0 or budget_year % 400 == 0)) else 28
        start_dt = date(budget_year, month_num, 1)
        end_dt = date(budget_year, month_num, last_day)
        report = client.get_report("ProfitAndLoss", {
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "accounting_method": "Accrual",
        })
        actual_entries = _parse_pl_single_month_entries(report, month_key=month_key)
        actuals = _actual_entries_by_name(actual_entries)
        actuals_by_id = _actual_entries_by_id(actual_entries)
        actual_names_without_id = {
            entry["account_name"]
            for entry in actual_entries
            if not entry.get("qbo_account_id")
        }
    except Exception as e:
        logger.warning("[Budget] Actuals fetch failed for %s: %s", month_key, redact_to_string(str(e), max_length=500))
        actuals_partial = True
        actual_entries = []
        actuals = {}
        actuals_by_id = {}
        actual_names_without_id = set()

    # Locked months — tell frontend which months are closed / read-only. This is the
    # UNION of manual budget locks and accounting month-end closes: a period locked in
    # the accounting calendar (FinancialCalendar.locked) surfaces here so the closed
    # month can be auto-selected and analysed for budget-vs-actual. period_closed_months
    # is split out so the UI can label the close distinctly from a manual budget lock.
    locks = db.query(BudgetMonthLock).filter(
        BudgetMonthLock.company_id == company_id,
        BudgetMonthLock.budget_year == budget_year,
    ).all()
    budget_locked_months = [lock.month for lock in locks]
    period_closed_months = accounting_closed_month_keys(
        db, company_id=company_id, budget_year=budget_year
    )
    locked_months = sorted(
        set(budget_locked_months) | set(period_closed_months),
        key=lambda m: MONTHS.index(m) if m in MONTHS else 99,
    )

    threshold_pct = 10.0
    flagged_count = 0
    total_favorable = 0.0
    total_unfavorable = 0.0

    # Build lines grouped by account_type
    groups: Dict[str, list] = {
        "revenue": [], "cogs": [], "opex": [],
        "other_income": [], "other_expense": [],
    }

    for line in budget_lines:
        budget_amt = _aggregate_budget_line_amount(line, month_key, children_by_parent)
        actual_amt = _aggregate_actual_amount(
            line,
            actuals,
            children_by_parent,
            actuals_by_id=actuals_by_id,
            actual_names_without_id=actual_names_without_id,
        )
        variance = round(actual_amt - budget_amt, 2)
        var_pct = round((variance / budget_amt * 100), 1) if budget_amt else None
        is_flagged = var_pct is not None and abs(var_pct) > threshold_pct
        acct_type = line.account_type or "opex"

        if is_flagged:
            flagged_count += 1
            # For revenue/other_income: positive variance (actual > budget) is favorable
            # For expenses: negative variance (actual < budget) is favorable
            is_expense = acct_type in ("cogs", "opex", "other_expense")
            if (is_expense and variance > 0) or (not is_expense and variance < 0):
                total_unfavorable += abs(variance)
            else:
                total_favorable += abs(variance)

        entry = {
            "account_name": line.account_name,
            "account_type": line.account_type,
            "budget": budget_amt,
            "actual": actual_amt,
            "variance": variance,
            "variance_pct": var_pct,
            "flagged": is_flagged,
            "qbo_account_id": line.qbo_account_id,
            "unbudgeted": False,
        }

        if acct_type in groups:
            groups[acct_type].append(entry)
        else:
            groups["opex"].append(entry)

    represented_account_ids: set[str] = set()
    represented_fallback_names: set[str] = set()
    represented_account_names: set[str] = set()
    for line in budget_lines:
        for rollup_line in _rollup_budget_lines(line, children_by_parent):
            represented_account_names.add(rollup_line.account_name)
            account_id = getattr(rollup_line, "qbo_account_id", None)
            if account_id:
                represented_account_ids.add(str(account_id))
            else:
                represented_fallback_names.add(rollup_line.account_name)

    for details in actual_entries:
        account_name = str(details.get("account_name") or "").strip()
        if not account_name:
            continue
        account_id = str(details.get("qbo_account_id") or "").strip()
        if account_id and account_id in represented_account_ids:
            continue
        if account_id and account_name in represented_fallback_names:
            continue
        if not account_id and account_name in represented_account_names:
            continue
        actual_amt = round(float(details.get("amount", 0) or 0), 2)
        if actual_amt == 0:
            continue

        acct_type = str(details.get("account_type") or "opex")
        variance = actual_amt
        is_flagged = abs(actual_amt) > 0

        if is_flagged:
            flagged_count += 1
            is_expense = acct_type in ("cogs", "opex", "other_expense")
            if is_expense:
                total_unfavorable += abs(variance)
            else:
                total_favorable += abs(variance)

        entry = {
            "account_name": account_name,
            "account_type": acct_type,
            "budget": 0.0,
            "actual": actual_amt,
            "variance": variance,
            "variance_pct": None,
            "flagged": True,
            "qbo_account_id": account_id or None,
            "unbudgeted": True,
        }

        if acct_type in groups:
            groups[acct_type].append(entry)
        else:
            groups["opex"].append(entry)

    # Compute section totals
    def _section_totals(lines):
        b = sum(l["budget"] for l in lines)  # noqa: E741
        a = sum(l["actual"] for l in lines)  # noqa: E741
        v = round(a - b, 2)
        vp = round((v / b * 100), 1) if b else None
        return {"budget": round(b, 2), "actual": round(a, 2), "variance": v, "variance_pct": vp}

    rev_t = _section_totals(groups["revenue"])
    cogs_t = _section_totals(groups["cogs"])
    gp = {
        "budget": round(rev_t["budget"] - cogs_t["budget"], 2),
        "actual": round(rev_t["actual"] - cogs_t["actual"], 2),
        "variance": round((rev_t["actual"] - cogs_t["actual"]) - (rev_t["budget"] - cogs_t["budget"]), 2),
    }
    gp["variance_pct"] = round((gp["variance"] / gp["budget"] * 100), 1) if gp["budget"] else None
    opex_t = _section_totals(groups["opex"])
    ebitda = {
        "budget": round(gp["budget"] - opex_t["budget"], 2),
        "actual": round(gp["actual"] - opex_t["actual"], 2),
        "variance": round((gp["actual"] - opex_t["actual"]) - (gp["budget"] - opex_t["budget"]), 2),
    }
    ebitda["variance_pct"] = round((ebitda["variance"] / ebitda["budget"] * 100), 1) if ebitda["budget"] else None
    oi_t = _section_totals(groups["other_income"])
    oe_t = _section_totals(groups["other_expense"])
    net_other = {
        "budget": round(oi_t["budget"] - oe_t["budget"], 2),
        "actual": round(oi_t["actual"] - oe_t["actual"], 2),
        "variance": round((oi_t["actual"] - oe_t["actual"]) - (oi_t["budget"] - oe_t["budget"]), 2),
    }
    net_other["variance_pct"] = round((net_other["variance"] / net_other["budget"] * 100), 1) if net_other["budget"] else None
    net_income = {
        "budget": round(ebitda["budget"] + net_other["budget"], 2),
        "actual": round(ebitda["actual"] + net_other["actual"], 2),
        "variance": round((ebitda["actual"] + net_other["actual"]) - (ebitda["budget"] + net_other["budget"]), 2),
    }
    net_income["variance_pct"] = round((net_income["variance"] / net_income["budget"] * 100), 1) if net_income["budget"] else None

    return {
        "budget_year": budget_year,
        "month": month_key,
        "month_label": MONTH_LABELS[month_idx],
        "revenue_lines": groups["revenue"],
        "cogs_lines": groups["cogs"],
        "opex_lines": groups["opex"],
        "other_income_lines": groups["other_income"],
        "other_expense_lines": groups["other_expense"],
        "totals": {
            "revenue": rev_t,
            "cogs": cogs_t,
            "gross_profit": gp,
            "opex": opex_t,
            "ebitda": ebitda,
            "other_income": oi_t,
            "other_expense": oe_t,
            "net_other": net_other,
            "net_income": net_income,
        },
        "threshold_pct": threshold_pct,
        "flagged_count": flagged_count,
        "total_unfavorable": round(total_unfavorable, 2),
        "total_favorable": round(total_favorable, 2),
        "locked_months": locked_months,
        "budget_locked_months": budget_locked_months,
        "period_closed_months": period_closed_months,
        "data_quality": {
            "is_partial": actuals_partial,
            "unavailable_sections": ["budget_actuals_unavailable"] if actuals_partial else [],
        },
    }


@router.post("/vs-actuals/ai-analysis")
def vs_actuals_ai_analysis(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    month: str = Query(..., description="Three-letter month key"),
    account_name: str = Query(..., description="Account name to analyze"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """Generate AI analysis for a flagged budget variance.

    Examines budget drivers and vendor-level GL detail for the month
    to explain why actuals diverged from budget.
    """
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    from ..services.llm import analysis_llm

    month_key = month.lower()[:3]
    month_idx = MONTHS.index(month_key) if month_key in MONTHS else 0
    month_num = month_idx + 1

    # 1) Get budget drivers for this account
    drivers = db.query(BudgetDriver).filter(
        BudgetDriver.company_id == company_id,
        BudgetDriver.budget_year == budget_year,
        BudgetDriver.account_name == account_name,
    ).all()

    driver_info = []
    for d in drivers:
        entry = {
            "method": d.driver_method,
            "params": d.driver_params or {},
            "vendor": getattr(d, "vendor_name", None),
            "customer": getattr(d, "customer_name", None),
        }
        driver_info.append(entry)

    # 2) Get the budget line for context
    budget_line = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.account_name == account_name,
        BudgetLine.parent_line_id == None,  # noqa: E711
    ).first()

    budget_amt = 0
    if budget_line:
        budget_amt = (budget_line.monthly_amounts or {}).get(month_key, 0)

    # 3) Get vendor sub-lines for budget context
    vendor_budget = []
    if budget_line:
        sub_lines = db.query(BudgetLine).filter(
            BudgetLine.parent_line_id == budget_line.id,
        ).all()
        for sl in sub_lines:
            v_name = getattr(sl, "vendor_name", None) or getattr(sl, "customer_name", None) or "Unknown"
            v_amt = (sl.monthly_amounts or {}).get(month_key, 0)
            if v_amt:
                vendor_budget.append({"name": v_name, "budgeted": round(v_amt, 2)})

    # 4) Fetch per-entity GL detail for this account + month from QBO. Resolution + parsing +
    #    the "why is it empty" outcome live in app/budget/gl_detail.py (robust AcctNum-first
    #    resolution + header-located columns; a broad-swallow no longer hides the reason).
    from ..budget.gl_detail import fetch_gl_vendor_actuals

    vendor_actuals: list[dict] = []
    gl_detail_status = "unresolved"
    client = None
    start_dt = f"{budget_year}-{month_num:02d}-01"
    last_day = 28
    if month_num in (1, 3, 5, 7, 8, 10, 12):
        last_day = 31
    elif month_num in (4, 6, 9, 11):
        last_day = 30
    elif month_num == 2:
        last_day = 29 if (budget_year % 4 == 0 and (budget_year % 100 != 0 or budget_year % 400 == 0)) else 28
    end_dt = f"{budget_year}-{month_num:02d}-{last_day}"

    try:
        client = get_qbo_client_from_db(db, company_id)
    except Exception as e:
        logger.warning(
            "[BudgetAI] QBO client init failed for %s: %s",
            company_id, redact_to_string(str(e), max_length=300),
        )

    if client:
        vendor_actuals, gl_detail_status = fetch_gl_vendor_actuals(
            client, budget_line, account_name, start_date=start_dt, end_date=end_dt,
        )
        if gl_detail_status != "resolved_detail":
            logger.info(
                "[BudgetAI] GL detail for %s (%s): status=%s",
                account_name, month_key, gl_detail_status,
            )

    # 5) Fetch actual amount from P&L
    actual_amt = 0
    if client:
        try:
            report = client.get_report("ProfitAndLoss", {
                "start_date": start_dt,
                "end_date": end_dt,
                "accounting_method": "Accrual",
            })
            actuals_map = _parse_pl_single_month(report, month_key=month_key)
            actual_amt = actuals_map.get(account_name, 0)
        except Exception:
            pass

    variance = round(actual_amt - budget_amt, 2)
    var_pct = round((variance / budget_amt * 100), 1) if budget_amt else None

    # 6) Build LLM prompt. The deterministic assembly lives in
    #    app/budget/variance_narrative.py so the EXACT context the model receives (account
    #    section + favorability framing + honest no-GL gating) is unit-testable without an
    #    LLM call. is_revenue_account drives the response's entity_label below.
    from ..budget.variance_narrative import (
        build_variance_analysis_prompt,
        is_revenue_account_type,
    )

    account_type = budget_line.account_type if budget_line else None
    is_revenue_account = is_revenue_account_type(account_type)
    entity_label = "Customer" if is_revenue_account else "Vendor"

    system_prompt, prompt = build_variance_analysis_prompt(
        account_name=account_name,
        month_label=f"{MONTH_LABELS[month_idx]} {budget_year}",
        budget_amt=budget_amt,
        actual_amt=actual_amt,
        variance=variance,
        var_pct=var_pct,
        account_type=account_type,
        driver_info=driver_info,
        vendor_budget=vendor_budget,
        vendor_actuals=vendor_actuals,
    )

    analysis = analysis_llm.predict_text(system_prompt, prompt, temperature=0.1)
    if not analysis:
        analysis = "Unable to generate analysis — LLM unavailable."

    return {
        "account_name": account_name,
        "month": month_key,
        "analysis": analysis,
        "budget": round(budget_amt, 2),
        "actual": round(actual_amt, 2),
        "variance": variance,
        "variance_pct": var_pct,
        # PLAN-EXEMPT: batch-4 bug 3 — tell the UI whether these entities are
        # Customers (revenue accounts) or Vendors, so its header is honest.
        "entity_label": entity_label,
        "vendor_actuals": vendor_actuals[:10],
        "vendor_budget": vendor_budget[:10],
        "drivers": driver_info,
        # Diagnosable outcome of the GL sub-fetch: resolved_detail / resolved_empty /
        # unresolved / report_error — so an empty breakdown is never a black box.
        "gl_detail_status": gl_detail_status,
    }


@router.post("/seed-from-actuals")
def seed_from_actuals(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    source_year: Optional[int] = Query(None, description="Year to pull actuals from"),
    growth_pct: float = Query(0.0, description="Growth factor to apply (0.05 = 5%)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    yr = source_year or (budget_year - 1)
    try:
        client = get_qbo_client_from_db(db, company_id)
        report = client.get_report("ProfitAndLoss", {
            "start_date": f"{yr}-01-01",
            "end_date": f"{yr}-12-31",
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        })
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="budget_actuals_fetch_failed",
            user_message="Budget actuals could not be loaded from QuickBooks.",
            exc=e,
            retryable=True,
            log_context="budget_actuals_fetch_failed",
        )

    actuals_with_types = _parse_pl_by_month_with_types(report)

    # Fetch the vendor / customer attribution BEFORE replacing anything. A breakdown
    # that cannot be read completely seeds no sub-lines at all (its parents keep their
    # P&L amounts) and is reported in data_quality; it never seeds a partial split.
    breakdown_errors: List[Dict[str, Any]] = []
    unavailable_sections: List[str] = []

    def _breakdown(fetch, error_code: str, user_message: str):
        try:
            return fetch(client, yr)
        except Exception as e:
            unavailable_sections.append(error_code)
            breakdown_errors.append(safe_error_detail(
                error_code=error_code,
                user_message=user_message,
                exc=e,
                retryable=True,
                log_context=error_code,
            ))
            return None

    vendor_by_acct = _breakdown(
        _fetch_vendor_expense_breakdown,
        "budget_seed_vendor_breakdown_unavailable",
        "QuickBooks vendor transactions could not be loaded completely, so no vendor "
        "sub-lines were created; expense accounts keep their P&L totals.",
    )
    customer_by_acct = _breakdown(
        _fetch_customer_revenue_breakdown,
        "budget_seed_customer_breakdown_unavailable",
        "QuickBooks customer transactions could not be loaded completely, so no customer "
        "sub-lines were created; revenue accounts keep their P&L totals.",
    )

    # Replace the year's lines in ONE transaction (parents + sub-lines + driver flags).
    db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).delete()

    multiplier = 1.0 + growth_pct
    created = 0
    parent_lines: List[BudgetLine] = []

    # PLAN-EXEMPT: batch-4 bug 4 — persist real P&L statement order. The actuals
    # dict preserves the report's row order (qbo_reports._parse_pl_by_month_with_types
    # builds it in insertion order); encode that enumerate index into sort_order so
    # ordered queries (account_type, sort_order, account_name) reproduce the statement.
    for idx, (acct_name, data) in enumerate(actuals_with_types.items()):
        monthly = {m: round(v * multiplier, 2) for m, v in data["monthly"].items()}
        line = BudgetLine(
            company_id=company_id,
            budget_year=budget_year,
            account_name=acct_name,
            account_type=data["type"],
            sort_order=idx,
            monthly_amounts=monthly,
            notes=f"Seeded from {yr} actuals" + (f" + {growth_pct*100:.0f}% growth" if growth_pct else ""),
        )
        db.add(line)  # LAYER-OK: budget seed persistence is router-local, matches existing seed_from_actuals pattern
        parent_lines.append(line)
        created += 1
    db.flush()

    # Type-specific maps {account_name: line_id} so same-named accounts in different
    # types don't collide.
    expense_parent_map: Dict[str, int] = {}
    revenue_parent_map: Dict[str, int] = {}
    for ln in parent_lines:
        if ln.account_type in ("opex", "cogs"):
            expense_parent_map[ln.account_name] = ln.id
        elif ln.account_type == "revenue":
            revenue_parent_map[ln.account_name] = ln.id
    parents_by_id = {ln.id: ln for ln in parent_lines}
    parent_amounts = {ln.id: dict(ln.monthly_amounts or {}) for ln in parent_lines}
    growth = Decimal(str(multiplier))

    sub_counts = {"vendor": 0, "customer": 0, "unallocated": 0}
    for breakdown, type_map, kind in (
        (vendor_by_acct, expense_parent_map, "vendor"),
        (customer_by_acct, revenue_parent_map, "customer"),
    ):
        if breakdown is None:
            continue
        for parent_id, sublines in plan_sublines(parent_amounts, breakdown, type_map, growth).items():
            parent_line = parents_by_id[parent_id]
            for sub in sublines:
                db.add(BudgetLine(
                    company_id=company_id,
                    budget_year=budget_year,
                    account_name=parent_line.account_name,
                    account_type=parent_line.account_type,
                    scenario=getattr(parent_line, "scenario", "base"),
                    parent_line_id=parent_id,
                    monthly_amounts=sub.monthly_amounts,
                    notes=unallocated_note(kind) if sub.unallocated else None,
                    **{f"{kind}_name": sub.name},
                ))
                sub_counts["unallocated" if sub.unallocated else kind] += 1

    # After re-seeding, mark all existing drivers as dirty so they'll be applied to the new data
    db.query(BudgetDriver).filter(
        BudgetDriver.company_id == company_id,
        BudgetDriver.budget_year == budget_year,
    ).update({"is_dirty": True})
    db.commit()

    return {
        "status": "partial" if unavailable_sections else "ok",
        "lines_created": created,
        "vendor_lines_created": sub_counts["vendor"],
        "customer_lines_created": sub_counts["customer"],
        "unallocated_lines_created": sub_counts["unallocated"],
        "source_year": yr,
        "growth_pct": growth_pct,
        "errors": breakdown_errors,
        "data_quality": {
            "is_partial": bool(unavailable_sections),
            "unavailable_sections": unavailable_sections,
        },
    }


@router.get("/rolling-forecast")
def rolling_forecast(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """Blend actuals (locked months) with budget (unlocked months)."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    # Only parent-level budget lines (not vendor sub-lines)
    # PLAN-EXEMPT: batch-4 bug 4 — emit P&L statement order (account_type, sort_order,
    # account_name), matching every sibling budget query (:101/:431/:1157); without
    # the ORDER BY the reforecast renders in arbitrary PK order.
    budget_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.parent_line_id == None,  # noqa: E711
    ).order_by(
        BudgetLine.account_type, BudgetLine.sort_order, BudgetLine.account_name
    ).all()

    # Use locked months as the "actual" boundary — NOT calendar date
    locks = db.query(BudgetMonthLock).filter(
        BudgetMonthLock.company_id == company_id,
        BudgetMonthLock.budget_year == budget_year,
    ).all()
    closed_months = {lock.month for lock in locks}

    # Try to fetch YTD actuals
    forecast_partial = False  # 2A: surface a failed QBO actuals fetch, not silent zeros
    try:
        client = get_qbo_client_from_db(db, company_id)
        report = client.get_report("ProfitAndLoss", {
            "start_date": f"{budget_year}-01-01",
            "end_date": f"{budget_year}-12-31",
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        })
        actuals = _parse_pl_by_month(report)
    except Exception as e:
        logger.warning("[Budget] Rolling-forecast actuals fetch failed for %s: %s", budget_year, redact_to_string(str(e), max_length=500))
        forecast_partial = True
        actuals = {m: {} for m in MONTHS}

    result = []
    seen_accounts = set()
    for line in budget_lines:
        amounts = line.monthly_amounts or {}
        seen_accounts.add(line.account_name)
        monthly = []
        for i, m in enumerate(MONTHS):
            if m in closed_months:
                # Use actual for locked months, fall back to budget if no actual
                amt = actuals.get(m, {}).get(line.account_name, amounts.get(m, 0))
                monthly.append({"month": MONTH_LABELS[i], "amount": round(amt, 2), "is_actual": True,
                                "budget": round(amounts.get(m, 0), 2)})
            else:
                # Use budget for unlocked months
                monthly.append({"month": MONTH_LABELS[i], "amount": round(amounts.get(m, 0), 2), "is_actual": False,
                                "budget": round(amounts.get(m, 0), 2)})
        result.append({
            "account_name": line.account_name,
            "account_type": line.account_type,
            "monthly": monthly,
        })

    # Add accounts that have actuals but no budget line (show actuals, zero budget)
    all_actual_accounts: set = set()
    for m_data in actuals.values():
        all_actual_accounts.update(m_data.keys())
    for acct in sorted(all_actual_accounts - seen_accounts):
        monthly = []
        for i, m in enumerate(MONTHS):
            actual_amt = actuals.get(m, {}).get(acct, 0)
            if m in closed_months:
                monthly.append({"month": MONTH_LABELS[i], "amount": round(actual_amt, 2), "is_actual": True,
                                "budget": 0})
            else:
                monthly.append({"month": MONTH_LABELS[i], "amount": 0, "is_actual": False, "budget": 0})
        result.append({
            "account_name": acct,
            "account_type": "unknown",
            "monthly": monthly,
        })

    return {
        "budget_year": budget_year,
        "lines": result,
        "locked_months": sorted(closed_months, key=lambda m: MONTHS.index(m)),
        "data_quality": {
            "is_partial": forecast_partial,
            "unavailable_sections": ["budget_actuals_unavailable"] if forecast_partial else [],
        },
    }


@router.get("/export-xlsx")
def export_xlsx(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Export budget as multi-sheet Excel workbook."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    from fastapi.responses import StreamingResponse
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment  # noqa: F401
    except ImportError:
        raise HTTPException(500, "openpyxl not installed")

    wb = openpyxl.Workbook()

    # ── Sheet 1: Budget Grid ──
    ws = wb.active
    ws.title = "Budget"
    header_fill = PatternFill(start_color="1e3a5f", end_color="1e3a5f", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")

    headers = ["Account", "Type", "Department"] + MONTH_LABELS + ["Annual"]
    for col_i, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_i, value=h)
        cell.fill = header_fill
        cell.font = header_font

    lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).order_by(BudgetLine.account_type, BudgetLine.sort_order).all()

    for row_i, line in enumerate(lines, 2):
        amounts = line.monthly_amounts or {}
        # Label vendor/customer sub-lines distinctly ("↳ Account — Name (Vendor|Customer)" + indent)
        # so the export isn't a flat list of identical account names — mirrors the grid's indented
        # ↳ sub-line render. Parent total rows keep the bare account name. Amounts are unchanged.
        if getattr(line, "parent_line_id", None):
            _sub = line.vendor_name or line.customer_name or ""
            _tag = "Vendor" if line.vendor_name else ("Customer" if line.customer_name else "Sub-line")
            _label = f"↳ {line.account_name} — {_sub} ({_tag})" if _sub else f"↳ {line.account_name}"
            _name_cell = ws.cell(row=row_i, column=1, value=_label)
            _name_cell.alignment = Alignment(indent=2)
        else:
            ws.cell(row=row_i, column=1, value=line.account_name)
        ws.cell(row=row_i, column=2, value=line.account_type)
        ws.cell(row=row_i, column=3, value=getattr(line, "department", "") or "")
        annual = 0
        for m_i, m in enumerate(MONTHS):
            v = amounts.get(m, 0)
            ws.cell(row=row_i, column=4 + m_i, value=v)
            annual += v
        ws.cell(row=row_i, column=16, value=round(annual, 2))

    # ── Sheet 2: vs Actuals ──
    ws2 = wb.create_sheet("vs Actuals")
    for col_i, h in enumerate(["Account", "Type"] + MONTH_LABELS, 1):
        cell = ws2.cell(row=1, column=col_i, value=h)
        cell.fill = header_fill
        cell.font = header_font

    try:
        # Fetch full-year actuals for the export (all 12 months)
        from ..qbo_client import get_qbo_client_from_db as _get_client
        _client = _get_client(db, company_id)
        _report = _client.get_report("ProfitAndLoss", {
            "start_date": f"{budget_year}-01-01",
            "end_date": f"{budget_year}-12-31",
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        })
        _actuals_by_month = _parse_pl_by_month(_report)
        _export_lines = db.query(BudgetLine).filter(
            BudgetLine.company_id == company_id,
            BudgetLine.budget_year == budget_year,
            BudgetLine.parent_line_id == None,  # noqa: E711
        ).order_by(BudgetLine.account_type, BudgetLine.sort_order).all()
        row_i = 2
        for _line in _export_lines:
            amts = _line.monthly_amounts or {}
            for key in ("budget", "actual", "variance"):
                ws2.cell(row=row_i, column=1, value=f"{_line.account_name} ({key})")
                ws2.cell(row=row_i, column=2, value=_line.account_type)
                for m_i, m in enumerate(MONTHS):
                    b = amts.get(m, 0)
                    a = _actuals_by_month.get(m, {}).get(_line.account_name, 0)
                    if key == "budget":
                        ws2.cell(row=row_i, column=3 + m_i, value=b)
                    elif key == "actual":
                        ws2.cell(row=row_i, column=3 + m_i, value=a)
                    else:
                        ws2.cell(row=row_i, column=3 + m_i, value=round(a - b, 2))
                row_i += 1
    except Exception as e:
        # 2A: a failed actuals fetch must not leave a silently-empty BvA sheet —
        # write an explicit unavailable marker so the export is honest.
        logger.warning("[Budget] XLSX budget-vs-actuals actuals fetch failed for %s: %s", budget_year, redact_to_string(str(e), max_length=500))
        _unavail = ws2.cell(
            row=2,
            column=1,
            value="Actuals unavailable — could not load from QuickBooks for this period.",
        )
        _unavail.data_type = "s"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Budget_{budget_year}.xlsx"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Approval Workflow
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/submit")
def submit_budget(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).all()
    for line in lines:
        line.status = "submitted"
    db.commit()
    return {"status": "submitted", "lines_updated": len(lines)}


@router.post("/approve")
def approve_budget(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("MANAGER", company_id=company_id, db=db, current_user=current_user)
    lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).all()
    for line in lines:
        line.status = "approved"
    db.commit()
    return {"status": "approved", "lines_updated": len(lines)}


@router.post("/unlock")
def unlock_budget(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("MANAGER", company_id=company_id, db=db, current_user=current_user)
    lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).all()
    for line in lines:
        line.status = "draft"
    db.commit()
    return {"status": "draft", "lines_updated": len(lines)}


# ─────────────────────────────────────────────────────────────────────────────
# Monthly Locks
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/lock-month")
def lock_month(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    month: str = Query(..., description="Month key: jan, feb, ..."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("MANAGER", company_id=company_id, db=db, current_user=current_user)
    month = month.lower()
    if month not in MONTHS:
        raise HTTPException(status_code=400, detail=f"Invalid month: {month}")
    existing = db.query(BudgetMonthLock).filter(
        BudgetMonthLock.company_id == company_id,
        BudgetMonthLock.budget_year == budget_year,
        BudgetMonthLock.month == month,
    ).first()
    if existing:
        return {"status": "already_locked", "month": month}
    lock = BudgetMonthLock(company_id=company_id, budget_year=budget_year, month=month)
    db.add(lock)
    db.commit()
    return {"status": "locked", "month": month}


@router.post("/unlock-month")
def unlock_month(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    month: str = Query(..., description="Month key: jan, feb, ..."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("MANAGER", company_id=company_id, db=db, current_user=current_user)
    month = month.lower()
    lock = db.query(BudgetMonthLock).filter(
        BudgetMonthLock.company_id == company_id,
        BudgetMonthLock.budget_year == budget_year,
        BudgetMonthLock.month == month,
    ).first()
    if lock:
        db.delete(lock)
        db.commit()
    return {"status": "unlocked", "month": month}


@router.get("/locked-months")
def get_locked_months(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    locks = db.query(BudgetMonthLock).filter(
        BudgetMonthLock.company_id == company_id,
        BudgetMonthLock.budget_year == budget_year,
    ).all()
    budget_locked = [lk.month for lk in locks]
    # Reflect the accounting month-end close: a period locked in the accounting
    # calendar (FinancialCalendar.locked) shows as read-only in the budget grid too,
    # so it can be analysed for budget-vs-actual. Read-only reflection — this never
    # writes a BudgetMonthLock; the two sources are kept distinguishable so the UI
    # can label a period-close differently from a manual budget lock.
    period_closed = accounting_closed_month_keys(
        db, company_id=company_id, budget_year=budget_year
    )
    union = sorted(set(budget_locked) | set(period_closed), key=lambda m: MONTHS.index(m) if m in MONTHS else 99)
    return {
        "budget_year": budget_year,
        # Union of both sources — the full set of months that are read-only in the grid.
        "locked_months": union,
        # The two sources, split so the UI can show the right lock reason.
        "budget_locked_months": budget_locked,
        "period_closed_months": period_closed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Audit Trail
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/audit-log")
def get_audit_log(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    line_id: Optional[int] = Query(None, description="Filter by budget line ID"),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    q = db.query(BudgetAuditLog).filter(BudgetAuditLog.company_id == company_id)
    if line_id is not None:
        q = q.filter(BudgetAuditLog.budget_line_id == line_id)
    logs = q.order_by(BudgetAuditLog.changed_at.desc()).limit(limit).all()
    return {
        "total": len(logs),
        "entries": [
            {
                "id": log.id,
                "budget_line_id": log.budget_line_id,
                "field": log.field,
                "old_value": log.old_value,
                "new_value": log.new_value,
                "changed_by": log.changed_by,
                "changed_at": log.changed_at.isoformat() if log.changed_at else None,
            }
            for log in logs
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Drivers (Phase B)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/drivers")
def list_drivers(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    scenario: str = Query("base"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    drivers = db.query(BudgetDriver).filter(
        BudgetDriver.company_id == company_id,
        BudgetDriver.budget_year == budget_year,
        BudgetDriver.scenario == scenario,
    ).all()
    all_dicts = [_driver_to_dict(d) for d in drivers]
    pending_count = sum(1 for d in drivers if getattr(d, "is_dirty", True))
    return {
        "revenue_drivers": [d for d in all_dicts if d["driver_category"] == "revenue"],
        "expense_drivers": [d for d in all_dicts if d["driver_category"] != "revenue"],
        "methods": DRIVER_METHODS,
        "revenue_methods": {k: v for k, v in DRIVER_METHODS.items() if k in REVENUE_METHODS},
        "expense_methods": {k: v for k, v in DRIVER_METHODS.items() if k in EXPENSE_METHODS},
        "pending_count": pending_count,
    }


@router.post("/drivers")
def create_driver(
    body: DriverCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("EDITOR", company_id=body.company_id, db=db, current_user=current_user)
    d = BudgetDriver(
        company_id=body.company_id,
        budget_year=body.budget_year,
        account_name=body.account_name,
        qbo_account_id=body.qbo_account_id,
        driver_method=body.driver_method,
        driver_category=body.driver_category,
        vendor_name=body.vendor_name,
        customer_name=body.customer_name,
        driver_params=body.driver_params,
        scenario=body.scenario,
    )
    try:
        return create_budget_driver_service(db, d)
    except BudgetPlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/drivers/reset")
def reset_drivers(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    scenario: str = Query("base"),
    driver_category: Optional[str] = Query(None, description="Filter: 'revenue' or 'expense'. Omit to delete ALL drivers."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Delete budget drivers so they can be recreated cleanly via bulk-set-method.

    Use driver_category='revenue' to only wipe revenue drivers (and reseed
    with correct customer_name fields), or omit to wipe everything.
    """
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    q = db.query(BudgetDriver).filter(
        BudgetDriver.company_id == company_id,
        BudgetDriver.budget_year == budget_year,
        BudgetDriver.scenario == scenario,
    )
    if driver_category:
        q = q.filter(BudgetDriver.driver_category == driver_category)

    count = q.count()
    q.delete(synchronize_session="fetch")
    db.commit()

    return {
        "deleted": count,
        "company_id": company_id,
        "budget_year": budget_year,
        "scenario": scenario,
        "driver_category": driver_category or "all",
        "next_step": (
            f"POST /budget/drivers/bulk-set-method?company_id={company_id}"
            f"&budget_year={budget_year}&method=ttm_avg&only_unset=false"
            f"&driver_category={driver_category or 'revenue'}&auto_create=true"
        ),
    }


@router.post("/budget-lines/restore-originals")
def restore_original_amounts(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    account_type: Optional[str] = Query(None, description="Filter: 'revenue' or 'opex'"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Restore sub-line monthly_amounts from original_monthly_amounts.

    Use this after bad driver applications have corrupted sub-line amounts.
    Only affects sub-lines (parent_line_id is not None) that have
    original_monthly_amounts saved.
    """
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    q = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.parent_line_id != None,  # noqa: E711
        BudgetLine.original_monthly_amounts != None,  # noqa: E711
    )
    if account_type:
        if account_type == "revenue":
            q = q.filter(BudgetLine.account_type == "revenue")
        else:
            q = q.filter(BudgetLine.account_type.in_(["opex", "cogs"]))

    sub_lines = q.all()
    restored = 0
    for sl in sub_lines:
        if sl.original_monthly_amounts and sl.original_monthly_amounts != sl.monthly_amounts:
            sl.monthly_amounts = dict(sl.original_monthly_amounts)
            restored += 1

    if restored > 0:
        db.commit()

    return {
        "status": "ok",
        "restored": restored,
        "total_checked": len(sub_lines),
        "account_type_filter": account_type or "all",
    }




@router.post("/drivers/bulk-set-method")
def bulk_set_driver_method(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    method: str = Query("ttm_avg", description="Driver method to apply: ttm_avg, pct_revenue, fixed, yoy_growth, volume"),
    scenario: str = Query("base"),
    only_unset: bool = Query(True, description="If true, only update drivers without a method set"),
    driver_category: Optional[str] = Query(None, description="Filter to 'revenue' or 'expense' drivers only"),
    auto_create: bool = Query(True, description="If true, also create drivers for vendor sub-lines missing them"),
    growth_pct: Optional[float] = Query(None, description="Growth rate percentage for yoy_growth method (e.g. 3 for 3%)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Bulk-update all budget drivers to use the specified method.

    By default, only updates drivers that don't have a method set yet.
    Set only_unset=false to update ALL drivers (override existing methods).

    With auto_create=true (default), also creates BudgetDriver records
    for any vendor sub-lines in BudgetLine that don't have a corresponding
    driver yet.

    For yoy_growth method, pass growth_pct to set the growth percentage
    (e.g. growth_pct=3 for 3% YoY growth applied to prior year actuals).
    """
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    if method not in DRIVER_METHODS:
        raise HTTPException(status_code=400, detail=f"Invalid method: {method}. Valid: {list(DRIVER_METHODS.keys())}")
    # PLAN-EXEMPT: DI-MISUSE remediation (closed plan 20260706-widen-internal-db-approval-execution
    # §10 follow-up) — the Phases 1-4 business logic moved verbatim to
    # app/services/budget_drivers.py::bulk_set_driver_method_core so the assistant approve-path
    # can execute it without this route's Depends params. Route behavior is unchanged: same
    # access check, same invalid-method 400, same response payload.
    return bulk_set_driver_method_core(
        db,
        company_id=company_id,
        budget_year=budget_year,
        method=method,
        scenario=scenario,
        only_unset=only_unset,
        driver_category=driver_category,
        auto_create=auto_create,
        growth_pct=growth_pct,
    )


@router.get("/drivers/revenue-services")
def get_revenue_services(
    company_id: str = Query(...),
    source_year: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Fetch revenue by service/item from QBO for revenue driver setup."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    # source_year is user-controlled; validate it to a bounded 4-digit int so it
    # cannot smuggle anything into the date literals (QBO has no bind params).
    yr = qbo_int(source_year or (date.today().year - 1), min=1900, max=9999)
    try:
        client = get_qbo_client_from_db(db, company_id)
        # Query invoices to get revenue by item/service
        data = client.query(
            f"SELECT * FROM Invoice WHERE TxnDate >= '{yr}-01-01' "
            f"AND TxnDate <= '{yr}-12-31' STARTPOSITION 1 MAXRESULTS 1000"
        )
        month_map = {1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
                     7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec"}
        services: Dict[str, Dict[str, float]] = {}
        invoices = data.get("QueryResponse", {}).get("Invoice", [])
        for inv in invoices:
            txn_date = inv.get("TxnDate", "")
            if not txn_date or len(txn_date) < 7:
                continue
            try:
                m_key = month_map.get(int(txn_date[5:7]), "")
            except (ValueError, IndexError):
                continue
            if not m_key:
                continue
            for line in inv.get("Line", []):
                detail = line.get("SalesItemLineDetail", {})
                item_ref = detail.get("ItemRef", {})
                item_name = item_ref.get("name", "").strip()
                if not item_name:
                    continue
                amt = abs(float(line.get("Amount", 0) or 0))
                qty = float(detail.get("Qty", 0) or 0)  # noqa: F841
                if item_name not in services:
                    services[item_name] = {m: 0.0 for m in MONTHS}
                services[item_name][m_key] += amt

        return {
            "source_year": yr,
            "services": [
                {"name": name, "monthly": monthly, "annual_total": round(sum(monthly.values()), 2)}
                for name, monthly in sorted(services.items(), key=lambda x: sum(x[1].values()), reverse=True)
            ],
        }
    except Exception as e:
        raise user_safe_http_exception(
            status_code=502,
            error_code="budget_revenue_services_fetch_failed",
            user_message="Revenue service detail could not be loaded from QuickBooks.",
            exc=e,
            retryable=True,
            log_context="budget_revenue_services_fetch_failed",
        )


@router.get("/drivers/vendor-summary")
def get_vendor_summary(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    scenario: str = Query("base"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Return vendor-level expense data for the vendor-driven expense drivers table.

    Aggregates vendor sub-lines from the budget and computes:
    - TTM average per month
    - TTM % of total revenue
    - Existing driver method (if any)
    """
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    # ── TTM data: use PRIOR YEAR actuals (budget_year - 1) to avoid circular
    #    reference when drivers have been applied to current-year budget lines.
    ttm_year = budget_year - 1

    # Get vendor sub-lines from the PRIOR YEAR for TTM calculations
    vendor_lines_ttm = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == ttm_year,
        BudgetLine.parent_line_id != None,  # noqa: E711
        BudgetLine.vendor_name != None,  # noqa: E711
        BudgetLine.vendor_name != "",
    ).all()

    # Also get current-year vendor sub-lines for listing purposes (account mapping)
    vendor_lines_current = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.parent_line_id != None,  # noqa: E711
        BudgetLine.vendor_name != None,  # noqa: E711
        BudgetLine.vendor_name != "",
    ).all()

    # Build TTM lookup from prior year: vendor_key -> monthly amounts
    # Key by vendor_name|account_name to handle same vendor in multiple COAs
    # Use original_monthly_amounts if available (preserves QBO actuals before drivers overwrote)
    ttm_by_key: Dict[str, Dict[str, float]] = {}
    for vl in vendor_lines_ttm:
        vkey = f"{vl.vendor_name}|{vl.account_name}"
        monthly = vl.original_monthly_amounts or vl.monthly_amounts or {}
        if vkey not in ttm_by_key:
            ttm_by_key[vkey] = {m: 0.0 for m in MONTHS}
        for m in MONTHS:
            ttm_by_key[vkey][m] += float(monthly.get(m, 0))

    # Use current-year vendor sub-lines if no prior year data exists
    # (fallback for first-year budgets)
    if not vendor_lines_ttm:
        for vl in vendor_lines_current:
            vkey = f"{vl.vendor_name}|{vl.account_name}"
            monthly = vl.original_monthly_amounts or vl.monthly_amounts or {}
            if vkey not in ttm_by_key:
                ttm_by_key[vkey] = {m: 0.0 for m in MONTHS}
            for m in MONTHS:
                ttm_by_key[vkey][m] += float(monthly.get(m, 0))

    # Get total revenue from PRIOR YEAR for % of revenue calculations
    rev_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == ttm_year,
        BudgetLine.account_type == "revenue",
        BudgetLine.parent_line_id == None,  # noqa: E711
    ).all()
    # Fallback to current year revenue if no prior year exists
    if not rev_lines:
        rev_lines = db.query(BudgetLine).filter(
            BudgetLine.company_id == company_id,
            BudgetLine.budget_year == budget_year,
            BudgetLine.account_type == "revenue",
            BudgetLine.parent_line_id == None,  # noqa: E711
        ).all()
    total_revenue = 0.0
    for rl in rev_lines:
        monthly = rl.monthly_amounts or {}
        total_revenue += sum(float(monthly.get(m, 0)) for m in MONTHS)

    # Get existing expense drivers keyed by vendor_name|account_name
    existing_drivers = db.query(BudgetDriver).filter(
        BudgetDriver.company_id == company_id,
        BudgetDriver.budget_year == budget_year,
        BudgetDriver.scenario == scenario,
    ).all()
    driver_by_key: Dict[str, Any] = {}
    for drv in existing_drivers:
        vn = getattr(drv, "vendor_name", None)
        an = getattr(drv, "account_name", None)
        cat = getattr(drv, "driver_category", "expense")
        if vn and cat != "revenue":
            dkey = f"{vn}|{an}" if an else vn
            driver_by_key[dkey] = _driver_to_dict(drv)

    # Build vendor rows — use current-year sub-lines for listing,
    # but TTM data from prior year, keyed by vendor|account
    vendors = []
    for vl in vendor_lines_current:
        vkey = f"{vl.vendor_name}|{vl.account_name}"
        ttm_monthly = ttm_by_key.get(vkey, {})
        amounts = [ttm_monthly.get(m, 0) for m in MONTHS]
        ttm_total = sum(amounts)
        # TTM average = total / 12 (full year, not just non-zero months)
        ttm_avg = round(ttm_total / 12, 2)
        ttm_pct_rev = round((ttm_total / total_revenue * 100), 2) if total_revenue else 0.0

        existing = driver_by_key.get(vkey)

        vendors.append({
            "vendor_key": vkey,
            "vendor_name": vl.vendor_name,
            "account_name": vl.account_name,
            "account_type": vl.account_type,
            "budget_line_id": vl.id,
            "parent_line_id": vl.parent_line_id,
            "monthly_amounts": {m: ttm_monthly.get(m, 0) for m in MONTHS},
            "ttm_total": round(ttm_total, 2),
            "ttm_avg_monthly": ttm_avg,
            "ttm_pct_revenue": ttm_pct_rev,
            "existing_driver": existing,
        })

    # Sort by TTM total descending
    vendors.sort(key=lambda x: x["ttm_total"], reverse=True)

    return {
        "vendors": vendors,
        "total_revenue": round(total_revenue, 2),
        "vendor_count": len(vendors),
        "ttm_source_year": ttm_year if vendor_lines_ttm else budget_year,
        "prior_year_lines": len(vendor_lines_ttm),
        "current_year_lines": len(vendor_lines_current),
    }


@router.get("/drivers/customer-summary")
def get_customer_summary(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    scenario: str = Query("base"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Return customer-level revenue data for the customer-driven revenue drivers table.

    Aggregates customer sub-lines from the budget and computes:
    - TTM average per month
    - TTM % of total revenue
    - Existing driver method (if any)
    """
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    ttm_year = budget_year - 1

    # Get total revenue for % calculation
    revenue_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.account_type == "revenue",
        BudgetLine.parent_line_id == None,  # noqa: E711
        (BudgetLine.scenario == scenario) | (BudgetLine.scenario == None),  # noqa: E711
    ).all()
    total_revenue = sum(
        sum((ln.monthly_amounts or {}).get(m, 0) for m in MONTHS) for ln in revenue_lines
    )

    # Get customer sub-lines from prior year for TTM
    customer_lines_ttm = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == ttm_year,
        BudgetLine.parent_line_id != None,  # noqa: E711
        BudgetLine.customer_name != None,  # noqa: E711
        BudgetLine.customer_name != "",
    ).all()

    # Get customer sub-lines from current year
    customer_lines_current = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.parent_line_id != None,  # noqa: E711
        BudgetLine.customer_name != None,  # noqa: E711
        BudgetLine.customer_name != "",
        (BudgetLine.scenario == scenario) | (BudgetLine.scenario == None),  # noqa: E711
    ).all()

    # Build lookup: customer_key -> TTM monthly data
    # Use original_monthly_amounts to avoid circular reference from driver overwrites
    ttm_lookup: Dict[str, Dict[str, float]] = {}
    for cl in customer_lines_ttm:
        key = f"{cl.customer_name}|{cl.account_name}"
        monthly = cl.original_monthly_amounts or cl.monthly_amounts or {}
        if key not in ttm_lookup:
            ttm_lookup[key] = {m: 0.0 for m in MONTHS}
        for m in MONTHS:
            ttm_lookup[key][m] += float(monthly.get(m, 0))

    # If no prior year data, use current year sub-lines as TTM
    if not ttm_lookup:
        for cl in customer_lines_current:
            key = f"{cl.customer_name}|{cl.account_name}"
            monthly = cl.original_monthly_amounts or cl.monthly_amounts or {}
            if key not in ttm_lookup:
                ttm_lookup[key] = {m: 0.0 for m in MONTHS}
            for m in MONTHS:
                ttm_lookup[key][m] += float(monthly.get(m, 0))

    # Get existing drivers for matching
    existing_drivers = db.query(BudgetDriver).filter(
        BudgetDriver.company_id == company_id,
        BudgetDriver.budget_year == budget_year,
        BudgetDriver.scenario == scenario,
        BudgetDriver.driver_category == "revenue",
        BudgetDriver.customer_name != None,  # noqa: E711
    ).all()
    driver_map = {}
    for d in existing_drivers:
        key = f"{d.customer_name}|{d.account_name}"
        driver_map[key] = d

    # Build account_name -> qbo_account_id lookup from parent revenue lines
    acct_id_map: Dict[str, str] = {}
    for rl in revenue_lines:
        if rl.qbo_account_id:
            acct_id_map[rl.account_name] = rl.qbo_account_id

    # Build output
    customers = []
    seen = set()
    for cl in customer_lines_current:
        key = f"{cl.customer_name}|{cl.account_name}"
        if key in seen:
            continue
        seen.add(key)

        ttm_data = ttm_lookup.get(key, {m: 0.0 for m in MONTHS})
        ttm_total = sum(ttm_data.values())
        ttm_avg = round(ttm_total / 12, 2)
        pct_rev = round((ttm_total / total_revenue * 100) if total_revenue else 0, 2)

        drv = driver_map.get(key)
        customers.append({
            "customer_key": key,
            "customer_name": cl.customer_name,
            "account_name": cl.account_name,
            "qbo_account_id": acct_id_map.get(cl.account_name, ""),
            "ttm_monthly": {m: round(ttm_data.get(m, 0), 2) for m in MONTHS},
            "ttm_total": round(ttm_total, 2),
            "ttm_avg": ttm_avg,
            "pct_revenue": pct_rev,
            "has_driver": drv is not None,
            "driver_id": drv.id if drv else None,
            "driver_method": drv.driver_method if drv else None,
            "driver_params": _safe_json(drv.driver_params) if drv else {},
        })

    # Sort by TTM total descending
    customers.sort(key=lambda c: c["ttm_total"], reverse=True)

    return {
        "customers": customers,
        "total_revenue": round(total_revenue, 2),
        "customer_count": len(customers),
    }


@router.get("/debug-vendor-ttm")
def debug_vendor_ttm(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    account_filter: str = Query("", description="Filter by account name substring"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
    _debug_access = Depends(require_debug_endpoint_access),
):
    """Debug endpoint: show raw vendor sub-line data for both years."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    ttm_year = budget_year - 1
    result = {"prior_year": ttm_year, "current_year": budget_year, "prior": [], "current": []}

    for yr_label, yr in [("prior", ttm_year), ("current", budget_year)]:
        lines = db.query(BudgetLine).filter(
            BudgetLine.company_id == company_id,
            BudgetLine.budget_year == yr,
            BudgetLine.parent_line_id != None,  # noqa: E711
            BudgetLine.vendor_name != None,  # noqa: E711
            BudgetLine.vendor_name != "",
        ).all()
        for vl in lines:
            if account_filter and account_filter.lower() not in (vl.account_name or "").lower():
                continue
            monthly = vl.monthly_amounts or {}
            total = sum(float(monthly.get(m, 0)) for m in MONTHS)
            result[yr_label].append({
                "vendor_key": f"{vl.vendor_name}|{vl.account_name}",
                "vendor_name": vl.vendor_name,
                "account_name": vl.account_name,
                "line_id": vl.id,
                "parent_line_id": vl.parent_line_id,
                "annual_total": round(total, 2),
                "avg_monthly": round(total / 12, 2),
                "monthly": {m: float(monthly.get(m, 0)) for m in MONTHS},
            })

    return result



@router.put("/drivers/{driver_id}")
def update_driver(
    driver_id: int,
    body: DriverUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    d = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetDriver,
        entity_id=driver_id,
        not_found_detail="Driver not found",
    )
    # Thin router: the field-write + recompute + serialize body lives in the driver-logic service
    # (behavior-identical; callable standalone by the agent approve-path). The tenant/role guard
    # above stays at the edge; the DTO is unpacked here so the service takes plain data.
    try:
        return update_budget_driver_service(db, d, body.model_dump(exclude_none=True))
    except BudgetPlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/drivers/{driver_id}")
def delete_driver(
    driver_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    d = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetDriver,
        entity_id=driver_id,
        not_found_detail="Driver not found",
    )
    db.delete(d)
    db.commit()
    return {"status": "deleted"}


@router.post("/apply-drivers")
def apply_drivers_to_budget(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    scenario: str = Query("base"),
    force: bool = Query(False, description="Force-apply all drivers regardless of dirty status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Apply all driver effective values to their corresponding budget lines.
    
    For expense drivers with a vendor_name, updates the vendor sub-line.
    Then re-aggregates parent lines from their children.
    For revenue drivers, updates the parent line directly.
    Set force=true to apply ALL drivers (e.g. after re-seed).
    """
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    # Thin router: the bulk apply+re-aggregate lives in the reforecast service (behavior-identical;
    # callable standalone by the agent approve-path, which needs the before/after for a proposal
    # payload + golden diff — row 13 is a HIGH-risk bulk overwrite). The guard above stays at the
    # edge. The HTTP response shape is unchanged: on no_drivers, {status, applied}; otherwise
    # {status, applied, parents_updated} — the before/after ride the service result only.
    try:
        result = apply_budget_drivers_service(db, company_id=company_id, budget_year=budget_year,
                                             scenario=scenario, force=force)
    except BudgetPlanError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {key: result[key] for key in ("status", "applied", "parents_updated", "excluded_locked_months")}


@router.put("/drivers/{driver_id}/cell")
def update_driver_cell(
    driver_id: int,
    month: str = Query(..., description="Month key: jan, feb, ..."),
    value: float = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Override a single month cell value for a driver."""
    d = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetDriver,
        entity_id=driver_id,
        not_found_detail="Driver not found",
    )
    if month not in MONTHS:
        raise HTTPException(status_code=400, detail=f"Invalid month: {month}")
    from ..budget.driver_calculation import cents
    try:
        value = cents(value)
    except BudgetPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lock_budget_write_scope(db, company_id=d.company_id)
    db.refresh(d)
    overrides = dict(d.overrides or {})
    overrides[month] = value
    d.overrides = overrides
    d.is_dirty = True
    db.commit()
    db.refresh(d)
    return _driver_to_dict(d)


@router.post("/drivers/{driver_id}/compute")
def compute_driver(
    driver_id: int,
    apply_to_budget: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Compute monthly values based on driver method, optionally apply to budget line."""
    d = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=BudgetDriver,
        entity_id=driver_id,
        not_found_detail="Driver not found",
    )

    try:
        return compute_budget_driver_service(db, d, apply_to_budget=apply_to_budget)
    except BudgetPlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ─────────────────────────────────────────────────────────────────────────────
# Scenario Planning (Phase B)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/clone-scenario")
def clone_scenario(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    source_scenario: str = Query("base"),
    target_scenario: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    # Delete existing target scenario lines
    db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.scenario == target_scenario,
    ).delete()
    # Copy source lines
    source_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        (BudgetLine.scenario == source_scenario) | (BudgetLine.scenario == None),  # noqa: E711
    ).all()
    count = 0
    for line in source_lines:
        new_line = BudgetLine(
            company_id=line.company_id,
            budget_year=line.budget_year,
            account_name=line.account_name,
            account_type=line.account_type,
            qbo_account_id=line.qbo_account_id,
            monthly_amounts=dict(line.monthly_amounts or {}),
            sort_order=line.sort_order,
            notes=line.notes,
            department=getattr(line, 'department', None),
            status="draft",
            scenario=target_scenario,
            driver_formula=getattr(line, 'driver_formula', None),
        )
        db.add(new_line)
        count += 1
    # Clone drivers too
    source_drivers = db.query(BudgetDriver).filter(
        BudgetDriver.company_id == company_id,
        BudgetDriver.budget_year == budget_year,
        BudgetDriver.scenario == source_scenario,
    ).all()
    for d in source_drivers:
        new_d = BudgetDriver(
            company_id=d.company_id,
            budget_year=d.budget_year,
            driver_name=d.driver_name,
            driver_type=d.driver_type,
            monthly_values=dict(d.monthly_values or {}),
            scenario=target_scenario,
        )
        db.add(new_d)
    db.commit()
    return {"status": "cloned", "target_scenario": target_scenario, "lines_cloned": count}


@router.get("/scenarios")
def list_scenarios(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """List all distinct scenarios that exist for this company/year."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    lines = db.query(BudgetLine.scenario).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).distinct().all()
    scenarios = sorted(set(s[0] or "base" for s in lines))
    return {"scenarios": scenarios}


# PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-6B (#9 / F-11 / S-5) — real scenario comparison.
def _ytd_month_keys(budget_year: int, today: "date | None" = None) -> list[str]:
    """The fully-closed months of ``budget_year`` that YTD actuals can cover.

    A past year → all 12 months; the current year → jan..(the month before today's,
    i.e. only fully-elapsed months — never the in-progress month); a future year →
    no months (there are no actuals to compare against yet).
    """
    today = today or date.today()
    if budget_year < today.year:
        return list(MONTHS)
    if budget_year > today.year:
        return []
    return list(MONTHS[: today.month - 1])


# TENANT-OK: _require_budget_access IS check_tenant_access (app/budget/permissions.py) — the in-handler tenant gate is the first line of the body, same as every budget-router handler.
@router.get("/scenarios/compare", dependencies=[Depends(global_tenant_guard)])
def compare_scenarios(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    scenarios: str | None = Query(
        None,
        description="Comma-separated scenario names to compare; default = every scenario that exists for the year.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> dict[str, Any]:  # CRAFT-OK: dynamic grouped-report payload, the prevailing budget-router response style
    """Side-by-side scenario comparison vs YTD actuals (W-6B #9 / F-11 / S-5).

    For each requested scenario: the YTD budget per top-level account (children rolled
    up via ``_rollup_budget_lines`` semantics), the annual budget, and the $ / % variance
    vs QBO YTD actuals (variance = actual − budget, the /vs-actuals sign convention;
    variance_pct = variance / budget, None when budget is 0). Each row carries a
    ``closest_scenario`` flag (min |variance|). Rows are grouped Revenue / COGS / OpEx
    (+ other income/expense) and ordered by the budget's COA order — ``sort_order`` then
    ``account_name`` — never dict-arbitrary (the S-5 fix). Totals add Gross Profit and
    EBITDA = (revenue − cogs) − opex, the same COGS-inclusive math as ``/vs-actuals``.

    Honest degradation: a failed QBO actuals fetch returns ``actuals_available=false``
    with None actual/variance/closest fields (flagged in ``data_quality``) — never
    silent zeros. A future budget_year has no closed months → no actuals lens at all.
    """
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)

    all_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).order_by(BudgetLine.account_type, BudgetLine.sort_order, BudgetLine.account_name).all()

    existing = sorted({(line.scenario or "base") for line in all_lines})
    if scenarios is not None:
        requested = [s.strip() for s in scenarios.split(",") if s.strip()]
        unknown = sorted(set(requested) - set(existing))
        if unknown:
            raise user_safe_http_exception(
                status_code=400,
                error_code="budget_scenario_unknown",
                user_message=f"Unknown scenario(s) for {budget_year}: {', '.join(unknown)}. Existing: {', '.join(existing) or '(none)'}",
            )
        scenario_names = list(dict.fromkeys(requested))  # de-dupe, preserve request order
    else:
        # base first (the anchor plan), then the rest alphabetically.
        scenario_names = [s for s in existing if s == "base"] + [s for s in existing if s != "base"]

    # Per-scenario parent lines + children maps (rollup identical to /vs-actuals).
    scen_parents: dict[str, dict[tuple[str, str], BudgetLine]] = {}
    scen_children: dict[str, dict[int, list[BudgetLine]]] = {}
    for s in scenario_names:
        lines_s = [line for line in all_lines if (line.scenario or "base") == s]
        children: dict[int, list[BudgetLine]] = defaultdict(list)
        for line in lines_s:
            if line.parent_line_id is not None:
                children[line.parent_line_id].append(line)
        scen_children[s] = children
        scen_parents[s] = {
            ((line.account_type or "opex"), line.account_name): line
            for line in lines_s if line.parent_line_id is None
        }

    ytd_months = _ytd_month_keys(budget_year)

    # YTD actuals from ONE full-year monthly P&L (the reforecast-comparison fetch shape),
    # summed over the closed months only.
    actuals_available = bool(ytd_months)
    actuals_failed = False
    ytd_actuals: dict[str, float] = {}
    if ytd_months:
        try:
            client = get_qbo_client_from_db(db, company_id)
            report = client.get_report("ProfitAndLoss", {
                "start_date": date(budget_year, 1, 1).isoformat(),
                "end_date": date(budget_year, 12, 31).isoformat(),
                "summarize_column_by": "Month",
                "accounting_method": "Accrual",
            })
            by_month = _parse_pl_by_month(report)
            for m in ytd_months:
                for name, amt in (by_month.get(m) or {}).items():  # SA-OK: bounded-loop 12 — dict .get on the parsed QBO report, not a DB query
                    ytd_actuals[name] = ytd_actuals.get(name, 0.0) + float(amt or 0)  # SA-OK: bounded-loop 12 — plain dict .get accumulation, not a DB query
        except Exception as e:
            logger.warning(
                "[Budget] Scenario-compare actuals fetch failed for %s: %s",
                budget_year, redact_to_string(str(e), max_length=500),
            )
            actuals_available = False
            actuals_failed = True
            ytd_actuals = {}

    # Union of row identities across scenarios, first-seen order (query is already
    # (account_type, sort_order, account_name)-ordered, so first-seen = COA order).
    row_keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for s in scenario_names:
        for key in scen_parents[s]:
            if key not in seen:
                seen.add(key)
                row_keys.append(key)

    def _variance_fields(budget_amt: float | None, actual_amt: float | None) -> dict[str, float | None]:
        if budget_amt is None or actual_amt is None:
            return {"variance": None, "variance_pct": None}
        variance = round(actual_amt - budget_amt, 2)
        variance_pct = round(variance / budget_amt * 100, 1) if budget_amt else None
        return {"variance": variance, "variance_pct": variance_pct}

    def _closest(scenario_cells: dict[str, dict[str, float | None]]) -> str | None:
        best: str | None = None
        best_abs: float | None = None
        for s in scenario_names:  # scenario order breaks ties deterministically
            cell = scenario_cells.get(s)  # SA-OK: bounded-loop — dict .get over the handful of scenario names, not a DB query
            if not cell or cell.get("variance") is None:
                continue
            abs_var = abs(cell["variance"])
            if best_abs is None or abs_var < best_abs:
                best, best_abs = s, abs_var
        return best

    groups: dict[str, list[dict[str, Any]]] = {  # CRAFT-OK: dynamic grouped-report rows, prevailing budget-router style
        "revenue": [], "cogs": [], "opex": [], "other_income": [], "other_expense": [],
    }
    # Per-group per-scenario running totals (+ actual), summed over rows.
    group_totals: dict[str, dict[str, Any]] = {  # CRAFT-OK: dynamic accumulator mirroring the response payload shape
        g: {"actual_ytd": 0.0, "scenarios": {s: {"budget_ytd": 0.0, "annual": 0.0} for s in scenario_names}}
        for g in groups
    }

    for acct_type, account_name in row_keys:
        group = acct_type if acct_type in groups else "opex"
        anchor_scenario = next(s for s in scenario_names if (acct_type, account_name) in scen_parents[s])
        anchor_line = scen_parents[anchor_scenario][(acct_type, account_name)]

        actual_ytd: float | None = None
        if actuals_available:
            actual_ytd = _aggregate_actual_amount(anchor_line, ytd_actuals, scen_children[anchor_scenario])
            group_totals[group]["actual_ytd"] += actual_ytd

        scenario_cells: dict[str, dict[str, float | None]] = {}
        for s in scenario_names:
            line = scen_parents[s].get((acct_type, account_name))  # SA-OK: bounded-loop — dict .get on the prebuilt in-memory parent map, not a DB query
            if line is None:
                # The account does not exist in this scenario — absent, not a fabricated 0.
                scenario_cells[s] = {"budget_ytd": None, "annual": None, "variance": None, "variance_pct": None}
                continue
            budget_ytd = round(sum(
                _aggregate_budget_line_amount(line, m, scen_children[s]) for m in ytd_months
            ), 2)
            annual = round(sum(
                _aggregate_budget_line_amount(line, m, scen_children[s]) for m in MONTHS
            ), 2)
            cell: dict[str, float | None] = {"budget_ytd": budget_ytd, "annual": annual}
            cell.update(_variance_fields(budget_ytd, actual_ytd))
            scenario_cells[s] = cell
            group_totals[group]["scenarios"][s]["budget_ytd"] += budget_ytd
            group_totals[group]["scenarios"][s]["annual"] += annual

        groups[group].append({
            "account_name": account_name,
            "account_type": acct_type,
            "actual_ytd": actual_ytd,
            "scenarios": scenario_cells,
            "closest_scenario": _closest(scenario_cells),
        })

    def _totals_row(actual_amt: float | None, per_scenario: dict[str, dict[str, float]]) -> dict[str, Any]:  # CRAFT-OK: dynamic totals-row payload, prevailing budget-router style
        cells: dict[str, dict[str, float | None]] = {}
        for s in scenario_names:
            budget_ytd = round(per_scenario[s]["budget_ytd"], 2)
            annual = round(per_scenario[s]["annual"], 2)
            cell: dict[str, float | None] = {"budget_ytd": budget_ytd, "annual": annual}
            cell.update(_variance_fields(budget_ytd, actual_amt))
            cells[s] = cell
        return {
            "actual_ytd": round(actual_amt, 2) if actual_amt is not None else None,
            "scenarios": cells,
            "closest_scenario": _closest(cells),
        }

    def _group_actual(g: str) -> float | None:
        return group_totals[g]["actual_ytd"] if actuals_available else None

    # Gross Profit = revenue − cogs; EBITDA = gross_profit − opex (the /vs-actuals convention).
    gp_scen = {
        s: {
            "budget_ytd": group_totals["revenue"]["scenarios"][s]["budget_ytd"] - group_totals["cogs"]["scenarios"][s]["budget_ytd"],
            "annual": group_totals["revenue"]["scenarios"][s]["annual"] - group_totals["cogs"]["scenarios"][s]["annual"],
        }
        for s in scenario_names
    }
    gp_actual: float | None = None
    if actuals_available:
        gp_actual = (_group_actual("revenue") or 0.0) - (_group_actual("cogs") or 0.0)
    ebitda_scen = {
        s: {
            "budget_ytd": gp_scen[s]["budget_ytd"] - group_totals["opex"]["scenarios"][s]["budget_ytd"],
            "annual": gp_scen[s]["annual"] - group_totals["opex"]["scenarios"][s]["annual"],
        }
        for s in scenario_names
    }
    ebitda_actual: float | None = None
    if actuals_available:
        ebitda_actual = (gp_actual or 0.0) - (_group_actual("opex") or 0.0)

    totals = {
        "revenue": _totals_row(_group_actual("revenue"), group_totals["revenue"]["scenarios"]),
        "cogs": _totals_row(_group_actual("cogs"), group_totals["cogs"]["scenarios"]),
        "gross_profit": _totals_row(gp_actual, gp_scen),
        "opex": _totals_row(_group_actual("opex"), group_totals["opex"]["scenarios"]),
        "ebitda": _totals_row(ebitda_actual, ebitda_scen),
    }

    return {
        "budget_year": budget_year,
        "scenarios": scenario_names,
        "ytd_months": ytd_months,
        "actuals_available": actuals_available,
        "groups": groups,
        "totals": totals,
        # The headline flag: which scenario tracks reality closest, judged on YTD EBITDA.
        "closest_overall": {
            "scenario": totals["ebitda"]["closest_scenario"],
            "basis": "ebitda_ytd",
        },
        "data_quality": {
            "is_partial": actuals_failed,
            "unavailable_sections": ["budget_actuals_unavailable"] if actuals_failed else [],
        },
    }


@router.get("/scenario-comparison")
def scenario_comparison(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Side-by-side comparison of all scenarios."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    all_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
    ).all()
    # Group by account_name
    by_account: Dict[str, Dict[str, Dict[str, float]]] = {}
    for line in all_lines:
        scenario = getattr(line, 'scenario', None) or 'base'
        name = line.account_name
        amounts = line.monthly_amounts or {}
        if name not in by_account:
            by_account[name] = {}
        by_account[name][scenario] = {m: amounts.get(m, 0) for m in MONTHS}
    result = []
    for name, scenarios in by_account.items():
        row = {"account_name": name, "scenarios": {}}
        for s, vals in scenarios.items():
            row["scenarios"][s] = {
                "monthly": vals,
                "annual": sum(vals.values()),
            }
        result.append(row)
    return {"budget_year": budget_year, "lines": result}


# ─────────────────────────────────────────────────────────────────────────────
# Reforecasting (Phase B)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/create-reforecast")
def create_reforecast(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """
    Reforecast: update the base budget's locked (closed) months with actuals.
    Only months that have been explicitly locked are considered closed.
    Unlocked months keep their budgeted amounts — no partial month data.
    """
    _require_budget_access("EDITOR", company_id=company_id, db=db, current_user=current_user)
    # Thin router: the clone-only reforecast workflow (locked-month actuals overwrite + per-cell
    # audit rows, base UNTOUCHED) lives in the reforecast service — behavior-identical, callable
    # standalone by the agent approve-path. The guard above stays at the edge; the service raises
    # ValueError for the no-locked-months case, which the router maps to its existing 400.
    try:
        return create_reforecast_service(db, company_id=company_id, budget_year=budget_year)
    except BudgetPlanError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/reforecast-comparison")
def reforecast_comparison(
    company_id: str = Query(...),
    budget_year: int = Query(date.today().year),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """3-column comparison: Original Budget | Reforecast | Actuals."""
    _require_budget_access("VIEWER", company_id=company_id, db=db, current_user=current_user)
    base_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.parent_line_id.is_(None),
        (BudgetLine.scenario == "base") | (BudgetLine.scenario == None),  # noqa: E711
    ).all()
    reforecast_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.parent_line_id.is_(None),
        BudgetLine.scenario == "reforecast",
    ).all()

    actuals_available = True
    try:
        client = get_qbo_client_from_db(db, company_id)
        start = date(budget_year, 1, 1)
        end = date(budget_year, 12, 31)
        report = client.get_report("ProfitAndLoss", {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        })
        from ..budget.qbo_reports import parse_reforecast_pl_actuals
        actuals = parse_reforecast_pl_actuals(report, budget_year=budget_year, months=MONTHS)
    except Exception:
        actuals_available = False
        actuals = {m: [] for m in MONTHS}

    def identity(line):
        return ("id", line.qbo_account_id) if line.qbo_account_id else ("name", line.account_name)
    if len({identity(line) for line in base_lines}) != len(base_lines) or len({identity(line) for line in reforecast_lines}) != len(reforecast_lines):
        raise HTTPException(status_code=409, detail="Duplicate budget account identity; resolve records before comparing.")
    reforecast_by_name = {identity(line): line for line in reforecast_lines}
    result = []
    for line in base_lines:
        name = line.account_name
        budget_amts = line.monthly_amounts or {}
        rf_line = reforecast_by_name.get(identity(line))
        rf_amts = rf_line.monthly_amounts if rf_line else budget_amts
        monthly = []
        for i, m in enumerate(MONTHS):
            matches = [entry for entry in actuals.get(m, []) if (
                str(entry.get("qbo_account_id")) == str(line.qbo_account_id)
                if line.qbo_account_id else entry["account_name"] == name
            )]
            actual = matches[0]["amount"] if len(matches) == 1 else None
            if actual is None:
                actuals_available = False
            monthly.append({
                "month": MONTH_LABELS[i],
                "budget": budget_amts.get(m, 0),
                "reforecast": (rf_amts or {}).get(m, 0),
                "actual": actual,
            })
        result.append({
            "account_name": name,
            "account_type": line.account_type,
            "monthly": monthly,
        })

    return {"budget_year": budget_year, "lines": result, "data_quality": {"is_partial": not actuals_available, "message": None if actuals_available else "Some actuals are unavailable or ambiguous; they are not shown as zero."}}

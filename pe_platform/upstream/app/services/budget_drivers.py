"""Explicit budget driver editing, pure calculation, and controlled application.

Callers own authentication/role checks. All mutations and calculations for one
request succeed together or roll back, including newly created drivers.
"""
from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import or_

from ..budget.driver_calculation import BudgetPlanError
from ..budget.serializers import DRIVER_METHODS, _driver_to_dict
from ..models import BudgetDriver, BudgetLine
from .budget_reforecast import (
    ReviewedBudgetPlan, _scenario_clause, apply_reviewed_budget_driver_plan,
    lock_budget_write_scope, plan_budget_drivers,
)


def _calculate_and_apply(db: Session, driver: BudgetDriver, *, apply_to_budget: bool) -> dict:
    # Editing a driver with no mapped line may store a calculation, but cannot
    # invent a budget target. A standalone compute is explicitly not an apply.
    has_lines = db.query(BudgetLine.id).filter(
        BudgetLine.company_id == driver.company_id,
        BudgetLine.budget_year == driver.budget_year,
        _scenario_clause(BudgetLine, driver.scenario),
        or_(BudgetLine.account_name == driver.account_name,
            BudgetLine.qbo_account_id == driver.qbo_account_id) if driver.qbo_account_id else BudgetLine.account_name == driver.account_name,
    ).first() is not None
    plan = plan_budget_drivers(db, company_id=driver.company_id, budget_year=driver.budget_year,
                               scenario=driver.scenario, force=True, driver_ids=[driver.id],
                               apply_to_budget=apply_to_budget and has_lines)
    result = apply_reviewed_budget_driver_plan(
        db, company_id=driver.company_id, reviewed=ReviewedBudgetPlan.from_payload(plan), commit=False,
    )
    return result


def create_budget_driver_service(db: Session, driver: BudgetDriver) -> dict:
    try:
        lock_budget_write_scope(db, company_id=driver.company_id)
        db.add(driver)
        db.flush()
        result = _calculate_and_apply(db, driver, apply_to_budget=True)
        db.commit()
        return {**_driver_to_dict(driver), "excluded_locked_months": result["excluded_locked_months"]}
    except Exception:
        db.rollback()
        raise


def update_budget_driver_service(db: Session, driver: BudgetDriver, updates: dict) -> dict:
    try:
        lock_budget_write_scope(db, company_id=driver.company_id)
        db.refresh(driver)
        if set(updates) - {"driver_method", "driver_params"}:
            raise BudgetPlanError("Only driver method and parameters may be changed here.")
        for field, value in updates.items():
            setattr(driver, field, value)
        driver.is_dirty = True
        db.flush()
        result = _calculate_and_apply(db, driver, apply_to_budget=True)
        db.commit()
        return {**_driver_to_dict(driver), "excluded_locked_months": result["excluded_locked_months"]}
    except Exception:
        db.rollback()
        raise


def compute_budget_driver_service(db: Session, driver: BudgetDriver, *, apply_to_budget: bool = False) -> dict:
    try:
        lock_budget_write_scope(db, company_id=driver.company_id)
        db.refresh(driver)
        result = _calculate_and_apply(db, driver, apply_to_budget=apply_to_budget)
        db.commit()
        return {**_driver_to_dict(driver), "excluded_locked_months": result["excluded_locked_months"]}
    except Exception:
        db.rollback()
        raise


def bulk_set_driver_method_core(
    db: Session, *, company_id: str, budget_year: int, method: str,
    scenario: str = "base", only_unset: bool = True, driver_category: str | None = None,
    auto_create: bool = True, growth_pct: float | None = None,
) -> dict:
    if method not in DRIVER_METHODS:
        raise BudgetPlanError(f"Invalid driver method: {method}.")
    try:
        lock_budget_write_scope(db, company_id=company_id)
        all_drivers = db.query(BudgetDriver).filter(
            BudgetDriver.company_id == company_id, BudgetDriver.budget_year == budget_year,
            _scenario_clause(BudgetDriver, scenario),
        ).populate_existing().all()
        drivers = [d for d in all_drivers if (not only_unset or not d.driver_method)
                   and (not driver_category or d.driver_category == driver_category)]
        for driver in drivers:
            driver.driver_method = method
            driver.is_dirty = True
            if method == "yoy_growth" and growth_pct is not None:
                driver.driver_params = {**(driver.driver_params or {}), "growth_pct": growth_pct}
        updated, created = len(drivers), 0
        if auto_create:
            lines = db.query(BudgetLine).filter(
                BudgetLine.company_id == company_id, BudgetLine.budget_year == budget_year,
                _scenario_clause(BudgetLine, scenario), BudgetLine.parent_line_id.is_not(None),
            ).all()
            existing = {(d.account_name, d.vendor_name, d.customer_name) for d in all_drivers}
            for line in lines:
                category = "revenue" if line.account_type == "revenue" else "expense"
                if driver_category and category != driver_category:
                    continue
                key = (line.account_name, line.vendor_name, line.customer_name)
                if key in existing or not (line.vendor_name or line.customer_name):
                    continue
                existing.add(key)
                driver = BudgetDriver(company_id=company_id, budget_year=budget_year, scenario=scenario,
                    account_name=line.account_name, qbo_account_id=line.qbo_account_id,
                    budget_line_id=line.parent_line_id, vendor_name=line.vendor_name,
                    customer_name=line.customer_name, driver_category=category, driver_method=method,
                    driver_params={"growth_pct": growth_pct} if method == "yoy_growth" and growth_pct is not None else {},
                    is_dirty=True)
                db.add(driver)
                drivers.append(driver)
                created += 1
        db.flush()
        if drivers:
            plan = plan_budget_drivers(db, company_id=company_id, budget_year=budget_year, scenario=scenario,
                                       force=True, driver_ids=[d.id for d in drivers])
            result = apply_reviewed_budget_driver_plan(db, company_id=company_id,
                        reviewed=ReviewedBudgetPlan.from_payload(plan), commit=False)
        else:
            result = {"excluded_locked_months": []}
        db.commit()
        return {"status": "ok", "method": method, "method_label": DRIVER_METHODS[method],
                "drivers_updated": updated, "drivers_created": created, "drivers_computed": len(drivers),
                "errors": [], "excluded_locked_months": result["excluded_locked_months"]}
    except Exception:
        db.rollback()
        raise

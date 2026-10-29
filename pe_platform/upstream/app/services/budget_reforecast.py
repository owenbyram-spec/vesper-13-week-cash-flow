"""Read-only financial plans, exact reviewed application, and verified reforecasts.

Authorization belongs to the HTTP/approval boundary. Every query and mutation is
company scoped. No source call occurs while application locks are held.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ..budget.constants import MONTHS
from ..budget.driver_calculation import (
    BudgetPlanError, calculate_driver_values, cents, effective_values, money, month_values,
)
from ..models import BudgetAuditLog, BudgetDriver, BudgetLine, BudgetMonthLock, FinancialCalendar

LINE_FIELDS = (
    "id", "company_id", "budget_year", "scenario", "account_name", "account_type",
    "qbo_account_id", "parent_line_id", "vendor_name", "customer_name", "department",
    "monthly_amounts", "original_monthly_amounts", "sort_order", "notes", "status",
    "driver_formula", "updated_at",
)
DRIVER_FIELDS = (
    "id", "company_id", "budget_year", "scenario", "account_name", "qbo_account_id",
    "budget_line_id", "driver_method", "driver_category", "vendor_name", "customer_name",
    "driver_params", "computed_values", "overrides", "is_dirty", "updated_at",
)
MAX_REFORECAST_DETAIL_REQUESTS = 120


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BudgetPlanError("Budget plan source contains invalid or nonfinite values.") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _row(row: Any, fields: tuple[str, ...]) -> dict:
    result = {}
    for key in fields:
        value = getattr(row, key)
        result[key] = value.isoformat() if hasattr(value, "isoformat") else copy.deepcopy(value)
    return result


def _scenario_clause(model, scenario: str):
    return or_(model.scenario == "base", model.scenario.is_(None)) if scenario == "base" else model.scenario == scenario


def _snapshot(db: Session, company_id: str, budget_year: int, scenario: str) -> dict:
    # Select columns, not identity-mapped ORM records: a preview must neither
    # flush nor overwrite pending edits, and apply must see fresh persisted state.
    lines = db.query(*(getattr(BudgetLine, field) for field in LINE_FIELDS)).filter(
        BudgetLine.company_id == company_id,
        BudgetLine.budget_year.in_([budget_year - 1, budget_year]),
        _scenario_clause(BudgetLine, scenario),
    ).order_by(BudgetLine.id).all()
    drivers = db.query(*(getattr(BudgetDriver, field) for field in DRIVER_FIELDS)).filter(
        BudgetDriver.company_id == company_id, BudgetDriver.budget_year == budget_year,
        _scenario_clause(BudgetDriver, scenario),
    ).order_by(BudgetDriver.id).all()
    locks = db.query(BudgetMonthLock.id, BudgetMonthLock.month, BudgetMonthLock.locked_at, BudgetMonthLock.locked_by).filter(
        BudgetMonthLock.company_id == company_id, BudgetMonthLock.budget_year == budget_year,
    ).order_by(BudgetMonthLock.id).all()
    calendars = db.query(FinancialCalendar.id, FinancialCalendar.period, FinancialCalendar.locked, FinancialCalendar.status, FinancialCalendar.locked_at).filter(
        FinancialCalendar.realm_id == company_id,
        FinancialCalendar.period.like(f"{budget_year}-%"),
    ).order_by(FinancialCalendar.id).all()
    return {
        "lines": [_row(r, LINE_FIELDS) for r in lines],
        "drivers": [_row(r, DRIVER_FIELDS) for r in drivers],
        "manual_locks": [_row(r, ("id", "month", "locked_at", "locked_by")) for r in locks],
        "accounting_locks": [_row(r, ("id", "period", "locked", "status", "locked_at")) for r in calendars],
    }


def lock_budget_write_scope(db: Session, *, company_id: str) -> None:
    """Cover existing records AND phantom driver/period-lock inserts until commit.

    Short table locks intentionally serialize financial writes across tenants on
    PostgreSQL. Existing accounting-calendar writers do not share advisory locks.
    A bounded timeout refuses contention rather than weakening stale validation.
    SQLite's no-op write obtains its database write reservation before rereading.
    """
    dialect = db.get_bind().dialect.name
    try:
        if dialect == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '2000ms'"))
            db.execute(text(
                "LOCK TABLE budget_drivers, budget_lines, budget_month_locks, financial_calendar "
                "IN SHARE ROW EXCLUSIVE MODE"
            ))
        elif dialect == "sqlite":
            db.execute(update(BudgetLine).where(BudgetLine.company_id == company_id).values(id=BudgetLine.id, updated_at=BudgetLine.updated_at))
        else:
            raise BudgetPlanError("This database cannot safely apply a reviewed budget plan.")
    except DBAPIError as exc:
        raise BudgetPlanError("Budget is busy; no changes were applied. Retry with a fresh review.") from exc


def _locks(snapshot: dict) -> list[str]:
    locked = {r["month"] for r in snapshot["manual_locks"]}
    for row in snapshot["accounting_locks"]:
        if row["locked"]:
            try:
                locked.add(MONTHS[int(row["period"].split("-")[1]) - 1])
            except (ValueError, IndexError):
                raise BudgetPlanError("Accounting lock period is invalid.")
    if not locked <= set(MONTHS):
        raise BudgetPlanError("Budget contains an invalid locked month.")
    return sorted(locked, key=MONTHS.index)


def _account_match(a: dict, b: dict) -> bool:
    if a.get("qbo_account_id") and b.get("qbo_account_id"):
        return str(a["qbo_account_id"]) == str(b["qbo_account_id"])
    return a.get("account_name") == b.get("account_name")


def _target(driver: dict, lines: list[dict], *, required: bool = True) -> dict | None:
    entity = "customer_name" if driver.get("driver_category") == "revenue" else "vendor_name"
    name = driver.get(entity)
    candidates = [r for r in lines if _account_match(driver, r) and (
        r.get(entity) == name and r.get("parent_line_id") is not None
        if name else r.get("parent_line_id") is None
    )]
    if driver.get("budget_line_id"):
        candidates = [r for r in candidates if driver["budget_line_id"] in (r["id"], r.get("parent_line_id"))]
    if len(candidates) > 1 or (required and len(candidates) != 1):
        raise BudgetPlanError(f"Driver {driver['id']} has no unique budget target; resolve its account/entity mapping.")
    return candidates[0] if candidates else None


def _validate_tree(lines: list[dict]) -> dict[int, list[dict]]:
    by_id = {r["id"]: r for r in lines}
    children: dict[int, list[dict]] = {}
    for row in lines:
        seen = {row["id"]}
        parent_id = row.get("parent_line_id")
        if parent_id:
            children.setdefault(parent_id, []).append(row)
        while parent_id:
            if parent_id not in by_id or parent_id in seen:
                raise BudgetPlanError("Budget hierarchy is cyclic or contains a missing scoped parent.")
            seen.add(parent_id)
            parent_id = by_id[parent_id].get("parent_line_id")
    return children


def _plan_from_snapshot(snapshot: dict, *, company_id: str, budget_year: int,
                        scenario: str, force: bool, driver_ids: list[int] | None = None,
                        apply_to_budget: bool = True) -> dict:
    current = [r for r in snapshot["lines"] if r["budget_year"] == budget_year]
    prior = [r for r in snapshot["lines"] if r["budget_year"] == budget_year - 1]
    children = _validate_tree(current)
    source = {r["id"]: r for r in current}
    amounts = {r["id"]: month_values(r["monthly_amounts"]) for r in current}
    # Normalized display maps fill omitted cells with zero. Track actual source
    # coverage separately so those placeholders cannot become calculation inputs.
    known_months = {r["id"]: set(r["monthly_amounts"]) for r in current}
    locked = _locks(snapshot)
    eligible = [m for m in MONTHS if m not in locked]
    selected = [d for d in snapshot["drivers"] if (driver_ids is None or d["id"] in driver_ids)
                and (force or d["is_dirty"])]
    if driver_ids is not None and set(driver_ids) != {d["id"] for d in snapshot["drivers"] if d["id"] in driver_ids}:
        raise BudgetPlanError("A selected driver is missing from the scoped budget.")
    selected.sort(key=lambda d: (d.get("driver_category") != "revenue", d["id"]))
    updates: dict[str, dict] = {}
    touched: set[int] = set()
    targets: set[int] = set()

    def aggregate(parent_id: int) -> None:
        for m in eligible:
            if any(m not in known_months[c["id"]] for c in children[parent_id]):
                known_months[parent_id].discard(m)
                continue
            amounts[parent_id][m] = cents(sum((money(amounts[c["id"]][m]) for c in children[parent_id]), Decimal(0)))
            known_months[parent_id].add(m)
        touched.add(parent_id)
        next_id = source[parent_id].get("parent_line_id")
        if next_id:
            aggregate(next_id)

    for driver in selected:
        target = _target(driver, current, required=apply_to_budget)
        if apply_to_budget and target["id"] in children:
            raise BudgetPlanError("A parent with entity children cannot be overwritten by an account-only driver.")
        if apply_to_budget and target["id"] in targets:
            raise BudgetPlanError("Multiple drivers target the same budget line.")
        historical = None
        if driver["driver_method"] in {"ttm_avg", "yoy_growth"}:
            historical_driver = {**driver, "budget_line_id": None}
            history_row = _target(historical_driver, prior, required=False)
            if history_row is not None:
                historical = history_row.get("original_monthly_amounts") or history_row.get("monthly_amounts")
            elif target is not None and target.get("original_monthly_amounts") is not None:
                historical = target["original_monthly_amounts"]
        revenue = None
        if driver["driver_method"] == "pct_revenue":
            revenue_rows = [r for r in current if r["account_type"] == "revenue" and not r["parent_line_id"]]
            if not revenue_rows:
                raise BudgetPlanError("Projected revenue source is missing; an absent account is not zero revenue.")
            if any(set(MONTHS) - known_months[r["id"]] for r in revenue_rows):
                raise BudgetPlanError("Projected revenue source must contain all twelve months; missing data is not zero revenue.")
            revenue = {m: cents(sum((money(amounts[r["id"]][m]) for r in revenue_rows), Decimal(0))) for m in MONTHS}
        computed, basis = calculate_driver_values(driver, historical=historical, revenue=revenue)
        effective = effective_values(computed, driver.get("overrides"))
        updates[str(driver["id"])] = {"computed_values": computed, "basis": basis,
                                      "is_dirty": not apply_to_budget}
        if not apply_to_budget:
            continue
        targets.add(target["id"])
        for m in eligible:
            amounts[target["id"]][m] = effective[m]
            known_months[target["id"]].add(m)
        touched.add(target["id"])
        if target.get("parent_line_id"):
            aggregate(target["parent_line_id"])
    if any(set(eligible) - known_months[line_id] for line_id in touched):
        raise BudgetPlanError("Budget hierarchy has missing required child amounts; no values were applied.")
    changed = {line_id for line_id in touched if amounts[line_id] != month_values(source[line_id]["monthly_amounts"])}
    plan = {
        "schema_version": 1, "company_id": company_id, "budget_year": budget_year,
        "scenario": scenario, "force": force, "apply_to_budget": apply_to_budget,
        "source_snapshot": snapshot, "source_fingerprint": _hash(snapshot),
        "before": {str(i): month_values(source[i]["monthly_amounts"]) for i in sorted(changed)},
        "after": {str(i): amounts[i] for i in sorted(changed)}, "driver_updates": updates,
        "applied": len(selected) if apply_to_budget and eligible else 0,
        "parents_updated": sum(i in children for i in changed), "excluded_locked_months": locked,
        "status": "applied" if selected else ("no_drivers" if not snapshot["drivers"] else "no_changes"),
    }
    plan["plan_fingerprint"] = _hash(plan)
    return plan


def plan_budget_drivers(db: Session, *, company_id: str, budget_year: int,
                        scenario: str = "base", force: bool = False,
                        driver_ids: list[int] | None = None, apply_to_budget: bool = True) -> dict:
    """Build a JSON-safe, complete proposal without writes, flushes, or provider calls."""
    with db.no_autoflush:
        snapshot = _snapshot(db, company_id, budget_year, scenario)
        return _plan_from_snapshot(snapshot, company_id=company_id, budget_year=budget_year,
                                   scenario=scenario, force=force, driver_ids=driver_ids,
                                   apply_to_budget=apply_to_budget)


@dataclass(frozen=True)
class ReviewedBudgetPlan:
    """Immutable serialization injected from a server-owned stored proposal field.

    JSON/model/tool inputs cannot construct this Python wrapper. This is not an
    authorization token: the execution boundary must still authenticate/authorize.
    """
    serialized: str

    @classmethod
    def from_payload(cls, payload: Any) -> "ReviewedBudgetPlan":
        if not isinstance(payload, dict):
            raise BudgetPlanError("Budget proposal has no reviewed calculation plan; draft it again.")
        try:
            return cls(_canonical(payload))
        except (TypeError, ValueError) as exc:
            raise BudgetPlanError("Reviewed budget plan contains invalid values; draft it again.") from exc


def _validate_plan(plan: dict, company_id: str) -> None:
    if plan.get("schema_version") != 1 or plan.get("company_id") != company_id:
        raise BudgetPlanError("Reviewed budget plan has invalid ownership/version; draft it again.")
    content = {k: v for k, v in plan.items() if k != "plan_fingerprint"}
    if plan.get("plan_fingerprint") != _hash(content) or plan.get("source_fingerprint") != _hash(plan.get("source_snapshot")):
        raise BudgetPlanError("Reviewed budget plan changed; draft it again.")


def apply_reviewed_budget_driver_plan(db: Session, *, company_id: str,
                                     reviewed: ReviewedBudgetPlan | None,
                                     changed_by: str = "reviewed_budget_driver_apply",
                                     commit: bool = True) -> dict:
    if not isinstance(reviewed, ReviewedBudgetPlan):
        raise BudgetPlanError("Budget proposal has no trusted reviewed calculation plan; draft it again.")
    plan = json.loads(reviewed.serialized)
    _validate_plan(plan, company_id)
    lock_budget_write_scope(db, company_id=company_id)
    current = _snapshot(db, company_id, plan["budget_year"], plan["scenario"])
    if _hash(current) != plan["source_fingerprint"]:
        raise BudgetPlanError("Budget drivers, values, or period locks changed; draft a fresh plan.")
    # Validation above and writes below share the same transaction and insertion locks.
    scoped_lines = {r["id"] for r in current["lines"] if r["budget_year"] == plan["budget_year"]}
    scoped_drivers = {r["id"] for r in current["drivers"]}
    if not {int(i) for i in plan["after"]} <= scoped_lines or not {int(i) for i in plan["driver_updates"]} <= scoped_drivers:
        raise BudgetPlanError("Reviewed plan references an unscoped financial record.")
    # Validate the entire write set before assigning even one ORM value.
    planned_lines = {key: month_values(values, complete=True) for key, values in plan["after"].items()}
    planned_drivers = {key: month_values(values["computed_values"], complete=True) for key, values in plan["driver_updates"].items()}
    if set(planned_lines) != set(plan["before"]):
        raise BudgetPlanError("Reviewed plan has incomplete before/after values.")
    for line_id, values in planned_lines.items():
        line = db.query(BudgetLine).filter(BudgetLine.company_id == company_id, BudgetLine.id == int(line_id)).populate_existing().one()
        if line.original_monthly_amounts is None:
            line.original_monthly_amounts = copy.deepcopy(line.monthly_amounts)
        for m in MONTHS:
            if values[m] != plan["before"][line_id][m]:
                db.add(BudgetAuditLog(company_id=company_id, budget_line_id=line.id, field=m,
                                      old_value=str(plan["before"][line_id][m]), new_value=str(values[m]),
                                      changed_by=changed_by))
        line.monthly_amounts = values
    for driver_id, values in plan["driver_updates"].items():
        driver = db.query(BudgetDriver).filter(BudgetDriver.company_id == company_id, BudgetDriver.id == int(driver_id)).populate_existing().one()
        driver.computed_values = planned_drivers[driver_id]
        driver.is_dirty = values["is_dirty"]
    db.flush()
    if commit:
        db.commit()
    return {key: plan[key] for key in ("status", "applied", "parents_updated", "before", "after", "excluded_locked_months")}


def apply_budget_drivers_service(db: Session, *, company_id: str, budget_year: int,
                                 scenario: str = "base", force: bool = False) -> dict:
    plan = plan_budget_drivers(db, company_id=company_id, budget_year=budget_year, scenario=scenario, force=force)
    return apply_reviewed_budget_driver_plan(db, company_id=company_id, reviewed=ReviewedBudgetPlan.from_payload(plan))


def _account_identity(row: dict) -> tuple:
    return ("id", str(row["qbo_account_id"])) if row.get("qbo_account_id") else ("name", row["account_name"])


def _hierarchy_index(lines: list[dict]) -> tuple[dict[tuple, dict], dict[tuple, list[dict]]]:
    _validate_tree(lines)
    parents: dict[tuple, dict] = {}
    children: dict[tuple, list[dict]] = {}
    by_id = {r["id"]: r for r in lines}
    for row in lines:
        if not row["parent_line_id"]:
            identity = _account_identity(row)
            if identity in parents:
                raise BudgetPlanError("Duplicate reforecast/base account identity; resolve existing records without automatic merging.")
            parents[identity] = row
    for row in lines:
        if not row["parent_line_id"]:
            continue
        parent = by_id[row["parent_line_id"]]
        if parent["parent_line_id"] or _account_identity(row) != _account_identity(parent):
            raise BudgetPlanError("Entity actuals require a direct child of the same account.")
        identity = _account_identity(parent)
        entity = row.get("customer_name") or row.get("vendor_name")
        if not entity or (row.get("customer_name") and row.get("vendor_name")):
            raise BudgetPlanError("Reforecast entity identity is missing or ambiguous.")
        siblings = children.setdefault(identity, [])
        if any((r.get("customer_name") or r.get("vendor_name")) == entity for r in siblings):
            raise BudgetPlanError("Duplicate entity name in account detail; actuals cannot be allocated uniquely.")
        siblings.append(row)
    return parents, children


def create_reforecast_service(db: Session, *, company_id: str, budget_year: int) -> dict:
    """Build all source-backed changes before writing a separate target hierarchy.

    The live GL adapter intentionally refuses until report completeness is proven.
    This cannot be enabled by a provider flag or an API-supplied assertion.
    """
    import calendar
    from ..budget.gl_detail import VerifiedGLDetail, fetch_reforecast_gl_detail
    from ..budget.qbo_reports import parse_reforecast_pl_actuals
    from ..routers.budget import get_qbo_client_from_db

    base_snapshot = _snapshot(db, company_id, budget_year, "base")
    target_snapshot = _snapshot(db, company_id, budget_year, "reforecast")
    base = [r for r in base_snapshot["lines"] if r["budget_year"] == budget_year]
    target = [r for r in target_snapshot["lines"] if r["budget_year"] == budget_year]
    base_parents, base_children = _hierarchy_index(base)
    target_parents, target_children = _hierarchy_index(target)
    closed = _locks(base_snapshot)
    if not closed:
        raise BudgetPlanError("No locked months found. Lock months before creating a reforecast.")
    if set(target_parents) - set(base_parents):
        raise BudgetPlanError("Reforecast contains accounts absent from the base; resolve the target explicitly.")
    hierarchical = {key for key in base_parents if base_children.get(key) or target_children.get(key)}
    if len(hierarchical) * len(closed) > MAX_REFORECAST_DETAIL_REQUESTS:
        raise BudgetPlanError("Reforecast exceeds 120 account-month GL reads; no source data was fetched or changed.")
    if any(not base_parents[key].get("qbo_account_id") for key in hierarchical):
        raise BudgetPlanError("Hierarchical actuals require an explicit QBO account ID.")

    desired: dict[tuple, dict] = {}
    try:
        client = get_qbo_client_from_db(db, company_id)
        report = client.get_report("ProfitAndLoss", {
            "start_date": date(budget_year, 1, 1).isoformat(),
            "end_date": date(budget_year, 12, 31).isoformat(),
            "summarize_column_by": "Month", "accounting_method": "Accrual",
        })
        actuals = parse_reforecast_pl_actuals(report, budget_year=budget_year, months=closed)
        for key, parent in base_parents.items():
            target_parent = target_parents.get(key)
            parent_amounts = month_values((target_parent or parent)["monthly_amounts"])
            base_by_name = {(r.get("customer_name") or r.get("vendor_name")): r for r in base_children.get(key, [])}
            target_by_name = {(r.get("customer_name") or r.get("vendor_name")): r for r in target_children.get(key, [])}
            child_plans = {}
            for name in set(base_by_name) | set(target_by_name):
                origin = target_by_name.get(name) or base_by_name[name]
                child_plans[name] = {"origin": origin, "target": target_by_name.get(name),
                                     "amounts": month_values(origin["monthly_amounts"])}
            for month in closed:
                matches = [entry for entry in actuals[month] if (
                    str(entry.get("qbo_account_id")) == str(parent["qbo_account_id"])
                    if parent.get("qbo_account_id") else entry["account_name"] == parent["account_name"]
                )]
                if len(matches) != 1:
                    raise BudgetPlanError(f"No unique {month} actual for {parent['account_name']}; missing is not zero.")
                amount = money(matches[0]["amount"])
                parent_amounts[month] = cents(amount)
                if key not in hierarchical:
                    continue
                month_index = MONTHS.index(month) + 1
                start = date(budget_year, month_index, 1).isoformat()
                end = date(budget_year, month_index, calendar.monthrange(budget_year, month_index)[1]).isoformat()
                detail = fetch_reforecast_gl_detail(client, account_id=str(parent["qbo_account_id"]), start_date=start, end_date=end)
                if (not isinstance(detail, VerifiedGLDetail) or not detail.completeness_reference
                        or detail.account_id != str(parent["qbo_account_id"])
                        or detail.start_date != start or detail.end_date != end or detail.basis != "Accrual"):
                    raise BudgetPlanError("Entity detail scope/completeness is unverified.")
                entities = dict(detail.entities)
                if len(entities) != len(detail.entities) or any(not name for name in entities):
                    raise BudgetPlanError("Entity detail contains duplicate or missing identities.")
                if sum((money(v) for v in entities.values()), Decimal(0)) != amount:
                    raise BudgetPlanError("Entity GL detail does not reconcile exactly to the account P&L actual.")
                for name in entities.keys() - child_plans.keys():
                    field = "customer_name" if parent["account_type"] == "revenue" else "vendor_name"
                    origin = {**parent, "id": None, "parent_line_id": parent["id"],
                              "vendor_name": None, "customer_name": None, field: name,
                              "monthly_amounts": {m: 0.0 for m in MONTHS}}
                    child_plans[name] = {"origin": origin, "target": None, "amounts": {m: 0.0 for m in MONTHS}}
                for name, child in child_plans.items():
                    # Zero is justified ONLY by complete verified detail for this account/month.
                    child["amounts"][month] = cents(entities.get(name, Decimal(0)))
                if sum((money(c["amounts"][month]) for c in child_plans.values()), Decimal(0)) != amount:
                    raise BudgetPlanError("Reforecast child amounts do not reconcile to their parent.")
            if child_plans:
                for month in MONTHS:
                    total = cents(sum((money(c["amounts"][month]) for c in child_plans.values()), Decimal(0)))
                    if total != parent_amounts[month]:
                        raise BudgetPlanError("Preserved open-month parent/child budget amounts do not reconcile; resolve them first.")
            desired[key] = {"origin": parent, "target": target_parent, "amounts": parent_amounts, "children": child_plans}
    except BudgetPlanError:
        raise
    except Exception as exc:
        raise BudgetPlanError("QuickBooks actuals are unavailable; the reforecast was not changed.") from exc

    lock_budget_write_scope(db, company_id=company_id)
    if (_hash(_snapshot(db, company_id, budget_year, "base")) != _hash(base_snapshot)
            or _hash(_snapshot(db, company_id, budget_year, "reforecast")) != _hash(target_snapshot)):
        raise BudgetPlanError("Budget or lock state changed while loading actuals; retry the reforecast.")
    updated = 0

    def persist(plan: dict, parent_id: int | None) -> BudgetLine:
        nonlocal updated
        origin, existing, values = plan["origin"], plan["target"], plan["amounts"]
        if existing:
            row = db.query(BudgetLine).filter(BudgetLine.company_id == company_id, BudgetLine.id == existing["id"]).populate_existing().one()
            old = month_values(row.monthly_amounts)
        else:
            fields = {k: copy.deepcopy(origin.get(k)) for k in (
                "account_name", "account_type", "qbo_account_id", "sort_order", "notes", "department",
                "driver_formula", "vendor_name", "customer_name",
            )}
            row = BudgetLine(company_id=company_id, budget_year=budget_year, scenario="reforecast",
                             parent_line_id=parent_id, monthly_amounts=values, status="draft", **fields)
            db.add(row)
            db.flush()
            old = month_values(origin["monthly_amounts"])
        if existing is None or old != values:
            updated += 1
            row.monthly_amounts = values
            for month in MONTHS:
                if old[month] != values[month]:
                    db.add(BudgetAuditLog(company_id=company_id, budget_line_id=row.id, field=month,
                                          old_value=str(old[month]), new_value=str(values[month]), changed_by="reforecast"))
        return row

    for plan in desired.values():
        row = persist(plan, None)
        for child in plan["children"].values():
            persist(child, row.id)
    db.commit()
    return {"status": "updated" if updated else "no_changes", "lines_updated": updated,
            "months_replaced": closed,
            "message": f"Reforecast updated on {updated} lines with verified actuals for {', '.join(closed)}."
                       if updated else "Reforecast is already in sync; no changes were made."}

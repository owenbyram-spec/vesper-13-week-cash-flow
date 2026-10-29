"""Frozen snapshot versions — capture, load, and accuracy-scoring assembly.

Plan 20260706-cfo-grade-financial-workflows AC-2B.1 (build-new): forecast-accuracy
tracking scores actuals against the forecast AS IT WAS MADE N weeks ago. The serve
cache (``CashForecastSnapshot``) is latest-only and continuously re-anchored, so
scoring against it is tautological (cashflow-modeling §6). This module:

- freezes the SERVED payload (overrides applied — the grid the CFO actually saw)
  into ``CashForecastSnapshotVersion``, one row per (company, as-of week): upserted
  while its week is current, IMMUTABLE once the calendar rolls past it;
- assembles scored weeks for the accuracy math: frozen forecast totals vs captured
  actuals, excluding ``pull_partial`` weeks and weeks whose actuals were never
  pulled — every exclusion is counted, never silent.

All queries carry the ``company_id`` tenant predicate (realm-keyed under live RLS).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..models import (
    CashForecastActual,
    CashForecastSnapshotVersion,
    CashForecastWeekStatus,
    OrganizationCompany,
)

logger = logging.getLogger(__name__)

# Week statuses whose actuals capture is complete enough to score against
# (mirrors the router's CASCADE_READY set — "not_pulled"/"pending" weeks have no
# trustworthy actuals and are excluded from scoring, counted in the output).
SCOREABLE_WEEK_STATUSES = {"pulled", "closed", "reconciled"}


def _next_friday(d: date) -> date:
    """Next Friday on or after *d* (the grid's week-ending convention)."""
    days_ahead = 4 - d.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def compact_grid_from_payload(payload: dict) -> dict:
    """The compact frozen form of a served /weekly payload (see model docstring)."""
    return {
        "week_headers": list(payload.get("week_headers") or []),
        "lookback": int(payload.get("lookback") or 0),
        "total_cash_in": [float(v) for v in (payload.get("total_cash_in") or [])],
        "total_cash_out": [float(v) for v in (payload.get("total_cash_out") or [])],
        "beginning_balances": [float(v) for v in (payload.get("beginning_balances") or [])],
        "ending_balances": [float(v) for v in (payload.get("ending_balances") or [])],
        "customers": [
            {"name": r.get("name"), "amounts": list(r.get("amounts") or [])}  # SA-OK: dict access on the in-memory payload rows — no DB query in this comprehension
            for r in (payload.get("customers") or [])
        ],
        "vendors": [
            {"name": r.get("name"), "amounts": list(r.get("amounts") or [])}  # SA-OK: dict access on the in-memory payload rows — no DB query in this comprehension
            for r in (payload.get("vendors") or [])
        ],
        "bank_balance": float(payload.get("bank_balance") or 0.0),
        "data_quality_is_partial": bool((payload.get("data_quality") or {}).get("is_partial")),
    }


def freeze_snapshot_version(db: Session, company_id: str, payload: dict) -> dict:
    """Upsert the frozen version row for the payload's as-of (first forecast) week.

    Immutability contract: within its own week the row is upserted (the last build of
    the week wins — nightly beat keeps it current); a row whose week has already
    rolled past is NEVER overwritten, so historical forecast provenance survives.
    """
    first_forecast = payload.get("first_forecast_week_ending")
    if not first_forecast:
        return {"version_frozen": False, "reason": "no_first_forecast_week"}
    as_of = date.fromisoformat(str(first_forecast)[:10])
    current_as_of = _next_friday(date.today())
    if as_of < current_as_of:
        # The payload describes a PAST as-of week — that version is frozen history.
        return {"version_frozen": False, "reason": "as_of_week_already_frozen"}

    grid = compact_grid_from_payload(payload)
    existing = (
        db.query(CashForecastSnapshotVersion)
        .filter(
            CashForecastSnapshotVersion.company_id == company_id,
            CashForecastSnapshotVersion.as_of_week_ending == as_of,
        )
        .first()
    )
    if existing:
        existing.grid = grid  # type: ignore[assignment]
        existing.lookback = grid["lookback"]  # type: ignore[assignment]
        existing.qbo_bank_balance = grid["bank_balance"]  # type: ignore[assignment]
    else:
        org_company = (
            db.query(OrganizationCompany)
            .filter(OrganizationCompany.realm_id == company_id)
            .first()
        )
        db.add(CashForecastSnapshotVersion(
            organization_id=org_company.organization_id if org_company else None,
            company_id=company_id,
            as_of_week_ending=as_of,
            lookback=grid["lookback"],
            grid=grid,
            qbo_bank_balance=grid["bank_balance"],
        ))
    db.commit()
    return {"version_frozen": True, "as_of_week_ending": as_of.isoformat()}


def version_for_target_week(
    db: Session, company_id: str, target_week: date, horizon_weeks: int = 1,
) -> CashForecastSnapshotVersion | None:
    """The frozen version made ``horizon_weeks`` before ``target_week`` (as-of week
    = target − 7×horizon), or None when no version was captured that week."""
    as_of = target_week - timedelta(weeks=horizon_weeks)
    return (
        db.query(CashForecastSnapshotVersion)
        .filter(
            CashForecastSnapshotVersion.company_id == company_id,
            CashForecastSnapshotVersion.as_of_week_ending == as_of,
        )
        .first()
    )


def actuals_by_entity_for_weeks(
    db: Session, company_id: str, week_endings: list[date],
) -> dict[date, dict[tuple[str, str], float]]:
    """Categorized actuals per (entity, type) per week — the SAME row-selection the
    live layer reconciles with (categorized parents + decomposed children; excluded
    rows never counted). ``direction`` defaults a missing entity_type, mirroring the
    live layer; amounts stay positive per lane (the direction column IS the sign —
    financial-modeling-correctness §11)."""
    if not week_endings:
        return {}
    rows = (
        db.query(CashForecastActual)
        .filter(
            CashForecastActual.company_id == company_id,
            CashForecastActual.week_ending.in_(week_endings),
            CashForecastActual.category.in_(["categorized", "decomposed"]),
        )
        .all()
    )
    usable = [
        r for r in rows
        if (r.parent_id is None and r.category == "categorized")
        or (r.parent_id is not None and r.category == "categorized")
    ]
    # Decomposed parents are containers; their categorized children carry the money.
    out: dict[date, dict[tuple[str, str], float]] = {}
    for r in usable:
        e_type = r.entity_type
        if not e_type and r.entity_name:
            e_type = "vendor" if r.direction == "out" else "customer"
        week = r.week_ending
        key = (r.entity_name or "", e_type or "")
        week_map = out.setdefault(week, {})
        week_map[key] = round(week_map.get(key, 0.0) + float(r.amount), 2)  # SA-OK: dict access on an in-memory accumulator — no DB query in this loop
    return out


def collect_scored_weeks(
    db: Session,
    company_id: str,
    *,
    horizon_weeks: int = 1,
    max_weeks: int = 13,
) -> list[dict]:
    """Assemble {week_ending, forecast_in/out, actual_in/out, pull_partial} entries
    for the accuracy math: frozen-version forecast totals vs captured actuals.

    Only COMPLETED weeks (week-ending Friday strictly before today's next Friday)
    with a scoreable status are emitted; ``pull_partial`` flows through so the
    accuracy math can exclude-and-count it. Queries are batched (no per-week query).
    """
    current_as_of = _next_friday(date.today())
    versions = (
        db.query(CashForecastSnapshotVersion)
        .filter(CashForecastSnapshotVersion.company_id == company_id)
        .order_by(CashForecastSnapshotVersion.as_of_week_ending.desc())
        .limit(max(max_weeks * 2, max_weeks))
        .all()
    )
    targets: list[tuple[date, CashForecastSnapshotVersion]] = []
    for v in versions:
        target = v.as_of_week_ending + timedelta(weeks=horizon_weeks)
        if target < current_as_of:
            targets.append((target, v))
    targets = targets[:max_weeks]
    if not targets:
        return []

    target_dates = [t for t, _ in targets]
    statuses = (
        db.query(CashForecastWeekStatus)
        .filter(
            CashForecastWeekStatus.company_id == company_id,
            CashForecastWeekStatus.week_ending.in_(target_dates),
        )
        .all()
    )
    status_by_week = {s.week_ending: s for s in statuses}
    actuals = actuals_by_entity_for_weeks(db, company_id, target_dates)

    scored: list[dict] = []
    for target, version in targets:
        status = status_by_week.get(target)  # SA-OK: dict access on a pre-fetched status map — no DB query in this loop
        if status is None or status.status not in SCOREABLE_WEEK_STATUSES:
            continue
        grid = version.grid or {}
        headers = list(grid.get("week_headers") or [])  # SA-OK: dict access on the stored JSON grid — no DB query in this loop
        iso = target.isoformat()
        if iso not in headers:
            continue
        col = headers.index(iso)
        total_in = grid.get("total_cash_in") or []  # SA-OK: dict access on the stored JSON grid — no DB query in this loop
        total_out = grid.get("total_cash_out") or []  # SA-OK: dict access on the stored JSON grid — no DB query in this loop
        week_actuals = actuals.get(target, {})  # SA-OK: dict access on a pre-fetched actuals map — no DB query in this loop
        actual_in = round(sum(v for (_, t), v in week_actuals.items() if t == "customer"), 2)
        actual_out = round(sum(v for (_, t), v in week_actuals.items() if t == "vendor"), 2)
        scored.append({
            "week_ending": iso,
            "as_of_week_ending": version.as_of_week_ending.isoformat(),
            "horizon_weeks": horizon_weeks,
            "forecast_in": float(total_in[col]) if col < len(total_in) else 0.0,
            "forecast_out": float(total_out[col]) if col < len(total_out) else 0.0,
            "actual_in": actual_in,
            "actual_out": actual_out,
            "pull_partial": bool(status.pull_partial),
        })
    scored.sort(key=lambda w: w["week_ending"])
    return scored

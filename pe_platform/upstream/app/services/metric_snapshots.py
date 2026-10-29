# PLAN-EXEMPT: item-4 MetricSnapshot capture service, ratified 2026-06-25 ("proceed with 1 and 2")
"""Item-4 custom reporting — compute + persist point-in-time metric snapshots.

Reuses the EXISTING QBO pulls (ap_aging / ar_aging / the CFF cash universe) so no new QBO
surface is added. A pull that fails omits its metric (never a fabricated 0). Writes are
idempotent on (company_id, metric, as_of_date) and DO NOT commit — the caller owns the
transaction so the row lands under the realm-scoped GUC (the FORCE-RLS lesson).
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from ..analysis.ap_aging import ap_aging
from ..analysis.ar_aging import ar_aging
from ..cashflow.qbo_cash import _cash_account_universe_for_date
from ..models import MetricSnapshot

logger = logging.getLogger(__name__)


def _account_cash(acct: dict) -> tuple[str | None, float | None]:
    """(account_id, balance) for one bank-account universe row; balance None when absent."""
    amount = acct.get("balance_sheet_amount")
    account_id = acct.get("id")
    return account_id, (None if amount is None else float(amount))


def compute_metrics_for_realm(client, as_of_date: date) -> dict[str, float]:
    """Compute {metric: value} for one realm as of ``as_of_date`` by reusing the existing
    QBO pulls. A pull that raises is logged and its metric is omitted — the snapshot simply
    has no row for that metric that day, never a fabricated zero."""
    metrics: dict[str, float] = {}
    try:
        metrics["ap_total"] = float(ap_aging(client, as_of_date=as_of_date).get("total_ap") or 0.0)
    except Exception as exc:
        logger.warning("[metric_snapshots] ap_total skipped for %s: %s", as_of_date, exc)
    try:
        metrics["ar_total"] = float(ar_aging(client, as_of_date=as_of_date).get("total_ar") or 0.0)
    except Exception as exc:
        logger.warning("[metric_snapshots] ar_total skipped for %s: %s", as_of_date, exc)
    try:
        universe = _cash_account_universe_for_date(client, str(client.realm_id), as_of_date)
        accounts = universe.get("accounts", [])
        blended = 0.0
        any_cash = False
        for acct in accounts:
            account_id, amount = _account_cash(acct)
            if amount is None:
                continue
            any_cash = True
            blended += amount
            if account_id:
                metrics[f"cash:{account_id}"] = round(amount, 2)
        if any_cash:
            metrics["cash_blended"] = round(blended, 2)
    except Exception as exc:
        logger.warning("[metric_snapshots] cash skipped for %s: %s", as_of_date, exc)
    return metrics


def upsert_metric_snapshots(
    db: Session,
    *,
    realm_id: str,
    organization_id: int | None,
    organization_company_id: int | None,
    metrics: dict[str, float],
    as_of_date: date,
    source: str,
) -> int:
    """Upsert one MetricSnapshot per metric for (realm, as_of_date). Idempotent: a re-run for
    the same day updates the value rather than duplicating. Batched — one query for the
    existing rows, then update-or-add in memory (no N+1). Does not commit."""
    if not metrics:
        return 0
    keys = list(metrics.keys())
    existing = {
        row.metric: row
        for row in db.query(MetricSnapshot).filter(
            MetricSnapshot.company_id == realm_id,
            MetricSnapshot.as_of_date == as_of_date,
            MetricSnapshot.metric.in_(keys),
        ).all()
    }
    for metric, value in metrics.items():
        row = existing[metric] if metric in existing else None
        if row is None:
            db.add(MetricSnapshot(
                organization_id=organization_id,
                organization_company_id=organization_company_id,
                company_id=realm_id,
                metric=metric,
                value=value,
                as_of_date=as_of_date,
                source=source,
            ))
        else:
            row.value = value
            row.source = source
            if organization_id is not None:
                row.organization_id = organization_id
    return len(metrics)


# PLAN-EXEMPT: item-4 on-demand + read service functions (ratified 2026-06-25 "proceed with 1 and 2")
def run_on_demand_snapshot(db: Session, *, client, company_id: str) -> int:
    """Compute + upsert today's snapshots for one realm on demand (source='on_demand') and
    commit. Resolves org+company from the realm for row attribution; the request scope already
    pins the org GUC so the org-keyed write passes FORCE RLS. Returns the metric count."""
    from .tenant_companies import organization_company_for_realm, organization_for_company

    org = organization_for_company(db, company_id)
    organization_id = org.id if org is not None else None
    org_company = (
        organization_company_for_realm(db, organization_id=organization_id, realm_id=company_id)
        if organization_id is not None else None
    )
    today = date.today()
    metrics = compute_metrics_for_realm(client, today)
    written = upsert_metric_snapshots(
        db,
        realm_id=company_id,
        organization_id=organization_id,
        organization_company_id=org_company.id if org_company is not None else None,
        metrics=metrics,
        as_of_date=today,
        source="on_demand",
    )
    db.commit()
    return written


def read_metric_series(
    db: Session,
    *,
    company_id: str,
    metric: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[MetricSnapshot]:
    """Ordered MetricSnapshot rows for one realm, optionally filtered by metric + date window.
    Realm-scoped (company_id); the request scope's RLS backstop fail-closes a forgotten scope."""
    query = db.query(MetricSnapshot).filter(MetricSnapshot.company_id == company_id)
    if metric:
        query = query.filter(MetricSnapshot.metric == metric)
    if start is not None:
        query = query.filter(MetricSnapshot.as_of_date >= start)
    if end is not None:
        query = query.filter(MetricSnapshot.as_of_date <= end)
    return query.order_by(MetricSnapshot.as_of_date, MetricSnapshot.metric).all()

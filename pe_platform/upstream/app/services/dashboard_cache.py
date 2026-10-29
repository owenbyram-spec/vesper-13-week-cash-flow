from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..analysis.ar_aging import ar_aging
from ..analysis.customer_revenue import customer_revenue_summary
from ..analysis.ratios import compute_balance_sheet_kpis, compute_trends, resolve_period
from ..qbo_client import get_qbo_client_from_db
from .cache import get_cached, set_cached
from .tenant_companies import organization_for_company


def _cache_scope(db: Session, realm_id: Optional[str]) -> str:
    if not realm_id:
        return "org:unmapped:realm:unmapped"
    organization = organization_for_company(db, str(realm_id))
    return f"org:{organization.id if organization else 'unmapped'}:realm:{realm_id}"


def cached_trends(
    db: Session,
    realm_id: Optional[str],
    year: Optional[int],
    month: Optional[int],
    *,
    ttl_seconds: int = 300,
) -> Dict[str, Any]:
    period = resolve_period(year, month)
    cache_key = f"{_cache_scope(db, realm_id)}:dashboard_trends:{period['ttm_start']}:{period['ttm_end']}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    client = get_qbo_client_from_db(db, realm_id)
    months, rev_vals, ebitda_vals, gp_vals = compute_trends(client, period)
    result = {
        "period": period,
        "months": months,
        "revenue_values": rev_vals,
        "ebitda_values": ebitda_vals,
        "gp_values": gp_vals,
    }
    set_cached(cache_key, result, ttl_seconds=ttl_seconds)
    return result


def cached_balance_sheet_kpis(
    db: Session,
    realm_id: str,
    as_of_date: str,
    *,
    accounting_method: str = "Accrual",
    ttl_seconds: int = 300,
) -> Dict[str, float]:
    cache_key = f"{_cache_scope(db, realm_id)}:balance_sheet_kpis:{as_of_date}:{accounting_method}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    client = get_qbo_client_from_db(db, realm_id)
    result = compute_balance_sheet_kpis(client, as_of_date, accounting_method=accounting_method)
    set_cached(cache_key, result, ttl_seconds=ttl_seconds)
    return result


def cached_ar_aging(
    db: Session,
    realm_id: str,
    *,
    limit: int = 2000,
    ttl_seconds: int = 300,
) -> Dict[str, Any]:
    cache_key = f"{_cache_scope(db, realm_id)}:ar_aging:{limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    client = get_qbo_client_from_db(db, realm_id)
    result = ar_aging(client, limit=limit)
    set_cached(cache_key, result, ttl_seconds=ttl_seconds)
    return result


def cached_customer_revenue(
    db: Session,
    realm_id: str,
    *,
    limit: int = 1000,
    ttl_seconds: int = 300,
) -> Dict[str, Any]:
    cache_key = f"{_cache_scope(db, realm_id)}:customer_revenue:{limit}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    client = get_qbo_client_from_db(db, realm_id)
    result = customer_revenue_summary(client, limit=limit)
    set_cached(cache_key, result, ttl_seconds=ttl_seconds)
    return result

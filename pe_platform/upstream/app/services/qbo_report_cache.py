from __future__ import annotations

import copy
import json
from typing import Any, Dict, Tuple

from sqlalchemy.orm import Session

from ..qbo_client import get_qbo_client_from_db
from .cache import get_cached, set_cached
from .tenant_companies import organization_for_company


def _report_cache_key(organization_id: str, realm_id: str, report_type: str, params: Dict[str, Any]) -> str:
    normalized = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    return f"org:{organization_id}:realm:{realm_id}:qbo_report:{report_type}:{normalized}"


def get_cached_qbo_report(
    db: Session,
    *,
    realm_id: str,
    report_type: str,
    params: Dict[str, Any],
    ttl_seconds: int = 300,
    organization_id: str | int | None = None,
) -> Tuple[Dict[str, Any], str]:
    """Fetch a QBO report with a short TTL cache.

    Returns a deep copy so callers can safely mutate or parse without changing
    the cached object shared with other requests.
    """
    if organization_id is None:
        org = organization_for_company(db, realm_id) if db is not None else None
        organization_id = org.id if org else "unmapped"
    cache_key = _report_cache_key(str(organization_id), realm_id, report_type, params)
    cached = get_cached(cache_key)
    if cached is not None:
        return copy.deepcopy(cached), "hit"

    client = get_qbo_client_from_db(db, realm_id)
    report = client.get_report(report_type, params=params)
    set_cached(cache_key, copy.deepcopy(report), ttl_seconds=ttl_seconds)
    return report, "miss"

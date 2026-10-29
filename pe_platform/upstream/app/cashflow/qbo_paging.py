"""Paginated QBO transaction reads for the cash-forecast history and weekly actuals.

A single ``MAXRESULTS`` query silently returns a partial, unordered subset once a
window holds more rows than one page. ``query_all_pages`` walks STARTPOSITION
pages under ``ORDERBY Id`` (stable paging, the sync.py precedent) and tells the
caller when it stopped at its page bound with rows still unread, so the caller
can mark the result partial instead of presenting it as complete.
"""
from __future__ import annotations

from typing import Any

from ..qbo_actions.qbo_query import qbo_entity, qbo_escape_str, qbo_int

QBO_MAX_PAGE_SIZE = 1000


def txn_date_where(since: str, until: str | None = None) -> str:
    """``TxnDate`` range clause with escaped date literals."""
    clause = f"TxnDate >= '{qbo_escape_str(since)}'"
    if until is not None:
        clause += f" AND TxnDate <= '{qbo_escape_str(until)}'"
    return clause


def query_all_pages(
    client: Any,
    entity: str,
    where: str,
    *,
    max_pages: int,
    page_size: int = QBO_MAX_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], bool]:
    """Every ``entity`` row matching ``where`` → ``(rows, truncated)``.

    ``truncated`` is True only when ``max_pages`` full pages were read AND a probe
    shows at least one more row; the rows returned are then a known-partial set.
    ``where`` must already be built from escaped literals (see ``txn_date_where``).
    """
    safe_entity = qbo_entity(entity)
    page = qbo_int(page_size, min=1, max=QBO_MAX_PAGE_SIZE)
    base = f"SELECT * FROM {safe_entity} WHERE {where} ORDERBY Id"
    rows: list[dict[str, Any]] = []
    start = 1
    for _ in range(max(1, max_pages)):
        resp = client.query(f"{base} STARTPOSITION {qbo_int(start, min=1)} MAXRESULTS {page}")
        batch = (resp.get("QueryResponse") or {}).get(entity) or []
        rows.extend(batch)
        if len(batch) < page:
            return rows, False
        start += page
    probe = client.query(f"{base} STARTPOSITION {qbo_int(start, min=1)} MAXRESULTS 1")
    return rows, bool((probe.get("QueryResponse") or {}).get(entity))

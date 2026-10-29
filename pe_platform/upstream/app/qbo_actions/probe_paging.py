"""Paged reads and proposal stamps shared by the reconcile probes.

A reconcile probe decides whether an UNCERTAIN prior write landed in
QuickBooks. Reading only the first page (MAXRESULTS 100) can miss the landed
entity on a busy day and post it twice, so the probes read every page. A
result set too large to read completely is itself uncertain: the probe raises
rather than guess.

The proposal stamp ``Vesper Bot proposal #<id>`` is written into the entity's
``PrivateNote`` by the drafting lanes (A/R) and the executor (journal entries);
``has_proposal_stamp`` matches it on a word boundary so #7 never matches #70.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..qbo_client import QBOClient
from ..qbo_errors import QBOUncertainOutcomeError

PAGE_SIZE = 100
MAX_PAGES = 20
# QBO's MetaData.CreateTime is Intuit's clock; allow for skew against ours.
CREATE_TIME_SKEW = timedelta(minutes=5)

_STAMP_PREFIX = "Vesper Bot proposal #"


def proposal_stamp(proposal_id: int) -> str:
    return f"{_STAMP_PREFIX}{proposal_id}"


def has_proposal_stamp(note: str | None, proposal_id: int) -> bool:
    return re.search(rf"{re.escape(_STAMP_PREFIX)}{int(proposal_id)}(?!\d)", note or "") is not None


def has_any_proposal_stamp(note: str | None) -> bool:
    return _STAMP_PREFIX in (note or "")


def create_time_filter(created_after: datetime | None) -> str:
    """`` AND MetaData.CreateTime >= '<iso>'`` for an attempt start, else ''."""
    if created_after is None:
        return ""
    if created_after.tzinfo is None:
        created_after = created_after.replace(tzinfo=timezone.utc)
    since = (created_after.astimezone(timezone.utc) - CREATE_TIME_SKEW).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return f" AND MetaData.CreateTime >= '{since}'"


def probe_or_uncertain(probe: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a reconcile probe. A probe that cannot read QuickBooks (a 5xx, a 401, a
    dropped connection) learns nothing about the earlier attempt, so the error
    becomes ``QBOUncertainOutcomeError`` and the ledger row stays ``uncertain``.
    Recorded as a plain failure it would be ``retryable_failed`` (the next retry
    skips the probe and posts blind) or ``failed`` (a re-draft could post twice).
    """
    try:
        return probe(*args, **kwargs)
    except QBOUncertainOutcomeError:
        raise
    except Exception as exc:
        raise QBOUncertainOutcomeError(
            f"The reconcile probe could not read QuickBooks ({type(exc).__name__}); "
            "the earlier attempt is still unconfirmed."
        ) from exc


def query_all(client: QBOClient, entity_name: str, where: str) -> list[dict]:
    """Every ``entity_name`` row matching ``where`` (already escaped), all pages."""
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        start = page * PAGE_SIZE + 1
        # A QBO query string, not SQL: every value in ``where`` is escaped by the caller.
        response = client.query(
            f"SELECT * FROM {entity_name} WHERE {where} "
            f"ORDERBY MetaData.CreateTime STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
        )
        batch = ((response or {}).get("QueryResponse") or {}).get(entity_name) or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
    raise QBOUncertainOutcomeError(
        f"Reconciliation could not read every {entity_name} candidate (more than "
        f"{MAX_PAGES * PAGE_SIZE} rows). A human must reconcile before this action is retried."
    )

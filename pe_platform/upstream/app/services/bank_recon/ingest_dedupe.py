"""
app/services/bank_recon/ingest_dedupe.py — statement-import COUNT-AWARE dedupe (persist-path).

History (three generations, each verified in prod):

- v1 (no dedupe): a doubled import persisted every parsed ``BankFeedItem`` line
  twice (4 unique lines → 8 rows; the B4-1 defect).
- v2 (SET-based dedupe on (txn_date, amount, normalized description)):
  over-corrected — N GENUINE same-day/same-amount/same-description statement
  lines (prod: contracted-rate insurance deposits whose feed description is the
  literal string "CREDIT", distinct_desc=1) collapsed to ONE persisted row, and
  the set-based re-import guard made the loss permanent. Root cause R-Dim-3,
  work/pilot-fixes-batch-5/research/20260716-post-remediation-regressions/SYNTHESIS.md.
- v3 (this module, plan 20260716-batch5-remediation AC-M1): MULTISET/count-aware.

The v3 contract:

1. Within-import: EVERY parsed occurrence survives. Three identical statement
   lines are three real bank transactions and persist as three rows — there is
   deliberately NO within-import drop pass anymore (v2's ``dedupe_within_import``
   is removed, one-shot cutover, not shimmed).
2. Re-import guard (count-aware): per dedupe key, persist
   ``max(0, parsed_count − existing_count)`` occurrences. An idempotent
   re-import of the same statement adds 0 rows; a corrected statement carrying
   one MORE occurrence adds exactly the missing one; a DB already holding MORE
   rows than the statement is never trimmed — this module is ADDITIVE-ONLY and
   never deletes or updates existing rows.

The across-requests whole-statement duplicate (the historical B4-1 doubling was
TWO import batches of the same statement — crit-eval Q4) is caught BEFORE this
module runs by the batch-level statement-identity guard in ``ingest.persist``;
this module is the line-level leg only.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from ...models import BankFeedItem

if TYPE_CHECKING:
    # Import-time only (avoids a runtime circular import — ingest.py imports this
    # module at module scope, before ParsedLine is defined).
    from .ingest import ParsedLine

DedupeKey = tuple[date, Decimal, str]


def dedupe_key(ln: ParsedLine) -> DedupeKey:
    """The shared dedupe identity: (date, amount, normalized description)."""
    return (ln.txn_date, ln.amount, (ln.description or "").strip().casefold())


def existing_line_key_counts(
    db: Session, *, realm_id: str, account_id: str, period_start: date, period_end: date,
) -> Counter[DedupeKey]:
    """One batched query (no N+1) of prior-import key COUNTS for the re-import guard.

    Count-aware successor of v2's set-returning ``existing_line_keys``: the
    Counter carries HOW MANY rows each key already has, so the caller can top
    up to the statement's occurrence count instead of suppressing the key
    entirely. Matches (realm_id, bank_account_ref, source='statement_upload',
    txn_date, amount, description) within the parsed batch's date range.
    realm_id== + bank_account_ref== stay explicit even though the caller pinned
    the realm GUC — the app-layer predicate remains load-bearing
    (multi-tenancy.md; RLS backstops, does not replace it).
    """
    rows = (
        db.query(BankFeedItem.txn_date, BankFeedItem.amount, BankFeedItem.description)
        .filter(
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.bank_account_ref == account_id,
            BankFeedItem.source == "statement_upload",
            BankFeedItem.txn_date >= period_start,
            BankFeedItem.txn_date <= period_end,
        )
        .all()
    )
    return Counter(
        (txn_date, amount, (description or "").strip().casefold())
        for txn_date, amount, description in rows
    )


def dedupe_lines_for_persist(
    db: Session, *, lines: list[ParsedLine], realm_id: str, account_id: str,
) -> list[ParsedLine]:
    """The count-aware persist filter (AC-M1 legs 1+2).

    Per dedupe key, keep the FIRST ``max(0, parsed_count − existing_count)``
    occurrences (order preserved). Every within-import occurrence counts toward
    ``parsed_count`` — nothing is dropped within one import unless the DB
    already holds it from a PRIOR import. ``max(0, …)`` makes the guard
    additive-only: when the DB holds MORE rows than the statement (the
    historical double-persist shape), zero lines are added and nothing is
    ever trimmed.
    """
    if not lines:
        return []
    period_start = min(ln.txn_date for ln in lines)
    period_end = max(ln.txn_date for ln in lines)
    existing = existing_line_key_counts(
        db, realm_id=realm_id, account_id=account_id,
        period_start=period_start, period_end=period_end,
    )
    parsed: Counter[DedupeKey] = Counter(dedupe_key(ln) for ln in lines)
    remaining: dict[DedupeKey, int] = {
        key: max(0, count - existing[key]) for key, count in parsed.items()
    }
    kept: list[ParsedLine] = []
    for ln in lines:
        key = dedupe_key(ln)
        if remaining[key] > 0:
            remaining[key] -= 1
            kept.append(ln)
    return kept

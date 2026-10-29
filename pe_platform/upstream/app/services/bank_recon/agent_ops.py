# PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
"""
Agent-lane bank-reconciliation operations (Wave 2.3, decisions 3/4/5/6).

Three seams the assistant tool handlers (app/assistant/handlers.py) compose:

1. ``check_session_openable`` — the CHAT-TIME (draft) guard surface for the
   visible self-drafter ``open_recon_session``. It calls the SAME primitives the
   service lifecycle uses (the open-exists query, the ``clean_start.get_anchor``
   comparison, the ``match_start_date`` None-check) and raises the SAME exception
   classes (``OpenSessionExistsError`` / ``PeriodAlreadyReconciledError`` /
   ``CleanStartNotConfiguredError``) so a doomed draft is refused honestly at
   draft time and the guard semantics can never drift from the service
   (drift-pinned by tests/test_tools_bank_recon.py).

2. ``serialize_unmatched`` — stable id-cursor pagination over ``find_matches``'
   unmatched buckets for the READ tool ``list_unmatched_items``. Ordering is by
   item id and the cursor is the last id (E9): a recomputed match result is not
   offset-stable, but an id-ordered page is — rows never duplicate or reorder
   across recomputes, they can only leave the pool.

3. ``check_bulk_pairs_liveness`` — the E5 EXECUTION-TIME steal-guard for the
   hidden executor ``recon_bulk_confirm_exec`` (and, single-pair, the dual-homed
   per-pair confirm handler — AC-6 parity). INDEXED reads only, batched ``IN``:
   BOOK side — the ``QBOTransaction`` exists in the realm, ``cleared`` is falsy,
   ``cleared_session_id`` is null; BANK side (``BankFeedItem`` has NO cleared
   column) — the row still EXISTS (dedup-delete can remove it) AND no APPROVED
   ``ReconciliationMatch`` claims its ``bank_txn_id``. It never re-runs
   ``suggest_pairings``/``find_matches`` — pairs can shrink, never grow.

Realm scoping (multi-tenancy-app-layer): every query carries an explicit
``realm_id ==`` predicate — the PRIMARY app-layer gate (the SQLite test lane has
no RLS); the caller has pinned the realm GUC upstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ...models import (
    BankFeedItem,
    QBOTransaction,
    ReconciliationMatch,
    ReconciliationSession,
    ReconciliationSessionStatementLine,
)
from . import clean_start
from .match import CleanStartNotConfiguredError, MatchResult
from .session import OpenSessionExistsError, PeriodAlreadyReconciledError
from .session_status import open_session_status_clause

# Handler-owned pair cap (decision 5): the self-draft lane bypasses the proposer's
# INV-EXACT size guard, so the drafting handler enforces this itself; it mirrors
# llm_suggest.MAX_CANDIDATES_PER_SIDE (40/side) and the executor re-refuses above it.
MAX_BULK_PAIRS = 40

# list_unmatched_items pagination bounds (E9).
DEFAULT_UNMATCHED_PAGE = 25
MAX_UNMATCHED_PAGE = 100

# Distinct skip vocabulary — ONE vocabulary across the bulk executor and the
# dual-homed per-pair confirm handler (AC-6): a skipped pair is always named.
SKIP_STALE = "skipped_stale"
SKIP_CLEARED_ELSEWHERE = "skipped_cleared_elsewhere"
SKIP_SESSION_CLOSED = "skipped_session_closed"


def check_session_openable(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
    period_start: date,
    period_end: date,
) -> None:
    """Raise the SAME guard exceptions the service would, at DRAFT time (decision 4).

    Order mirrors the honest-refusal priority (AC-2): an existing OPEN session
    first (the refusal carries resume info), then already-reconciled, then the
    no-anchor gate ``find_matches`` enforces (DEC-BREC-5: no anchor ⇒ no
    reconciliation — ``exclude_pre_anchor(None)`` would make ALL history
    matchable). Execution re-checks via ``create_session`` itself; this function
    exists so a doomed draft never reaches the approval queue.

    ``period_start`` is accepted for signature parity with ``create_session``'s
    inputs (the draft carries it); the guards themselves key on ``period_end``
    exactly as ``create_session`` does.
    """
    del period_start  # parity with the create_session input set; guards key on period_end
    existing_open = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.realm_id == realm_id,
            ReconciliationSession.account_id == account_id,
            open_session_status_clause(ReconciliationSession.status),
        )
        .first()
    )
    if existing_open is not None:
        raise OpenSessionExistsError(existing_open.id)

    anchor = clean_start.get_anchor(db, realm_id, account_id)
    if (
        anchor.configured
        and anchor.opening_reconciled_date is not None
        and period_end <= anchor.opening_reconciled_date
    ):
        raise PeriodAlreadyReconciledError(account_id, anchor.opening_reconciled_date)

    if clean_start.match_start_date(db, realm_id, account_id) is None:
        raise CleanStartNotConfiguredError(account_id)


def serialize_unmatched(
    result: MatchResult,
    *,
    side: str,
    cursor: int = 0,
    limit: int = DEFAULT_UNMATCHED_PAGE,
) -> dict:
    """One stable id-cursor page over an unmatched bucket (decision 6 / E9).

    ``side`` is ``"bank"`` (statement lines with no book counterpart, each with
    its ``why_not_matched`` diagnostic) or ``"book"`` (outstanding book txns).
    Rows are ordered by their item id; the page starts strictly AFTER ``cursor``
    (the last id of the prior page). ``limit`` clamps to 1..MAX_UNMATCHED_PAGE.
    """
    limit = max(1, min(int(limit or DEFAULT_UNMATCHED_PAGE), MAX_UNMATCHED_PAGE))
    cursor = int(cursor or 0)

    if side == "book":
        rows = sorted(result.in_books_not_bank, key=lambda r: r.book_txn_id)
        window = [r for r in rows if r.book_txn_id > cursor][:limit]
        items = [
            {
                "book_txn_id": r.book_txn_id,
                "amount": str(r.amount) if r.amount is not None else None,
                "date": r.txn_date.isoformat() if r.txn_date is not None else None,
                "description": r.description or "",
            }
            for r in window
        ]
        last_id = window[-1].book_txn_id if window else cursor
        has_more = any(r.book_txn_id > last_id for r in rows)
    else:
        rows = sorted(result.in_bank_not_books, key=lambda r: r.bank_item_id)
        window = [r for r in rows if r.bank_item_id > cursor][:limit]
        items = [
            {
                "bank_item_id": r.bank_item_id,
                "amount": str(r.amount) if r.amount is not None else None,
                "date": r.txn_date.isoformat() if r.txn_date is not None else None,
                "description": r.description or "",
                "why_not_matched": r.why_not_matched,
                "why_detail": r.why_detail,
            }
            for r in window
        ]
        last_id = window[-1].bank_item_id if window else cursor
        has_more = any(r.bank_item_id > last_id for r in rows)

    return {
        "side": side,
        "total_unmatched": len(rows),
        "limit": limit,
        "items": items,
        "next_cursor": last_id if has_more else None,
        "has_more": has_more,
    }


@dataclass(frozen=True)
class BulkLiveness:
    """Per-pair liveness verdicts for one stored pair list (E5).

    ``valid`` pairs may confirm; the two skip lists carry ``(bank_item_id,
    book_txn_id)`` tuples under the shared skip vocabulary. Session-state
    (``skipped_session_closed``) is the CALLER's check — it is session-level,
    not pair-level, so it never reaches this classifier.
    """

    valid: list[tuple[int, int]] = field(default_factory=list)
    skipped_stale: list[tuple[int, int]] = field(default_factory=list)
    skipped_cleared_elsewhere: list[tuple[int, int]] = field(default_factory=list)


def check_bulk_pairs_liveness(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    pairs: list[tuple[int, int]],
) -> BulkLiveness:
    """Classify every stored ``(bank_item_id, book_txn_id)`` pair with INDEXED reads (E5).

    Three batched queries total (never per-pair): book rows must be in the exact
    account/anchored window, bank rows must still be in this session's frozen
    relation (including controlled provenance), and APPROVED claims are checked
    on BOTH bank and book identities. This is a cheap user-facing preflight; the
    locked ``confirm_match`` mutator independently repeats every invariant.

    Missing row (either side) → ``skipped_stale``; cleared/claimed →
    ``skipped_cleared_elsewhere``; otherwise valid. This function reads only —
    confirming the valid subset is the caller's explicit act.
    """
    book_ids = [book_id for _, book_id in pairs]
    bank_ids = [bank_id for bank_id, _ in pairs]
    anchor_date = clean_start.match_start_date(db, realm_id, session.account_id)

    book_query = db.query(QBOTransaction).filter(
        QBOTransaction.realm_id == realm_id,
        QBOTransaction.id.in_(book_ids),
        QBOTransaction.bank_account_ref == session.account_id,
        QBOTransaction.date <= session.period_end,
    )
    if anchor_date is None:
        book_rows: dict[int, QBOTransaction] = {}
    else:
        book_rows = {
            row.id: row
            for row in book_query.filter(QBOTransaction.date > anchor_date).all()
        }

    bank_query = (
        db.query(BankFeedItem.id)
        .join(
            ReconciliationSessionStatementLine,
            and_(
                ReconciliationSessionStatementLine.bank_feed_item_id
                == BankFeedItem.id,
                ReconciliationSessionStatementLine.realm_id == BankFeedItem.realm_id,
            ),
        )
        .filter(
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.id.in_(bank_ids),
            BankFeedItem.source == "statement_upload",
            BankFeedItem.bank_account_ref == session.account_id,
            BankFeedItem.txn_date <= session.period_end,
            ReconciliationSessionStatementLine.realm_id == realm_id,
            ReconciliationSessionStatementLine.reconciliation_session_id
            == session.id,
            ReconciliationSessionStatementLine.statement_import_id
            == session.statement_import_id,
            ReconciliationSessionStatementLine.lineage_status
            == session.lineage_status,
        )
    )
    if anchor_date is not None:
        bank_query = bank_query.filter(BankFeedItem.txn_date > anchor_date)
    if session.lineage_status == "controlled":
        bank_query = bank_query.filter(
            BankFeedItem.statement_import_id
            == ReconciliationSessionStatementLine.statement_import_id,
            BankFeedItem.parse_chunk_id
            == ReconciliationSessionStatementLine.parse_chunk_id,
            BankFeedItem.source_ordinal
            == ReconciliationSessionStatementLine.source_ordinal,
            BankFeedItem.lineage_status == "controlled",
        )
    bank_present = {row_id for (row_id,) in bank_query.all()}

    approved_claims = (
        db.query(ReconciliationMatch)
        .filter(
            ReconciliationMatch.realm_id == realm_id,
            ReconciliationMatch.status == "APPROVED",
            or_(
                ReconciliationMatch.bank_txn_id.in_([str(b) for b in bank_ids]),
                ReconciliationMatch.book_txn_local_id.in_(book_ids),
            ),
        )
        .all()
    )
    claimed_bank_refs = {row.bank_txn_id for row in approved_claims}
    claimed_book_ids = {
        row.book_txn_local_id
        for row in approved_claims
        if row.book_txn_local_id is not None
    }

    verdicts = BulkLiveness()
    request_bank_ids: set[int] = set()
    request_book_ids: set[int] = set()
    for bank_id, book_id in pairs:  # SA-OK: bounded-loop ≤40 pairs classified against the three pre-fetched maps — no query inside
        book = book_rows.get(book_id)
        if book is None or bank_id not in bank_present:
            verdicts.skipped_stale.append((bank_id, book_id))
            continue
        if (
            book.cleared
            or book.cleared_session_id is not None
            or str(bank_id) in claimed_bank_refs
            or book_id in claimed_book_ids
            or bank_id in request_bank_ids
            or book_id in request_book_ids
        ):
            verdicts.skipped_cleared_elsewhere.append((bank_id, book_id))
            continue
        verdicts.valid.append((bank_id, book_id))
        request_bank_ids.add(bank_id)
        request_book_ids.add(book_id)
    return verdicts


__all__ = [
    "MAX_BULK_PAIRS",
    "DEFAULT_UNMATCHED_PAGE",
    "MAX_UNMATCHED_PAGE",
    "SKIP_STALE",
    "SKIP_CLEARED_ELSEWHERE",
    "SKIP_SESSION_CLOSED",
    "BulkLiveness",
    "check_session_openable",
    "serialize_unmatched",
    "check_bulk_pairs_liveness",
]

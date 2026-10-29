"""
Group-aware duplicate pairing — the PRE-FUZZY stage over identical-amount clusters.

Chunk M2 of plan 20260716-batch5-remediation (AC-M2), designed in
work/pilot-fixes-batch-5/research/20260716-post-remediation-regressions/SYNTHESIS.md
§R-Dim-3 (post-crit-eval findings 5 + 6). The prod shape: 2–3 identical
$241.98/day book deposits vs statement lines — the greedy newest-first EXACT
pass could pair cross-day twins, re-assign confirmed rows on recompute, and the
leftover twin's ``no_candidate_in_window`` diagnosis was misleading. This stage
runs BEFORE the deterministic/fuzzy passes and, per identical-amount
direction-compatible window-connected cluster with duplicates:

  1. PINS already-confirmed pairs first via the keyed ``ReconciliationMatch``
     identity (``session_id`` + ``book_txn_local_id``, migration c9f4b2e7a1d6) —
     a confirmed association is REPORTED, never re-derived, so it survives any
     pool shift (crit-eval finding 6). Pinned pairs are exempt from the window
     bound: they record a human decision (manual confirms never had a window).
  2. Pairs the remaining rows by a date-ASC/id-ASC positional TWO-POINTER with
     EVERY derived pair individually bounded to ``window_days`` (crit-eval
     finding 5: transitive cluster chaining can exceed the window — a member
     that cannot pair inside the bound falls to the remainder). The two-pointer
     yields the maximum-cardinality window-bounded assignment for points on a
     date line and is deterministic, so recomputes are idempotent.
  3. Returns the |N−M| remainder per side; match.py stamps the bank-side
     remainder with the teaching why-code ``duplicate_group_remainder`` and
     routes the book-side remainder to the outstanding bucket. ALL cluster
     members (paired + remainder) are consumed — excluded from the EXACT and
     fuzzy passes (a leftover duplicate must never be fuzzy cross-wired weeks
     away on a homogeneous memo).

Cluster construction: bank rows bucket by their SIGNED amount; each book row
joins the bucket it is ``_amounts_match``-compatible with (the REAL predicate,
injected — zero drift risk). That predicate admits exactly one sign per book
row (its known direction, else its stored sign), so an Expense +300 joins the
bank −300 bucket and never the +300 deposit bucket. Within a
bucket, members sort by date and split where a consecutive gap exceeds
``window_days`` (for points on a line this IS the transitive window closure).
A cluster qualifies as a duplicate group iff BOTH sides are non-empty and at
least one side has ≥2 members; 1×1 clusters (and one-sided ones) stay with the
ordinary passes byte-identically, keeping their honest diagnostics.

Zero-amount and NULL-amount/NULL-date rows never reach this module (match.py
excludes $0.00 lines from ALL auto-pairing upstream; None dates cannot be
window-connected).

Layering: pure computation over in-memory rows plus ONE batched keyed pin
query. Injects ``amounts_match`` from match.py (the
diagnostics.py zero-import-coupling precedent) and returns detached row
references; match.py stamps match types and why-codes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ...models import BankFeedItem, QBOTransaction, ReconciliationMatch, ReconciliationSession


@dataclass(frozen=True)
class GroupPair:
    """One group-stage pairing. `pinned` = reported from a persisted APPROVED
    ReconciliationMatch (a human decision), not derived by the two-pointer."""

    bank: BankFeedItem
    book: QBOTransaction
    pinned: bool = False


@dataclass(frozen=True)
class GroupRemainderBank:
    """A bank-side |N−M| leftover: the row plus its cluster's side counts so the
    caller can render an honest teaching detail ("one of N identical lines;
    only M matching book entries in this window")."""

    row: BankFeedItem
    cluster_bank_count: int
    cluster_book_count: int


@dataclass
class GroupStageResult:
    """Everything the group stage decided. `consumed_*_ids` cover EVERY member
    of every qualifying cluster (paired AND remainder) — match.py removes them
    from the pools the EXACT/fuzzy passes run over."""

    pairs: list[GroupPair] = field(default_factory=list)
    bank_remainder: list[GroupRemainderBank] = field(default_factory=list)
    book_remainder: list[QBOTransaction] = field(default_factory=list)
    consumed_bank_ids: set[int] = field(default_factory=set)
    consumed_book_ids: set[int] = field(default_factory=set)


def _within_window(a: date | None, b: date | None, window_days: int) -> bool:
    """Local twin of match._within_window (zero import coupling back)."""
    if a is None or b is None:
        return False
    return abs((a - b).days) <= window_days


def _clusters(members: list[tuple[date, object]], window_days: int) -> list[list[object]]:
    """Split date-sorted members into window-connected components.

    For points on a date line the transitive closure of |a−b| ≤ window_days is
    exactly: sort by date, break where a consecutive gap exceeds window_days.
    """
    if not members:
        return []
    members = sorted(members, key=lambda m: m[0])
    clusters: list[list[object]] = [[members[0][1]]]
    prev_date = members[0][0]
    for member_date, row in members[1:]:
        if (member_date - prev_date).days > window_days:
            clusters.append([])
        clusters[-1].append(row)
        prev_date = member_date
    return clusters


def run_group_stage(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    bank_rows: list[BankFeedItem],
    book_rows: list[QBOTransaction],
    window_days: int,
    amounts_match: Callable[[Decimal | None, QBOTransaction], bool],
) -> GroupStageResult:
    """Run the group stage; see the module docstring for the full contract."""
    result = GroupStageResult()

    # ── 1. Bank buckets by SIGNED amount (dated, non-null, non-zero rows only) ──
    bank_buckets: dict[Decimal, list[BankFeedItem]] = {}
    for bank in bank_rows:
        if bank.amount is None or bank.amount == 0 or bank.txn_date is None:
            continue
        bank_buckets.setdefault(bank.amount, []).append(bank)

    # Perf (AC-P1 / D-B6-3): index the bucket keys by ABSOLUTE amount so each book
    # row probes only same-magnitude keys, not every bucket — `amounts_match`
    # requires abs-equality (match.py:236/238), so a different-magnitude key was
    # never compatible. Turns the O(book × distinct-amount) assignment scan into
    # O(book × ≤2 signed variants). Behavior-preserving: `compatible` stays the same
    # keys in the same insertion order (a non-abs key would have failed amounts_match).
    keys_by_abs: dict[Decimal, list[Decimal]] = {}
    for _key in bank_buckets:
        keys_by_abs.setdefault(abs(_key), []).append(_key)

    # ── 2. Assign each book row to ONE compatible bucket (dir-preferred) ────
    book_buckets: dict[Decimal, list[QBOTransaction]] = {key: [] for key in bank_buckets}
    for book in book_rows:
        if book.amount is None or book.amount == 0 or book.date is None:
            continue
        candidate_keys = keys_by_abs.get(abs(book.amount), [])  # SA-OK: in-memory dict index, not a DB query — the batch-and-dict-index N+1 fix
        compatible = [key for key in candidate_keys if amounts_match(key, book)]
        if len(compatible) != 1:
            # None: no same-direction statement line. More than one cannot occur
            # under the one-sign predicate; if it ever did, leave the row to the
            # ordinary passes rather than guess its direction here.
            continue
        book_buckets[compatible[0]].append(book)

    # ── 3. Window-connected clusters per bucket; qualify the duplicate groups ──
    qualifying: list[tuple[list[BankFeedItem], list[QBOTransaction]]] = []
    for key, bucket_banks in bank_buckets.items():
        members: list[tuple[date, object]] = [(b.txn_date, b) for b in bucket_banks]
        members += [(k.date, k) for k in book_buckets[key]]
        for cluster in _clusters(members, window_days):
            cluster_banks = [m for m in cluster if isinstance(m, BankFeedItem)]
            cluster_books = [m for m in cluster if isinstance(m, QBOTransaction)]
            # Duplicate group ⟺ both sides present AND one side has ≥2 members.
            # 1×1 / one-sided clusters stay with the ordinary passes (their
            # existing behavior + diagnostics are byte-identical).
            if cluster_banks and cluster_books and (len(cluster_banks) >= 2 or len(cluster_books) >= 2):
                qualifying.append((cluster_banks, cluster_books))

    if not qualifying:
        return result

    # ── 4. ONE batched keyed pin query across every qualifying cluster ─────
    all_bank_refs = {str(b.id) for banks, _ in qualifying for b in banks}
    all_book_ids = {k.id for _, books in qualifying for k in books}
    pinned_rows = (
        db.query(ReconciliationMatch)
        .filter(
            ReconciliationMatch.realm_id == realm_id,
            ReconciliationMatch.session_id == session.id,
            ReconciliationMatch.status == "APPROVED",
            ReconciliationMatch.bank_txn_id.in_(all_bank_refs),
            ReconciliationMatch.book_txn_local_id.in_(all_book_ids),
        )
        .all()
    )
    pins: dict[int, int] = {}  # book_txn_local_id -> bank_item_id
    for row in pinned_rows:
        try:
            pins[row.book_txn_local_id] = int(row.bank_txn_id)
        except (TypeError, ValueError):
            continue  # a non-statement bank_txn_id shape can never pin a statement line

    # ── 5. Per cluster: pin first, then the window-bounded two-pointer ─────
    for cluster_banks, cluster_books in qualifying:
        n_bank, n_book = len(cluster_banks), len(cluster_books)
        result.consumed_bank_ids.update(b.id for b in cluster_banks)
        result.consumed_book_ids.update(k.id for k in cluster_books)

        banks_by_id = {b.id: b for b in cluster_banks}
        pinned_bank_ids: set[int] = set()
        pinned_book_ids: set[int] = set()
        for book in cluster_books:
            if book.id not in pins:
                continue
            bank_id = pins[book.id]
            if bank_id not in banks_by_id or bank_id in pinned_bank_ids:
                continue
            result.pairs.append(GroupPair(bank=banks_by_id[bank_id], book=book, pinned=True))
            pinned_bank_ids.add(bank_id)
            pinned_book_ids.add(book.id)

        free_banks = sorted(
            (b for b in cluster_banks if b.id not in pinned_bank_ids),
            key=lambda b: (b.txn_date, b.id),
        )
        free_books = sorted(
            (k for k in cluster_books if k.id not in pinned_book_ids),
            key=lambda k: (k.date, k.id),
        )
        i = j = 0
        while i < len(free_banks) and j < len(free_books):
            bank, book = free_banks[i], free_books[j]
            if _within_window(bank.txn_date, book.date, window_days):
                result.pairs.append(GroupPair(bank=bank, book=book, pinned=False))
                i += 1
                j += 1
            elif bank.txn_date < book.date:
                # Books are date-ASC: this bank row can never reach a later
                # book inside the bound → remainder (the finding-5 window drop).
                result.bank_remainder.append(GroupRemainderBank(bank, n_bank, n_book))
                i += 1
            else:
                result.book_remainder.append(book)
                j += 1
        result.bank_remainder.extend(
            GroupRemainderBank(b, n_bank, n_book) for b in free_banks[i:]
        )
        result.book_remainder.extend(free_books[j:])

    return result

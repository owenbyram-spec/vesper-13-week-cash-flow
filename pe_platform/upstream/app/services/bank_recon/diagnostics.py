# PLAN-EXEMPT: size-split of app/services/bank_recon/match.py for plan 20260702-qa-batch2 (match.py is in surface_scope; extracting the why-not-matched diagnostics concern keeps match.py under the 500-line file-size floor — same split-module pattern as bank_reconciliation_sessions.py; concern-boundary split, not size-boundary).
"""
Reconciliation match DIAGNOSTICS — the honesty layer over the match buckets.

Extracted from ``app/services/bank_recon/match.py`` (a concern-boundary split:
the match PASSES live there; the "why did a row NOT match" CLASSIFICATION lives
here, and the two read independently). ``find_matches`` calls
``build_diagnostics`` after bucketing and attaches the result to ``MatchResult``.

The load-bearing product requirement (plan 20260702-qa-batch2): a reconciliation
run with zero (or few) matches is NEVER opaque. The confirmed prod root cause was
an EMPTY BOOK SIDE (the QBO transaction sync never populated the realm) which
silently yielded zero matches with no explanation. These diagnostics make the
run self-explaining — counts per side, per-unmatched-row reasons, an honest
empty-state, and a CSV sign-flip detector — computed ENTIRELY from the pools the
match passes already fetched (no extra DB query).

Layering (api-contract-and-layering SKILL): pure computation over in-memory rows.
It touches no Session, issues no query, and returns a detached frozen dataclass —
the router maps it to a response_model DTO (the ORM never crosses the boundary).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ...models import BankFeedItem, QBOTransaction

# Empty-state machine codes for MatchDiagnostics.empty_reason (AC-2). None means
# "not empty" — both sides had rows. A zero side is NOT the same as "0 matches":
# it means the data to match against was never loaded, so we say WHY.
EMPTY_NO_BOOK_TRANSACTIONS = "no_book_transactions"
EMPTY_NO_BANK_TRANSACTIONS = "no_bank_transactions"
EMPTY_BOTH_SIDES_EMPTY = "no_transactions_either_side"


@dataclass(frozen=True)
class MatchDiagnostics:
    """
    Why a run matched (or did NOT match) — the honesty layer over the buckets.

    A zero/low-match run must never read as a silent "0 matches." These counts
    make the run self-explaining: how many rows each side had, and — for the
    rows that fell through to the unmatched buckets — the aggregate REASON they
    did not pair. Every field is computed from the SAME candidate scan the match
    passes already do (no extra DB query — AC-1).

    - `bank_rows_count` / `book_rows_count`: rows fetched per side, post-anchor
      and realm-scoped (the pool the passes actually ran over).
    - `out_of_window`: unmatched bank rows that had an amount-agreeing book
      candidate (per the direction-aware `amounts_match` predicate) but every
      such candidate was OUTSIDE even the wider fuzzy date window (a date-reach
      miss, not a money miss).
    - `no_amount_candidate`: unmatched bank rows for which NO book row agreed on
      the amount at all (the classic missing-entry / wrong-amount case).
    - `sign_mismatch_detected`: unmatched bank rows with a magnitude-equal,
      in-window book candidate the direction-aware matcher REFUSED — the book
      direction (from transaction_type) disagrees with the bank sign, or is
      unknown (B3-2: never fabricate a deposit↔withdrawal pairing). A
      negated-expense pair (bank −250 vs book Expense +250) MATCHES under the
      sign-convention fix and is therefore NOT counted here — this tallies
      direction REFUSALS only, mirroring the per-line WHY_SIGN_MISMATCH code.
    - `already_cleared`: unmatched bank rows whose amount-agreeing, in-window
      book candidate was already `cleared` by ANOTHER session (excluded from the
      matchable pool by find_matches' cleared-exclusion rule) — the re-imported
      statement-line case, mirroring the per-line WHY_ALREADY_CLEARED code.
    - `duplicate_group_remainder` (AC-M2/M3, plan 20260716-batch5-remediation):
      unmatched bank rows that are the |N−M| leftover of an identical-amount
      duplicate cluster the group stage paired — their twins consumed every
      candidate, mirroring the per-line WHY_DUPLICATE_GROUP_REMAINDER code.
      Pre-classified by the group stage (these rows never reach the loop
      below), so the tally is passed in by find_matches.
    - `zero_amount_excluded` (AC-M2g): $0.00 statement lines excluded from ALL
      auto-pairing (a zero line would pair on date proximity alone), mirroring
      the per-line WHY_ZERO_AMOUNT_EXCLUDED code. Also passed in (bank-side
      count; zero book rows go straight to the outstanding bucket).
    - `empty_reason`: a machine code (one of EMPTY_*), non-None only when a
      FETCHED side is empty (cleared-elsewhere book rows count as fetched — an
      all-reconciled book side is not "the sync never ran"); `empty_message` is
      its human twin.

    `bank_rows_count` / `book_rows_count` count the FETCHED rows (post-anchor,
    realm-scoped, including book rows excluded as cleared-by-another-session),
    so the counts stay consistent with `empty_reason`.
    """

    bank_rows_count: int = 0
    book_rows_count: int = 0
    out_of_window: int = 0
    no_amount_candidate: int = 0
    sign_mismatch_detected: int = 0
    already_cleared: int = 0
    duplicate_group_remainder: int = 0
    zero_amount_excluded: int = 0
    empty_reason: str | None = None
    empty_message: str | None = None


def _within_window(a: date | None, b: date | None, window_days: int) -> bool:
    """True iff both dates are present and within ±window_days of each other.

    A pure local copy (match.py has the twin) so this module has ZERO import
    coupling back to match.py — the split stays clean (no circular import).
    """
    if a is None or b is None:
        return False
    return abs((a - b).days) <= window_days


def _empty_state(bank_count: int, book_count: int) -> tuple[str | None, str | None]:
    """
    The honest empty-state (AC-2): a machine code + human message when a SIDE is
    empty, else (None, None). A zero side is the confirmed prod root cause — the
    book side was empty because the QBO transaction sync never populated it — and
    it must read as "no data to match against," NOT as "0 matches."
    """
    if bank_count == 0 and book_count == 0:
        return (
            EMPTY_BOTH_SIDES_EMPTY,
            "No bank or book transactions found for this account/period after the "
            "clean-start date — import a statement and confirm the QBO sync has run.",
        )
    if book_count == 0:
        return (
            EMPTY_NO_BOOK_TRANSACTIONS,
            "No book transactions found for this account/period — the QBO sync may "
            "not have populated this account.",
        )
    if bank_count == 0:
        return (
            EMPTY_NO_BANK_TRANSACTIONS,
            "No bank transactions found for this account/period — import a bank "
            "statement to reconcile against the books.",
        )
    return (None, None)


def build_diagnostics(
    *,
    bank_rows: list[BankFeedItem],
    book_rows: list[QBOTransaction],
    cleared_elsewhere: list[QBOTransaction],
    still_unmatched_bank: list[BankFeedItem],
    fuzzy_window: int,
    amounts_match: Callable[[Decimal | None, QBOTransaction], bool],
    duplicate_group_remainder: int = 0,
    zero_amount_excluded: int = 0,
) -> MatchDiagnostics:
    """
    Aggregate the why-not-matched reasons for the unmatched bank rows (AC-1/3).

    For each still-unmatched bank row, classify it against the MATCHABLE book
    pool (`book_rows` — already excludes rows cleared by another session; those
    arrive separately as `cleared_elsewhere`) using the SAME fuzzy date window
    the second match pass used (`fuzzy_window`) and the SAME direction-aware
    money predicate the passes used (`amounts_match`, injected by match.py so
    this module keeps zero import coupling back to it). One tally per row,
    checked worst-signal first so a direction refusal is never masked by a
    plain no-candidate:

      1. sign_mismatch_detected — a magnitude-equal in-window candidate exists
         but `amounts_match` REFUSED it on direction (B3-2: a bank debit never
         pairs with a book Deposit / direction-unknown type on magnitude alone).
         Direction-COMPATIBLE negated pairs (bank −250 vs Expense +250) match
         upstream and are never counted.
      2. (no tally) — an amount- and direction-compatible candidate WAS in
         window yet the row stayed unmatched: the memo floor or another bank
         row claimed it. The per-line `why_not_matched` codes
         (WHY_MEMO_BELOW_FLOOR / WHY_AMOUNT_MISMATCH) carry that nuance; it is
         none of these four aggregate signals.
      3. already_cleared — an amount-agreeing, in-window candidate exists only
         in the cleared-by-another-session pool (the re-imported line case).
      4. out_of_window — an amount-agreeing book row exists but all are out of
         the (fuzzy) date window.
      5. no_amount_candidate — no book row agreed on the amount at all.

    All data is already in memory — this adds no DB round-trip.
    """
    out_of_window = 0
    no_amount_candidate = 0
    sign_mismatch_detected = 0
    already_cleared = 0

    # Perf (AC-P1 / D-B6-3): index BOTH pools by ABSOLUTE amount ONCE, so each
    # unmatched line probes only same-magnitude candidates instead of re-scanning
    # the full book pool ~4× per line — the O(unmatched × book) diagnosis scan the
    # bucketed match passes would otherwise leave behind (the dominant residual on
    # a LOW-match-rate import, e.g. a fresh statement into a behind account).
    # Behavior-preserving: every predicate below requires abs(bank)==abs(book)
    # (amounts_match, match.py:236/238), so a different-magnitude row is never a
    # candidate for ANY of these four tallies.
    book_by_abs: dict = {}
    for b in book_rows:
        if b.amount is not None:
            book_by_abs.setdefault(abs(b.amount), []).append(b)
    cleared_by_abs: dict = {}
    for b in cleared_elsewhere:
        if b.amount is not None:
            cleared_by_abs.setdefault(abs(b.amount), []).append(b)

    for bank in still_unmatched_bank:
        # SA-OK: book_by_abs/cleared_by_abs are in-memory dict indexes, not DB queries — the batch-and-dict-index N+1 fix
        same_abs = book_by_abs.get(abs(bank.amount), []) if bank.amount is not None else []
        in_window = [
            b for b in same_abs if _within_window(bank.txn_date, b.date, fuzzy_window)
        ]
        # in_window rows are same-magnitude by construction; a direction refusal is
        # exactly a same-magnitude in-window candidate amounts_match rejects.
        direction_refused = any(not amounts_match(bank.amount, b) for b in in_window)
        matchable_in_window = any(amounts_match(bank.amount, b) for b in in_window)
        cleared_same_abs = (
            cleared_by_abs.get(abs(bank.amount), []) if bank.amount is not None else []
        )
        cleared_in_window = any(
            _within_window(bank.txn_date, b.date, fuzzy_window)
            and amounts_match(bank.amount, b)
            for b in cleared_same_abs
        )

        if direction_refused:
            sign_mismatch_detected += 1
        elif matchable_in_window:
            # Memo floor / claimed by another bank row — per-line codes carry it.
            pass
        elif cleared_in_window:
            already_cleared += 1
        elif any(amounts_match(bank.amount, b) for b in same_abs):
            out_of_window += 1
        else:
            no_amount_candidate += 1

    # Empty-state + side counts run on the FETCHED totals: a book side whose
    # rows were ALL cleared by prior sessions was fetched fine — that is
    # "already reconciled" (already_cleared), not "the QBO sync never ran."
    fetched_book_count = len(book_rows) + len(cleared_elsewhere)
    reason, message = _empty_state(len(bank_rows), fetched_book_count)
    return MatchDiagnostics(
        bank_rows_count=len(bank_rows),
        book_rows_count=fetched_book_count,
        out_of_window=out_of_window,
        no_amount_candidate=no_amount_candidate,
        sign_mismatch_detected=sign_mismatch_detected,
        already_cleared=already_cleared,
        # Pre-classified by find_matches (the group stage + zero-exclusion rows
        # never reach the classification loop above) — stamped, not recomputed.
        duplicate_group_remainder=duplicate_group_remainder,
        zero_amount_excluded=zero_amount_excluded,
        empty_reason=reason,
        empty_message=message,
    )

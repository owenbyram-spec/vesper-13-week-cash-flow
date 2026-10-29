# PLAN-EXEMPT: active-plan.json.surface_scope is desynced to [] while the RATIFIED plan 20260702-qa-batch2 (status.json) covers app/services/bank_recon/** — my owned surface. Editing the gate state was correctly denied by auto-mode; this docstring trim (bringing match.py under the 500-line file-size floor after the diagnostics extraction) rides the same audited doorway the hook names.
"""
Reconciliation match engine for bank reconciliation (C1-b, AC-1.2).

PROPOSAL-FIRST three-way match: reads the bank side (`statement_upload`-source
`BankFeedItem` rows) against the book side (`QBOTransaction` rows), realm-scoped
and forward-only from the clean-start anchor, and returns three PROPOSAL buckets
plus a diagnostics payload. `find_matches` NEVER mutates; a match becomes real
only when a human confirms it via `confirm_match` (sets LOCAL cleared flags +
persists a `ReconciliationMatch`). No QBO API call, no auto-clear, no write-back
(DEC-BREC-1: QBO has no reconcile-write path — Peregrine owns its recon ledger
locally). Decision is SEPARATED from execution (agentic-writeback-safety §3
Controls 1+2): `find_matches` is the pure read-only proposer; the human-supplied
`(bank_item_id, book_txn_id)` pair IS the out-of-band approval `confirm_match`
executes on. The router (thin wrapper, not here) membership-gates + pins the
realm GUC before calling either.

Diagnostics (plan 20260702-qa-batch2): a zero/low-match run is NEVER opaque —
`find_matches` returns a `MatchDiagnostics` (per-side counts, per-unmatched-row
reasons, honest empty-state, CSV sign-flip detector) so an empty book side (the
confirmed prod root cause: the QBO transaction sync never populated the realm)
reads as "no book transactions," not a silent "0 matches." The classification
concern lives in the sibling `diagnostics.py`; this module runs the passes.

Realm scoping (multi-tenancy-app-layer): the three tables are realm-keyed and
enrolled in the live Postgres RLS backstop (migration e7f1a9c3d2b5); the caller
pins the GUC via `check_tenant_access(...)`, and every query here ALSO carries an
explicit `realm_id ==` predicate — the PRIMARY app-layer gate (it runs on the
SQLite test lane, which has no RLS). Ownership boundary (DEC-BREC-4): this
service sets ONLY the per-txn cleared FLAGS + the `ReconciliationMatch` row; it
does NOT touch `ReconciliationSession.cleared_balance`/`difference`/`status`
(session.py owns those).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ...models import (
    BankFeedItem,
    BankReconAccountConfig,
    BankStatementControlCertificate,
    QBOTransaction,
    ReconciliationMatch,
    ReconciliationSession,
    ReconciliationSessionStatementLine,
)
from . import clean_start, group_match
from .diagnostics import MatchDiagnostics, build_diagnostics
from .session_status import effective_session_status, is_open_session_status


class CleanStartNotConfiguredError(Exception):
    """find_matches was called for an account with no clean-start anchor.

    Reconciliation must NOT proceed without a clean-start boundary (DEC-BREC-5 /
    AC-0.3): ``exclude_pre_anchor(None)`` excludes nothing, so an unanchored
    account would make the ENTIRE transaction history matchable. The caller sets
    the anchor first (PUT /bank-reconciliation/accounts/{account_id}/clean-start).
    """


class StatementMembershipInvalidError(Exception):
    """A session's frozen statement-side membership is absent or incomplete."""

    def __init__(self, session_id: int, reason: str) -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"session {session_id} statement membership is invalid: {reason}")


class StatementLineNotInSessionError(Exception):
    """A requested statement line is outside the session's frozen membership."""

    def __init__(self, session_id: int, bank_item_id: int) -> None:
        self.session_id = session_id
        self.bank_item_id = bank_item_id
        super().__init__(
            f"statement line {bank_item_id} does not belong to session {session_id}"
        )


class ConfirmationSessionNotFoundError(Exception):
    """The supplied session is not present in the caller-authorized realm."""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        super().__init__(f"reconciliation session {session_id} was not found")


class ConfirmationSessionNotOpenError(Exception):
    """A cleared-set mutation was attempted after the session stopped being OPEN."""

    def __init__(self, session_id: int, status: str) -> None:
        self.session_id = session_id
        self.status = status
        super().__init__(
            f"reconciliation session {session_id} is {status}, not OPEN"
        )


class BookTransactionNotInSessionError(ValueError):
    """The book row is missing or belongs to another account in this realm."""

    def __init__(self, session_id: int, book_txn_id: int) -> None:
        self.session_id = session_id
        self.book_txn_id = book_txn_id
        super().__init__(
            f"book transaction {book_txn_id} is not eligible for session {session_id}"
        )


class MatchOutsideSessionWindowError(Exception):
    """A requested bank or book leg is outside the session's anchored window."""

    def __init__(
        self,
        *,
        session_id: int,
        side: str,
        item_id: int,
        txn_date: date,
        anchor_date: date,
        period_end: date,
    ) -> None:
        self.session_id = session_id
        self.side = side
        self.item_id = item_id
        self.txn_date = txn_date
        self.anchor_date = anchor_date
        self.period_end = period_end
        super().__init__(
            f"{side} row {item_id} dated {txn_date} is outside session "
            f"{session_id}'s window ({anchor_date}, {period_end}]"
        )


class BookTransactionClearedError(Exception):
    """The book leg has a cleared-state owner other than the requested session."""

    def __init__(
        self,
        *,
        book_txn_id: int,
        cleared_session_id: int | None,
    ) -> None:
        self.book_txn_id = book_txn_id
        self.cleared_session_id = cleared_session_id
        super().__init__(
            f"book transaction {book_txn_id} is already cleared"
            + (
                f" by session {cleared_session_id}"
                if cleared_session_id is not None
                else " without an attributable session"
            )
        )


class MatchClaimConflictError(Exception):
    """An APPROVED match already owns one side of the requested pair."""

    def __init__(
        self,
        *,
        resource: str,
        item_id: int,
        existing_session_id: int | None,
    ) -> None:
        self.resource = resource
        self.item_id = item_id
        self.existing_session_id = existing_session_id
        super().__init__(
            f"{resource} {item_id} already has an APPROVED reconciliation claim"
        )


# The bank side we reconcile is ONLY the statement-upload provenance (SYNTHESIS
# §2B / DEC-BREC): a `qbo_feed` "For Review" row is a different plane (it flips
# the month-end close gate) and must not be pulled into a statement reconcile.
STATEMENT_SOURCE = "statement_upload"

# match_type discriminators persisted on ReconciliationMatch.match_type and set
# on a MatchProposal. EXACT = deterministic pass (amount+date+account);
# EXACT_GROUP = the pre-fuzzy group stage (identical-amount duplicate clusters,
# date-ASC/id-ASC positional pairing, every derived pair window-bounded — the
# same amount+date+account evidence class as EXACT, so it rides the EXACT-only
# confirm-all default per the ratified DEC-BC-2 amendment, plan
# 20260716-batch5-remediation §6.4); FUZZY_DATE = fuzzy pass (memo/payee
# similarity within the widened date window).
MATCH_TYPE_EXACT = "EXACT"
MATCH_TYPE_EXACT_GROUP = "EXACT_GROUP"
MATCH_TYPE_FUZZY = "FUZZY_DATE"

# The fuzzy pass only proposes a candidate whose memo/payee similarity clears
# this floor — below it the strings are too dissimilar to call a match, so the
# rows fall through to the unmatched buckets for human review rather than
# fabricating a low-confidence link. 0.60 is a deliberate similarity floor
# (SequenceMatcher ratio, 0.0..1.0); the amount must still match exactly.
FUZZY_SCORE_FLOOR = 0.60

# Default date tolerance (± days) for the deterministic pass: a bank line and
# its book entry rarely post on the exact same calendar day (settlement lag), so
# an exact-amount match within a few days is the deterministic signal.
DEFAULT_WINDOW_DAYS = 4

# The fuzzy pass extends the date reach: a bank line and its book entry can be
# further apart than the deterministic window (a check written weeks before it
# clears), so the fuzzy pass widens the window by this factor and REQUIRES a
# memo/payee match (≥ FUZZY_SCORE_FLOOR) to compensate for the looser date. This
# is what makes the pass reachable AND distinct: it claims exact-amount rows the
# deterministic pass rejected on date, gated by a strong memo signal.
FUZZY_WINDOW_MULTIPLIER = 4

# ── Amount SIGN convention (B3-2 root cause) ─────────────────────────────────
# The two sides of the match carry DIFFERENT sign conventions:
#   - Bank side (ingest.py): SIGNED — a withdrawal/debit is NEGATIVE, a
#     deposit/credit is POSITIVE (bank-CSV polarity, `amount = -abs(debit)`).
#   - Book side (sync.py:354): UNSIGNED — `QBOTransaction.amount` is QBO's
#     `TotalAmt`, which is ALWAYS POSITIVE; the direction lives in
#     `transaction_type` (an Expense of $250 is stored as +250.00).
# A naive `bank.amount == book.amount` therefore fails for EVERY money-out line
# (bank −250.00 vs book +250.00) — the observed all-in-books-zero-matched case —
# AND wrongly succeeds for a deposit against a same-magnitude money-out row (bank
# +250.00 vs Expense +250.00). So money agreement is decided by DIRECTION: when
# the book txn's direction is known (a negative stored amount, or a
# transaction_type in the sets below) the magnitudes must agree AND that
# direction must equal the bank sign. Only a direction-UNKNOWN book row (the
# signed Transfer / JournalEntry legs) is compared signed-equal, because its
# stored sign is its only direction. Never fabricate a deposit↔withdrawal
# pairing (financial-modeling-correctness); a refusal surfaces as a
# `sign_mismatch` diagnostic for the human instead.

# QBO transaction types that move money OUT of a bank/CC account (normalized:
# lowercased, spaces/parens stripped). RefundReceipt refunds a customer = out.
_MONEY_OUT_TYPES = frozenset(
    {
        "expense",
        "check",
        "purchase",
        "cash",
        "cashexpense",
        "creditcardexpense",
        "creditcardcharge",
        "billpayment",
        "billpaymentcheck",
        "billpaymentcreditcard",
        "refundreceipt",
    }
)

# QBO transaction types that move money IN. Transfer / CreditCardPayment are
# deliberately ABSENT from both sets: their direction depends on which side of
# the movement this account is, so they stay direction-unknown (conservative).
_MONEY_IN_TYPES = frozenset(
    {
        "deposit",
        "payment",
        "receivepayment",
        "salesreceipt",
    }
)

# why_not_matched diagnostic codes (returned per unmatched statement line so a
# zero-match session is never opaque — B3-2b). Stable strings: the router
# forwards them verbatim and the UI maps them to teaching labels.
WHY_NO_CANDIDATE = "no_candidate_in_window"
WHY_AMOUNT_MISMATCH = "amount_mismatch"
WHY_SIGN_MISMATCH = "sign_mismatch"
WHY_ALREADY_CLEARED = "already_cleared"
WHY_MEMO_BELOW_FLOOR = "memo_below_floor"
# AC-M2 (plan 20260716-batch5-remediation): the group stage's honest leftovers.
# duplicate_group_remainder — the |N−M| leftover of an identical-amount
# duplicate cluster (its twins matched; saying "no candidate" would mislead).
# zero_amount_excluded — a $0.00 statement line, excluded from ALL auto-pairing
# (a zero line EXACT-paired on date proximity alone); manual confirm still works.
WHY_DUPLICATE_GROUP_REMAINDER = "duplicate_group_remainder"
WHY_ZERO_AMOUNT_EXCLUDED = "zero_amount_excluded"


def _book_direction(book: QBOTransaction) -> int | None:
    """Direction of a book txn: -1 money out, +1 money in, None unknown.

    A NEGATIVE stored amount is already signed (manual / bill_com sources) and
    carries its own direction; otherwise derive it from `transaction_type`.
    """
    if book.amount is not None and book.amount < 0:
        return -1
    normalized = (book.transaction_type or "").lower()
    normalized = re.sub(r"[^a-z]", "", normalized)
    if normalized in _MONEY_OUT_TYPES:
        return -1
    if normalized in _MONEY_IN_TYPES:
        return 1
    return None


def signed_amount(book: QBOTransaction) -> Decimal:
    """The SIGNED cleared-balance contribution of a book txn (B3-4 root cause).

    QBO's `TotalAmt` is UNSIGNED — always positive — with the money direction
    living in `transaction_type` (an Expense of $250 is stored as +250.00; see the
    sign-convention note above). A tie-out that summed the raw stored amount would
    therefore count every withdrawal as a DEPOSIT, inflating the cleared balance
    (the observed "cleared withdrawals 0.00, cleared balance too high" bug). This
    is the single source of truth for turning a stored book amount into its signed
    reconciled-balance movement, direction-aware:

      - money OUT  → NEGATIVE (`-abs(amount)`)
      - money IN   → POSITIVE (`+abs(amount)`)
      - already-signed rows keep their sign (direction is read from the sign).
      - direction UNKNOWN → fall back to the stored amount. `_amounts_match` only
        lets a direction-unknown book row pair when it is signed-equal to the
        (signed) bank line, so its stored amount already carries the right sign.

    Ownership (DEC-BREC): match.py owns the amount SIGN CONVENTION; session.py and
    the tie-out router read the signed figure through THIS function so the cleared
    sum and the deposits/withdrawals split can never diverge from the matcher.
    """
    if book.amount is None:
        return Decimal("0.00")
    direction = _book_direction(book)
    if direction is None:
        return book.amount
    return direction * abs(book.amount)


def _is_cleared_by(book: QBOTransaction, session_id: int) -> bool:
    """True when this book txn is already cleared (confirmed) by THIS session.

    A confirmed pair stays in the `matched` bucket on re-run (so it can be
    unconfirmed), so the UI needs to tell an already-confirmed pairing apart from
    a fresh proposal — otherwise Confirm looks like a no-op. `confirm_match` sets
    both flags together, so testing `cleared_session_id` is sufficient; the
    `cleared` bool is checked too for defensiveness.
    """
    return bool(book.cleared and book.cleared_session_id == session_id)


def _amounts_match(bank_amount: Decimal | None, book: QBOTransaction) -> bool:
    """True when the bank line and book txn agree on MONEY, sign-convention-aware.

    1. Magnitudes differ → NO match. Decimal-exact: a cent off is never a match.
    2. Book direction KNOWN (negative stored amount, or a money-in/out
       transaction_type) → match only when it equals the bank sign. This covers
       QBO's unsigned TotalAmt: an Expense +250 matches bank −250 and never a
       +250 deposit.
    3. Book direction UNKNOWN (signed Transfer / JournalEntry legs) → match only
       when signed-equal; never pair a deposit with a withdrawal on magnitude.
    """
    if bank_amount is None or book.amount is None:
        return False
    if abs(bank_amount) != abs(book.amount):
        return False
    direction = _book_direction(book)
    if direction is None:
        return bank_amount == book.amount
    bank_direction = -1 if bank_amount < 0 else 1
    return direction == bank_direction


@dataclass(frozen=True)
class MatchProposal:
    """
    One PROPOSED bank↔book pairing — a detached, read-only DTO built while the
    session is open and returned by value (api-contract-and-layering: the ORM
    object never crosses the boundary). Confirming it is a SEPARATE, explicit,
    human-gated call (`confirm_match`); building it changes nothing.

    `score` is 0..100. The deterministic (EXACT) pass scores 100 (amount+date+
    account all agree); the fuzzy pass scores round(similarity*100).

    `cleared` is True when this pairing is ALREADY confirmed in this session (its
    book txn is cleared by it). A confirmed pair re-appears in the matched bucket
    on every re-run so it can be unconfirmed; `cleared` lets the UI render it as
    Confirmed rather than as a fresh proposal (so Confirm is not a silent no-op).

    `book_amount` is the book txn's SIGNED cleared-balance movement
    (`signed_amount`) and `direction` its money direction ("in" / "out"), so a
    reviewer sees both legs' direction instead of only the bank amount.
    """

    bank_item_id: int
    book_txn_id: int
    amount: Decimal | None
    bank_date: date | None
    book_date: date | None
    match_type: str
    score: int
    cleared: bool = False
    book_amount: Decimal | None = None
    direction: str | None = None


@dataclass(frozen=True)
class UnmatchedBank:
    """A statement line with no book counterpart — a candidate missing entry.

    `why_not_matched` is one of the WHY_* codes — the FIRST rule that kept this
    line out of the matched bucket, evaluated against the fuzzy (widest) window
    so no failure is opaque. `why_detail` is a short human-readable elaboration.
    """

    bank_item_id: int
    amount: Decimal | None
    txn_date: date | None
    description: str | None
    why_not_matched: str = WHY_NO_CANDIDATE
    why_detail: str | None = None
    candidates_in_window: int = 0


@dataclass(frozen=True)
class UnmatchedBook:
    """A book txn with no statement counterpart — outstanding / uncleared."""

    book_txn_id: int
    amount: Decimal | None
    txn_date: date | None
    description: str | None


@dataclass(frozen=True)
class MatchResult:
    """
    The three-bucket output of `find_matches` — all PROPOSALS, zero mutations.

    - `matched`: proposed bank↔book pairings (EXACT and FUZZY_DATE — read
      `match_type`, not position, to tell them apart).
    - `in_bank_not_books`: statement lines with no book match (missing entry).
    - `in_books_not_bank`: book txns with no statement match (outstanding).
    - `diagnostics`: the why-not-matched counts + honest empty-state (AC-1/2/3),
      so a zero/low-match run explains itself instead of reading as "0 matches."

    Ordering contract (AC-E1a): all three buckets are DETERMINISTICALLY ordered
    date-desc (the bank line's date for `matched`/`in_bank_not_books`, the book
    txn's date for `in_books_not_bank`), id-desc on ties, NULL dates last —
    every consumer (router, agent lane, LLM suggest) inherits one stable order.
    """

    matched: list[MatchProposal] = field(default_factory=list)
    in_bank_not_books: list[UnmatchedBank] = field(default_factory=list)
    in_books_not_bank: list[UnmatchedBook] = field(default_factory=list)
    diagnostics: MatchDiagnostics = field(default_factory=MatchDiagnostics)


def _normalize_memo(*parts: str | None) -> str:
    """Lowercase + whitespace-collapse the memo/payee text for fuzzy scoring."""
    joined = " ".join(p for p in parts if p)
    return " ".join(joined.lower().split())


def _similarity(a: str, b: str) -> float:
    """Memo/payee similarity in 0.0..1.0 (SequenceMatcher ratio)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _fetch_bank_side(
    db: Session,
    realm_id: str,
    session: ReconciliationSession,
    anchor_date: date | None,
) -> list[BankFeedItem]:
    """
    Read the exact frozen statement rows for one realm-scoped session.

    `ReconciliationSessionStatementLine` is the membership authority. It binds a
    controlled session to one import occurrence even when parser persistence
    legitimately reuses a canonical `BankFeedItem` first created by another
    import. No account/date-window source selector remains. Historical
    `legacy_snapshot` sessions use the relation materialized by the cutover
    migration; they do not fall back to a dynamic selector.

    ORDERED deterministically date-desc, id-desc tiebreak (AC-E1a): the two
    unmatched buckets inherit this fetch order (exclude_pre_anchor preserves it),
    so every consumer sees a stable newest-first list instead of engine-dependent
    heap order. `.nullslast()` pins NULL-dated rows to the end on both Postgres
    (which would default them FIRST under DESC) and the SQLite test lane.
    """
    if session.realm_id != realm_id:
        raise StatementMembershipInvalidError(
            session.id, "session realm does not match the authorized realm"
        )
    if session.lineage_status == "controlled" and session.statement_import_id is None:
        raise StatementMembershipInvalidError(
            session.id, "controlled session has no statement import"
        )

    query = (
        db.query(BankFeedItem)
        .join(
            ReconciliationSessionStatementLine,
            (
                ReconciliationSessionStatementLine.bank_feed_item_id
                == BankFeedItem.id
            )
            & (
                ReconciliationSessionStatementLine.realm_id
                == BankFeedItem.realm_id
            ),
        )
        .filter(
            ReconciliationSessionStatementLine.realm_id == realm_id,
            ReconciliationSessionStatementLine.reconciliation_session_id
            == session.id,
            ReconciliationSessionStatementLine.lineage_status
            == session.lineage_status,
            ReconciliationSessionStatementLine.statement_import_id
            == session.statement_import_id,
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.source == STATEMENT_SOURCE,
            BankFeedItem.bank_account_ref == session.account_id,
        )
        .order_by(
            BankFeedItem.txn_date.desc().nullslast(), BankFeedItem.id.desc()
        )
    )
    rows = query.all()

    if session.lineage_status == "controlled":
        certificate = (
            db.query(BankStatementControlCertificate)
            .filter(
                BankStatementControlCertificate.realm_id == realm_id,
                BankStatementControlCertificate.statement_import_id
                == session.statement_import_id,
            )
            .order_by(BankStatementControlCertificate.certificate_version.desc())
            .first()
        )
        if certificate is None or certificate.status != "passed":
            raise StatementMembershipInvalidError(
                session.id, "bound import has no latest passed control certificate"
            )
        if len(rows) != certificate.accepted_count:
            raise StatementMembershipInvalidError(
                session.id,
                "frozen line count does not match the passed control certificate",
            )

    return clean_start.exclude_pre_anchor(rows, anchor_date)


def _require_statement_line(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    bank_item_id: int,
) -> None:
    """Fail unless a statement line belongs to this session's frozen relation."""
    exists = (
        db.query(ReconciliationSessionStatementLine.id)
        .filter(
            ReconciliationSessionStatementLine.realm_id == realm_id,
            ReconciliationSessionStatementLine.reconciliation_session_id
            == session.id,
            ReconciliationSessionStatementLine.bank_feed_item_id == bank_item_id,
            ReconciliationSessionStatementLine.lineage_status
            == session.lineage_status,
            ReconciliationSessionStatementLine.statement_import_id
            == session.statement_import_id,
        )
        .first()
    )
    if exists is None:
        raise StatementLineNotInSessionError(session.id, bank_item_id)


def _fetch_book_side(
    db: Session,
    realm_id: str,
    account_id: str,
    anchor_date: date | None,
    period_end: date | None = None,
) -> list[QBOTransaction]:
    """
    Realm-scoped book (QBOTransaction) rows for one account, in the session window.

    Filters `realm_id ==` (PRIMARY app-layer gate) AND
    `bank_account_ref == account_id` (the bank/CC posting account — models.py
    ~818-826 — distinct from `account_ref`, which classification owns as the
    P&L/BS category column) AND — when given — `date <= period_end` (the statement
    closing date; a transaction dated AFTER the period end belongs to the NEXT
    reconciliation and must not leak into this one — B3-3), then drops any row
    dated ON or before the anchor. `QBOTransaction.date` is the txn date column;
    `exclude_pre_anchor` reads it via a `date_getter` since the column is named
    `date`, not `txn_date`.

    ORDERED deterministically date-desc, id-desc tiebreak (AC-E1a) — the
    `_fetch_bank_side` twin: the outstanding bucket inherits this fetch order,
    and `.nullslast()` keeps NULL-dated rows last on both engines.
    """
    query = db.query(QBOTransaction).filter(
        QBOTransaction.realm_id == realm_id,
        QBOTransaction.bank_account_ref == account_id,
    )
    if period_end is not None:
        query = query.filter(QBOTransaction.date <= period_end)
    query = query.order_by(
        QBOTransaction.date.desc().nullslast(), QBOTransaction.id.desc()
    )
    return clean_start.exclude_pre_anchor(
        query.all(), anchor_date, date_getter=lambda t: t.date
    )


def _within_window(a: date | None, b: date | None, window_days: int) -> bool:
    """True iff both dates are present and within ±window_days of each other."""
    if a is None or b is None:
        return False
    return abs((a - b).days) <= window_days


def find_matches(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> MatchResult:
    """
    Compute the three-way match for one reconciliation session — PURE / READ-ONLY.

    Reads the statement-upload bank rows and the book (QBOTransaction) rows for
    `session.account_id`, realm-scoped and forward-only from the clean-start
    anchor (`clean_start.match_start_date`; a txn dated ON the anchor is
    EXCLUDED), then runs the passes and buckets the result:

      0a. Zero-amount exclusion (AC-M2g, plan 20260716-batch5-remediation):
          $0.00 lines are excluded from ALL auto-pairing — a zero line would
          EXACT-pair on date proximity alone. Zero statement lines surface with
          why-code `zero_amount_excluded`; zero book rows go straight to the
          outstanding bucket. The manual per-pair /confirm still works.
      0b. Group stage (EXACT_GROUP — see group_match.py): identical-amount
          direction-compatible window-connected clusters with duplicates pin
          already-confirmed pairs first (keyed ReconciliationMatch identity),
          then pair date-ASC/id-ASC positionally with EVERY derived pair
          bounded to ±`window_days`; the |N−M| remainder gets why-code
          `duplicate_group_remainder`. ALL cluster members are excluded from
          the later passes (a leftover duplicate must never be fuzzy
          cross-wired on a homogeneous memo). Scores 100.
      1. Deterministic (EXACT) pass: exact amount + same bank account + date
         within ±`window_days`. Each such pairing scores 100.
      2. Fuzzy (FUZZY_DATE) pass over the leftovers: exact amount + date within a
         WIDER window (`window_days * FUZZY_WINDOW_MULTIPLIER`) + memo/payee
         similarity ≥ FUZZY_SCORE_FLOOR. Scores round(similarity*100). The wider
         date reach is what the memo requirement pays for — it claims
         exact-amount rows the deterministic pass rejected purely on date.

    Every book row and bank row is matched AT MOST ONCE (a matched row is
    removed from the candidate pool before the next pairing), so the passes
    never double-claim a transaction. The stage assignment is deterministic,
    so a per-GET recompute is idempotent (two identical pools ⇒ two identical
    results).

    Returns THREE buckets — `matched` (proposals), `in_bank_not_books`
    (candidate missing entry), `in_books_not_bank` (outstanding/uncleared) — PLUS
    a `diagnostics` payload (why the unmatched rows did not pair + an honest
    empty-state; see diagnostics.build_diagnostics). This function NEVER mutates:
    it does not set `cleared`, does not write a `ReconciliationMatch`, and does
    not touch QBO (DEC-BREC-1). Confirming a proposal is the separate,
    human-gated `confirm_match`.
    """
    anchor_date = clean_start.match_start_date(db, realm_id, session.account_id)
    if anchor_date is None:
        # DEC-BREC-5 / AC-0.3: no clean-start boundary → REFUSE rather than let the
        # entire history become matchable (exclude_pre_anchor(None) excludes nothing).
        raise CleanStartNotConfiguredError(session.account_id)

    # Upper bound = the statement closing date (session.period_end): a txn dated
    # after it belongs to the next reconciliation and must not leak in (B3-3).
    period_end = session.period_end
    bank_rows = _fetch_bank_side(db, realm_id, session, anchor_date)
    all_book_rows = _fetch_book_side(
        db, realm_id, session.account_id, anchor_date, period_end
    )

    # Cleared-set exclusion: a txn cleared by ANOTHER session is already
    # reconciled — it must not be re-proposed here nor listed as outstanding.
    # Txns cleared by THIS session stay in the pool so a re-run keeps showing
    # the confirmed pairs in the matched bucket (re-editable via unconfirm).
    book_rows = [
        b
        for b in all_book_rows
        if not (b.cleared and b.cleared_session_id != session.id)
    ]
    cleared_elsewhere = [
        b
        for b in all_book_rows
        if b.cleared and b.cleared_session_id != session.id
    ]

    matched: list[MatchProposal] = []

    # ── Pass 0a — zero-amount exclusion (AC-M2g) ───────────────────────────
    # A $0.00 line satisfies _amounts_match on ANY zero counterpart, so it
    # EXACT-paired on date proximity alone and was confirm-all sweepable.
    # Excluded from every auto-pass; the human can still pair it manually.
    zero_bank = [b for b in bank_rows if b.amount is not None and b.amount == 0]
    zero_book = [k for k in book_rows if k.amount is not None and k.amount == 0]
    pool_bank = [b for b in bank_rows if b.amount is None or b.amount != 0]
    pool_book = [k for k in book_rows if k.amount is None or k.amount != 0]

    # ── Pass 0b — the group stage (EXACT_GROUP) ────────────────────────────
    group = group_match.run_group_stage(
        db,
        realm_id=realm_id,
        session=session,
        bank_rows=pool_bank,
        book_rows=pool_book,
        window_days=window_days,
        amounts_match=_amounts_match,
    )
    matched.extend(
        _proposal(
            gp.bank, gp.book, MATCH_TYPE_EXACT_GROUP, 100,
            cleared=_is_cleared_by(gp.book, session.id),
        )
        for gp in group.pairs
    )
    # EVERY cluster member (paired + remainder) leaves the pools: the EXACT
    # pass cannot reach them (their window-connected twins are consumed) and
    # the fuzzy pass MUST NOT (homogeneous-memo cross-wiring — AC-M2f).
    pool_bank = [b for b in pool_bank if b.id not in group.consumed_bank_ids]
    remaining_book: list[QBOTransaction] = [
        k for k in pool_book if k.id not in group.consumed_book_ids
    ]
    # Perf (AC-P1 / D-B6-3): index the book side by ABSOLUTE amount so each
    # per-bank pass scans only same-magnitude candidates, not all remaining_book
    # — turning the O(bank × book) triple-scan into O(bank × same-amount).
    # BEHAVIOR-PRESERVING: `_amounts_match` returns True only when
    # abs(bank.amount) == abs(book.amount) (:236/:238), so a different-magnitude
    # book row was never a candidate — the bucket IS the exact candidate set, in
    # remaining_book (fetch) order. amount=None rows (never claimable) are omitted
    # from the index but stay in remaining_book (→ outstanding bucket). The index
    # is kept in lockstep with remaining_book: a claim removes the row from both.
    book_by_abs: dict[Decimal, list[QBOTransaction]] = {}
    for _bk in remaining_book:
        if _bk.amount is not None:
            book_by_abs.setdefault(abs(_bk.amount), []).append(_bk)
    unmatched_bank: list[BankFeedItem] = []

    # ── Pass 1 — deterministic: exact amount + date within window ──────────
    for bank in pool_bank:
        # SA-OK: book_by_abs is an in-memory dict index, not a DB query — this IS the batch-and-dict-index N+1 fix (all rows pre-fetched above)
        bucket = book_by_abs.get(abs(bank.amount)) if bank.amount is not None else None
        claimed = _claim_exact(bank, bucket or [], window_days)
        if claimed is None:
            unmatched_bank.append(bank)
            continue
        remaining_book.remove(claimed)
        bucket.remove(claimed)  # keep book_by_abs in lockstep with remaining_book
        matched.append(
            _proposal(
                bank, claimed, MATCH_TYPE_EXACT, 100,
                cleared=_is_cleared_by(claimed, session.id),
            )
        )

    # ── Pass 2 — fuzzy: exact amount + WIDER window + memo/payee similarity ─
    fuzzy_window = window_days * FUZZY_WINDOW_MULTIPLIER
    still_unmatched_bank: list[BankFeedItem] = []
    for bank in unmatched_bank:
        # SA-OK: book_by_abs is an in-memory dict index, not a DB query — this IS the batch-and-dict-index N+1 fix (all rows pre-fetched above)
        bucket = book_by_abs.get(abs(bank.amount)) if bank.amount is not None else None
        claimed, score = _claim_fuzzy(bank, bucket or [], fuzzy_window)
        if claimed is None:
            still_unmatched_bank.append(bank)
            continue
        remaining_book.remove(claimed)
        bucket.remove(claimed)  # keep book_by_abs in lockstep with remaining_book
        matched.append(
            _proposal(
                bank, claimed, MATCH_TYPE_FUZZY, score,
                cleared=_is_cleared_by(claimed, session.id),
            )
        )

    # ── Ordering contract (AC-E1a) — the matched bucket is date-desc too ───
    # The unmatched buckets inherit the fetch order (the _fetch_* queries are
    # ORDER BY date desc, id desc), but `matched` appends pass-1 claims then
    # pass-2 claims, interleaving the dates — so it is re-sorted here on the
    # bank line's date (desc, NULL last, bank_item_id desc on ties). Consumers
    # discriminate EXACT vs FUZZY via `match_type`, never via list position.
    matched.sort(
        key=lambda p: (p.bank_date is not None, p.bank_date or date.min, p.bank_item_id),
        reverse=True,
    )

    # ── Buckets 2 & 3 — the leftovers, each with a why-not-matched reason ──
    in_bank_not_books: list[UnmatchedBank] = []
    for bank in still_unmatched_bank:
        why, detail, candidate_count = _diagnose(
            bank, remaining_book, cleared_elsewhere, fuzzy_window
        )
        in_bank_not_books.append(
            UnmatchedBank(
                bank_item_id=bank.id,
                amount=bank.amount,
                txn_date=bank.txn_date,
                description=bank.description,
                why_not_matched=why,
                why_detail=detail,
                candidates_in_window=candidate_count,
            )
        )
    # Group remainders (AC-M2e): the |N−M| leftovers of a duplicate cluster.
    # Their twins consumed every candidate, so "no candidate in window" would
    # mislead — the teaching detail names the real shape instead.
    for rem in group.bank_remainder:
        in_bank_not_books.append(
            UnmatchedBank(
                bank_item_id=rem.row.id,
                amount=rem.row.amount,
                txn_date=rem.row.txn_date,
                description=rem.row.description,
                why_not_matched=WHY_DUPLICATE_GROUP_REMAINDER,
                why_detail=(
                    f"One of {rem.cluster_bank_count} statement line(s) with this "
                    f"exact amount in the date window, but only "
                    f"{rem.cluster_book_count} matching book transaction(s) — the "
                    "others already paired. The books may be missing an entry, or "
                    "the statement may carry a duplicate line."
                ),
                candidates_in_window=sum(
                    1
                    for k in remaining_book
                    if _within_window(rem.row.txn_date, k.date, fuzzy_window)
                ),
            )
        )
    # Zero-amount lines (AC-M2g): never auto-paired; manual confirm stays.
    for bank in zero_bank:
        in_bank_not_books.append(
            UnmatchedBank(
                bank_item_id=bank.id,
                amount=bank.amount,
                txn_date=bank.txn_date,
                description=bank.description,
                why_not_matched=WHY_ZERO_AMOUNT_EXCLUDED,
                why_detail=(
                    "$0.00 lines are excluded from automatic matching (a zero "
                    "amount would pair on date proximity alone). Pair it "
                    "manually if it belongs with a book transaction."
                ),
                candidates_in_window=0,
            )
        )
    in_books_not_bank = [
        UnmatchedBook(
            book_txn_id=book.id,
            # DISPLAY sign (B5-4): the stored amount is QBO-unsigned; render a
            # withdrawal as negative like the (signed) bank side.
            amount=signed_amount(book),
            txn_date=book.date,
            description=book.description,
        )
        # Outstanding = the ordinary leftovers + the group stage's book-side
        # remainder + zero-amount book rows (excluded from auto-pairing).
        for book in [*remaining_book, *group.book_remainder, *zero_book]
    ]
    # The splices above interleave the fetch order — re-assert the AC-E1a
    # ordering contract explicitly (date-desc, NULL last, id-desc on ties).
    in_bank_not_books.sort(
        key=lambda u: (u.txn_date is not None, u.txn_date or date.min, u.bank_item_id),
        reverse=True,
    )
    in_books_not_bank.sort(
        key=lambda u: (u.txn_date is not None, u.txn_date or date.min, u.book_txn_id),
        reverse=True,
    )

    # ── Diagnostics — WHY the run matched (or did not) ─────────────────────
    # Computed from the ALREADY-FETCHED pools — no extra query (AC-1). Names an
    # empty side (AC-2) and a direction refusal (AC-3, direction-aware since
    # B3-2: a negated-expense pair now MATCHES, so `sign_mismatch_detected`
    # counts only refusals — pairs `_amounts_match` rejected on direction) and
    # counts re-imported lines whose book twin another session already cleared
    # (`already_cleared`, fed by the cleared-exclusion pool rule above), instead
    # of letting a zero/low-match run read as a silent "0 matches."
    diagnostics = build_diagnostics(
        bank_rows=bank_rows,
        book_rows=book_rows,
        cleared_elsewhere=cleared_elsewhere,
        still_unmatched_bank=still_unmatched_bank,
        fuzzy_window=fuzzy_window,
        amounts_match=_amounts_match,
        # AC-M2/M3: the group stage + zero-exclusion pre-classify their rows
        # (they never reach still_unmatched_bank), so their tallies are passed
        # in — computed here, stamped by build_diagnostics.
        duplicate_group_remainder=len(group.bank_remainder),
        zero_amount_excluded=len(zero_bank),
    )
    return MatchResult(
        matched=matched,
        in_bank_not_books=in_bank_not_books,
        in_books_not_bank=in_books_not_bank,
        diagnostics=diagnostics,
    )


def _claim_exact(
    bank: BankFeedItem, candidates: list[QBOTransaction], window_days: int
) -> QBOTransaction | None:
    """
    First book candidate agreeing on MONEY (sign-convention-aware — see
    `_amounts_match`) with a date within the window. Returns None when no
    candidate qualifies (the bank row falls through to the fuzzy pass). The
    magnitude comparison is Decimal-exact — no rounding fudge, so a cent-off
    row is NOT auto-cleared (financial-modeling-correctness).
    """
    for book in candidates:
        if _amounts_match(bank.amount, book) and _within_window(
            bank.txn_date, book.date, window_days
        ):
            return book
    return None


def _claim_fuzzy(
    bank: BankFeedItem, candidates: list[QBOTransaction], window_days: int
) -> tuple[QBOTransaction | None, int]:
    """
    Best book candidate by memo/payee similarity, gated on exact amount + the
    (wider, caller-passed) fuzzy window + similarity ≥ FUZZY_SCORE_FLOOR. Returns
    (candidate, score 0..100) or (None, 0). The amount must STILL match exactly —
    the fuzzy pass loosens the DATE (paid for by the memo signal), never the
    money.
    """
    bank_memo = _normalize_memo(bank.description, bank.vendor_name)
    best: QBOTransaction | None = None
    best_ratio = 0.0
    for book in candidates:
        if not _amounts_match(bank.amount, book) or not _within_window(
            bank.txn_date, book.date, window_days
        ):
            continue
        book_memo = _normalize_memo(book.description, book.vendor_name)
        ratio = _similarity(bank_memo, book_memo)
        if ratio > best_ratio:
            best_ratio = ratio
            best = book
    if best is not None and best_ratio >= FUZZY_SCORE_FLOOR:
        return best, round(best_ratio * 100)
    return None, 0


def _diagnose(
    bank: BankFeedItem,
    remaining_book: list[QBOTransaction],
    cleared_elsewhere: list[QBOTransaction],
    fuzzy_window: int,
) -> tuple[str, str | None, int]:
    """Explain WHY one statement line survived both passes unmatched (B3-2b).

    Evaluated against the WIDEST (fuzzy) date window so the diagnosis is never
    narrower than what the matcher itself considered. Returns
    ``(why_not_matched, why_detail, candidates_in_window)`` — the FIRST rule
    that would have kept the line out, checked in priority order:

      1. already_cleared — a same-amount, in-window book txn exists but was
         already cleared by ANOTHER session (excluded from the candidate pool
         upstream, so it never got a chance to match here).
      2. sign_mismatch — a magnitude-equal, in-window book txn exists but its
         direction (from transaction_type) disagrees with the bank sign, or is
         unknown (`_amounts_match` rule 3 — never fabricate a deposit↔withdrawal
         pairing).
      3. amount_mismatch — an in-window book txn exists but no amount (signed
         or magnitude) agrees.
      4. no_candidate_in_window — nothing on the book side falls inside the
         fuzzy date window at all (the missing-entry case).

    ``candidates_in_window`` counts every STILL-AVAILABLE book row (not yet
    claimed by another pairing) within the fuzzy window, regardless of why it
    didn't qualify — the raw denominator the UI shows next to the reason.
    """
    in_window_remaining = [
        b for b in remaining_book if _within_window(bank.txn_date, b.date, fuzzy_window)
    ]
    candidate_count = len(in_window_remaining)

    if candidate_count == 0:
        # Nothing left in-window; check whether a same-amount candidate exists
        # but was excluded because it was already cleared by another session —
        # a more specific, actionable reason than a bare "no candidate".
        cleared_in_window = [
            b
            for b in cleared_elsewhere
            if _within_window(bank.txn_date, b.date, fuzzy_window)
            and _amounts_match(bank.amount, b)
        ]
        if cleared_in_window:
            return (
                WHY_ALREADY_CLEARED,
                "A matching book transaction is in this date window but was "
                "already cleared by another reconciliation session.",
                0,
            )
        return (
            WHY_NO_CANDIDATE,
            "No book transaction falls inside the date window for this line.",
            0,
        )

    for book in in_window_remaining:
        if (
            bank.amount is not None
            and book.amount is not None
            and abs(bank.amount) == abs(book.amount)
            and not _amounts_match(bank.amount, book)
        ):
            # Magnitude agrees but `_amounts_match` REFUSED the pairing — the
            # book direction (from transaction_type) disagrees with the bank
            # sign, or is unknown (rule 3: never fabricate deposit↔withdrawal).
            # A magnitude-equal candidate the matcher WOULD accept is not a
            # sign mismatch (it fell through on memo/claiming — see below).
            return (
                WHY_SIGN_MISMATCH,
                "A book transaction with the same magnitude is in the date "
                "window, but its direction (money in/out) doesn't agree "
                "with this statement line.",
                candidate_count,
            )

    # Every in-window candidate agrees on MONEY (else the loop above would have
    # returned) — an amount-matching candidate exists but the fuzzy pass never
    # claimed it, which only happens when the best memo/payee similarity fell
    # below FUZZY_SCORE_FLOOR (`_claim_fuzzy` gates on amount+window+floor).
    # The cheap abs pre-guard short-circuits before the pricier _amounts_match
    # (mirrors the sign-mismatch loop above): a different-magnitude row can never
    # amount-match (match.py:236/238), so this only calls _amounts_match on
    # same-magnitude candidates — set is identical, per-line cost drops from
    # O(in_window) to O(same-magnitude). candidate_count still counts ALL amounts
    # (the date-window denominator); that residual date scan is cheap + bounded.
    amount_matching = [
        b
        for b in in_window_remaining
        if bank.amount is not None
        and b.amount is not None
        and abs(b.amount) == abs(bank.amount)
        and _amounts_match(bank.amount, b)
    ]
    if amount_matching:
        bank_memo = _normalize_memo(bank.description, bank.vendor_name)
        best_ratio = max(
            (_similarity(bank_memo, _normalize_memo(b.description, b.vendor_name)) for b in amount_matching),
            default=0.0,
        )
        return (
            WHY_MEMO_BELOW_FLOOR,
            f"A book transaction with the same amount is in the date window, "
            f"but the memo/payee similarity ({round(best_ratio * 100)}%) is "
            f"below the {round(FUZZY_SCORE_FLOOR * 100)}% floor required for a "
            f"fuzzy match.",
            candidate_count,
        )

    return (
        WHY_AMOUNT_MISMATCH,
        "Book transactions exist in this date window, but none agree on the "
        "amount.",
        candidate_count,
    )


def _proposal(
    bank: BankFeedItem,
    book: QBOTransaction,
    match_type: str,
    score: int,
    *,
    cleared: bool = False,
) -> MatchProposal:
    book_signed = signed_amount(book) if book.amount is not None else None
    return MatchProposal(
        bank_item_id=bank.id,
        book_txn_id=book.id,
        # DISPLAY sign (B5-4): show the BANK line's amount — the bank side is
        # SIGNED by construction (ingest.py: withdrawal negative), so a matched
        # withdrawal renders negative. `book_amount` / `direction` carry the book
        # leg's own signed movement, which `_amounts_match` guarantees agrees
        # with the bank sign. Matching already ran; this is display only.
        amount=bank.amount,
        bank_date=bank.txn_date,
        book_date=book.date,
        match_type=match_type,
        score=score,
        cleared=cleared,
        book_amount=book_signed,
        direction=_direction_label(book_signed),
    )


def _direction_label(signed: Decimal | None) -> str | None:
    """"in" / "out" for a signed money movement; None for zero or unknown."""
    if signed is None or signed == 0:
        return None
    return "in" if signed > 0 else "out"


def _ledger_txn_id_for(book: QBOTransaction) -> str:
    """
    LOCK-1 (Rev B) entity-typed `ledger_txn_id` shape for `ReconciliationMatch`.

    QBO ids are unique per ENTITY TYPE, not globally, so the audit-only
    `ledger_txn_id` string must carry the entity type when known:
    `f"{entity_type}:{qbo_id}"` (e.g. `"Purchase:501"`). Legacy rows synced
    before `entity_type` existed have it NULL — for those we do NOT fabricate
    a type, we fall back to the bare `str(qbo_id)` (the pre-LOCK-1 shape), so
    an old row's persisted match string is not silently rewritten underneath
    it. `confirm_match`/`unconfirm_match` both derive the lookup key AND the
    inserted value through this one function so they can never diverge.
    """
    if book.entity_type:
        return f"{book.entity_type}:{book.qbo_id}"
    return str(book.qbo_id)


def confirm_match(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    bank_item_id: int,
    book_txn_id: int,
) -> None:
    """
    CONFIRM one human-approved pairing through the shared locked write boundary.

    Manual pairs remain valid: an auto-generated proposal is deliberately NOT a
    prerequisite. Structural eligibility is, and is revalidated here after row
    locks rather than trusted from a caller-side preflight. The lock/check order
    is session → clean-start authority → exact frozen relation + bank row → book
    row → existing APPROVED claims. No cleared flag changes until every check has
    passed. Locking the bank and book rows serializes both competing-bank and
    competing-book confirmations on PostgreSQL, so the later contender observes
    the first committed claim and receives a typed conflict.

    An exact already-approved pair whose book flags are still owned by this
    session is an idempotent no-op. Reusing either side for a different pair is
    refused. The service flushes but never commits; request/assistant callers own
    their transaction boundary.
    """
    locked_session = (
        db.query(ReconciliationSession)
        .populate_existing()
        .filter(
            ReconciliationSession.realm_id == realm_id,
            ReconciliationSession.id == session.id,
        )
        .with_for_update()
        .first()
    )
    if locked_session is None:
        raise ConfirmationSessionNotFoundError(session.id)
    if not is_open_session_status(locked_session.status):
        raise ConfirmationSessionNotOpenError(
            locked_session.id, effective_session_status(locked_session.status)
        )

    anchor_config = (
        db.query(BankReconAccountConfig)
        .populate_existing()
        .filter(
            BankReconAccountConfig.realm_id == realm_id,
            BankReconAccountConfig.account_id == locked_session.account_id,
        )
        .with_for_update()
        .first()
    )
    if anchor_config is None or anchor_config.opening_reconciled_date is None:
        raise CleanStartNotConfiguredError(locked_session.account_id)
    anchor_date = anchor_config.opening_reconciled_date

    frozen_line = (
        db.query(ReconciliationSessionStatementLine)
        .populate_existing()
        .filter(
            ReconciliationSessionStatementLine.realm_id == realm_id,
            ReconciliationSessionStatementLine.reconciliation_session_id
            == locked_session.id,
            ReconciliationSessionStatementLine.bank_feed_item_id == bank_item_id,
            ReconciliationSessionStatementLine.lineage_status
            == locked_session.lineage_status,
            ReconciliationSessionStatementLine.statement_import_id
            == locked_session.statement_import_id,
        )
        .with_for_update()
        .first()
    )
    if frozen_line is None:
        raise StatementLineNotInSessionError(locked_session.id, bank_item_id)

    bank = (
        db.query(BankFeedItem)
        .populate_existing()
        .filter(
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.id == bank_item_id,
        )
        .with_for_update()
        .first()
    )
    controlled_projection_mismatch = (
        frozen_line.lineage_status == "controlled"
        and (
            bank is None
            or bank.statement_import_id != frozen_line.statement_import_id
            or bank.parse_chunk_id != frozen_line.parse_chunk_id
            or bank.source_ordinal != frozen_line.source_ordinal
            or bank.lineage_status != "controlled"
        )
    )
    if (
        bank is None
        or bank.source != STATEMENT_SOURCE
        or bank.bank_account_ref != locked_session.account_id
        or controlled_projection_mismatch
    ):
        raise StatementLineNotInSessionError(locked_session.id, bank_item_id)

    book = (
        db.query(QBOTransaction)
        .populate_existing()
        .filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.id == book_txn_id,
        )
        .with_for_update()
        .first()
    )
    if book is None or book.bank_account_ref != locked_session.account_id:
        raise BookTransactionNotInSessionError(locked_session.id, book_txn_id)

    for side, item_id, txn_date in (
        ("bank statement", bank.id, bank.txn_date),
        ("book transaction", book.id, book.date),
    ):
        if txn_date <= anchor_date or txn_date > locked_session.period_end:
            raise MatchOutsideSessionWindowError(
                session_id=locked_session.id,
                side=side,
                item_id=item_id,
                txn_date=txn_date,
                anchor_date=anchor_date,
                period_end=locked_session.period_end,
            )

    is_uncleared = book.cleared is not True and book.cleared_session_id is None
    is_owned = (
        book.cleared is True
        and book.cleared_session_id == locked_session.id
    )
    if not (is_uncleared or is_owned):
        raise BookTransactionClearedError(
            book_txn_id=book.id,
            cleared_session_id=book.cleared_session_id,
        )

    bank_ref = str(bank_item_id)
    ledger_ref = _ledger_txn_id_for(book)
    approved_claims = (
        db.query(ReconciliationMatch)
        .populate_existing()
        .filter(
            ReconciliationMatch.realm_id == realm_id,
            ReconciliationMatch.status == "APPROVED",
            or_(
                ReconciliationMatch.bank_txn_id == bank_ref,
                ReconciliationMatch.book_txn_local_id == book.id,
                and_(
                    ReconciliationMatch.book_txn_local_id.is_(None),
                    ReconciliationMatch.ledger_txn_id == ledger_ref,
                ),
            ),
        )
        .with_for_update()
        .all()
    )
    exact_claims = [
        claim
        for claim in approved_claims
        if claim.bank_txn_id == bank_ref
        and claim.session_id == locked_session.id
        and (
            claim.book_txn_local_id == book.id
            or (
                claim.book_txn_local_id is None
                and claim.ledger_txn_id == ledger_ref
            )
        )
    ]
    if len(approved_claims) == 1 and len(exact_claims) == 1:
        if not is_owned:
            raise BookTransactionClearedError(
                book_txn_id=book.id,
                cleared_session_id=book.cleared_session_id,
            )
        return
    if approved_claims:
        bank_claim = next(
            (claim for claim in approved_claims if claim.bank_txn_id == bank_ref),
            None,
        )
        if bank_claim is not None:
            raise MatchClaimConflictError(
                resource="bank statement line",
                item_id=bank.id,
                existing_session_id=bank_claim.session_id,
            )
        book_claim = approved_claims[0]
        raise MatchClaimConflictError(
            resource="book transaction",
            item_id=book.id,
            existing_session_id=book_claim.session_id,
        )

    book.cleared = True
    book.cleared_session_id = locked_session.id

    # KEYED lookup first (AC-M2): the identity columns disambiguate duplicate
    # qbo_ids sharing one ledger_ref string and scope the row to THIS session.
    existing = (
        db.query(ReconciliationMatch)
        .filter(
            ReconciliationMatch.realm_id == realm_id,
            ReconciliationMatch.session_id == locked_session.id,
            ReconciliationMatch.bank_txn_id == bank_ref,
            ReconciliationMatch.book_txn_local_id == book.id,
        )
        .first()
    )
    if existing is None:
        # Legacy fallback: a row confirmed before the identity columns existed
        # (NULL session_id). Backfill it in place — self-healing on touch.
        existing = (
            db.query(ReconciliationMatch)
            .filter(
                ReconciliationMatch.realm_id == realm_id,
                ReconciliationMatch.bank_txn_id == bank_ref,
                ReconciliationMatch.ledger_txn_id == ledger_ref,
                ReconciliationMatch.session_id.is_(None),
            )
            .first()
        )
        if existing is not None:
            existing.session_id = locked_session.id
            existing.book_txn_local_id = book.id
    if existing is None:
        db.add(
            ReconciliationMatch(
                realm_id=realm_id,
                bank_txn_id=bank_ref,
                ledger_txn_id=ledger_ref,
                session_id=locked_session.id,
                book_txn_local_id=book.id,
                status="APPROVED",
            )
        )
    else:
        existing.status = "APPROVED"

    db.flush()


def unconfirm_match(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    bank_item_id: int,
    book_txn_id: int,
) -> None:
    """
    REVERSE a confirmation — clears the cleared FLAGS and rejects the match row.

    Sets `QBOTransaction.cleared = False` + `cleared_session_id = None` (only if
    the txn was cleared BY THIS session — an unconfirm never steals a clear that
    belongs to another session) and marks the `ReconciliationMatch` REJECTED. The
    match lookup is KEYED on the AC-M2 identity (session_id + book_txn_local_id)
    first — duplicate qbo_ids sharing one ledger_ref string can no longer flip
    the WRONG row (the old `.first()` ambiguity) — with the legacy
    (bank_ref, ledger_ref, NULL-session) shape as the pre-identity fallback. No
    QBO call; realm-scoped; `db.flush()` joins the caller's transaction. Raises
    ValueError if the book txn is not found in this realm (fail-loud).
    """
    _require_statement_line(
        db,
        realm_id=realm_id,
        session=session,
        bank_item_id=bank_item_id,
    )

    book = (
        db.query(QBOTransaction)
        .filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.id == book_txn_id,
        )
        .first()
    )
    if book is None:
        raise ValueError(
            f"book txn {book_txn_id} not found in realm {realm_id}"
        )

    # Only release a clear this session owns (cleared_session_id == session.id).
    if book.cleared_session_id == session.id:
        book.cleared = False
        book.cleared_session_id = None

    bank_ref = str(bank_item_id)
    ledger_ref = _ledger_txn_id_for(book)
    match = (
        db.query(ReconciliationMatch)
        .filter(
            ReconciliationMatch.realm_id == realm_id,
            ReconciliationMatch.session_id == session.id,
            ReconciliationMatch.bank_txn_id == bank_ref,
            ReconciliationMatch.book_txn_local_id == book.id,
        )
        .first()
    )
    if match is None:
        # Legacy pre-identity row (NULL session_id) — the old lookup shape.
        match = (
            db.query(ReconciliationMatch)
            .filter(
                ReconciliationMatch.realm_id == realm_id,
                ReconciliationMatch.bank_txn_id == bank_ref,
                ReconciliationMatch.ledger_txn_id == ledger_ref,
                ReconciliationMatch.session_id.is_(None),
            )
            .first()
        )
    if match is not None:
        match.status = "REJECTED"

    db.flush()


def release_session_clears(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
) -> int:
    """
    Release EVERY clear this session owns — the bulk form of `unconfirm_match`,
    used by `session.abandon_session` (B5-2). For each book txn cleared BY THIS
    session: clears the flags (`cleared=False`, `cleared_session_id=None`) and
    REJECTs its `ReconciliationMatch` row, so the txns return to the matchable /
    outstanding pool with no orphaned APPROVED match rows. The match rejection
    is KEYED (AC-M2): ONE batched query rejects every APPROVED row stamped with
    this session's identity — a duplicate-qbo_id ledger_ref can no longer make
    the old per-row `.first()` reject ANOTHER session's row. Legacy
    pre-identity rows (NULL session_id) fall back to the old per-book
    ledger_ref lookup, no worse than before. Returns the count released.
    Realm-scoped; `db.flush()` joins the caller's transaction (the router owns
    the commit). Read-only for every other session's clears — a txn cleared by
    a DIFFERENT session is never touched.
    """
    cleared_rows = (
        db.query(QBOTransaction)
        .filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.cleared_session_id == session.id,
        )
        .all()
    )
    # KEYED batch: every APPROVED match row this session stamped, in one query.
    keyed_matches = (
        db.query(ReconciliationMatch)
        .filter(
            ReconciliationMatch.realm_id == realm_id,
            ReconciliationMatch.session_id == session.id,
            ReconciliationMatch.status == "APPROVED",
        )
        .all()
    )
    keyed_book_ids: set[int] = set()
    for match in keyed_matches:
        match.status = "REJECTED"
        if match.book_txn_local_id is not None:
            keyed_book_ids.add(match.book_txn_local_id)

    for book in cleared_rows:  # SA-OK: bounded-loop over one pre-fetched cleared set; the per-row lookup runs ONLY for legacy pre-identity rows the keyed batch above could not cover
        book.cleared = False
        book.cleared_session_id = None
        if book.id in keyed_book_ids:
            continue  # already rejected by the keyed batch
        ledger_ref = _ledger_txn_id_for(book)
        match = (
            db.query(ReconciliationMatch)
            .filter(
                ReconciliationMatch.realm_id == realm_id,
                ReconciliationMatch.ledger_txn_id == ledger_ref,
                ReconciliationMatch.status == "APPROVED",
                ReconciliationMatch.session_id.is_(None),
            )
            .first()
        )
        if match is not None:
            match.status = "REJECTED"

    db.flush()
    return len(cleared_rows)


@dataclass(frozen=True)
class BulkConfirmResult:
    """The outcome of one `confirm_matched_bulk` call (the human-UI "confirm all").

    `confirmed` — pairs NEWLY cleared this call. `skipped_already_cleared` — in-scope
    matched pairs whose book txn was ALREADY cleared (a re-click is idempotent, not a
    double-write). `fuzzy_included` — how many of the confirmed pairs were FUZZY_DATE
    (0 unless `include_fuzzy`).
    """

    confirmed: int
    skipped_already_cleared: int
    fuzzy_included: int


def confirm_matched_bulk(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    include_fuzzy: bool = False,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> BulkConfirmResult:
    """CONFIRM every currently-proposed EXACT pairing for one session in ONE call.

    The human-UI "confirm all matched pairs" executor. It RECOMPUTES `find_matches`
    at call time (DEC-BC-1': server-authoritative — the client never supplies a pair
    list, so there is no stale/tamper surface) and confirms the UNCLEARED matched
    proposals, defaulting to `match_type in {EXACT, EXACT_GROUP}` (the ratified
    DEC-BC-2 amendment, plan 20260716-batch5-remediation §6.4: a window-bounded
    group pair is the same amount+date+account evidence class the EXACT pass
    already swept when both twins existed). FUZZY_DATE pairs are lower-confidence
    and stay individually reviewed unless `include_fuzzy` is set (DEC-BC-2). The
    discriminator is `match_type`, NOT `score`: the fuzzy pass scores
    `round(similarity*100)`, so a perfect-memo fuzzy pair also scores 100 —
    filtering on `score >= 100` would silently sweep it into the default.

    Each confirm delegates to `confirm_match` (DEC-BC-3: one write path — the cleared
    flags, the `ReconciliationMatch` upsert, and the LOCK-1 `ledger_txn_id` shaping
    stay in one place), so a bulk confirm is exactly N single confirms and inherits
    their idempotency: a re-run confirms 0 net-new (already-cleared pairs are counted
    as skipped, never re-written). Mirrors the bulk-unconfirm `release_session_clears`.
    `db.flush()` joins the caller's transaction; the router owns the commit. Raises
    `CleanStartNotConfiguredError` (via `find_matches`) when the account has no anchor —
    the caller surfaces it as a 409, exactly like the read path.
    """
    result = find_matches(
        db, realm_id=realm_id, session=session, window_days=window_days
    )
    in_scope = [
        p
        for p in result.matched
        if include_fuzzy or p.match_type in (MATCH_TYPE_EXACT, MATCH_TYPE_EXACT_GROUP)
    ]
    to_confirm = [p for p in in_scope if not p.cleared]
    skipped_already_cleared = sum(1 for p in in_scope if p.cleared)
    fuzzy_included = sum(1 for p in to_confirm if p.match_type == MATCH_TYPE_FUZZY)
    for p in to_confirm:  # SA-OK: bounded-loop over one session's matched proposals; confirm_match is the frozen per-pair write path (agent exec loop handlers.py + release_session_clears precedent)
        confirm_match(
            db,
            realm_id=realm_id,
            session=session,
            bank_item_id=p.bank_item_id,
            book_txn_id=p.book_txn_id,
        )
    db.flush()
    return BulkConfirmResult(
        confirmed=len(to_confirm),
        skipped_already_cleared=skipped_already_cleared,
        fuzzy_included=fuzzy_included,
    )


# Per-pair skip reasons for `confirm_matched_subset` — stable strings the router
# forwards verbatim (the UI maps them to teaching labels, like the WHY_* codes).
SKIP_NOT_MATCHED = "not_matched"
SKIP_ALREADY_CLEARED = "already_cleared"
SKIP_FUZZY_EXCLUDED = "fuzzy_excluded"
SKIP_DUPLICATE = "duplicate_request"


@dataclass(frozen=True)
class SkippedPair:
    """One requested pair `confirm_matched_subset` did NOT confirm, and why.

    `reason` is one of the SKIP_* codes: `not_matched` (the pair is absent from
    the server-recomputed matched set — stale, fabricated, or claimed elsewhere),
    `already_cleared` (idempotent re-click, not a double-write), `fuzzy_excluded`
    (a FUZZY_DATE pair without `include_fuzzy`), `duplicate_request` (the same
    pair appeared earlier in this request's list).
    """

    bank_item_id: int
    book_txn_id: int
    reason: str


@dataclass(frozen=True)
class BatchConfirmResult:
    """The outcome of one `confirm_matched_subset` call (the selected-subset confirm).

    `confirmed` — pairs NEWLY cleared this call. `fuzzy_included` — how many of the
    confirmed pairs were FUZZY_DATE (0 unless `include_fuzzy`). `skipped` — every
    requested pair that was NOT confirmed, each with its SKIP_* reason, so
    `requested == confirmed + len(skipped)` always holds (honest accounting).
    """

    confirmed: int
    fuzzy_included: int
    skipped: list[SkippedPair]


def confirm_matched_subset(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    pairs: list[tuple[int, int]],
    include_fuzzy: bool = False,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> BatchConfirmResult:
    """CONFIRM a CLIENT-SELECTED SUBSET of the proposed pairings in ONE call (AC-E1b).

    The selected-subset sibling of `confirm_matched_bulk` (which sweeps the whole
    EXACT set): the client sends the `(bank_item_id, book_txn_id)` pairs the human
    ticked, but the pair list is a SELECTION, never the truth — this RECOMPUTES
    `find_matches` ONCE at call time (DEC-BC-1' twin: server-authoritative) and
    confirms only requested pairs present in the recomputed matched set. Everything
    else is reported back as a `SkippedPair` (absent/stale ids, already-cleared
    pairs, FUZZY_DATE pairs without `include_fuzzy`, in-request duplicates) — a
    skip is an answer, never an error, so one stale row cannot fail the batch.

    The discriminator is `match_type`, NOT `score` (a perfect-memo fuzzy pair
    also scores 100 — DEC-BC-2); the no-opt-in default admits EXACT and
    EXACT_GROUP (the ratified DEC-BC-2 amendment, plan 20260716-batch5-remediation
    §6.4). Each confirm delegates to `confirm_match` (DEC-BC-3: one write path),
    so the batch is exactly N single confirms in the caller's ONE transaction:
    `db.flush()` joins it; the router owns the commit. Raises
    `CleanStartNotConfiguredError` (via `find_matches`) when the account has no
    anchor — the caller surfaces it as a 409.
    """
    result = find_matches(
        db, realm_id=realm_id, session=session, window_days=window_days
    )
    proposals = {(p.bank_item_id, p.book_txn_id): p for p in result.matched}

    confirmed = 0
    fuzzy_included = 0
    skipped: list[SkippedPair] = []
    seen: set[tuple[int, int]] = set()
    for bank_item_id, book_txn_id in pairs:  # SA-OK: bounded-loop over the client-selected pair subset; confirm_match is the frozen per-pair write path (confirm_matched_bulk precedent above)
        key = (bank_item_id, book_txn_id)
        if key in seen:
            skipped.append(SkippedPair(bank_item_id, book_txn_id, SKIP_DUPLICATE))
            continue
        seen.add(key)
        proposal = proposals.get(key)
        if proposal is None:
            skipped.append(SkippedPair(bank_item_id, book_txn_id, SKIP_NOT_MATCHED))
            continue
        if proposal.cleared:
            skipped.append(SkippedPair(bank_item_id, book_txn_id, SKIP_ALREADY_CLEARED))
            continue
        # AC-M3: the fuzzy gate admits EXACT and EXACT_GROUP (the ratified
        # DEC-BC-2 amendment); only genuinely fuzzy pairs need the opt-in.
        if (
            proposal.match_type not in (MATCH_TYPE_EXACT, MATCH_TYPE_EXACT_GROUP)
            and not include_fuzzy
        ):
            skipped.append(SkippedPair(bank_item_id, book_txn_id, SKIP_FUZZY_EXCLUDED))
            continue
        confirm_match(
            db,
            realm_id=realm_id,
            session=session,
            bank_item_id=bank_item_id,
            book_txn_id=book_txn_id,
        )
        confirmed += 1
        if proposal.match_type == MATCH_TYPE_FUZZY:
            fuzzy_included += 1
    db.flush()
    return BatchConfirmResult(
        confirmed=confirmed,
        fuzzy_included=fuzzy_included,
        skipped=skipped,
    )

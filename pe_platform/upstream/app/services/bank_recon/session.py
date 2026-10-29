"""
Reconciliation session lifecycle + tie-out compute (C1-c, AC-1.3).

The reconciliation tie-out (SYNTHESIS DEC-BREC-3 + §3): a session ties out when

    cleared_balance = opening + Σ(cleared book txns)
    difference      = cleared_balance − ending_balance   (the statement closing)

and the session may only reach COMPLETE when `difference == 0` (or the residual
is fully explained by caller-supplied outstanding items). An unexplained non-zero
difference BLOCKS completion and the itemized number is returned — this service
NEVER fabricates a zero completion (financial-modeling-correctness: honest gap,
no fabricated zero).

Ownership boundary (Wave-3 interface contract): the MATCH ENGINE (`match.py`,
owned by a parallel agent) SETS the per-txn cleared flags —
`QBOTransaction.cleared` + `QBOTransaction.cleared_session_id`. THIS service READS
those flags to compute the balances and OWNS the writes to `ReconciliationSession`
(`cleared_balance` / `status` / `completed_at`). The two do not cross: match.py
never touches the session row; session.py never sets a per-txn cleared flag.

Money math (financial-modeling-correctness): every balance is `Decimal`, never
float — a float sum of currency amounts drifts by fractions of a cent and would
make a genuinely-balanced session read as out-of-balance (or vice versa). The
model columns are `Numeric(precision=18, scale=2)` (models.py:1073-1075), which
SQLAlchemy returns as `Decimal`; the opening anchor is `Decimal | None`
(clean_start.CleanStartAnchor.opening_reconciled_balance).

Realm scoping (multi-tenancy-app-layer): every query carries an explicit
`realm_id ==` predicate — the app-layer PRIMARY gate (it runs on the SQLite test
lane, which has no RLS) — and the caller (the THIN router) has already
membership-gated + pinned the realm GUC via `check_tenant_access` before this
FAT service runs (api-contract-and-layering). Never authorize off a
client-supplied realm.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, text
from sqlalchemy.orm import Session

from ...models import (
    BankFeedItem,
    BankStatementControlCertificate,
    BankStatementImport,
    BankStatementParseCandidate,
    QBOTransaction,
    ReconciliationSession,
    ReconciliationSessionStatementLine,
)
from .. import audit
from . import clean_start
from . import match as match_engine
from .session_status import (
    STATUS_ABANDONED,
    STATUS_COMPLETE,
    STATUS_OPEN,
    effective_session_status,
    is_open_session_status,
    live_session_status_clause,
    open_session_status_clause,
)

# Money quantum: cent-precision matching the Numeric(18, 2) columns. Comparisons
# and the returned DTO values are quantized to this so a Decimal("0.00") and a
# Decimal("0") compare equal and a residual is reported at cent resolution.
_ZERO = Decimal("0.00")

_CONTROLLED_LINEAGE = "controlled"
_PARSED_IMPORT = "parsed"
_PASSED_CONTROL = "passed"
_ACCEPTED_CANDIDATE_STATUSES = ("accepted", "corrected")


class ControlledImportRequiredError(Exception):
    """A new session was requested without an exact controlled import id."""


class StatementImportNotFoundError(Exception):
    """The requested import does not exist in the caller-authorized realm."""

    def __init__(self, statement_import_id: int) -> None:
        self.statement_import_id = statement_import_id
        super().__init__(f"statement import {statement_import_id} was not found")


class StatementImportNotEligibleError(Exception):
    """The import cannot seed a session because its computed control is not final."""

    def __init__(self, statement_import_id: int, reason: str) -> None:
        self.statement_import_id = statement_import_id
        self.reason = reason
        super().__init__(
            f"statement import {statement_import_id} cannot seed a session: {reason}"
        )


class StatementImportMismatchError(Exception):
    """Caller-supplied session facts disagree with the controlled import."""

    def __init__(self, statement_import_id: int, field: str) -> None:
        self.statement_import_id = statement_import_id
        self.field = field
        super().__init__(
            f"statement import {statement_import_id} does not match requested {field}"
        )


class StatementImportProjectionError(Exception):
    """The passed certificate and durable candidate/feed projection disagree."""

    def __init__(self, statement_import_id: int, reason: str) -> None:
        self.statement_import_id = statement_import_id
        self.reason = reason
        super().__init__(
            f"statement import {statement_import_id} projection is invalid: {reason}"
        )


class PeriodAlreadyReconciledError(Exception):
    """
    Raised by `create_session` when the requested statement period is already
    reconciled for this account — the clean-start anchor date is ON or AFTER the
    requested `period_end` (B5-2). Without this guard a same-period re-create
    opens a session whose opening == the rolled-forward anchor == its own ending,
    with every in-period txn pre-anchor-excluded: a trivially-"balanced" $0.00
    session that reconciles nothing. The correct path is `reopen_session`.
    """

    def __init__(self, account_id: str, anchor_date: date) -> None:
        self.account_id = account_id
        self.anchor_date = anchor_date
        super().__init__(
            f"account {account_id} is already reconciled through {anchor_date}; "
            "reopen the prior reconciliation instead of creating a new session "
            "for the same period"
        )


class OpenSessionExistsError(Exception):
    """
    Raised by `create_session` when an OPEN session already exists for the
    account (B5-2). Two concurrent OPEN sessions share one uncleared-txn pool —
    their matched/outstanding views interleave incoherently. Resume (or abandon)
    the existing session instead.
    """

    def __init__(self, existing_session_id: int) -> None:
        self.existing_session_id = existing_session_id
        super().__init__(
            f"an OPEN reconciliation session ({existing_session_id}) already "
            "exists for this account; resume or abandon it first"
        )


class SessionNotAbandonableError(Exception):
    """
    Raised by `abandon_session` when the session is not OPEN. A COMPLETE
    reconciliation is a finished financial record — reversing it is
    `reopen_session`'s audited job, never a silent abandon; an already-ABANDONED
    session is refused for idempotence (no second cleared-set release).
    """

    def __init__(self, session_id: int, status: str) -> None:
        self.session_id = session_id
        self.status = status
        super().__init__(
            f"session {session_id} is {status}, not {STATUS_OPEN}; only an OPEN "
            "session can be abandoned (a COMPLETE one is reversed via reopen)"
        )


class SessionNotCompletableError(Exception):
    """The shared completion boundary received a session that is not OPEN.

    Historical ``NULL`` is canonical OPEN and therefore never reaches this
    exception.  Every other non-OPEN value is carried as its effective status
    so transport boundaries can map one typed conflict without reimplementing
    lifecycle semantics.
    """

    def __init__(self, session_id: int, status: str) -> None:
        self.session_id = session_id
        self.status = status
        super().__init__(
            f"session {session_id} is {status}, not {STATUS_OPEN}; only an OPEN "
            "session can be completed"
        )


class ReconciliationNotBalancedError(Exception):
    """
    Raised by `try_complete` when a session cannot reach COMPLETE because its
    tie-out difference is not zero and is not fully explained by the caller-
    supplied outstanding total. Carries the itemized `TieOut` + the residual so
    the caller can surface the honest, non-zero gap — never a fabricated $0.

    The session is left OPEN (unchanged) when this is raised.
    """

    def __init__(self, tieout: "TieOut", residual: Decimal) -> None:
        self.tieout = tieout
        self.residual = residual
        super().__init__(
            f"reconciliation not balanced: residual difference {residual} "
            f"(cleared_balance {tieout.cleared_balance} − ending_balance "
            f"{tieout.ending_balance}); session stays OPEN"
        )


class SessionNotCompleteError(Exception):
    """
    Raised by `reopen_session` when the session is not COMPLETE. This is also the
    idempotence guard (AC-2d): reopening an already-OPEN (in-progress) session is
    a refusal, never a second anchor rollback.
    """


class NotLatestCompletedSessionError(Exception):
    """
    Raised by `reopen_session` when the session is NOT the latest completed
    session for its account+realm (period_end, then completed_at, ordering).

    Mid-chain unwinds are forbidden (AC-1): each completion rolled the anchor
    forward over the previous one, so undoing an EARLIER session while a later
    one stands would strand the anchor chain — later reconciliations were tied
    out against this session's ending balance. Carries the id of the session that
    IS the latest so the caller can teach the user the unwind order.
    """

    def __init__(self, latest_session_id: int | None) -> None:
        self.latest_session_id = latest_session_id
        super().__init__(
            "only the latest completed reconciliation for the account can be "
            f"reopened (latest is session {latest_session_id})"
        )


class AnchorRollbackUnprovableError(Exception):
    """
    Raised by `reopen_session` when the session's stored fields cannot PROVE the
    pre-completion opening (its stamped `cleared_balance` is missing), so the
    anchor rollback would be a guess. We refuse rather than fabricate an opening
    balance (financial-modeling-correctness: never fabricate a finance figure).
    """


@dataclass(frozen=True)
class ReopenResult:
    """
    Detached result of `reopen_session` (the ORM row never crosses the boundary).

    - `anchor_rolled_back` — True when the account anchor was rolled back to this
      session's opening; False when the current anchor is no longer THIS
      session's forward-roll (e.g. it was manually re-anchored after completion),
      in which case the newer anchor is deliberately left untouched.
    - `restored_opening` / `restored_anchor_date` — the rolled-back anchor value
      (None when `anchor_rolled_back` is False).
    """

    session_id: int
    status: str
    anchor_rolled_back: bool
    restored_opening: Decimal | None
    restored_anchor_date: date | None


@dataclass(frozen=True)
class TieOut:
    """
    Detached tie-out DTO — computed while the session is open and returned by
    value so a caller (router) never reads it off a Session-bound ORM row after
    the request transaction closes (api-contract-and-layering: the ORM object
    never crosses the boundary).

    All money fields are `Decimal` at cent precision.

    - `opening`          — the clean-start anchor's opening_reconciled_balance,
                           or Decimal("0.00") when the account is unanchored.
    - `anchor_configured`— False when the account has no clean-start anchor; the
                           `opening` 0 is then a PLACEHOLDER, not a real baseline.
                           Never silently fabricate the anchor: surface this bool.
    - `cleared_sum`      — Σ amount of book txns cleared in THIS session.
    - `cleared_balance`  — opening + cleared_sum.
    - `ending_balance`   — the statement closing the accountant entered.
    - `difference`       — cleared_balance − ending_balance (0 == tied out).
    """

    session_id: int
    realm_id: str
    account_id: str
    opening: Decimal
    anchor_configured: bool
    cleared_sum: Decimal
    cleared_balance: Decimal
    ending_balance: Decimal
    difference: Decimal


def _q(value: Decimal) -> Decimal:
    """Quantize a Decimal to cent precision (matches the Numeric(18, 2) columns)."""
    return value.quantize(_ZERO)


def _validated_import_projection(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int | None,
    account_id: str,
    period_start: date,
    period_end: date,
    ending_balance: Decimal,
    lock_rows: bool,
) -> tuple[
    BankStatementImport,
    BankStatementControlCertificate,
    list[tuple[BankStatementParseCandidate, BankFeedItem]],
]:
    """Lock and validate the one controlled import allowed to seed a session."""
    if statement_import_id is None:
        raise ControlledImportRequiredError(
            "new reconciliation sessions require statement_import_id"
        )

    import_query = (
        db.query(BankStatementImport)
        .filter(
            BankStatementImport.realm_id == realm_id,
            BankStatementImport.id == statement_import_id,
        )
    )
    if lock_rows:
        import_query = import_query.with_for_update()
    statement_import = import_query.first()
    if statement_import is None:
        raise StatementImportNotFoundError(statement_import_id)
    if (
        statement_import.lineage_status != _CONTROLLED_LINEAGE
        or statement_import.lifecycle_status != _PARSED_IMPORT
        or statement_import.status != _PARSED_IMPORT
    ):
        raise StatementImportNotEligibleError(
            statement_import_id,
            "the import is not a controlled parsed statement",
        )
    if statement_import.account_id != account_id:
        raise StatementImportMismatchError(statement_import_id, "account_id")
    if statement_import.period_start != period_start:
        raise StatementImportMismatchError(statement_import_id, "period_start")
    if statement_import.period_end != period_end:
        raise StatementImportMismatchError(statement_import_id, "period_end")
    if statement_import.closing_balance is None or _q(
        statement_import.closing_balance
    ) != _q(ending_balance):
        raise StatementImportMismatchError(statement_import_id, "ending_balance")

    certificate_query = (
        db.query(BankStatementControlCertificate)
        .filter(
            BankStatementControlCertificate.realm_id == realm_id,
            BankStatementControlCertificate.statement_import_id
            == statement_import_id,
        )
        .order_by(BankStatementControlCertificate.certificate_version.desc())
    )
    if lock_rows:
        certificate_query = certificate_query.with_for_update()
    certificate = certificate_query.first()
    if certificate is None or certificate.status != _PASSED_CONTROL:
        raise StatementImportNotEligibleError(
            statement_import_id,
            "the latest statement control certificate has not passed",
        )
    if (
        certificate.source_sha256 != statement_import.source_sha256
        or certificate.parser_version != statement_import.parser_version
        or certificate.extractor_version != statement_import.extractor_version
        or certificate.prompt_schema_version
        != statement_import.prompt_schema_version
    ):
        raise StatementImportProjectionError(
            statement_import_id,
            "the latest certificate does not match the import version identity",
        )
    if certificate.closing_balance is None or _q(
        certificate.closing_balance
    ) != _q(statement_import.closing_balance):
        raise StatementImportProjectionError(
            statement_import_id,
            "the certificate closing balance does not match the import",
        )

    # An inner join makes a missing projection reduce the row count below the
    # certificate's accepted_count (the check below catches it) and remains
    # lockable on PostgreSQL; `FOR UPDATE` cannot lock an outer join's nullable side.
    projection_query = (
        db.query(BankStatementParseCandidate, BankFeedItem)
        .join(
            BankFeedItem,
            and_(
                BankFeedItem.id
                == BankStatementParseCandidate.bank_feed_item_id,
                BankFeedItem.realm_id == BankStatementParseCandidate.realm_id,
            ),
        )
        .filter(
            BankStatementParseCandidate.realm_id == realm_id,
            BankStatementParseCandidate.statement_import_id
            == statement_import_id,
            BankStatementParseCandidate.resolution_status.in_(
                _ACCEPTED_CANDIDATE_STATUSES
            ),
        )
        .order_by(BankStatementParseCandidate.source_ordinal)
    )
    if lock_rows:
        projection_query = projection_query.with_for_update()
    projection_rows = projection_query.all()
    if len(projection_rows) != certificate.accepted_count:
        raise StatementImportProjectionError(
            statement_import_id,
            "accepted candidate count does not match the passed certificate",
        )
    for candidate, feed_item in projection_rows:
        if (
            feed_item.source != "statement_upload"
            or feed_item.bank_account_ref != account_id
            or feed_item.txn_date != candidate.txn_date
            or feed_item.amount != candidate.signed_amount
        ):
            raise StatementImportProjectionError(
                statement_import_id,
                f"source ordinal {candidate.source_ordinal} conflicts with its feed projection",
            )
    return statement_import, certificate, projection_rows


def validate_statement_import_for_session(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int | None,
    account_id: str,
    period_start: date,
    period_end: date,
    ending_balance: Decimal,
) -> None:
    """Validate the exact import contract without creating or modifying a session."""
    _validated_import_projection(
        db,
        realm_id=realm_id,
        statement_import_id=statement_import_id,
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
        ending_balance=ending_balance,
        lock_rows=False,
    )


def _lock_session_creation_authority(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
) -> None:
    """Serialize the one live-session authority for a realm/account.

    PostgreSQL transaction-scoped advisory locks provide a stable authority when
    no session row exists yet. Periods deliberately do not participate: every
    period for one account draws from the same uncleared transaction pool. The
    length-prefixed identity is unambiguous even if a realm or account contains
    punctuation. SQLite remains covered by the app-layer checks plus the narrow
    exact-period savepoint recovery below; it is not evidence for production
    concurrency.
    """
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    values = (realm_id, account_id)
    lock_identity = "".join(f"{len(value)}:{value}" for value in values)
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": lock_identity},
    )


def _live_period_session(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
    period_start: date,
    period_end: date,
) -> ReconciliationSession | None:
    """Lock and return the live row for the exact creation authority."""
    return (
        db.query(ReconciliationSession)
        .populate_existing()
        .filter(
            ReconciliationSession.realm_id == realm_id,
            ReconciliationSession.account_id == account_id,
            ReconciliationSession.period_start == period_start,
            ReconciliationSession.period_end == period_end,
            live_session_status_clause(ReconciliationSession.status),
        )
        .with_for_update()
        .first()
    )


def _open_account_session(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
) -> ReconciliationSession | None:
    """Lock and return the account's OPEN row under creation authority."""
    return (
        db.query(ReconciliationSession)
        .populate_existing()
        .filter(
            ReconciliationSession.realm_id == realm_id,
            ReconciliationSession.account_id == account_id,
            open_session_status_clause(ReconciliationSession.status),
        )
        .order_by(ReconciliationSession.id)
        .with_for_update()
        .first()
    )


def _resume_existing_period_session(
    db: Session,
    *,
    existing: ReconciliationSession,
    realm_id: str,
    statement_import_id: int,
    accepted_count: int,
) -> ReconciliationSession:
    """Return an exact-import retry or refuse a different-import collision."""
    if (
        existing.lineage_status != _CONTROLLED_LINEAGE
        or existing.statement_import_id != statement_import_id
    ):
        raise OpenSessionExistsError(existing.id)
    frozen_count = (
        db.query(ReconciliationSessionStatementLine)
        .filter(
            ReconciliationSessionStatementLine.realm_id == realm_id,
            ReconciliationSessionStatementLine.reconciliation_session_id
            == existing.id,
            ReconciliationSessionStatementLine.statement_import_id
            == statement_import_id,
            ReconciliationSessionStatementLine.lineage_status
            == _CONTROLLED_LINEAGE,
        )
        .count()
    )
    if frozen_count != accepted_count:
        raise StatementImportProjectionError(
            statement_import_id,
            "existing session has incomplete frozen statement membership",
        )
    return existing


def create_session(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int | None = None,
    account_id: str,
    account_name: str,
    period_start: date,
    period_end: date,
    ending_balance: Decimal,
) -> ReconciliationSession:
    """
    Create a realm-scoped reconciliation session bound to one controlled import.

    `ending_balance` is the statement CLOSING balance the accountant reads off
    the bank statement — the target the cleared balance must tie out to. It is
    stored as `Decimal` (the Numeric(18, 2) column), never float.

    `db.flush()` (not commit) so the row joins the caller's request transaction
    and gets its `id` populated for the return; the router owns the commit
    boundary (api-contract-and-layering: the service owns data-access, the router
    owns the request lifecycle via `get_db`).

    `statement_import_id` is optional only at the Python signature boundary so a
    stale internal caller receives a typed fail-closed error instead of creating a
    legacy row. The HTTP contract requires it. New `legacy_snapshot` sessions are
    never created; that lineage exists only for migration-frozen historical rows.

    Realm-scoped: every import, certificate, candidate, feed, existing-session,
    and line query carries `realm_id`; the caller has membership-gated + pinned
    the realm GUC before this runs.

    Idempotent (duplicate-session defect): after validating the import, look for an
    existing LIVE (non-ABANDONED) session with the SAME
    (realm_id, account_id, period_start, period_end). If one exists, return it
    UNCHANGED — a re-submit (double-click, retried request) must resume the same
    reconciliation, never fork a second row for the same statement period. The
    partial-unique DB index `uq_reconciliation_session_period` (models.py) is the
    fail-closed backstop for a concurrent race the read cannot see; this
    app-layer check is the primary, race-free-in-the-common-case guard. The
    lookup is realm-scoped (the app-layer PRIMARY isolation gate on the SQLite
    test lane). ABANDONED rows are excluded on BOTH layers so an abandoned
    reconciliation (kept for audit) never blocks — nor is silently resumed by —
    a fresh start on the same statement period.

    Controlled-import guards run before any session write:
    - the import is same-realm, `controlled`, terminal `parsed`, and carries the
      exact account/period/closing balance supplied by the caller;
    - its latest code-calculated certificate is `passed`, matches the import's
      frozen parser identity, and agrees on the closing balance;
    - every accepted/corrected candidate has a canonical feed projection, and the
      projection count equals the certificate's accepted count.

    Two lifecycle guards (B5-2):
    - `PeriodAlreadyReconciledError` when the clean-start anchor date is ON or
      after the requested `period_end` — the period is already reconciled
      through the anchor (completing a rec rolls the anchor forward), and a
      re-create would open a trivially-"balanced" session whose opening equals
      its own ending with every in-period txn pre-anchor-excluded. Reopen the
      prior reconciliation instead.
    - `OpenSessionExistsError` when the account already has an OPEN session —
      two concurrent sessions share one uncleared pool; resume or abandon the
      existing one.
    """
    if statement_import_id is None:
        raise ControlledImportRequiredError(
            "new reconciliation sessions require statement_import_id"
        )

    # The selected import AND statement period are deliberately absent from this
    # identity. Every request for one account queues behind the same authority;
    # periods share one uncleared pool, so a different-period contender must
    # re-read the winner and receive the same typed conflict.
    _lock_session_creation_authority(
        db,
        realm_id=realm_id,
        account_id=account_id,
    )

    statement_import, certificate, projection_rows = _validated_import_projection(
        db,
        realm_id=realm_id,
        statement_import_id=statement_import_id,
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
        ending_balance=ending_balance,
        lock_rows=True,
    )
    statement_import_id = statement_import.id

    existing = _live_period_session(
        db,
        realm_id=realm_id,
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
    )
    if existing is not None:
        return _resume_existing_period_session(
            db,
            existing=existing,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
            accepted_count=certificate.accepted_count,
        )

    anchor = clean_start.get_anchor(db, realm_id, account_id)
    if (
        anchor.configured
        and anchor.opening_reconciled_date is not None
        and period_end <= anchor.opening_reconciled_date
    ):
        raise PeriodAlreadyReconciledError(account_id, anchor.opening_reconciled_date)

    existing_open = _open_account_session(
        db,
        realm_id=realm_id,
        account_id=account_id,
    )
    if existing_open is not None:
        raise OpenSessionExistsError(existing_open.id)

    reconciliation = ReconciliationSession(
        realm_id=realm_id,
        statement_import_id=statement_import_id,
        lineage_status=_CONTROLLED_LINEAGE,
        account_id=account_id,
        account_name=account_name,
        period_start=period_start,
        period_end=period_end,
        # Store the controlled evidence value, not a caller Decimal with excess
        # precision that merely quantized equal during validation.
        ending_balance=_q(statement_import.closing_balance),
        cleared_balance=_ZERO,
        status=STATUS_OPEN,
    )
    db.add(reconciliation)
    db.flush()
    db.add_all(
        [
            ReconciliationSessionStatementLine(
                reconciliation_session_id=reconciliation.id,
                bank_feed_item_id=feed_item.id,
                realm_id=realm_id,
                statement_import_id=statement_import_id,
                parse_chunk_id=candidate.chunk_id,
                source_ordinal=candidate.source_ordinal,
                lineage_status=_CONTROLLED_LINEAGE,
            )
            for candidate, feed_item in projection_rows
        ]
    )
    db.flush()
    return reconciliation


def _cleared_sum(db: Session, *, realm_id: str, session_id: int) -> Decimal:
    """
    Σ SIGNED amount over the book txns the match engine has flagged cleared in THIS
    session — realm-scoped.

    A book txn counts iff `cleared_session_id == session_id` (the linkage the
    match engine sets). Each row's contribution is its DIRECTION-AWARE signed
    amount (`match.signed_amount`): QBO's `TotalAmt` is unsigned (a withdrawal is
    stored POSITIVE, with the direction in `transaction_type`), so summing the raw
    column would count withdrawals as deposits and inflate the cleared balance
    (B3-4). We sum in Python over `Decimal` rather than a DB `func.sum` so the
    arithmetic is exact on every lane (SQLite returns a float from SQL SUM over
    NUMERIC; Python Decimal addition does not drift). A `None` amount contributes 0
    (handled inside `signed_amount`). Full ORM rows are loaded because the signing
    reads `transaction_type` as well as `amount`.
    """
    rows = (
        db.query(QBOTransaction)
        .filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.cleared_session_id == session_id,
        )
        .all()
    )
    total = _ZERO
    for row in rows:
        total += match_engine.signed_amount(row)
    return _q(total)


def cleared_split(
    db: Session, *, realm_id: str, session_id: int
) -> tuple[Decimal, Decimal]:
    """
    (deposits, withdrawals) split of the txns cleared in THIS session — the ONE
    canonical implementation (B5-3); the tie-out router and the reconciliation
    report builder BOTH consume this so the split can never diverge from the
    matcher's sign convention.

    Each row's contribution is its DIRECTION-AWARE signed amount
    (`match.signed_amount` — QBO's `TotalAmt` is unsigned, direction lives in
    `transaction_type`); deposits = Σ signed ≥ 0, withdrawals = Σ signed < 0, so
    `deposits + withdrawals == _cleared_sum` by construction (the DTO invariant).
    A raw-column split here counted every withdrawal as a deposit (the B3-4 /
    B5-3 bug — the report builder held a private raw copy this replaces).
    Realm-scoped; summed in Python over Decimal (SQLite SUM returns float).
    """
    rows = (
        db.query(QBOTransaction)
        .filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.cleared_session_id == session_id,
        )
        .all()
    )
    deposits = _ZERO
    withdrawals = _ZERO
    for row in rows:
        signed = match_engine.signed_amount(row)
        if signed >= 0:
            deposits += signed
        else:
            withdrawals += signed
    return _q(deposits), _q(withdrawals)


def compute_tieout(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
) -> TieOut:
    """
    Compute the tie-out for a session as a detached DTO (DEC-BREC-3 + §3).

        opening         = clean-start anchor's opening_reconciled_balance
                          (0 placeholder if unanchored — anchor_configured=False)
        cleared_sum     = Σ amount of book txns cleared in THIS session
        cleared_balance = opening + cleared_sum
        difference      = cleared_balance − ending_balance (statement closing)

    Honest anchor (financial-modeling-correctness): when the account has NO
    clean-start anchor, `opening` is a placeholder 0 and `anchor_configured` is
    False — the 0 is surfaced as unconfigured, NEVER a silently-fabricated
    baseline. A caller must not treat an unanchored tie-out as authoritative.

    Opening for a COMPLETED session (B5-6): completing a rec rolls the LIVE
    anchor forward to its own ending, so a completed session must NOT re-read
    the live anchor (opening would equal its own ending → a correct rec displays
    out-of-balance). A COMPLETE session reads its `opening_balance` SNAPSHOT
    (stamped by `try_complete`); a legacy COMPLETE row with no snapshot derives
    opening = stamped `cleared_balance` − the signed cleared sum — exact by
    construction, because the stamp was opening + cleared_sum. OPEN sessions
    keep reading the live anchor (supports mid-session re-anchoring).
    """
    cleared_sum = _cleared_sum(db, realm_id=realm_id, session_id=session.id)

    if session.status == STATUS_COMPLETE:
        if session.opening_balance is not None:
            opening = _q(session.opening_balance)
        else:
            # Legacy pre-snapshot COMPLETE row: the completion stamp was
            # opening + cleared_sum, so the original opening is recoverable
            # exactly — never read the (since rolled-forward) live anchor.
            opening = _q(Decimal(session.cleared_balance or 0) - cleared_sum)
        anchor_configured = True
        cleared_balance = _q(opening + cleared_sum)
        ending_balance = _q(session.ending_balance)
        return TieOut(
            session_id=session.id,
            realm_id=realm_id,
            account_id=session.account_id,
            opening=opening,
            anchor_configured=anchor_configured,
            cleared_sum=cleared_sum,
            cleared_balance=cleared_balance,
            ending_balance=ending_balance,
            difference=_q(cleared_balance - ending_balance),
        )

    anchor = clean_start.get_anchor(db, realm_id=realm_id, account_id=session.account_id)
    if anchor.configured and anchor.opening_reconciled_balance is not None:
        opening = _q(anchor.opening_reconciled_balance)
        anchor_configured = True
    else:
        opening = _ZERO
        anchor_configured = False
    cleared_balance = _q(opening + cleared_sum)
    ending_balance = _q(session.ending_balance)
    difference = _q(cleared_balance - ending_balance)

    return TieOut(
        session_id=session.id,
        realm_id=realm_id,
        account_id=session.account_id,
        opening=opening,
        anchor_configured=anchor_configured,
        cleared_sum=cleared_sum,
        cleared_balance=cleared_balance,
        ending_balance=ending_balance,
        difference=difference,
    )


def try_complete(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    explained_outstanding: Decimal | None = None,
) -> ReconciliationSession:
    """
    Flip an OPEN session to COMPLETE — but ONLY when it genuinely ties out.

    A session completes when the tie-out `difference` is zero, OR when the
    residual difference is fully accounted for by `explained_outstanding` (the
    caller-supplied total of legitimately-outstanding items — cheques not yet
    cleared, deposits in transit — so `difference − explained_outstanding == 0`).

    Otherwise the session STAYS OPEN and `ReconciliationNotBalancedError` is
    raised carrying the itemized `TieOut` + the residual — this service NEVER
    fabricates a zero completion (financial-modeling-correctness: honest gap).

    On completion this service OWNS the writes to the session row: it stamps
    `cleared_balance` (the tied-out figure) + `status='COMPLETE'` +
    `completed_at`. The match engine does not touch these. `db.flush()` joins the
    caller's request transaction; the router owns the commit.

    Realm-scoped: `compute_tieout` filters on `realm_id`; the caller has
    membership-gated the realm before this runs.
    """
    # Completion is the shared OPEN -> COMPLETE mutation boundary.  Enforce the
    # canonical status contract before tie-out or any session/anchor write:
    # historical NULL alone is OPEN-compatible; empty/corrupt/terminal values
    # are typed conflicts, never alternate spellings of OPEN.
    if not is_open_session_status(session.status):
        raise SessionNotCompletableError(
            session.id, effective_session_status(session.status)
        )

    tieout = compute_tieout(db, realm_id=realm_id, session=session)

    explained = _q(explained_outstanding) if explained_outstanding is not None else _ZERO
    residual = _q(tieout.difference - explained)

    if residual != _ZERO:
        # Unexplained non-zero gap — the session stays OPEN, unchanged.
        raise ReconciliationNotBalancedError(tieout, residual)

    session.cleared_balance = tieout.cleared_balance
    # Snapshot the opening BEFORE the anchor rolls forward below (B5-6): the
    # completed session's tie-out re-reads THIS stamp, never the live anchor
    # (which is about to become this rec's own ending).
    session.opening_balance = tieout.opening
    session.status = STATUS_COMPLETE
    session.completed_at = datetime.now(timezone.utc)
    db.flush()

    # Roll THIS rec's ending balance forward to the clean-start anchor so the
    # NEXT reconciliation's beginning balance defaults to it (AC-6a-3, Steven
    # feedback #2). The anchor upsert flushes into the same request transaction;
    # the router owns the commit.
    clean_start.roll_forward_anchor(
        db,
        realm_id=realm_id,
        account_id=session.account_id,
        ending_balance=session.ending_balance,
        as_of_date=session.period_end,
    )
    return session


def _completion_order_key(session: ReconciliationSession) -> tuple:
    """
    Sort key defining "latest completed" for the reopen guard: statement period
    (period_end) first, completion instant second, id as the final tiebreak.

    `completed_at` is compared via `.timestamp()` so a lane returning naive
    datetimes (SQLite) and one returning aware datetimes (Postgres) both order
    without a naive/aware comparison crash; a missing completed_at sorts oldest.
    """
    return (
        session.period_end or date.min,
        session.completed_at.timestamp() if session.completed_at is not None else float("-inf"),
        session.id,
    )


def _latest_completed_session(
    db: Session, *, realm_id: str, account_id: str
) -> ReconciliationSession | None:
    """The latest COMPLETE session for one account+realm, or None. Realm-scoped."""
    rows = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.realm_id == realm_id,
            ReconciliationSession.account_id == account_id,
            ReconciliationSession.status == STATUS_COMPLETE,
        )
        .all()
    )
    if not rows:
        return None
    return max(rows, key=_completion_order_key)


def reopen_session(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    reopened_by_user_id: int | None = None,
) -> ReopenResult:
    """
    Reopen a COMPLETED reconciliation session — the guarded inverse of
    `try_complete` (plan 20260701-bank-recon-phase-1-5-parity item 2).

    Guards (each a refusal, never a silent partial):
      1. The session must be COMPLETE. An OPEN (in-progress) session raises
         `SessionNotCompleteError` — which is also the idempotence guard: a
         second reopen call cannot trigger a second anchor rollback (AC-2d).
      2. The session must be the LATEST completed session for its account+realm
         (`_completion_order_key`); otherwise `NotLatestCompletedSessionError`.
         No mid-chain unwinds — later completions tied out against this one's
         forward-rolled ending balance (AC-1).
      3. The pre-completion opening must be PROVABLE from stored data (below);
         otherwise `AnchorRollbackUnprovableError` — refuse over guess.

    Anchor rollback (the forward-roll inverse, AC-2b). `try_complete` stamped
    `cleared_balance = opening_at_completion + Σ(cleared txn amounts)` and rolled
    the anchor forward to `(ending_balance, period_end)`. The inverse derives the
    opening from the session's own STORED fields — never by re-running the
    tie-out against the CURRENT anchor (which now holds the forward-rolled value;
    that recomputation would be circular):

        restored opening balance = stored cleared_balance − Σ(amounts of txns
                                   with cleared_session_id == session.id)
        restored anchor date     = period_start − 1 day  (the session's own
                                   stored period boundary — the day through which
                                   the books were reconciled when this session
                                   began; in the designed contiguous flow this is
                                   exactly the previous roll-forward's period_end)

    The rollback only fires when the current anchor IS still this session's own
    forward-roll (balance == ending_balance, date == period_end,
    seed_source == 'reconciliation_rollforward'). If the anchor was re-set after
    completion (e.g. a manual correction), that NEWER user-asserted anchor is
    left untouched and `anchor_rolled_back=False` is returned + audited — a
    reopen never clobbers a later baseline.

    Confirmed matches are kept intact and re-editable (AC-2c): no cleared flag is
    touched, so the reopened session's live tie-out reproduces the pre-completion
    numbers against the restored opening. Completing again re-runs the normal
    `try_complete` forward-roll — one roll, same upsert.

    Audit (AC-4, honest history): a `SystemAuditLog` row (via
    `audit.log_audit_event`, joining this request transaction) records who
    reopened, the prior completed_at (there is no completed_by column on the
    model — recorded as an honest null), the prior stamped cleared_balance /
    certificate_path, the prior anchor value, and the restored anchor value.
    Reopening never silently rewrites — the audit row is the record.

    `db.flush()` (not commit): the router owns the commit boundary. Realm-scoped
    throughout; the caller has membership-gated + pinned the realm GUC.
    """
    if effective_session_status(session.status) != STATUS_COMPLETE:
        raise SessionNotCompleteError(
            f"session {session.id} is {effective_session_status(session.status)}, "
            f"not {STATUS_COMPLETE}; "
            "only a completed reconciliation can be reopened"
        )

    latest = _latest_completed_session(
        db, realm_id=realm_id, account_id=session.account_id
    )
    if latest is None or latest.id != session.id:
        raise NotLatestCompletedSessionError(latest.id if latest is not None else None)

    if session.cleared_balance is None:
        # Without the completion-stamped cleared_balance the pre-completion
        # opening cannot be proven from stored data — refuse, never guess.
        raise AnchorRollbackUnprovableError(
            f"session {session.id} has no stamped cleared_balance; the "
            "pre-completion opening cannot be proven from stored data"
        )

    # ── derive the rollback target from STORED fields ───────────────────────
    prior_cleared_balance = _q(session.cleared_balance)
    cleared_sum = _cleared_sum(db, realm_id=realm_id, session_id=session.id)
    restored_opening = _q(prior_cleared_balance - cleared_sum)
    restored_anchor_date = session.period_start - timedelta(days=1)

    prior_completed_at = session.completed_at
    prior_certificate_path = session.certificate_path

    anchor = clean_start.get_anchor(
        db, realm_id=realm_id, account_id=session.account_id
    )
    anchor_is_this_sessions_roll = (
        anchor.configured
        and anchor.seed_source == clean_start.SEED_SOURCE_ROLLFORWARD
        and anchor.opening_reconciled_balance is not None
        and _q(anchor.opening_reconciled_balance) == _q(session.ending_balance)
        and anchor.opening_reconciled_date == session.period_end
    )

    if anchor_is_this_sessions_roll:
        clean_start.roll_back_anchor(
            db,
            realm_id=realm_id,
            account_id=session.account_id,
            opening_balance=restored_opening,
            as_of_date=restored_anchor_date,
        )

    # ── audit BEFORE the state flip (same transaction — atomic with it) ─────
    audit.log_audit_event(
        db=db,
        company_id=realm_id,
        action="REOPEN",
        entity_type="ReconciliationSession",
        entity_id=str(session.id),
        user_id=reopened_by_user_id,
        previous_state={
            "status": STATUS_COMPLETE,
            "completed_at": (
                prior_completed_at.isoformat() if prior_completed_at else None
            ),
            # The model carries no completed_by column — recorded as an honest
            # null, never fabricated from the reopener's identity.
            "completed_by": None,
            "cleared_balance": str(prior_cleared_balance),
            "certificate_path": prior_certificate_path,
            "anchor": {
                "opening_reconciled_balance": (
                    str(anchor.opening_reconciled_balance)
                    if anchor.opening_reconciled_balance is not None
                    else None
                ),
                "opening_reconciled_date": (
                    anchor.opening_reconciled_date.isoformat()
                    if anchor.opening_reconciled_date is not None
                    else None
                ),
                "seed_source": anchor.seed_source,
                "configured": anchor.configured,
            },
        },
        new_state={
            "status": STATUS_OPEN,
            "anchor_rolled_back": anchor_is_this_sessions_roll,
            "restored_anchor": (
                {
                    "opening_reconciled_balance": str(restored_opening),
                    "opening_reconciled_date": restored_anchor_date.isoformat(),
                    "seed_source": clean_start.SEED_SOURCE_REOPEN_ROLLBACK,
                }
                if anchor_is_this_sessions_roll
                else None
            ),
            "reopened_by_user_id": reopened_by_user_id,
        },
    )

    # ── flip the session back to in-progress (completion fields cleared but
    # PRESERVED in the audit row above; the cleared set is untouched) ────────
    session.status = STATUS_OPEN
    session.completed_at = None
    session.certificate_path = None
    db.flush()

    return ReopenResult(
        session_id=session.id,
        status=session.status,
        anchor_rolled_back=anchor_is_this_sessions_roll,
        restored_opening=restored_opening if anchor_is_this_sessions_roll else None,
        restored_anchor_date=(
            restored_anchor_date if anchor_is_this_sessions_roll else None
        ),
    )


def outstanding_items(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
) -> list[QBOTransaction]:
    """
    List the book transactions in this account that are NOT cleared in this
    session — the itemized outstanding set behind a non-zero tie-out gap.

    Realm-scoped and account-scoped: a book txn is outstanding for THIS session
    when its `bank_account_ref` (the bank/CC posting account — AC-5 repoint;
    `account_ref` is classification's CATEGORY column, NULL = pending) matches
    the session's account and it has NEVER been cleared (B5-1): a txn cleared by
    ANY session — this one or a prior one — is already reconciled, not
    outstanding (this mirrors `find_matches`' cleared-set exclusion; the old
    `!= session.id` filter re-listed a prior rec's cleared txns as outstanding).
    Windowed exactly like the match side: `date <= period_end` (the statement
    closing — B3-3) AND after the clean-start anchor (`exclude_pre_anchor` — a
    pre-anchor txn predates the reconciliation baseline and is not this rec's
    outstanding item). Ordered by date then id for a stable, reviewable list.

    Returns live ORM rows for the caller (a router) to serialize under a
    response_model within the request scope.
    """
    # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
    rows = (
        db.query(QBOTransaction)
        .filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.bank_account_ref == session.account_id,
            QBOTransaction.date <= session.period_end,
            QBOTransaction.cleared_session_id.is_(None),
            QBOTransaction.cleared.isnot(True),
        )
        .order_by(QBOTransaction.date, QBOTransaction.id)
        .all()
    )
    anchor_date = clean_start.match_start_date(db, realm_id, session.account_id)
    return clean_start.exclude_pre_anchor(
        rows, anchor_date, date_getter=lambda t: t.date
    )


def abandon_session(
    db: Session,
    *,
    realm_id: str,
    session: ReconciliationSession,
    user_id: int | None = None,
) -> ReconciliationSession:
    """
    Abandon an OPEN reconciliation session (B5-2): release every clear it owns
    (via `match.release_session_clears` — flags cleared, match rows REJECTED, the
    txns return to the pool) and flip `status` to ABANDONED. The row is KEPT for
    audit — never deleted; an audit event records who abandoned it and how many
    clears were released.

    Only OPEN abandons: a COMPLETE rec is a finished financial record — reversing
    it is `reopen_session`'s audited job; an already-ABANDONED session is refused
    for idempotence (`SessionNotAbandonableError` both ways). Touches NO anchor
    state — an OPEN session never rolled the anchor forward, so there is nothing
    to roll back. `db.flush()` joins the caller's transaction; the router owns
    the commit.
    """
    if not is_open_session_status(session.status):
        raise SessionNotAbandonableError(
            session.id, effective_session_status(session.status)
        )

    released = match_engine.release_session_clears(
        db, realm_id=realm_id, session=session
    )
    session.status = STATUS_ABANDONED
    db.flush()

    audit.log_audit_event(
        db,
        company_id=realm_id,
        action="bank_recon_session_abandoned",
        entity_type="reconciliation_session",
        entity_id=str(session.id),
        user_id=user_id,
        previous_state={"status": STATUS_OPEN},
        new_state={"status": STATUS_ABANDONED, "clears_released": released},
    )
    return session

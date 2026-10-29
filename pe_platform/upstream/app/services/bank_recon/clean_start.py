"""
Clean-start anchor service for bank reconciliation (C0-b, AC-0.3).

The manual-primary clean-start anchor: a per-account `BankReconAccountConfig`
row (models.py:2172) records the last reconciled balance + date the USER
confirmed. Reconciliation NEVER touches transactions dated on/before that
anchor date, so a fresh reconciliation starts from a known-good baseline
instead of re-tying-out years of already-reconciled history.

Layering (api-contract-and-layering SKILL): this is the FAT service — all the
read/write logic lives here; the router (returned as text to the orchestrator,
NOT written here) is a THIN wrapper that authorizes + pins the realm GUC via
`check_tenant_access(...)` and calls one function below.

Realm scoping (multi-tenancy-app-layer SKILL): `bank_recon_account_configs` is
realm-keyed and enrolled in the live Postgres RLS backstop on the `realm` GUC
(models.py:2183-2190; RLS migration e7f1a9c3d2b5). The CALLER (the router) pins
the realm scope by calling `check_tenant_access("EDITOR"/"VIEWER",
company_id=realm_id, db=db, user=current_user)` which routes through
`_check_company_access` → `pin_tenant_scope_live(db, org_id=..., realm_id=...)`
(dependencies.py:204). Every query in this service therefore ALSO carries an
explicit `realm_id ==` predicate — the app-layer predicate is the PRIMARY gate
(it is what runs on the SQLite test lane, which has no RLS), and RLS is the
second wall. Never authorize off a client-supplied realm; the router has already
membership-gated it before it reaches here.

# DEFERRED (AC-0.3): QBO-inference clean-start.
# The QBO `cleared=Reconciled` TransactionList inference (deriving an anchor by
# enumerating already-reconciled QBO transactions) is a SEPARATE, deferred
# follow-on — it is only an error-bounded approximation (UNPROVEN, 75%
# confidence; SYNTHESIS DEC-BREC-5) and is gated on a confirmed-reachable QBO
# sandbox proof. It is deliberately NOT built here: the manual anchor below is
# the authoritative, buildable PRIMARY path and stands alone. No QBO call, no
# inference, no TransactionList read lives in this module. When the deferred
# path lands it will WRITE the same BankReconAccountConfig row with
# seed_source='qbo_inferred' after a user confirms the suggestion.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TypeVar

from sqlalchemy.orm import Session

from ...models import BankReconAccountConfig

T = TypeVar("T")

# seed_source discriminators for BankReconAccountConfig.seed_source.
# "manual" — the user set/corrected the anchor directly (the PRIMARY path).
# "reconciliation_rollforward" — a COMPLETED reconciliation rolled its ending
#   balance forward to become the NEXT reconciliation's clean-start anchor
#   (session.try_complete; AC-6a-2/AC-6a-3).
# "reconciliation_reopen" — a completed reconciliation was REOPENED and its
#   forward-roll was inverted back to the session's opening (session.reopen_session;
#   plan 20260701-bank-recon-phase-1-5-parity item 2). Honest provenance: the anchor
#   was system-rolled BACK, not user-set and not a completion roll-forward.
# "qbo_inferred" — reserved for the DEFERRED QBO-inference follow-on (not built).
SEED_SOURCE_MANUAL = "manual"
SEED_SOURCE_ROLLFORWARD = "reconciliation_rollforward"
SEED_SOURCE_REOPEN_ROLLBACK = "reconciliation_reopen"


@dataclass(frozen=True)
class CleanStartAnchor:
    """
    Plain, detached DTO for a clean-start anchor — built while the session is
    open and returned by value so a caller (router) never reads attributes off a
    Session-bound ORM row after the request transaction closes
    (api-contract-and-layering: the ORM object never crosses the boundary).

    `opening_reconciled_date` / `opening_reconciled_balance` are None when the
    account has a config row that has not yet been anchored (columns are
    nullable, models.py:2194-2195); `configured` is False when no row exists.
    """

    realm_id: str
    account_id: str
    opening_reconciled_balance: Decimal | None
    opening_reconciled_date: date | None
    seed_source: str | None
    configured: bool


def _to_anchor(config: BankReconAccountConfig) -> CleanStartAnchor:
    return CleanStartAnchor(
        realm_id=config.realm_id,
        account_id=config.account_id,
        opening_reconciled_balance=config.opening_reconciled_balance,
        opening_reconciled_date=config.opening_reconciled_date,
        seed_source=config.seed_source,
        configured=True,
    )


def get_or_none(
    db: Session, realm_id: str, account_id: str
) -> BankReconAccountConfig | None:
    """
    Return the realm-scoped `BankReconAccountConfig` ORM row for one account, or
    None if the account has no anchor row yet.

    Realm-scoped: filters on `realm_id ==` (the app-layer PRIMARY gate) AND
    `account_id ==`. The caller must have pinned the realm GUC (via
    `check_tenant_access`) before calling — under FORCE-RLS an unpinned request
    reads zero rows (a broken read), never another tenant's row.

    Returns the live ORM row (used internally by `upsert_manual_anchor` and by a
    router that immediately serializes it under a response_model); callers that
    return a value out of the session scope should use `get_anchor` /
    `match_start_date` instead, which return detached values.
    """
    return (
        db.query(BankReconAccountConfig)
        .filter(
            BankReconAccountConfig.realm_id == realm_id,
            BankReconAccountConfig.account_id == account_id,
        )
        .first()
    )


def get_anchor(db: Session, realm_id: str, account_id: str) -> CleanStartAnchor:
    """
    Read the clean-start anchor for an account as a detached DTO.

    Returns an unconfigured `CleanStartAnchor` (configured=False, all anchor
    fields None) when no config row exists — the honest "not yet anchored"
    state, never a fabricated zero-dollar anchor (a fake $0/None anchor would
    silently let reconciliation touch ALL of history).
    """
    config = get_or_none(db, realm_id, account_id)
    if config is None:
        return CleanStartAnchor(
            realm_id=realm_id,
            account_id=account_id,
            opening_reconciled_balance=None,
            opening_reconciled_date=None,
            seed_source=None,
            configured=False,
        )
    return _to_anchor(config)


def upsert_manual_anchor(
    db: Session,
    realm_id: str,
    account_id: str,
    opening_reconciled_balance: Decimal,
    opening_reconciled_date: date,
) -> CleanStartAnchor:
    """
    Set or correct the MANUAL clean-start anchor for an account and return it as
    a detached DTO.

    Manual override is PRIMARY and standalone (AC-0.3): this always stamps
    `seed_source='manual'`, so re-anchoring an account previously seeded by the
    deferred QBO-inference path re-asserts it as user-authoritative.

    Idempotent upsert: updates the existing realm-scoped row in place, or inserts
    a new one. `db.flush()` (not commit) so the write joins the caller's request
    transaction — the router owns the commit boundary
    (api-contract-and-layering: the service owns the data-access, the router owns
    the request lifecycle via `get_db`).

    Realm-scoped: the lookup + the inserted row both carry `realm_id`; the caller
    has membership-gated + pinned the realm GUC before this runs.
    """
    config = get_or_none(db, realm_id, account_id)
    if config is None:
        config = BankReconAccountConfig(
            realm_id=realm_id,
            account_id=account_id,
        )
        db.add(config)

    config.opening_reconciled_balance = opening_reconciled_balance
    config.opening_reconciled_date = opening_reconciled_date
    config.seed_source = SEED_SOURCE_MANUAL

    db.flush()
    return _to_anchor(config)


def roll_forward_anchor(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
    ending_balance: Decimal,
    as_of_date: date,
) -> CleanStartAnchor:
    """
    Roll a just-COMPLETED reconciliation's ending balance forward to become the
    NEXT reconciliation's clean-start anchor, and return it as a detached DTO.

    The roll-forward is the automatic complement to `upsert_manual_anchor`
    (AC-6a-2, Steven feedback #2): when a session ties out and flips COMPLETE,
    its ending balance IS the confirmed opening baseline for the next period, so
    the next reconciliation's beginning balance defaults to it instead of forcing
    the user to re-enter it. Stamps `seed_source='reconciliation_rollforward'` so
    the anchor's provenance (system-rolled, not user-set) is legible.

    Idempotent upsert mirroring `upsert_manual_anchor`: updates the existing
    realm-scoped row in place, or inserts a new one. `db.flush()` (not commit) so
    the write joins the caller's request transaction — `session.try_complete`
    already runs inside the router's commit boundary.

    Realm-scoped: the lookup + the inserted row both carry `realm_id`; the caller
    (session.try_complete) has membership-gated + pinned the realm GUC upstream.
    """
    config = get_or_none(db, realm_id, account_id)
    if config is None:
        config = BankReconAccountConfig(
            realm_id=realm_id,
            account_id=account_id,
        )
        db.add(config)

    config.opening_reconciled_balance = ending_balance
    config.opening_reconciled_date = as_of_date
    config.seed_source = SEED_SOURCE_ROLLFORWARD

    db.flush()
    return _to_anchor(config)


# PLAN-EXEMPT: ratified plan 20260701-bank-recon-phase-1-5-parity — item 2 (reopen)
# strictly requires the anchor-ROLLBACK writer to live beside its forward-roll twin
# (this module owns every BankReconAccountConfig write); the dispatch authorized a
# helper here when the rollback strictly requires it.
def roll_back_anchor(
    db: Session,
    *,
    realm_id: str,
    account_id: str,
    opening_balance: Decimal,
    as_of_date: date,
) -> CleanStartAnchor:
    """
    Roll the clean-start anchor BACK to a reopened reconciliation's opening —
    the inverse of `roll_forward_anchor` — and return it as a detached DTO.

    Called ONLY by `session.reopen_session` after it has (a) verified the session
    being reopened is the LATEST completed session for the account and (b) proved
    from the session's stored fields that the current anchor is that session's
    own forward-roll (so this write undoes exactly one roll, never a later one,
    and never clobbers a manual re-anchor). Stamps
    `seed_source='reconciliation_reopen'` so the anchor's provenance (system
    rolled-BACK on reopen) stays legible — honest history, never a silent rewrite
    (the caller writes the audit row recording both values).

    Idempotent upsert mirroring `roll_forward_anchor`: updates the existing
    realm-scoped row in place, or inserts one. `db.flush()` (not commit) so the
    write joins the caller's request transaction — the router owns the commit.

    Realm-scoped: the lookup + the inserted row both carry `realm_id`; the caller
    (session.reopen_session) has been membership-gated + realm-pinned upstream.
    """
    config = get_or_none(db, realm_id, account_id)
    if config is None:
        config = BankReconAccountConfig(
            realm_id=realm_id,
            account_id=account_id,
        )
        db.add(config)

    config.opening_reconciled_balance = opening_balance
    config.opening_reconciled_date = as_of_date
    config.seed_source = SEED_SOURCE_REOPEN_ROLLBACK

    db.flush()
    return _to_anchor(config)


def match_start_date(
    db: Session, realm_id: str, account_id: str
) -> date | None:
    """
    Return the clean-start anchor date for an account, or None if unset.

    This is the single source of truth the match engine (a later chunk, C1-b)
    reads to bound its candidate set. None means "no anchor configured" — the
    match engine MUST treat None as "do not reconcile yet" (surface an honest
    "set a clean start first" state), NOT as "reconcile all of history".
    """
    anchor = get_anchor(db, realm_id, account_id)
    return anchor.opening_reconciled_date


def is_pre_anchor(txn_date: date | None, anchor_date: date | None) -> bool:
    """
    The pre-anchor exclusion predicate — the one boundary rule the match engine
    (C1-b) applies so reconciliation NEVER touches a transaction dated ON or
    BEFORE the clean-start date.

    Boundary (AC-0.3, load-bearing): a transaction dated EXACTLY ON the anchor
    date is EXCLUDED (`txn_date <= anchor_date`). The anchor date is the last
    already-reconciled day, so its transactions are part of the confirmed
    opening baseline, not the new session's candidate set.

    Fail-safe defaults:
      - anchor_date is None (account not yet anchored) → returns False: with no
        anchor there is nothing to exclude BY DATE. The match engine must
        separately gate on `match_start_date(...) is None` before reconciling —
        this predicate only answers "is this txn before a KNOWN anchor".
      - txn_date is None (an undated candidate) → returns False: an undated txn
        is not provably pre-anchor, so it is not excluded here (the engine
        surfaces it for review rather than silently dropping it).
    """
    if anchor_date is None or txn_date is None:
        return False
    return txn_date <= anchor_date


def exclude_pre_anchor(
    txns: Iterable[T],
    anchor_date: date | None,
    *,
    date_getter: Callable[[T], date | None] = lambda t: t.txn_date,
) -> list[T]:
    """
    Filter an iterable of transaction-like objects down to the post-anchor set
    the match engine may reconcile — the applied form of `is_pre_anchor`.

    `date_getter` extracts the comparison date from each item (default:
    `t.txn_date`) so the match engine can pass QBOTransaction rows, statement
    lines, or plain DTOs without this service importing their type. Returns a
    list preserving input order; any item whose date is ON or before the anchor
    is dropped. With `anchor_date=None` NOTHING is excluded by date (see the
    `is_pre_anchor` fail-safe note) — the engine gates the None case upstream.
    """
    return [t for t in txns if not is_pre_anchor(date_getter(t), anchor_date)]

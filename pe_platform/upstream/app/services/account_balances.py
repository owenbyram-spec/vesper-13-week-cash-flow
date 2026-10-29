"""Per-period account-balance snapshots (0D).

`QBOAccount.current_balance` is a single scalar; `QBOAccountBalanceSnapshot` records
one balance per account per month (written on sync) so period-over-period deltas —
e.g. a debt account's principal reduction, the trailing-DSCR term — become derivable.
Forward-only: history accrues from when 0D ships; a delta needs >=2 synced months,
and `get_period_delta` returns None on a missing period so the consumer surfaces
"unavailable" rather than a fabricated 0.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import QBOAccountBalanceSnapshot


def record_balance_snapshots(
    db: Session,
    realm_id: str,
    organization_id: int | None,
    balances_by_qbo_id: dict[str, float | None],
    period: str,
) -> int:
    """Upsert one snapshot per account for `period` ('YYYY-MM'). Idempotent: a re-run
    for the same period updates the balance, never duplicates. Batched — one query for
    the existing period rows, then update-or-add in memory (no N+1). Does not commit;
    the caller owns the transaction.
    """
    if not balances_by_qbo_id:
        return 0
    qbo_ids = list(balances_by_qbo_id.keys())
    existing = {
        row.qbo_account_id: row
        for row in db.query(QBOAccountBalanceSnapshot).filter(
            QBOAccountBalanceSnapshot.realm_id == realm_id,
            QBOAccountBalanceSnapshot.period == period,
            QBOAccountBalanceSnapshot.qbo_account_id.in_(qbo_ids),
        ).all()
    }
    for qbo_id, balance in balances_by_qbo_id.items():
        # SA-OK: dict.get on the batched `existing` map (one query above), not a DB query
        row = existing.get(qbo_id)
        if row is None:
            db.add(QBOAccountBalanceSnapshot(
                organization_id=organization_id,
                realm_id=realm_id,
                qbo_account_id=qbo_id,
                period=period,
                balance=balance,
            ))
        else:
            row.balance = balance
            if organization_id is not None:
                row.organization_id = organization_id
    return len(balances_by_qbo_id)


def get_balance_series(
    db: Session,
    realm_id: str,
    qbo_account_id: str,
) -> list[tuple[str, float | None]]:
    """Ordered [(period, balance)] for one account, realm-scoped."""
    rows = db.query(QBOAccountBalanceSnapshot).filter(
        QBOAccountBalanceSnapshot.realm_id == realm_id,
        QBOAccountBalanceSnapshot.qbo_account_id == qbo_account_id,
    ).order_by(QBOAccountBalanceSnapshot.period).all()
    return [(r.period, float(r.balance) if r.balance is not None else None) for r in rows]


def get_period_delta(
    db: Session,
    realm_id: str,
    qbo_account_id: str,
    period_from: str,
    period_to: str,
) -> float | None:
    """`balance_from - balance_to` (positive = paid down over the window). Returns None
    if either period's snapshot is missing — the consumer surfaces "unavailable", never
    a fabricated 0.
    """
    rows = {
        r.period: r.balance
        for r in db.query(QBOAccountBalanceSnapshot).filter(
            QBOAccountBalanceSnapshot.realm_id == realm_id,
            QBOAccountBalanceSnapshot.qbo_account_id == qbo_account_id,
            QBOAccountBalanceSnapshot.period.in_([period_from, period_to]),
        ).all()
    }
    b_from = rows.get(period_from)
    b_to = rows.get(period_to)
    if b_from is None or b_to is None:
        return None
    return float(b_from) - float(b_to)

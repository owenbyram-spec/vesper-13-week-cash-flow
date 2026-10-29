"""
app/budget/period_locks.py
───────────────────────────
Bridge between the accounting month-end close and the budget module.

The authoritative "this month is locked for editing" signal lives in the
accounting layer as ``FinancialCalendar.locked`` (per ``realm_id`` + ``period``
in ``YYYY-MM`` form — see app/routers/accounting.py). The budget module speaks a
different dialect: ``company_id`` + a three-letter month key ("jan".."dec") within
a ``budget_year`` (see app/budget/constants.py). Crucially, ``company_id`` IS the
``realm_id`` — both are the QBO realm id (get_qbo_client_from_db takes the budget
``company_id`` as its ``realm_id`` argument), so no id translation is needed, only
a period ⇄ (year, month-key) format conversion.

This module converts between the two and answers ONE question for the budget UI:
which months of a budget year are closed in the accounting month-end close, and
therefore should surface as read-only in the budget grid so they can be analysed
for budget-vs-actual. It is READ-ONLY and one-directional — reflecting the
accounting close into the budget view. Closing a period never writes a
``BudgetMonthLock``; locking a month in the budget module never closes an
accounting period.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import FinancialCalendar
from .constants import MONTHS


def period_for(budget_year: int, month_key: str) -> str | None:
    """('2026', 'mar') → '2026-03'. Returns None for an unknown month key."""
    key = month_key.lower()
    if key not in MONTHS:
        return None
    return f"{budget_year}-{MONTHS.index(key) + 1:02d}"


def month_key_for(period: str) -> str | None:
    """'2026-03' → 'mar'. Returns None if the period isn't a 1..12 month in YYYY-MM form."""
    parts = period.split("-")
    if len(parts) != 2:
        return None
    try:
        month_num = int(parts[1])
    except ValueError:
        return None
    if not 1 <= month_num <= 12:
        return None
    return MONTHS[month_num - 1]


def accounting_closed_month_keys(
    db: Session, *, company_id: str, budget_year: int
) -> list[str]:
    """Return the budget month keys whose accounting period is hard-locked (closed).

    A month is included when a ``FinancialCalendar`` row for this realm
    (``realm_id == company_id``) and this year's period (``{budget_year}-MM``) has
    ``locked == True``. Result is ordered Jan→Dec and de-duplicated.

    Realm-scoped by the explicit ``realm_id`` predicate — the caller has already
    authorized the principal on ``company_id`` and pinned the realm GUC.
    """
    rows = (
        db.query(FinancialCalendar.period)
        .filter(
            FinancialCalendar.realm_id == company_id,
            FinancialCalendar.period.like(f"{budget_year}-%"),
            FinancialCalendar.locked.is_(True),
        )
        .all()
    )
    keys = {
        key
        for (period,) in rows
        if (key := month_key_for(period)) is not None
    }
    return sorted(keys, key=MONTHS.index)


def budget_locked_month_keys(db: Session, *, company_id: str, budget_year: int) -> list[str]:
    """The shared manual-budget + hard-accounting lock contract."""
    from ..models import BudgetMonthLock

    manual = db.query(BudgetMonthLock.month).filter(
        BudgetMonthLock.company_id == company_id,
        BudgetMonthLock.budget_year == budget_year,
    ).all()
    return sorted(
        {m for (m,) in manual if m in MONTHS}
        | set(accounting_closed_month_keys(db, company_id=company_id, budget_year=budget_year)),
        key=MONTHS.index,
    )

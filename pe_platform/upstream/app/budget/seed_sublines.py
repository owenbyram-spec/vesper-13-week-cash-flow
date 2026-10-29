"""Vendor / customer sub-lines for a budget seeded from P&L actuals.

Pure planning (no DB, no QuickBooks). A parent line's displayed amount is the sum of
its sub-lines (app/budget/rollups.py, GET /budget), so sub-lines built only from
vendor- or customer-tagged transactions would drop everything without a payee
(payroll, payee-less card spend, untagged journals) from the budget. Every parent
that receives named sub-lines therefore also gets an Unallocated sub-line equal to
the P&L-seeded parent minus the named sub-lines, month by month, so the rollup
always ties to the P&L.
"""
from __future__ import annotations

import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, NamedTuple, Optional

from .constants import MONTHS

log = logging.getLogger(__name__)

UNALLOCATED_SUBLINE_NAME = "Unallocated"
_UNALLOCATED_FALLBACK_NAME = "Unallocated (P&L residual)"
_CENT = Decimal("0.01")


def unallocated_note(kind: str) -> str:
    return f"P&L amount not attributed to a named {kind} in QuickBooks"


def _normalize(name: str) -> str:
    """Strip leading account number and whitespace, lowercase."""
    return re.sub(r"^\d+\s*", "", name).strip().lower()


def find_parent(acct_name: str, type_map: Mapping[str, int]) -> Optional[int]:
    """Find parent line id with exact, then normalized, then substring match.
    Uses the type-specific map so same-named accounts in different types don't collide."""
    t_norm: Dict[str, int] = {}
    for name, pid in type_map.items():
        t_norm[_normalize(name)] = pid
        if ":" in name:
            t_norm[_normalize(name.split(":")[-1])] = pid

    if acct_name in type_map:
        return type_map[acct_name]
    n = _normalize(acct_name)
    if n in t_norm:
        return t_norm[n]
    if ":" in acct_name:
        n2 = _normalize(acct_name.split(":")[-1])
        if n2 in t_norm:
            return t_norm[n2]
    for pname, pid in type_map.items():
        pn = _normalize(pname)
        if pn and (pn in n or n in pn):
            return pid
    return None


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


class PlannedSubline(NamedTuple):
    name: str
    monthly_amounts: Dict[str, float]
    unallocated: bool


def plan_sublines(
    parent_amounts: Mapping[int, Mapping[str, float]],
    breakdown: Mapping[str, Mapping[str, Mapping[str, float]]],
    type_map: Mapping[str, int],
    multiplier: Decimal,
) -> Dict[int, List[PlannedSubline]]:
    """parent line id -> the sub-lines to create under it.

    ``breakdown`` is {QBO account name: {vendor/customer: {month: signed amount}}};
    accounts are matched to parents with find_parent and the same name under several
    matched accounts is merged. Named sub-lines are scaled by ``multiplier`` and
    rounded to cents; a parent whose named sub-lines net to zero in every month keeps
    its own amounts (no sub-lines). Otherwise an Unallocated sub-line carries
    parent - sum(named) for each month whenever that residual is non-zero (negative
    when the attribution exceeds the P&L), so the sub-lines sum to the parent exactly.
    """
    per_parent: Dict[int, Dict[str, Dict[str, Decimal]]] = {}
    for acct_name, by_name in breakdown.items():
        parent_id = find_parent(acct_name, type_map)
        if parent_id is None:
            log.info("Budget seeding: no parent match for QBO account %r", acct_name)
            continue
        target = per_parent.setdefault(parent_id, {})
        for name, monthly in by_name.items():
            row = target.setdefault(name, {m: Decimal("0") for m in MONTHS})
            for m in MONTHS:
                row[m] += Decimal(str(monthly.get(m, 0) or 0))

    planned: Dict[int, List[PlannedSubline]] = {}
    for parent_id, by_name in per_parent.items():
        named = {
            name: {m: _cents(v * multiplier) for m, v in monthly.items()}
            for name, monthly in by_name.items()
        }
        named = {name: monthly for name, monthly in named.items() if any(monthly.values())}
        if not named:
            continue
        parent = parent_amounts.get(parent_id) or {}
        residual = {
            m: _cents(Decimal(str(parent.get(m, 0) or 0)) - sum((sub[m] for sub in named.values()), Decimal("0")))
            for m in MONTHS
        }
        subs = [
            PlannedSubline(name, {m: float(v) for m, v in monthly.items()}, unallocated=False)
            for name, monthly in named.items()
        ]
        if any(residual.values()):
            label = UNALLOCATED_SUBLINE_NAME if UNALLOCATED_SUBLINE_NAME not in named else _UNALLOCATED_FALLBACK_NAME
            subs.append(PlannedSubline(label, {m: float(v) for m, v in residual.items()}, unallocated=True))
        planned[parent_id] = subs
    return planned

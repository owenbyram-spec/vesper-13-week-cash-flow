# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns app/services/review_checks/** (plan.md Appendix A); the active-plan.json surface_scope entry carries an inline comment inside the glob string so the gate matcher misses it — scope is ratified, the state string is malformed.
"""report_lines — shared numeric line extraction for QBO report JSON.

Chunk C2C-CLOSE (plan 20260706-cfo-grade-financial-workflows, AC-2C.1). The flux
check and the BS roll-forward coverage check both need LEAF-ACCOUNT lines
(label + optional QBO account id + numeric value) out of a BalanceSheet report.
Per the qbo-integration one-shared-parser rule this module REUSES the existing
walkers instead of adding a divergent one:

  - P&L leaf lines: callers use the ONE canonical single-month parser
    ``app.budget.qbo_reports._parse_pl_single_month_entries`` directly (the same
    parser the covenant EBITDA add-backs read through, coa_formula.py:371).
  - BS leaf lines: this module walks with the existing
    ``app.services.coa_formula._flatten_rows`` recursion and reads each Data
    row's first (label/id) + last (money) column — the same ColData shape
    ``_get_bs_total`` / ``_bs_row_amount`` read.

No fabricated zeros: an unparseable money cell yields NO line (the caller sees
the account as absent and degrades honestly), never a fabricated 0.0 balance.
"""

from __future__ import annotations

from typing import TypedDict


class ReportLeafLine(TypedDict):
    """One leaf (Data-row) account line from a QBO report."""

    label: str
    account_id: str | None
    value: float


def _parse_money(raw: object) -> float | None:
    """Parse a QBO money cell; None (never 0) when unparseable/absent."""
    try:
        text = str(raw).replace(",", "").strip()
        if not text:
            return None
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        return float(text)
    except (ValueError, TypeError):
        return None


def bs_leaf_lines(report: dict) -> list[ReportLeafLine]:
    """Extract leaf account lines from a BalanceSheet report.

    Walks every nested row via the existing ``coa_formula._flatten_rows``
    recursion and keeps rows that carry ColData with a non-empty, non-"Total"
    label and a parseable money value in the LAST column (the QBO BS Data-row
    shape: ColData[0] = account label (+``id`` when QBO supplies it),
    ColData[-1] = the as-of balance).
    """
    from ..coa_formula import _flatten_rows

    lines: list[ReportLeafLine] = []
    rows = report.get("Rows", {}).get("Row", []) if isinstance(report, dict) else []
    for row in _flatten_rows(rows):
        col_data = row.get("ColData", [])  # SA-OK: dict access on parsed report JSON, not a DB query
        if len(col_data) < 2:
            continue
        label = str(col_data[0].get("value", "") or "").strip()  # SA-OK: dict access on report ColData, not a DB query
        if not label or label.lower().startswith("total"):
            continue
        value = _parse_money(col_data[-1].get("value"))  # SA-OK: dict access on report ColData, not a DB query
        if value is None:
            continue  # honest absence — never a fabricated 0 balance
        account_id = str(col_data[0].get("id", "") or "").strip() or None  # SA-OK: dict access on report ColData, not a DB query
        lines.append(ReportLeafLine(label=label, account_id=account_id, value=value))
    return lines

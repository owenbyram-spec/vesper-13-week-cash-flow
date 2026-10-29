# Plan 20260713-budget-gl-detail-resolution — robust GL-detail resolution + fetch for the
# budget vs-actuals AI analysis. Extracted from app/routers/budget.py so the resolution
# branches and the "why is it empty" outcome are unit-testable against a fake QBO client.
"""Resolve a budget account to its QBO Account Id (reliably) and fetch its per-entity
(customer/vendor) GL actuals — returning WHY the result is what it is.

The bug this fixes: the vs-actuals AI analysis reported "No GL detail available" for a
revenue account with working QBO access. The GL sub-fetch depended on a possibly-null
``qbo_account_id`` with a fragile ``Name LIKE`` fallback, the row parser assumed fixed
column positions (now header-located in ``qbo_reports.parse_gl_vendor_totals``), and a broad
except swallowed the reason. Here resolution prefers the account NUMBER (QBO ``AcctNum`` — an
exact key), and every empty result carries a status so it is never a black box.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..services.redaction import redact_to_string
from .driver_calculation import BudgetPlanError, money
from .qbo_reports import parse_gl_vendor_totals

logger = logging.getLogger(__name__)

_LEADING_NUM_RE = re.compile(r"^\s*(\d+)\b")

# Resolution reasons.
R_STORED = "stored"
R_BY_ACCTNUM = "by_acctnum"
R_BY_NAME = "by_name"
R_UNRESOLVED = "unresolved"

# GL-detail outcome statuses (surfaced to the log + response).
GL_RESOLVED_DETAIL = "resolved_detail"
GL_RESOLVED_EMPTY = "resolved_empty"
GL_UNRESOLVED = "unresolved"
GL_REPORT_ERROR = "report_error"


def leading_account_number(account_name: str) -> str | None:
    """The leading account-number token of a display name ('4565 Administrative Services'
    → '4565'), or None when the name does not start with digits."""
    match = _LEADING_NUM_RE.match(account_name or "")
    return match.group(1) if match else None


def _list_accounts(client) -> list[dict]:
    """Every account (Id, Name, AcctNum, FullyQualifiedName) in ONE query.

    QBO does NOT allow filtering by ``AcctNum`` in a WHERE clause — ``AcctNum`` is a
    RETURNABLE field but not a QUERYABLE one, so ``WHERE AcctNum = '4565'`` is rejected
    with a 400 Bad Request. We fetch the account list once and match ``AcctNum`` in Python.
    """
    rows = client.query(
        "SELECT Id, Name, AcctNum, FullyQualifiedName FROM Account MAXRESULTS 1000"
    )  # QBO-OK: minorversion-pinned-upstream (query() pins minorversion=73)
    return rows.get("QueryResponse", {}).get("Account", [])  # SA-OK: in-memory QBO query response, not a DB query


def resolve_qbo_account_id(client, budget_line, account_name: str) -> tuple[str | None, str]:
    """Resolve the QBO Account Id for a budget line, preferring the reliable keys.

    Order: the stored ``qbo_account_id`` → QBO ``AcctNum`` (the leading account number, an
    exact key) → a ``Name`` substring match (the legacy fallback). Returns
    ``(account_id, reason)`` with reason ∈ {stored, by_acctnum, by_name, unresolved}. QBO
    query failures propagate to the caller (which classifies them as report_error).
    """
    stored = getattr(budget_line, "qbo_account_id", None) if budget_line is not None else None
    if stored:
        return str(stored), R_STORED

    accounts = _list_accounts(client)

    acct_num = leading_account_number(account_name)
    if acct_num:
        for account in accounts:                                       # SA-OK: in-memory QBO account list, not a DB query
            if str(account.get("AcctNum", "")).strip() == acct_num:    # SA-OK: in-memory QBO account dict, not a DB query
                return str(account["Id"]), R_BY_ACCTNUM

    # Name fallback on the descriptive part of the display name.
    low = (account_name or "").lower()
    desc = account_name.split(" ", 1)[-1].strip().lower() if account_name else ""
    for account in accounts:                                           # SA-OK: in-memory QBO account list, not a DB query
        name = str(account.get("Name", "")).lower()                    # SA-OK: in-memory QBO account dict, not a DB query
        fqn = str(account.get("FullyQualifiedName", "")).lower()       # SA-OK: in-memory QBO account dict, not a DB query
        if not name:
            continue
        if name in low or (desc and desc == name) or (low and low in fqn):
            return str(account["Id"]), R_BY_NAME

    return None, R_UNRESOLVED


def fetch_gl_vendor_actuals(
    client, budget_line, account_name: str, *, start_date: str, end_date: str
) -> tuple[list[dict], str]:
    """Fetch per-entity (customer/vendor) GL actuals for one account + window.

    Returns ``(vendor_actuals, status)`` where status ∈ {resolved_detail, resolved_empty,
    unresolved, report_error} — so an empty result is never a black box. A QBO failure is
    logged with its (redacted) reason and mapped to report_error, not silently swallowed.
    """
    try:
        acct_id, _reason = resolve_qbo_account_id(client, budget_line, account_name)
        if not acct_id:
            return [], GL_UNRESOLVED
        gl = client.get_report("GeneralLedger", {
            "start_date": start_date,
            "end_date": end_date,
            "account": acct_id,
            "accounting_method": "Accrual",
        })  # QBO-OK: minorversion-pinned-upstream (get_report pins minorversion=73)
        totals = parse_gl_vendor_totals(gl)
        actuals = [
            {"name": name, "actual": round(amount, 2)}
            for name, amount in sorted(totals.items(), key=lambda kv: abs(kv[1]), reverse=True)
        ]
        return actuals, (GL_RESOLVED_DETAIL if actuals else GL_RESOLVED_EMPTY)
    except Exception as exc:  # surfaced, not swallowed: logged with reason + returned as status
        logger.warning(
            "[BudgetAI] GL detail fetch failed for %s: %s",
            account_name, redact_to_string(str(exc), max_length=500),
        )
        return [], GL_REPORT_ERROR


# Reforecast is a financial write. The older narrative-only parser's best-effort
# fallbacks cannot establish complete actuals for this boundary.


@dataclass(frozen=True)
class GLCompletenessEvidence:
    """Trusted adapter evidence, never a field accepted from a QBO response/user.

    No live adapter currently establishes this contract. Synthetic tests can
    supply it explicitly; provider activation requires separately verified limits
    and continuation semantics, not entity-query pagination assumptions.
    """
    reference: str


@dataclass(frozen=True)
class VerifiedGLDetail:
    account_id: str
    start_date: str
    end_date: str
    basis: str
    entities: tuple[tuple[str, Decimal], ...]
    transaction_count: int
    completeness_reference: str


def parse_reforecast_gl_detail(
    report: dict, *, account_id: str, start_date: str, end_date: str,
    completeness: GLCompletenessEvidence | None,
) -> VerifiedGLDetail:
    """Strict signed detail parser; malformed rows refuse instead of disappearing."""
    if not isinstance(completeness, GLCompletenessEvidence) or not completeness.reference:
        raise BudgetPlanError("GeneralLedger report completeness is unverified; reforecast was not changed.")
    header = report.get("Header", {})
    if (header.get("StartPeriod") != start_date or header.get("EndPeriod") != end_date
            or str(header.get("ReportBasis", "")).lower() != "accrual"):
        raise BudgetPlanError("GeneralLedger date/basis metadata does not match the requested actuals.")
    if any(report.get(k) for k in ("Fault", "Warnings", "Truncated", "ContinuationToken")):
        raise BudgetPlanError("GeneralLedger reports incomplete or failed source data.")
    columns = report.get("Columns", {}).get("Column", [])
    positions = {}
    for role, titles in (("date", {"date"}), ("entity", {"name", "vendor", "customer", "payee"}), ("amount", {"amount"})):
        matches = [i for i, col in enumerate(columns) if str(col.get("ColTitle", "")).strip().lower() in titles]
        if len(matches) != 1:
            raise BudgetPlanError(f"GeneralLedger has no unique {role} column.")
        positions[role] = matches[0]
    totals: dict[str, Decimal] = {}
    count = 0
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)

    def walk(rows):
        nonlocal count
        if not isinstance(rows, list):
            raise BudgetPlanError("GeneralLedger rows are malformed.")
        for row in rows:
            if not isinstance(row, dict):
                raise BudgetPlanError("GeneralLedger row is malformed.")
            if row.get("type") == "Section":
                walk(row.get("Rows", {}).get("Row", []))
                continue
            cells = row.get("ColData")
            if cells is None:
                if row.get("Summary") is not None:
                    continue
                raise BudgetPlanError("GeneralLedger transaction has no cells.")
            if not isinstance(cells, list) or len(cells) <= max(positions.values()):
                raise BudgetPlanError("GeneralLedger transaction has missing columns.")
            date_text = str(cells[positions["date"]].get("value", "")).strip()
            # Totals are explicit report structure, never transaction evidence.
            if date_text.lower().startswith("total"):
                continue
            try:
                when = date.fromisoformat(date_text)
            except ValueError as exc:
                raise BudgetPlanError("GeneralLedger contains an invalid transaction date.") from exc
            if not start <= when <= end:
                raise BudgetPlanError("GeneralLedger transaction is outside the requested period.")
            name = str(cells[positions["entity"]].get("value", "")).strip() or "Unassigned"
            raw = cells[positions["amount"]].get("value")
            if isinstance(raw, str):
                raw = raw.replace(",", "").strip()
                if raw.startswith("(") and raw.endswith(")"):
                    raw = "-" + raw[1:-1]
            amount = money(raw, "GeneralLedger transaction amount")
            if amount != amount.quantize(Decimal("0.01")):
                raise BudgetPlanError("GeneralLedger currency has unsupported fractional cents.")
            totals[name] = totals.get(name, Decimal(0)) + amount
            count += 1
            if row.get("Rows"):
                raise BudgetPlanError("GeneralLedger transaction unexpectedly contains nested rows.")
    walk(report.get("Rows", {}).get("Row", []))
    return VerifiedGLDetail(account_id, start_date, end_date, "Accrual", tuple(sorted(totals.items())), count, completeness.reference)


def fetch_reforecast_gl_detail(client, *, account_id: str, start_date: str, end_date: str) -> VerifiedGLDetail:
    """Refuse until a live provider completeness contract has primary evidence.

    Deliberately no response flag, environment switch, or query-pagination guess
    enables financial writes. This seam is replaced only by synthetic fixtures in
    source-remediation tests. Narrative GL reporting remains independently usable.
    """
    raise BudgetPlanError("GeneralLedger report completeness is unverified; hierarchical reforecast is unavailable.")

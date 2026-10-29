"""
app/services/coa_formula.py
────────────────────────────
Shared COA-level formula evaluator used by both Covenants and Custom KPIs.

A formula is stored as JSON:
  {
    "numerator":   ["4000", "4010", "4020"],   # QBO account IDs or names to sum
    "denominator": ["6000", "6010"],            # optional — if absent, result = numerator sum
    "negate_numerator":   false,                # flip sign (useful for expense accounts)
    "negate_denominator": false
  }

Each account contributes according to its QBO classification:
  - a profit-and-loss account (Revenue / Expense) contributes its net activity over the
    evaluation window — the sum of the General Ledger's Amount column for that account (never
    the Beginning Balance row or the running Balance column);
  - a balance-sheet account (Asset / Liability / Equity) contributes its closing balance as of
    the window end, read from the Balance Sheet as of that date (never its net change).
A reference the chart of accounts does not carry fails the evaluation; it is never summed as 0.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .redaction import redact_to_string

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Not-meaningful ratios (zero or negative denominator)
# ─────────────────────────────────────────────────────────────────────────────
# Debt/EBITDA at EBITDA <= 0 is unbounded, not small: dividing by the magnitude (or by the signed
# negative) turns a loss into a passing multiple. A ratio whose denominator is zero or negative
# therefore returns value None and names its code under ``breakdown["not_meaningful"]`` (with the
# same ``error_code`` / ``reason`` keys every no-data breakdown carries). The status classifier
# (routers/covenants._compute_status) judges it BREACH for a maximum-ratio covenant (breach ABOVE)
# and UNKNOWN otherwise — never PASS.
NOT_MEANINGFUL_REASONS: dict[str, str] = {
    "covenant_ebitda_nonpositive": (
        "EBITDA is zero or negative, so Debt/EBITDA is not meaningful; "
        "a maximum Debt/EBITDA covenant is treated as breached."
    ),
    "covenant_equity_nonpositive": (
        "Total equity is zero or negative, so Liabilities/Equity is not meaningful; "
        "a maximum leverage covenant is treated as breached."
    ),
    "covenant_current_liabilities_nonpositive": (
        "Current liabilities are zero or negative, so the current ratio is not meaningful."
    ),
    "covenant_formula_denominator_nonpositive": (
        "The formula denominator is zero or negative, so the ratio is not meaningful; "
        "a maximum-ratio covenant is treated as breached."
    ),
}


def mark_not_meaningful(breakdown: dict, code: str) -> dict:
    """Record a not-meaningful ratio on ``breakdown`` (in place) and return it."""
    breakdown["not_meaningful"] = code
    breakdown["error_code"] = code
    breakdown["reason"] = NOT_MEANINGFUL_REASONS[code]
    return breakdown


def not_meaningful_code(breakdown: object) -> str | None:
    """The not-meaningful code a compute recorded on ``breakdown``; None when the value stands."""
    if isinstance(breakdown, dict):
        code = breakdown.get("not_meaningful")
        if isinstance(code, str) and code in NOT_MEANINGFUL_REASONS:
            return code
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Built-in metric calculators
# ─────────────────────────────────────────────────────────────────────────────

# PLAN-EXEMPT: item-3 EBITDA add-backs, ratified 2026-06-25 ("commit and ratify 3+4"); design work/batch-6-reporting-covenants/design/20260625-addbacks-and-metric-snapshots.md
def compute_builtin(metric_type: str, client, start_date: str, end_date: str, db: Session | None = None, realm_id: str | None = None, addbacks: list | None = None, debt_accounts: list | None = None) -> tuple[float | None, dict | None]:
    """
    Compute a standard financial metric from live QBO data over the closed-month TTM window
    [start_date, end_date]. Returns (value, breakdown_dict).

    ``addbacks`` are the covenant's user-defined bank-EBITDA adjustment lines; they lift
    book EBITDA to the bank-reportable figure for DSCR / Debt-EBITDA (None -> book EBITDA only).
    ``debt_accounts`` are the QBO liability account refs (ids/names) the covenant services;
    DSCR principal-paid is summed over them from QBO Balance-Sheet-as-of-date at TTM_start and
    TTM_end (None/[] -> DSCR returns an explicit "map the serviced debt account" no-data reason,
    never a fabricated 0). ``db`` / ``realm_id`` are retained for signature compatibility but the
    DSCR principal term no longer depends on the 0D snapshot history (R2 rewrite).
    """
    try:
        if metric_type == "DSCR":
            return _compute_dscr(
                client, start_date, end_date, db=db, realm_id=realm_id,
                addbacks=addbacks, debt_accounts=debt_accounts,
            )
        if metric_type == "DEBT_EBITDA":
            return _compute_debt_ebitda(client, start_date, end_date, addbacks=addbacks)
        if metric_type == "LEVERAGE":
            return _compute_leverage(client, end_date)
        if metric_type == "CURRENT_RATIO":
            return _compute_current_ratio(client, end_date)
        if metric_type == "MIN_CASH":
            return _get_cash(client, end_date)
        return None, None
    except Exception as e:
        logger.warning(
            "[CoaFormula] Built-in %s failed: %s",
            metric_type,
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError(f"{metric_type} covenant metric data could not be loaded from QuickBooks.") from e


def compute_coa_formula(formula: Dict[str, Any], client, start_date: str, end_date: str) -> tuple[Optional[float], Optional[Dict]]:
    """
    Evaluate a COA formula: sum accounts in numerator / sum accounts in denominator.
    `formula` should have keys: numerator (list), denominator (list, optional),
    negate_numerator (bool), negate_denominator (bool). A balance-sheet account contributes its
    closing balance as of ``end_date``; a P&L account its activity over the window.
    Returns (value, breakdown_dict).
    """
    try:
        num_ids = _formula_refs(formula, "numerator")
        num_sub_ids = _formula_refs(formula, "numerator_subtract")
        den_ids = _formula_refs(formula, "denominator")
        den_sub_ids = _formula_refs(formula, "denominator_subtract")
        values = _account_values(
            num_ids + num_sub_ids + den_ids + den_sub_ids, client, start_date, end_date,
        )

        num_sum = _sum_refs(values, num_ids) - _sum_refs(values, num_sub_ids)
        if formula.get("negate_numerator"):
            num_sum = -num_sum

        if not den_ids:
            return float(num_sum), {"numerator": {"label": "Selected Accounts (Num)", "value": float(num_sum)}}

        den_sum = _sum_refs(values, den_ids) - _sum_refs(values, den_sub_ids)
        if formula.get("negate_denominator"):
            den_sum = -den_sum

        breakdown = {
            "numerator": {"label": "Selected Accounts (Num)", "value": float(num_sum)},
            "denominator": {"label": "Selected Accounts (Den)", "value": float(den_sum)}
        }

        if den_sum <= 0:
            return None, mark_not_meaningful(breakdown, "covenant_formula_denominator_nonpositive")
        return float(round(num_sum / den_sum, 4)), breakdown
    except Exception as e:
        logger.warning(
            "[CoaFormula] COA formula eval failed: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("COA formula data could not be loaded from QuickBooks.") from e


# ─────────────────────────────────────────────────────────────────────────────
# Account balance fetcher
# ─────────────────────────────────────────────────────────────────────────────

_BALANCE_SHEET_CLASSIFICATIONS = frozenset({"asset", "liability", "equity"})
_ACTIVITY_CLASSIFICATIONS = frozenset({"revenue", "expense"})
_ACCOUNT_PAGE_SIZE = 1000
_ACCOUNT_MAX_PAGES = 50


def _formula_refs(formula: Dict[str, Any], key: str) -> List[str]:
    return [str(ref).strip() for ref in (formula.get(key) or []) if str(ref).strip()]


def _sum_refs(values: Dict[str, Decimal], refs: List[str]) -> Decimal:
    return sum((values[ref] for ref in refs), Decimal("0"))


def _account_values(refs: List[str], client, start_date: str, end_date: str) -> Dict[str, Decimal]:
    """Each formula ref's contribution: a balance-sheet account's closing balance as of
    ``end_date``; a profit-and-loss account's net GL activity over [start_date, end_date]."""
    unique = list(dict.fromkeys(refs))
    if not unique:
        return {}
    resolved = _resolve_formula_accounts(client, unique)
    balance_ids = list(dict.fromkeys(aid for aid, is_bs in resolved.values() if is_bs))
    closing = _bs_closing_balances(client, balance_ids, end_date) if balance_ids else {}
    values: Dict[str, Decimal] = {}
    for ref in unique:
        account_id, is_balance_sheet = resolved[ref]
        values[ref] = (
            closing[account_id] if is_balance_sheet
            else _fetch_account_balance(account_id, client, start_date, end_date)
        )
    return values


def _resolve_formula_accounts(client, refs: List[str]) -> Dict[str, tuple[str, bool]]:
    """Map each ref (a QBO account id or name) to ``(account_id, is_balance_sheet)`` from the
    QBO chart of accounts, inactive accounts included. Raises on a ref the chart does not carry or
    an account with no balance-sheet / P&L classification — the basis is never guessed."""
    by_id: Dict[str, tuple[str, str]] = {}
    by_name: Dict[str, tuple[str, str]] = {}
    start = 1
    for _page in range(_ACCOUNT_MAX_PAGES):
        resp = client.query(
            "SELECT * FROM Account WHERE Active IN (true, false) "
            f"STARTPOSITION {start} MAXRESULTS {_ACCOUNT_PAGE_SIZE}"
        )
        accounts = (resp.get("QueryResponse", {}) or {}).get("Account", []) or []
        for acct in accounts:
            acct_id = str(acct.get("Id", "") or "").strip()
            entry = (acct_id, str(acct.get("Classification", "") or "").strip().lower())
            if acct_id:
                by_id.setdefault(acct_id, entry)
            for name in (acct.get("Name"), acct.get("FullyQualifiedName")):
                if str(name or "").strip():
                    by_name.setdefault(str(name).strip(), entry)
        if len(accounts) < _ACCOUNT_PAGE_SIZE:
            break
        start += _ACCOUNT_PAGE_SIZE
    else:
        raise ValueError("QuickBooks chart of accounts exceeds the readable page limit")

    resolved: Dict[str, tuple[str, bool]] = {}
    for ref in refs:
        hit = by_id.get(ref) or by_name.get(ref)
        if hit is None or not hit[0]:
            raise ValueError(f"Account {ref} is not in the QuickBooks chart of accounts")
        account_id, classification = hit
        if classification in _BALANCE_SHEET_CLASSIFICATIONS:
            resolved[ref] = (account_id, True)
        elif classification in _ACTIVITY_CLASSIFICATIONS:
            resolved[ref] = (account_id, False)
        else:
            raise ValueError(f"Account {ref} has no balance-sheet or profit-and-loss classification")
    return resolved


def _bs_closing_balances(client, account_ids: List[str], end_date: str) -> Dict[str, Decimal]:
    """Closing balance of each balance-sheet account AS OF ``end_date``, from the Balance Sheet for
    that date. A parent account's section total includes its sub-accounts (as its GL section
    does). An account the as-of Balance Sheet does not list had no balance that day (0). Raises on
    a report for another date, one without an amount column or account ids, or a listed account
    whose amount cannot be read."""
    from app.cashflow.qbo_cash import _report_total_amount_index

    bs = client.get_report("BalanceSheet", {
        "start_date": end_date, "end_date": end_date,
        "accounting_method": "Accrual", "summarize_column_by": "Total",
    })
    end_period = (bs.get("Header", {}) or {}).get("EndPeriod")
    if end_period and end_period != end_date:
        raise ValueError(f"BalanceSheet returned EndPeriod {end_period} for requested {end_date}")
    amount_idx = _report_total_amount_index(bs)
    if amount_idx is None:
        raise ValueError("BalanceSheet report has no amount column")

    wanted = set(account_ids)
    found: Dict[str, Decimal] = {}
    saw_account_id = False

    def _row_id(cols: list) -> str:
        first = cols[0] if cols and isinstance(cols[0], dict) else {}
        return str(first.get("id", "") or "").strip()

    def _amount(cols: list) -> Decimal:
        value = _gl_money(_gl_cell(cols, amount_idx))
        if value is None:
            raise ValueError("BalanceSheet amount is blank for a listed account")
        return value

    def _walk(rows: list) -> None:
        nonlocal saw_account_id
        for row in rows:
            header = (row.get("Header", {}) or {}).get("ColData", []) or []
            header_id = _row_id(header)
            saw_account_id = saw_account_id or bool(header_id)
            if row.get("type") == "Section" and header_id in wanted:
                found.setdefault(header_id, _amount((row.get("Summary", {}) or {}).get("ColData", []) or []))
                continue
            col_data = row.get("ColData") or []
            row_id = _row_id(col_data)
            saw_account_id = saw_account_id or bool(row_id)
            if row_id in wanted:
                found.setdefault(row_id, _amount(col_data))
            _walk(row.get("Rows", {}).get("Row", []) or [])

    _walk(bs.get("Rows", {}).get("Row", []) or [])
    if len(found) < len(wanted) and not saw_account_id:
        raise ValueError("BalanceSheet report carries no account ids")
    return {aid: found.get(aid, Decimal("0")) for aid in account_ids}


def _fetch_account_balance(account_id: str, client, start_date: str, end_date: str) -> Decimal:
    """Net GL activity of one account over [start_date, end_date]: the sum of the General
    Ledger's Amount column over the account's transaction rows.

    The report is requested with QBO's ``account`` filter and read by column HEADER, so the
    Beginning Balance row and the running Balance column are never summed. Only the section whose
    header names this account (by id or exact name) is read; a parent account's section includes
    its sub-account sections, as QBO's "Total for" line does. Raises on a report without an Amount
    (or Debit/Credit) column, a non-numeric amount, or transactions for other accounts only."""
    try:
        gl = client.get_report("GeneralLedger", {
            "start_date": start_date,
            "end_date": end_date,
            "account": account_id,
            "accounting_method": "Accrual",
        })
        return _gl_account_activity(gl, str(account_id).strip())
    except Exception as e:
        logger.warning(
            "[CoaFormula] Balance fetch for account %s failed: %s",
            account_id,
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError(f"Account balance for {account_id} could not be loaded from QuickBooks.") from e


def _gl_column_roles(columns: list) -> dict[str, int]:
    """Column index by role (date / amount / debit / credit / balance) from the GL headers.
    Balance is tested first: its QBO column key (``rbal_nat_amount``) also contains "amount"."""
    roles: dict[str, int] = {}
    for idx, col in enumerate(columns):
        if not isinstance(col, dict):
            continue
        meta = " ".join(
            str(item.get("Value", "")) for item in col.get("MetaData", []) or [] if isinstance(item, dict)
        )
        text = "".join(
            ch for ch in f"{col.get('ColTitle', '')} {col.get('ColType', '')} {meta}".lower() if ch.isalnum()
        )
        for role in ("balance", "debit", "credit", "amount"):
            if role in text:
                roles.setdefault(role, idx)
                break
        else:
            if "date" in text:
                roles.setdefault("date", idx)
    return roles


def _gl_cell(col_data: list, idx: int | None) -> str:
    if idx is None or idx >= len(col_data) or not isinstance(col_data[idx], dict):
        return ""
    return str(col_data[idx].get("value", "") or "").strip()


def _gl_money(text: str) -> Decimal | None:
    """Parse a report money cell (commas, ``$``, parenthesised negatives); None when blank.
    Raises ValueError on a non-numeric cell — an unreadable amount is never treated as 0."""
    s = text.replace(",", "").replace("$", "").strip()
    if not s or s == "-":
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"non-numeric amount {text!r}") from exc
    if not value.is_finite():
        raise ValueError(f"non-numeric amount {text!r}")
    return value


def _gl_is_txn_date(text: str) -> bool:
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            continue
    return False


def _gl_account_activity(gl: dict, account_ref: str) -> Decimal:
    """Σ Amount over the transaction rows of ``account_ref``'s section(s) in a GL report."""
    roles = _gl_column_roles(gl.get("Columns", {}).get("Column", []) or [])
    date_idx = roles.get("date", 0)

    def _header_matches(row: dict) -> bool:
        cols = (row.get("Header", {}) or {}).get("ColData", []) or []
        first = cols[0] if cols and isinstance(cols[0], dict) else {}
        return bool(account_ref) and account_ref in (
            str(first.get("id", "") or "").strip(), str(first.get("value", "") or "").strip(),
        )

    def _txn_rows(rows: list):
        for row in rows:
            col_data = row.get("ColData") or []
            if col_data:
                yield col_data
            yield from _txn_rows(row.get("Rows", {}).get("Row", []) or [])

    def _matched_sections(rows: list):
        for row in rows:
            if row.get("type") == "Section" and _header_matches(row):
                yield row
                continue
            yield from _matched_sections(row.get("Rows", {}).get("Row", []) or [])

    top_rows = gl.get("Rows", {}).get("Row", []) or []
    sections = list(_matched_sections(top_rows))
    if not sections:
        if any(_gl_is_txn_date(_gl_cell(cd, date_idx)) for cd in _txn_rows(top_rows)):
            raise ValueError(f"GeneralLedger report did not return account {account_ref}")
        return Decimal("0")  # no activity for the account in the window
    if "amount" not in roles and not ("debit" in roles and "credit" in roles):
        raise ValueError("GeneralLedger report has no Amount column")

    total = Decimal("0")
    for section in sections:
        previous_balance: Decimal | None = None
        for col_data in _txn_rows(section.get("Rows", {}).get("Row", []) or []):
            balance = _gl_money(_gl_cell(col_data, roles.get("balance")))
            if not _gl_is_txn_date(_gl_cell(col_data, date_idx)):
                # Beginning Balance / subtotal rows: never summed, only the running-balance base.
                previous_balance = balance if balance is not None else previous_balance
                continue
            amount = _gl_money(_gl_cell(col_data, roles.get("amount")))
            if amount is None and "debit" in roles and "credit" in roles:
                debit = _gl_money(_gl_cell(col_data, roles["debit"]))
                credit = _gl_money(_gl_cell(col_data, roles["credit"]))
                if debit is not None or credit is not None:
                    amount = (debit or Decimal("0")) - (credit or Decimal("0"))
            if amount is None and balance is not None and previous_balance is not None:
                # QBO can leave Amount blank on a row; the running-balance move is its amount.
                amount = balance - previous_balance
            total += amount or Decimal("0")
            if balance is not None:
                previous_balance = balance
    return round(total, 2)


def _flatten_rows(rows):
    for row in rows:
        if row.get("ColData"):
            yield row
        sub = row.get("Rows", {}).get("Row", [])
        if sub:
            yield from _flatten_rows(sub)


def get_chart_of_accounts(client) -> List[Dict[str, Any]]:
    """Return the full COA from QBO as [{id, name, type, sub_type}]."""
    try:
        resp = client.query("SELECT * FROM Account WHERE Active = true MAXRESULTS 1000")
        accounts = resp.get("QueryResponse", {}).get("Account", [])
        return [
            {
                "id": str(a.get("Id", "")),
                "acct_num": a.get("AcctNum", ""),
                "name": a.get("Name", ""),
                "type": a.get("AccountType", ""),
                "sub_type": a.get("AccountSubType", ""),
                "classification": a.get("Classification", ""),
                "parent_ref": str(a.get("ParentRef", {}).get("value", "")) if isinstance(a.get("ParentRef"), dict) else str(a.get("ParentRef")) if a.get("ParentRef") else None,
            }
            for a in accounts
        ]
    except Exception as e:
        logger.warning("[CoaFormula] COA fetch failed: %s", redact_to_string(str(e), max_length=500))
        raise RuntimeError("Chart of accounts could not be loaded from QuickBooks.") from e


# ─────────────────────────────────────────────────────────────────────────────
# Built-in metric helper implementations
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# DSCR debt-service: principal paid from QBO Balance-Sheet-as-of-date (R2 rewrite)
# ─────────────────────────────────────────────────────────────────────────────
# Covenant-tracking Wave 1 (plan 20260630-covenant-tracking AC-2). Principal paid over the
# TTM window = Σ over the covenant's MAPPED liability accounts of
#   max(0, balance_as_of(TTM_start) − balance_as_of(TTM_end))
# from QBO BalanceSheet-as-of-date at the two dates (BS is point-in-time). abs-of-DECREASE
# only: a draw / new borrowing (balance increase) contributes 0, never negative debt service.
# The 2-month snapshot-delta path (_principal_reduction over statically-typed accounts) is
# REPLACED — principal is now tied to the lender-mapped accounts at the two real period-ends.


def _bs_row_amount(col_data: list) -> float:
    """Parse the trailing money cell of a BS row's ColData (parens = negative)."""
    amount_str = str(col_data[-1].get("value", "0")).replace(",", "").strip()
    if amount_str.startswith("(") and amount_str.endswith(")"):
        amount_str = "-" + amount_str[1:-1]
    try:
        return float(amount_str) if amount_str else 0.0
    except ValueError:
        return 0.0


def _bs_balance_as_of(client, account_ref: str, as_of_date: str) -> float | None:
    """Balance of one QBO account AS OF ``as_of_date``, from a BalanceSheet-as-of-date call.

    Resolves the account whether it renders as a LEAF ``Data`` row OR an id-bearing account
    ``Section`` header (a PARENT liability account with sub-accounts carries its balance in its
    section Header, not a leaf row — R1). The header case reuses the ONE canonical id-bearing
    account-header detector, ``app/budget/qbo_reports._is_account_section_header`` — not a forked
    report walk. Matches by account id (``ColData[0]['id']``) OR account name
    (``ColData[0]['value']``) — ``account_ref`` may be either, mirroring the COA-formula
    flexibility. Returns None when the as-of BalanceSheet cannot be fetched (honest no-data,
    surfaced by the caller as a reason — never a fabricated 0). A genuinely absent account on a
    fetched report returns 0.0 (the account had no balance at that date).
    """
    from app.budget.qbo_reports import _is_account_section_header  # QBO-OK: canonical-parser

    bs = client.get_report("BalanceSheet", {
        "end_date": as_of_date, "accounting_method": "Accrual",
    })
    ref = str(account_ref).strip()

    def _matches(first: dict) -> bool:
        row_id = str(first.get("id", "") or "").strip()
        row_name = str(first.get("value", "") or "").strip()
        return bool(ref) and (ref == row_id or ref == row_name)

    def _walk(rows) -> float | None:
        for row in rows:  # SA-OK: in-memory QBO BalanceSheet report walk, not a DB query — no Session in this pure parser
            # A PARENT account renders as a Section whose id-bearing Header carries its balance.
            if row.get("type") == "Section":
                header_cols = row.get("Header", {}).get("ColData", [])
                if (
                    _is_account_section_header(header_cols)
                    and len(header_cols) >= 2
                    and _matches(header_cols[0])
                ):
                    return _bs_row_amount(header_cols)
            # A LEAF account renders as a Data row (or an untyped ColData-bearing row).
            col_data = row.get("ColData", [])
            if len(col_data) >= 2 and _matches(col_data[0]):
                return _bs_row_amount(col_data)
            sub = row.get("Rows", {}).get("Row", [])
            if sub:
                found = _walk(sub)
                if found is not None:
                    return found
        return None

    hit = _walk(bs.get("Rows", {}).get("Row", []))
    return hit if hit is not None else 0.0


def _principal_paid_from_bs(
    client, debt_accounts: list | None, ttm_start: str, ttm_end: str
) -> tuple[float | None, str | None]:
    """DSCR principal-paid over [ttm_start, ttm_end] from QBO BS-as-of-date.

    = Σ over the mapped liability accounts of max(0, balance_as_of(ttm_start) − balance_as_of(ttm_end)).
    Returns (principal_paid, error_code). error_code is non-None on a no-data path:
      - "covenant_debt_accounts_unmapped" → no accounts mapped (caller surfaces "map the
        serviced debt account"); never a fabricated 0.
      - "covenant_bs_asof_unavailable"    → an as-of BalanceSheet could not be fetched.
    """
    refs = [str(a).strip() for a in (debt_accounts or []) if str(a).strip()]
    if not refs:
        return None, "covenant_debt_accounts_unmapped"
    total = 0.0
    for ref in refs:
        # SA-OK: bounded-loop — the covenant's mapped debt accounts are a small fixed set,
        # not result-scaled. (No DB query here; these are QBO report reads.)
        try:
            start_bal = _bs_balance_as_of(client, ref, ttm_start)
            end_bal = _bs_balance_as_of(client, ref, ttm_end)
        except Exception as exc:
            logger.warning(
                "[CoaFormula] BS-as-of fetch for debt account %s failed: %s",
                ref, redact_to_string(str(exc), max_length=300),
            )
            return None, "covenant_bs_asof_unavailable"
        if start_bal is None or end_bal is None:
            return None, "covenant_bs_asof_unavailable"
        # abs-of-DECREASE only: a balance increase (a draw / new borrowing) contributes 0.
        total += max(0.0, start_bal - end_bal)
    return total, None


# EBITDA add-back categories (SYNTHESIS 20260630). The user MAPS each add-back account to
# one of these; the compute sums exactly the mapped accounts by id — no P&L keyword/section
# inference. The load-bearing ``interest`` category feeds BOTH the EBITDA numerator add-back
# AND the DSCR debt-service interest term (one mapping, both places).
ADDBACK_CATEGORIES = ("interest", "tax", "depreciation", "amortization", "other")
# The three PROMPTS the builder asks the user to map. "d_and_a" is satisfied by EITHER a
# depreciation OR an amortization line (the builder's "Depreciation & Amortization" select is one
# bucket). An unmapped prompt renders a visible "map your <prompt>" message, never a silent $0.
EBITDA_MAPPED_PROMPTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("interest", ("interest",)),
    ("tax", ("tax",)),
    ("d_and_a", ("depreciation", "amortization")),
)


def _resolve_one_addback(line: dict, pl_by_id: dict[str, float]) -> tuple[float, str, str, str]:
    """Resolve ONE covenant EBITDA add-back line to (signed_value, label, sign_str, category).
    An ``account`` line reads the mapped account's PERIOD TOTAL by id from the P&L
    (``pl_by_id``) — the reliable primitive: an exact account-id lookup, NOT a GL
    running-balance sum and NOT any P&L name/section inference. A ``fixed`` line uses its
    ``amount``. interest/tax/depreciation/amortization are STRUCTURAL add-backs — always added
    back at their positive expense magnitude (a subtract sign on interest would wrongly shrink
    EBITDA and mask a breach); only a discretionary ``other`` line may subtract.
    Raises ValueError/TypeError on a malformed fixed amount."""
    kind = line.get("kind", "fixed")
    label = line.get("label", "Adjustment")
    category = str(line.get("category") or "other").lower()
    if category not in ADDBACK_CATEGORIES:
        category = "other"
    if kind == "account" and line.get("account_ref"):
        raw = pl_by_id.get(str(line["account_ref"]), 0.0)
    else:
        raw = float(line.get("amount") or 0.0)
    if category == "other" and line.get("sign") == "subtract":
        return -raw, label, "subtract", category
    return raw, label, "add", category


def _resolve_addbacks(addbacks: list | None, pl_by_id: dict[str, float]) -> tuple[float, list[dict], dict[str, float]]:
    """Sum the covenant's user-MAPPED EBITDA add-back lines. Returns
    (signed_total, breakdown_lines, category_subtotals). Each account line is resolved to its
    P&L period total by account id via ``pl_by_id``; ``category_subtotals`` maps every category
    in ``ADDBACK_CATEGORIES`` to its subtotal (0.0 when nothing is mapped there). A malformed
    line is skipped (logged), never fabricated. The line list is a small per-covenant set."""
    subtotals: dict[str, float] = {c: 0.0 for c in ADDBACK_CATEGORIES}
    if not addbacks:
        return 0.0, [], subtotals
    total = 0.0
    lines: list[dict] = []
    for line in addbacks:
        if not isinstance(line, dict):
            continue
        try:
            signed, label, sign_str, category = _resolve_one_addback(line, pl_by_id)
        except (ValueError, TypeError) as exc:
            logger.warning("[CoaFormula] add-back line skipped: %s",
                           redact_to_string(str(exc), max_length=200))
            continue
        total += signed
        subtotals[category] += signed
        lines.append({"label": label, "sign": sign_str, "value": round(signed, 2), "category": category})
    return total, lines, {c: round(v, 2) for c, v in subtotals.items()}


def _pl_totals_by_account_id(pl: dict) -> dict[str, float]:
    """Build ``{qbo_account_id: period_total}`` from a P&L via the ONE canonical parser
    (``app/budget/qbo_reports._parse_pl_single_month_entries``). The mapped EBITDA add-back
    accounts read their period total by id from this map — correct (a P&L account's period
    total, NOT a GL running-balance sum) and reliable (exact id, no name/section inference)."""
    from app.budget.qbo_reports import _parse_pl_single_month_entries

    return {
        str(entry["qbo_account_id"]): float(entry["amount"] or 0)
        for entry in _parse_pl_single_month_entries(pl)
        if entry["qbo_account_id"]
    }


def _compute_dscr(client, start_date: str, end_date: str, db: Session | None = None, realm_id: str | None = None, addbacks: list | None = None, debt_accounts: list | None = None) -> tuple[float | None, dict | None]:
    """DSCR = TTM EBITDA / (TTM interest + principal paid), where
    ``EBITDA = Net Income + Σ(user-MAPPED add-back accounts)`` over the TTM window
    [start_date, end_date]. Each add-back is the GL balance of a user-mapped account summed by
    id (SYNTHESIS 20260630) — NO P&L keyword/section inference of interest/tax/D&A. The
    debt-service interest term is the Σ of the mapped ``category=="interest"`` add-back lines
    (the SAME accounts), never a P&L lookup. Principal paid = Σ over the covenant's MAPPED
    liability accounts of ``max(0, balance_as_of(TTM_start) − balance_as_of(TTM_end))`` from QBO
    BalanceSheet-as-of-date (R2). Returns (None, breakdown) — surfaced by the caller as 'no_data'
    with a specific reason — when EBITDA or the principal term cannot be computed; never a
    fabricated denominator. ``db`` / ``realm_id`` are retained for signature compatibility.
    """
    pl = client.get_report("ProfitAndLoss", {
        "start_date": start_date, "end_date": end_date, "accounting_method": "Accrual"
    })
    # AC-1 (FOS-1 Wave B): label-resolution diagnostics ride the breakdown's data_quality list
    # (the existing reason/error_code channel) — an unresolved or ambiguous label is recorded,
    # never silently absorbed.
    dq: list[str] = []
    net_income = _get_pl_total(pl, ["Net Income", "Net Earnings"], dq=dq)

    if net_income is None:
        # Keep the "numerator" key — _compute_debt_ebitda reads breakdown["numerator"]["value"].
        return None, {
            "numerator": {"label": "EBITDA", "value": None},
            "reason": "EBITDA unavailable — Net Income not found in the P&L",
            "error_code": "covenant_ebitda_unavailable",
            "data_quality": dq,
        }

    # EBITDA = Net Income + Σ(mapped add-back accounts). Zero P&L keyword/section inference —
    # every add-back account reads its PERIOD TOTAL by id from the P&L, or is a fixed user line.
    pl_by_id = _pl_totals_by_account_id(pl)
    addback_total, addback_lines, cat_subtotals = _resolve_addbacks(addbacks, pl_by_id)
    ebitda = net_income + addback_total
    # D&A is depreciation + amortization; the debt-service interest term is the mapped interest.
    d_and_a = round(cat_subtotals["depreciation"] + cat_subtotals["amortization"], 2)
    # Debt-service interest is a positive COST — clamp at 0 so a stray credit/negative can never
    # shrink debt service and mask a breach (interest expense is never negative for coverage).
    interest = max(0.0, cat_subtotals["interest"])
    # Which of the three prompted buckets the user has NOT mapped — surfaced honestly so the
    # breakdown shows a "map your <prompt> account" message instead of a silent $0. A prompt is
    # mapped if ANY of its member categories carries a line (D&A = depreciation OR amortization).
    mapped_cats = {ln["category"] for ln in addback_lines}
    unmapped_categories = [
        prompt for prompt, members in EBITDA_MAPPED_PROMPTS
        if not any(m in mapped_cats for m in members)
    ]
    breakdown = {
        "numerator": {
            "label": "EBITDA", "value": round(ebitda, 2),
            "components": {
                "net_income": net_income,
                "interest": interest,
                "taxes": cat_subtotals["tax"],
                "d_and_a": d_and_a,
            },
            "net_income": net_income,
            "addbacks": addback_lines,
            "category_subtotals": cat_subtotals,
            "unmapped_categories": unmapped_categories,
        },
    }
    if dq:
        # An ambiguous-but-agreeing duplicate label resolved to a value — the number stands,
        # the ambiguity is surfaced (clean lookups leave the breakdown byte-identical).
        breakdown["data_quality"] = dq

    principal_paid, principal_error = _principal_paid_from_bs(client, debt_accounts, start_date, end_date)
    if principal_paid is None:
        reason = (
            "Debt service unavailable — map the serviced debt account(s) on this covenant "
            "so principal paydown can be measured."
            if principal_error == "covenant_debt_accounts_unmapped"
            else "Debt service unavailable — the as-of QuickBooks Balance Sheet for one of the "
            "mapped debt accounts could not be loaded."
        )
        breakdown["denominator"] = {
            "label": "Debt Service", "value": None,
            "reason": reason, "error_code": principal_error,
        }
        return None, breakdown

    debt_service = interest + principal_paid
    breakdown["denominator"] = {
        "label": "Debt Service (interest + principal)", "value": round(debt_service, 2),
        "components": {"interest": interest, "principal_paid": round(principal_paid, 2)},
    }
    if debt_service <= 0:
        breakdown["denominator"]["reason"] = (
            "Debt service is zero or negative over the period — DSCR is undefined."
        )
        breakdown["denominator"]["error_code"] = "covenant_debt_service_nonpositive"
        return None, breakdown
    return round(ebitda / debt_service, 2), breakdown


def _compute_debt_ebitda(client, start_date: str, end_date: str, addbacks: list | None = None) -> tuple[float | None, dict | None]:
    """Debt/EBITDA = Total Debt / EBITDA (bank-reportable, with the covenant's add-backs)."""
    # PLAN-EXEMPT: item-3 EBITDA add-backs (ratified 2026-06-25 "commit and ratify 3+4")
    _ebitda_val, ebitda_b = _compute_dscr(client, start_date, end_date, addbacks=addbacks)
    ebitda = ebitda_b["numerator"]["value"] if ebitda_b else None
    # AC-1: inherit the EBITDA leg's label diagnostics, then collect this leg's own.
    dq: list[str] = list(ebitda_b.get("data_quality", [])) if ebitda_b else []

    # Total Liabilities is a stock metric — read AS OF the TTM period-end (R1), not over a window.
    bs = client.get_report("BalanceSheet", {
        "end_date": end_date, "accounting_method": "Accrual"
    })
    total_liabilities = _get_bs_total(bs, ["Total Liabilities"], dq=dq)

    breakdown = {
        "numerator": {"label": "Total Liabilities", "value": total_liabilities},
        "denominator": {"label": "EBITDA", "value": ebitda}
    }
    if dq:
        breakdown["data_quality"] = dq

    if total_liabilities is None or ebitda is None:
        return None, breakdown
    if ebitda <= 0:
        return None, mark_not_meaningful(breakdown, "covenant_ebitda_nonpositive")
    return round(total_liabilities / ebitda, 4), breakdown


def _compute_leverage(client, end_date: str) -> tuple[float | None, dict | None]:
    """Leverage = Total Liabilities / Total Equity, read AS OF the TTM period-end (a real
    month-end), NOT a YTD <yr>-01-01→end window. The Balance Sheet is point-in-time, so the
    as-of ``end_date`` is the only date that matters; the prior YTD start was a meaningless
    smell on a stock metric (R1 — one consistent TTM boundary for every covenant metric)."""
    bs = client.get_report("BalanceSheet", {
        "end_date": end_date, "accounting_method": "Accrual"
    })
    dq: list[str] = []  # AC-1: label-resolution diagnostics for BOTH legs
    liabilities = _get_bs_total(bs, ["Total Liabilities"], dq=dq)
    equity = _get_bs_total(bs, ["Total Equity", "Total Stockholders Equity"], dq=dq)

    breakdown = {
        "numerator": {"label": "Total Liabilities", "value": liabilities},
        "denominator": {"label": "Total Equity", "value": equity}
    }
    if dq:
        breakdown["data_quality"] = dq

    if liabilities is None or equity is None:
        return None, breakdown
    if equity <= 0:
        return None, mark_not_meaningful(breakdown, "covenant_equity_nonpositive")
    return round(liabilities / equity, 4), breakdown


def _compute_current_ratio(client, end_date: str) -> tuple[float | None, dict | None]:
    """Current Ratio = Current Assets / Current Liabilities, read AS OF the TTM period-end (a
    real month-end), NOT a YTD <yr>-01-01→end window. The Balance Sheet is point-in-time, so
    the as-of ``end_date`` is the only date that matters (R1 — one consistent TTM boundary)."""
    bs = client.get_report("BalanceSheet", {
        "end_date": end_date, "accounting_method": "Accrual"
    })
    dq: list[str] = []  # AC-1: label-resolution diagnostics for BOTH legs
    current_assets = _get_bs_total(bs, ["Total Current Assets"], dq=dq)
    current_liabilities = _get_bs_total(bs, ["Total Current Liabilities"], dq=dq)

    breakdown = {
        "numerator": {"label": "Total Current Assets", "value": current_assets},
        "denominator": {"label": "Total Current Liabilities", "value": current_liabilities}
    }
    if dq:
        breakdown["data_quality"] = dq

    if current_assets is None or current_liabilities is None:
        return None, breakdown
    if current_liabilities <= 0:
        return None, mark_not_meaningful(breakdown, "covenant_current_liabilities_nonpositive")
    # Signed: negative current assets is a failing ratio, never lifted to a passing magnitude.
    return round(current_assets / current_liabilities, 4), breakdown


def _get_cash(client, end_date: str) -> tuple[Optional[float], Optional[Dict]]:
    """Cash = the Balance Sheet "Bank Accounts" total AS OF ``end_date`` (a real month-end), read
    with the cash forecast's anchor parser (``app/cashflow/qbo_cash``). Today's live
    ``Account.CurrentBalance`` is never used: a covenant tested at a month-end must see that
    month-end's cash. A Balance Sheet for another date raises; one without a Bank Accounts section
    returns None with a specific reason — never a fabricated 0."""
    from app.cashflow.qbo_cash import _bank_accounts_section_detail, _report_total_amount_index

    bs = client.get_report("BalanceSheet", {
        "start_date": end_date, "end_date": end_date,
        "accounting_method": "Accrual", "summarize_column_by": "Total",
    })
    end_period = (bs.get("Header", {}) or {}).get("EndPeriod")
    if end_period and end_period != end_date:
        raise ValueError(f"BalanceSheet returned EndPeriod {end_period} for requested {end_date}")
    label = f"Bank Accounts (Balance Sheet as of {end_date})"
    detail = _bank_accounts_section_detail(
        bs.get("Rows", {}).get("Row", []), _report_total_amount_index(bs),
    )
    if detail is None:
        return None, {"numerator": {
            "label": label, "value": None,
            "error_code": "covenant_bank_accounts_unavailable",
            "reason": "Cash unavailable — the QuickBooks Balance Sheet has no Bank Accounts total "
                      f"as of {end_date}.",
        }}
    total = round(float(detail["total"]), 2)
    return total, {"numerator": {"label": label, "value": total}}


# PLAN-EXEMPT: one-off correctness nit surfaced by the covenant-tracking crit-eval for plan 20260630-covenant-tracking as pre-existing + non-blocking; Steven-requested single-helper fix, no gated-surface plan in flight
def _get_pl_total(
    report: dict, label_options: list[str], dq: list[str] | None = None
) -> float | None:
    """Extract a summary row value from P&L by label, honoring option PRIORITY.

    ``label_options`` is an ordered preference list. Each label is tried in turn and
    the first one that matches a Summary row anywhere in the report tree wins — so a
    caller passing ``["Interest Expense", "Interest"]`` gets the specific
    "Interest Expense" row even when a bare "Interest" row appears earlier in
    report-traversal (tree) order. The earlier implementation returned the first tree
    row matching ANY label, which silently preferred the earlier-but-less-specific
    row over the caller's first choice — a latent correctness bug in a lender-facing
    helper (it feeds DSCR interest + the covenant metrics).

    ``dq`` (FOS-1 Wave B, AC-1) is an optional data-quality sink: a lookup matching NO
    option records ``label_unresolved:<opt|opt>``; an option matching MULTIPLE Summary
    rows records ``label_ambiguous:<label>`` — and returns the common value when the
    duplicates agree, or an honest None when they conflict (never an arbitrary pick).
    The first option with at least one parseable match OWNS the resolution (ambiguity
    within it does not fall through to a less-specific option). A clean single-match
    lookup behaves exactly as before and records nothing.
    """
    rows = report.get("Rows", {}).get("Row", [])

    def _collect(rows, label, hits):
        for row in rows:  # SA-OK: in-memory QBO report walk, not a DB query — no Session in this pure parser
            summary = row.get("Summary", {})
            col_data = summary.get("ColData", [])
            if col_data and col_data[0].get("value", "") == label:
                try:
                    hits.append(float(str(col_data[-1].get("value", "0")).replace(",", "")))
                except ValueError:
                    pass  # a matched row without a parseable cell is not a usable hit
            sub = row.get("Rows", {}).get("Row", [])
            if sub:
                _collect(sub, label, hits)

    for label in label_options:
        hits: list[float] = []
        _collect(rows, label, hits)
        if not hits:
            continue
        if len(hits) > 1:
            if dq is not None:
                dq.append(f"label_ambiguous:{label}")
            if any(h != hits[0] for h in hits[1:]):
                return None  # conflicting duplicate rows — honest no-data, never a fabricated pick
        return hits[0]
    if dq is not None:
        dq.append("label_unresolved:" + "|".join(label_options))
    return None


def _get_bs_total(
    report: Dict, label_options: List[str], dq: list[str] | None = None
) -> Optional[float]:
    """Extract a balance sheet total row by label (shares ``_get_pl_total`` priority +
    ``dq`` data-quality semantics)."""
    return _get_pl_total(report, label_options, dq)

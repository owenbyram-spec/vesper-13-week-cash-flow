from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _parse_amount(s) -> float:
    try:
        s = str(s).replace(",", "").strip()
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        return float(s)
    except (ValueError, TypeError):
        return 0.0




_REVENUE_TYPES = {
    "Income", "OtherIncome", "Other Income",
    "income", "otherincome", "other income",
}
_COGS_TYPES = {
    "CostOfGoodsSold", "Cost of Goods Sold",
    "costofgoodssold", "cost of goods sold",
}


def _infer_account_type(section_header: str) -> str:
    """Classify a QBO **Profit-and-Loss** section header into a budget account type.

    P&L ONLY — every caller is a ``_parse_pl_*`` walker. This function carries no
    balance-sheet vocabulary, so a BS header ("ASSETS", "LIABILITIES AND EQUITY")
    would fall through to ``"opex"`` and be silently counted as operating expense.
    Do not reuse it for a balance sheet; classify from ``QBOAccount.account_type``.

    Branch ORDER is the contract (these are substring tests over one lowercased
    string, so a broader token tested first swallows a narrower one). Both orderings
    below are pinned by ``tests/test_infer_account_type.py``: the ``"other …"``
    variants precede their generic counterparts, and cost-of-sales precedes the
    generic ``"sales"``/``"income"``/``"revenue"`` token. Without the second,
    "Cost of Sales" matches ``"sales"`` and returns ``"revenue"`` — the live-prod
    incident recorded at ``app/lbo/coa_categorizer.py:1-5`` ($5.47M balance break),
    fixed there by going type-first and left unfixed here until plan
    20260910-consolidator-prerequisites AC-3.

    Name-based and therefore a WEAKER signal than ``coa_categorizer``: the input is
    a report section header, not a ``QBOAccount``, so QBO's authoritative
    ``AccountType`` is not available at this call site. Not authoritative.
    """
    h = (section_header or "").lower()
    # Order 1: "other ..." variants before their generic counterparts.
    if any(k in h for k in ("other income", "otherincome", "other revenue")):
        return "other_income"
    if any(k in h for k in ("other expense", "otherexpense", "other cost")):
        return "other_expense"
    # Order 2: cost-of-sales before the generic revenue tokens ("Cost of Sales"
    # contains "sales"). Moving this below the next branch reintroduces the bug.
    if any(k in h for k in ("cost of goods", "cogs", "cost of sales")):
        return "cogs"
    if any(k in h for k in ("income", "revenue", "sales")):
        return "revenue"
    return "opex"


def _selected_money_column_index(report: Dict, month_key: Optional[str] = None) -> int:
    columns = report.get("Columns", {}).get("Column", [])
    normalized_month = (month_key or "").strip().lower()[:3]
    money_columns = [
        (i, str(col.get("ColTitle", "") or "").strip())
        for i, col in enumerate(columns)
        if col.get("ColType") == "Money"
    ]
    money_col = None
    if normalized_month:
        for idx, title in money_columns:
            title_lower = title.lower()
            if "total" in title_lower:
                continue
            if title_lower.startswith(normalized_month):
                money_col = idx
                break
    if money_col is None:
        for idx, title in money_columns:
            if "total" not in title.lower():
                money_col = idx
                break
    if money_col is None:
        money_col = 1  # fallback
    return money_col


def _is_account_section_header(header_cols: List[Dict[str, Any]]) -> bool:
    if not header_cols:
        return False
    label = str(header_cols[0].get("value", "") or "").strip()
    # QBO uses section headers for both P&L groups and account headers. Account
    # headers carry an account id; group headers like Income/Expenses do not.
    return bool(label and header_cols[0].get("id"))


def _parse_pl_single_month_entries(report: Dict, month_key: Optional[str] = None, *, strict: bool = False) -> List[Dict[str, Any]]:
    """Parse single-month QBO P&L rows into account entries with COA identity."""
    money_col = _selected_money_column_index(report, month_key)
    entries_by_identity: Dict[tuple[str, str], Dict[str, Any]] = {}

    def parse_cell(cells):
        if not strict:
            return _parse_amount(cells[money_col].get("value", "0"))
        from .driver_calculation import money
        raw = cells[money_col].get("value")
        if isinstance(raw, str):
            raw = raw.replace(",", "").strip()
            if raw.startswith("(") and raw.endswith(")"):
                raw = "-" + raw[1:-1]
        value = money(raw, "P&L account amount")
        if value != value.quantize(money("0.01")):
            raise ValueError("P&L account amount has unsupported fractional cents")
        return float(value)

    def _walk(rows, inherited_type: str = "", depth: int = 0):
        if strict and not isinstance(rows, list):
            raise ValueError("P&L rows are malformed")
        for row in rows:
            if strict and not isinstance(row, dict):
                raise ValueError("P&L row is malformed")
            row_type = row.get("type", "")
            if row_type == "Section":
                header_row = row.get("Header", {})
                header_cols = header_row.get("ColData", [])
                section_name = header_cols[0].get("value", "") if header_cols else ""
                effective_type = _infer_account_type(section_name) if depth == 0 else inherited_type
                if _is_account_section_header(header_cols):
                    account_id = str(header_cols[0].get("id", "") or "").strip()
                    try:
                        amt = parse_cell(header_cols)
                    except (IndexError, KeyError, AttributeError):
                        if strict:
                            raise ValueError("P&L account row has missing cells")
                        amt = 0.0
                    identity = ("id", account_id) if account_id else ("name", section_name)
                    if strict and identity in entries_by_identity:
                        raise ValueError("P&L account identity is ambiguous")
                    if identity not in entries_by_identity:
                        entries_by_identity[identity] = {
                            "account_name": section_name,
                            "qbo_account_id": account_id or None,
                            "account_type": effective_type or _infer_account_type(section_name),
                            "amount": 0.0,
                        }
                    entries_by_identity[identity]["amount"] = round(
                        float(entries_by_identity[identity]["amount"]) + amt,
                        2,
                    )
                sub = row.get("Rows", {}).get("Row", [])
                _walk(sub, effective_type, depth + 1)
                continue

            if row_type == "Data":
                col_data = row.get("ColData", [])
                account_col = col_data[0] if col_data else {}
                acct = str(account_col.get("value", "") or "").strip()
                if strict and not acct:
                    raise ValueError("P&L account identity is missing")
                if not acct or acct.lower() == "total":
                    continue
                account_id = str(account_col.get("id", "") or "").strip()
                try:
                    amt = parse_cell(col_data)
                except (IndexError, KeyError, AttributeError):
                    if strict:
                        raise ValueError("P&L account row has missing cells")
                    amt = 0.0
                identity = ("id", account_id) if account_id else ("name", acct)
                if strict and identity in entries_by_identity:
                    raise ValueError("P&L account identity is ambiguous")
                if identity not in entries_by_identity:
                    entries_by_identity[identity] = {
                        "account_name": acct,
                        "qbo_account_id": account_id or None,
                        "account_type": inherited_type or _infer_account_type(acct),
                        "amount": 0.0,
                    }
                entries_by_identity[identity]["amount"] = round(
                    float(entries_by_identity[identity]["amount"]) + amt,
                    2,
                )
            elif strict and row and row.get("Summary") is None:
                raise ValueError("P&L row type is unsupported")

            sub = row.get("Rows", {}).get("Row", [])
            if sub and row_type != "Section":
                _walk(sub, inherited_type, depth)

    _walk(report.get("Rows", {}).get("Row", []))
    return list(entries_by_identity.values())


def _actual_entries_by_name(entries: List[Dict[str, Any]]) -> Dict[str, float]:
    actuals: Dict[str, float] = {}
    for entry in entries:
        name = str(entry.get("account_name") or "").strip()
        if not name:
            continue
        actuals[name] = round(actuals.get(name, 0.0) + float(entry.get("amount", 0) or 0), 2)
    return actuals


def _actual_entries_by_id(entries: List[Dict[str, Any]]) -> Dict[str, float]:
    actuals: Dict[str, float] = {}
    for entry in entries:
        account_id = str(entry.get("qbo_account_id") or "").strip()
        if not account_id:
            continue
        actuals[account_id] = round(actuals.get(account_id, 0.0) + float(entry.get("amount", 0) or 0), 2)
    return actuals


def _parse_pl_single_month(report: Dict, month_key: Optional[str] = None) -> Dict[str, float]:
    """Parse a QBO P&L report for one selected month into ``{account_name: amount}``.

    Some QBO tenants return both a month column and a Total column for the same
    report window. When ``month_key`` is provided, prefer that month and ignore
    Total-style columns so budget-vs-actuals stays month-specific.
    """
    money_col = _selected_money_column_index(report, month_key)

    result: Dict[str, float] = {}

    def _walk(rows):
        for row in rows:
            if row.get("type") == "Section":
                header_cols = row.get("Header", {}).get("ColData", [])
                if _is_account_section_header(header_cols):
                    acct = str(header_cols[0].get("value", "") or "").strip()
                    try:
                        amt = _parse_amount(header_cols[money_col].get("value", "0"))
                    except (IndexError, KeyError, AttributeError):
                        amt = 0.0
                    if acct and amt:
                        result[acct] = result.get(acct, 0) + amt

            if row.get("type") == "Data":
                col_data = row.get("ColData", [])
                acct = col_data[0].get("value", "") if col_data else ""
                if not acct:
                    continue
                try:
                    amt = _parse_amount(col_data[money_col].get("value", "0"))
                except (IndexError, KeyError):
                    amt = 0.0
                result[acct] = result.get(acct, 0) + amt
            sub = row.get("Rows", {}).get("Row", [])
            if sub:
                _walk(sub)

    _walk(report.get("Rows", {}).get("Row", []))
    return result


def _parse_pl_single_month_with_types(report: Dict, month_key: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Parse a single-month P&L into ``{account_name: {amount, account_type}}``."""
    money_col = _selected_money_column_index(report, month_key)

    accounts: Dict[str, Dict[str, Any]] = {}

    def _walk(rows, inherited_type: str = "", depth: int = 0):
        for row in rows:
            row_type = row.get("type", "")
            if row_type == "Section":
                header_row = row.get("Header", {})
                header_cols = header_row.get("ColData", [])
                section_name = header_cols[0].get("value", "") if header_cols else ""
                if depth == 0:
                    effective_type = _infer_account_type(section_name)
                else:
                    effective_type = inherited_type
                if _is_account_section_header(header_cols):
                    try:
                        amt = _parse_amount(header_cols[money_col].get("value", "0"))
                    except (IndexError, KeyError, AttributeError):
                        amt = 0.0
                    if section_name and amt:
                        if section_name not in accounts:
                            accounts[section_name] = {
                                "amount": 0.0,
                                "account_type": effective_type or _infer_account_type(section_name),
                            }
                        accounts[section_name]["amount"] = round(float(accounts[section_name]["amount"]) + amt, 2)
                sub = row.get("Rows", {}).get("Row", [])
                _walk(sub, effective_type, depth + 1)
                continue

            if row_type == "Data":
                col_data = row.get("ColData", [])
                acct = col_data[0].get("value", "") if col_data else ""
                if not acct or acct.lower() == "total":
                    continue
                try:
                    amt = _parse_amount(col_data[money_col].get("value", "0"))
                except (IndexError, KeyError, AttributeError):
                    amt = 0.0
                if acct not in accounts:
                    accounts[acct] = {
                        "amount": 0.0,
                        "account_type": inherited_type or _infer_account_type(acct),
                    }
                accounts[acct]["amount"] = round(float(accounts[acct]["amount"]) + amt, 2)

            sub = row.get("Rows", {}).get("Row", [])
            if sub and row_type != "Section":
                _walk(sub, inherited_type, depth)

    _walk(report.get("Rows", {}).get("Row", []))
    return accounts


def _gl_column_indices(columns: list) -> tuple[int, int]:
    """Locate ``(name_idx, amount_idx)`` from GL report ``Columns`` metadata.

    The GL layout is NOT guaranteed to be the historical
    Date|TxnType|No|Name|Memo|Split|Amount|Balance — Intuit can reorder/omit columns, so
    positions are discovered from headers (falling back to the historical 3/6 only when a
    header is absent). The report has two Money columns (Amount + Balance); the amount is the
    Money column titled "amount" (never the running Balance).
    """
    name_idx: int | None = None
    amount_idx: int | None = None
    balance_idx: int | None = None
    first_money_idx: int | None = None
    for i, col in enumerate(columns):
        title = str(col.get("ColTitle", "")).strip().lower()   # SA-OK: in-memory report Columns metadata, not a DB query
        ctype = str(col.get("ColType", "")).strip().lower()    # SA-OK: in-memory report Columns metadata, not a DB query
        if name_idx is None and title in ("name", "vendor", "customer", "payee"):
            name_idx = i
        if title == "amount":
            amount_idx = i
        elif title == "balance":
            balance_idx = i
        if ctype == "money" and first_money_idx is None:
            first_money_idx = i
    if amount_idx is None and first_money_idx is not None and first_money_idx != balance_idx:
        amount_idx = first_money_idx  # the first Money column that is not the running Balance
    return (name_idx if name_idx is not None else 3,
            amount_idx if amount_idx is not None else 6)


def parse_gl_vendor_totals(report: dict) -> dict[str, float]:
    """Aggregate GL transaction amounts by entity (customer/vendor) name.

    Columns are located from the report's ``Columns`` metadata (header-based, not fixed
    indices) so a reordered/short GL layout no longer silently drops every row — the bug
    that made the budget vs-actuals AI analysis report "No GL detail available".
    """
    columns = report.get("Columns", {}).get("Column", [])
    name_idx, amount_idx = _gl_column_indices(columns)
    totals: dict[str, float] = {}
    _walk_gl_rows(report.get("Rows", {}).get("Row", []), totals, name_idx, amount_idx)
    return totals


def _walk_gl_rows(rows: list, totals: dict[str, float], name_idx: int, amount_idx: int) -> None:
    """Recursively sum ``amount_idx`` grouped by ``name_idx`` over GL data rows."""
    need = max(name_idx, amount_idx)
    for row in rows:
        if str(row.get("type", "")) == "Section":                      # SA-OK: in-memory report dict walk, not a DB query
            _walk_gl_rows(row.get("Rows", {}).get("Row", []), totals, name_idx, amount_idx)  # SA-OK: in-memory report dict walk, not a DB query
            continue
        col_data = row.get("ColData", [])                              # SA-OK: in-memory report dict walk, not a DB query
        if not col_data or len(col_data) <= need:
            _walk_gl_rows(row.get("Rows", {}).get("Row", []), totals, name_idx, amount_idx)  # SA-OK: in-memory report dict walk, not a DB query
            continue
        date_val = str(col_data[0].get("value", "")).strip()           # SA-OK: in-memory report cell, not a DB query
        if not date_val or date_val.lower() in ("date", "") or date_val.lower().startswith("total"):
            continue
        name = str(col_data[name_idx].get("value", "")).strip()        # SA-OK: in-memory report cell, not a DB query
        amount_str = str(col_data[amount_idx].get("value", "0"))       # SA-OK: in-memory report cell, not a DB query
        try:
            amount = _parse_amount(amount_str)
        except Exception:
            amount = 0.0
        key = name or "Unassigned"
        totals[key] = totals.get(key, 0) + amount


def _parse_pl_by_month(report: Dict) -> Dict[str, Dict[str, float]]:
    columns = report.get("Columns", {}).get("Column", [])
    month_cols = []
    for i, col in enumerate(columns):
        if col.get("ColType") == "Money" and "Total" not in col.get("ColTitle", ""):
            month_cols.append((i, col.get("ColTitle", "")))

    result: Dict[str, Dict[str, float]] = {m: {} for m in MONTHS}

    def _walk(rows):
        for row in rows:
            if row.get("type") == "Section":
                header_cols = row.get("Header", {}).get("ColData", [])
                if _is_account_section_header(header_cols):
                    acct = str(header_cols[0].get("value", "") or "").strip()
                    for col_i, col_title in month_cols:
                        try:
                            amt = _parse_amount(header_cols[col_i].get("value", "0"))
                        except (IndexError, KeyError, AttributeError):
                            amt = 0.0
                        m_key = col_title[:3].lower()
                        if acct and m_key in result and amt:
                            result[m_key][acct] = result[m_key].get(acct, 0) + amt

            if row.get("type") == "Data":
                col_data = row.get("ColData", [])
                acct = col_data[0].get("value", "") if col_data else ""
                if not acct:
                    continue
                for col_i, col_title in month_cols:
                    try:
                        amt = _parse_amount(col_data[col_i].get("value", "0"))
                    except IndexError:
                        amt = 0.0
                    m_key = col_title[:3].lower()
                    if m_key in result:
                        result[m_key][acct] = result[m_key].get(acct, 0) + amt
            sub = row.get("Rows", {}).get("Row", [])
            if sub:
                _walk(sub)

    _walk(report.get("Rows", {}).get("Row", []))
    return result


def _parse_pl_by_month_with_types(report: Dict) -> Dict[str, Dict[str, Any]]:
    """Parse a QBO P&L report into accounts with monthly amounts and account types.
    
    Account types are inferred from the TOP-LEVEL P&L section each account belongs to
    (Income, Cost of Goods Sold, Expenses, Other Income, Other Expense), NOT from
    sub-section names. This prevents misclassification when sub-sections have names
    that look like other categories (e.g., accounts named '5110 US COS - ...' nested
    under the Income section).
    """
    columns = report.get("Columns", {}).get("Column", [])
    month_cols = []
    for i, col in enumerate(columns):
        if col.get("ColType") == "Money" and "Total" not in col.get("ColTitle", ""):
            month_cols.append((i, col.get("ColTitle", "")))

    accounts: Dict[str, Any] = {}

    # Known top-level section patterns — only these update the account type context
    _TOP_LEVEL_SECTIONS = {
        "other income": "other_income",        # must be before "income"
        "other revenue": "other_income",
        "other expense": "other_expense",
        "other cost": "other_expense",
        "income": "revenue",
        "revenue": "revenue",
        "sales": "revenue",
        "cost of goods sold": "cogs",
        "cost of sales": "cogs",
        "expenses": "opex",
        "operating expenses": "opex",
    }

    def _match_top_level_section(header: str) -> str:
        """If header matches a known top-level P&L section, return its type. Else ''."""
        h = header.lower().strip()
        # Check "other" variants first (longer match) before generic
        for pattern, acct_type in _TOP_LEVEL_SECTIONS.items():
            if pattern in h:
                return acct_type
        return ""

    def _walk(rows, inherited_type: str = "", depth: int = 0):
        for row in rows:
            row_type = row.get("type", "")
            if row_type == "Section":
                header_row = row.get("Header", {})
                header_cols = header_row.get("ColData", [])
                section_name = header_cols[0].get("value", "") if header_cols else ""
                
                # ONLY update type context at depth 0 (true top-level P&L sections).
                # Sub-sections (depth > 0) ALWAYS inherit from their parent, even if
                # their name matches a known pattern. This prevents QBO's nested
                # sub-sections (e.g. "Cost of Sales" under "Income") from overriding
                # the parent section's type.
                if depth == 0:
                    matched_type = _match_top_level_section(section_name)
                    effective_type = matched_type if matched_type else inherited_type
                else:
                    effective_type = inherited_type

                if _is_account_section_header(header_cols):
                    acct_type = effective_type if effective_type else _infer_account_type(section_name)
                    if section_name not in accounts:
                        accounts[section_name] = {"type": acct_type, "monthly": {m: 0.0 for m in MONTHS}}
                    for col_i, col_title in month_cols:
                        try:
                            amt = _parse_amount(header_cols[col_i].get("value", "0"))
                        except (IndexError, KeyError, AttributeError):
                            amt = 0.0
                        m_key = col_title[:3].lower()
                        if m_key in MONTHS and amt:
                            accounts[section_name]["monthly"][m_key] = (
                                accounts[section_name]["monthly"].get(m_key, 0) + amt
                            )
                
                log.info(f"[BudgetSeed] Section depth={depth}: '{section_name}' → type='{effective_type}' (inherited='{inherited_type}')")
                
                sub = row.get("Rows", {}).get("Row", [])
                _walk(sub, effective_type, depth + 1)
                continue

            if row_type == "Data":
                col_data = row.get("ColData", [])
                acct = col_data[0].get("value", "") if col_data else ""
                if not acct:
                    continue
                # Use the inherited type from the top-level section,
                # or fall back to _infer_account_type for ungrouped accounts
                acct_type = inherited_type if inherited_type else _infer_account_type(acct)
                if acct not in accounts:
                    accounts[acct] = {"type": acct_type, "monthly": {m: 0.0 for m in MONTHS}}
                for col_i, col_title in month_cols:
                    try:
                        amt = _parse_amount(col_data[col_i].get("value", "0"))
                    except IndexError:
                        amt = 0.0
                    m_key = col_title[:3].lower()
                    if m_key in MONTHS:
                        accounts[acct]["monthly"][m_key] = accounts[acct]["monthly"].get(m_key, 0) + amt

            sub = row.get("Rows", {}).get("Row", [])
            if sub and row_type != "Section":
                _walk(sub, inherited_type, depth)

    _walk(report.get("Rows", {}).get("Row", []))
    return accounts


_QBO_PAGE_SIZE = 1000
_MONTH_BY_NUMBER = {i + 1: m for i, m in enumerate(MONTHS)}


class BreakdownFetchError(RuntimeError):
    """A QuickBooks query behind a vendor/customer breakdown failed or returned data
    that cannot be read, so the breakdown would be incomplete. Callers must not seed
    sub-lines from a partial breakdown."""


def _query_all(client, entity: str, where: str = "") -> List[Dict[str, Any]]:
    """Every row of a QBO query, paging STARTPOSITION/MAXRESULTS until a short page.

    A single 1000-row page silently truncated a busy year; any failure raises
    BreakdownFetchError instead of returning what was read so far."""
    rows: List[Dict[str, Any]] = []
    start = 1
    clause = f" WHERE {where}" if where else ""
    while True:
        try:
            data = client.query(
                f"SELECT * FROM {entity}{clause} STARTPOSITION {start} MAXRESULTS {_QBO_PAGE_SIZE}"
            )
        except Exception as exc:
            raise BreakdownFetchError(f"QuickBooks {entity} query failed") from exc
        if not isinstance(data, dict) or data.get("Fault"):
            raise BreakdownFetchError(f"QuickBooks {entity} query returned a fault")
        page = (data.get("QueryResponse") or {}).get(entity) or []
        rows.extend(page)
        if len(page) < _QBO_PAGE_SIZE:
            return rows
        start += len(page)


def _year_where(source_year: int) -> str:
    return f"TxnDate >= '{source_year}-01-01' AND TxnDate <= '{source_year}-12-31'"


def _txn_month(txn: Dict[str, Any]) -> str:
    txn_date = str(txn.get("TxnDate") or "")
    try:
        return _MONTH_BY_NUMBER.get(int(txn_date[5:7]), "")
    except ValueError:
        return ""


def _line_amount(line: Dict[str, Any]) -> Decimal:
    from .driver_calculation import BudgetPlanError, money

    try:
        return money(line.get("Amount", 0) or 0, "QuickBooks line amount")
    except BudgetPlanError as exc:
        raise BreakdownFetchError("QuickBooks line amount could not be read") from exc


def _add(
    acc: Dict[str, Dict[str, Dict[str, Decimal]]], account: str, name: str, month: str, amount: Decimal
) -> None:
    by_name = acc.setdefault(account, {})
    monthly = by_name.setdefault(name, {m: Decimal("0") for m in MONTHS})
    monthly[month] += amount


def _as_floats(acc: Dict[str, Dict[str, Dict[str, Decimal]]]) -> Dict[str, Dict[str, Dict[str, float]]]:
    return {
        account: {name: {m: float(v) for m, v in monthly.items()} for name, monthly in by_name.items()}
        for account, by_name in acc.items()
    }


def _fetch_vendor_expense_breakdown(client, source_year: int) -> Dict[str, Dict[str, Dict[str, float]]]:
    """
    Query Bills, Purchases, VendorCredits AND Journal Entries from QBO and build:
    {account_name: {vendor_name: {jan: amount, feb: amount, ...}}}

    Amounts are signed spend: Bills and Purchase charges add; card refunds
    (Purchase.Credit = true) and VendorCredits subtract; JE lines with a Vendor entity
    add on the debit side and subtract on the credit side (reversals). Every query is
    paginated, and any failed or unreadable query raises BreakdownFetchError: a
    partial breakdown must never be seeded as if it were complete.

    Journal Entries are critical because QBO does NOT carry the vendor name
    from JE lines into the General Ledger or P&L reports. By querying the
    JournalEntry objects directly, we capture the Entity reference that QBO
    otherwise suppresses.
    """
    result: Dict[str, Dict[str, Dict[str, Decimal]]] = {}
    where = _year_where(source_year)

    def _process_txns(txn_type: str, vendor_key: str, sign_for) -> None:
        for txn in _query_all(client, txn_type, where):
            vendor = str((txn.get(vendor_key) or {}).get("name") or "").strip()
            m_key = _txn_month(txn)
            if not vendor or not m_key:
                continue
            sign = sign_for(txn)
            for line in txn.get("Line", []):
                amt = _line_amount(line)
                if amt == 0:
                    continue
                # Try all possible detail types for account reference
                acct_name = ""
                for detail_key in ["AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"]:
                    detail = line.get(detail_key)
                    if detail:
                        acct_ref = detail.get("AccountRef", detail.get("ItemRef", {}))
                        acct_name = acct_ref.get("name", "").strip()
                        if acct_name:
                            break
                if acct_name:
                    _add(result, acct_name, vendor, m_key, sign * amt)

    _process_txns("Bill", "VendorRef", lambda txn: 1)
    # Purchase.Credit = true is a refund to the card/account, i.e. negative spend.
    _process_txns("Purchase", "EntityRef", lambda txn: -1 if txn.get("Credit") is True else 1)
    _process_txns("VendorCredit", "VendorRef", lambda txn: -1)

    # ── Journal Entries: vendor-tagged lines, debit = spend, credit = reversal ──
    je_count = 0
    for je in _query_all(client, "JournalEntry", where):
        m_key = _txn_month(je)
        if not m_key:
            continue
        for line in je.get("Line", []):
            detail = line.get("JournalEntryLineDetail", {})
            if not detail:
                continue
            posting = str(detail.get("PostingType", "")).lower()
            if posting not in ("debit", "credit"):
                continue
            entity = detail.get("Entity", {})
            if str(entity.get("Type", "")).lower() != "vendor":
                continue
            vendor = str(entity.get("EntityRef", {}).get("name", "")).strip()
            acct_name = str(detail.get("AccountRef", {}).get("name", "")).strip()
            if not vendor or not acct_name:
                continue
            amt = _line_amount(line)
            if amt == 0:
                continue
            _add(result, acct_name, vendor, m_key, amt if posting == "debit" else -amt)
            je_count += 1

    log.info(f"Vendor breakdown found {len(result)} accounts ({je_count} JE lines): {list(result.keys())[:20]}")
    return _as_floats(result)


def _fetch_customer_revenue_breakdown(client, source_year: int) -> Dict[str, Dict[str, Dict[str, float]]]:
    """
    Query Invoices, CreditMemos AND Journal Entries from QBO and build:
    {account_name: {customer_name: {jan: amount, feb: amount, ...}}}

    Uses the actual GL income account from each QBO Item's IncomeAccountRef,
    NOT the Item product/service name. This ensures revenue lands in the
    correct chart-of-accounts bucket. Invoices add; CreditMemos subtract; JE lines
    with a Customer entity add on the credit side and subtract on the debit side.
    Every query is paginated and any failure raises BreakdownFetchError.

    Journal Entries use their AccountRef directly (already a GL account).
    """
    result: Dict[str, Dict[str, Dict[str, Decimal]]] = {}
    where = _year_where(source_year)

    # ── Step 1: Build Item ID → Income Account Name lookup ──
    # QBO Items have an IncomeAccountRef that tells us which GL account
    # the item's revenue posts to. We need this because invoice lines
    # only carry ItemRef (product name), not the GL account.
    item_to_account: Dict[str, str] = {}  # item_value (ID) → income account name
    for item in _query_all(client, "Item"):
        item_id = str(item.get("Id", ""))
        income_acct = item.get("IncomeAccountRef", {})
        if income_acct and income_acct.get("name"):
            item_to_account[item_id] = income_acct["name"].strip()
    log.info(f"Item→Account lookup built: {len(item_to_account)} items mapped")

    # ── Step 2: Invoices (+) and CreditMemos (−) using actual GL accounts ──
    unmapped_items = set()
    for txn_type, sign in (("Invoice", 1), ("CreditMemo", -1)):
        for inv in _query_all(client, txn_type, where):
            customer = str((inv.get("CustomerRef") or {}).get("name") or "").strip()
            m_key = _txn_month(inv)
            if not customer or not m_key:
                continue
            for line in inv.get("Line", []):
                detail = line.get("SalesItemLineDetail", {})
                if not detail:
                    continue
                amt = _line_amount(line)
                if amt == 0:
                    continue

                # Look up the actual GL income account via Item ID
                item_ref = detail.get("ItemRef", {})
                item_id = str(item_ref.get("value", ""))
                item_name = item_ref.get("name", "").strip()
                acct_name = item_to_account.get(item_id, "")
                if not acct_name:
                    # No Item mapping: left out of the attribution (the parent's
                    # Unallocated sub-line keeps the P&L total whole).
                    if item_name and item_name not in unmapped_items:
                        unmapped_items.add(item_name)
                        log.info(f"Customer revenue: Item '{item_name}' (id={item_id}) has no IncomeAccountRef, skipping")
                    continue
                _add(result, acct_name, customer, m_key, sign * amt)

    # ── Step 3: Journal Entries (already use GL AccountRef directly) ──
    je_count = 0
    for je in _query_all(client, "JournalEntry", where):
        m_key = _txn_month(je)
        if not m_key:
            continue
        for line in je.get("Line", []):
            detail = line.get("JournalEntryLineDetail", {})
            if not detail:
                continue
            posting = str(detail.get("PostingType", "")).lower()
            if posting not in ("debit", "credit"):
                continue
            entity = detail.get("Entity", {})
            if str(entity.get("Type", "")).lower() != "customer":
                continue
            customer = str(entity.get("EntityRef", {}).get("name", "")).strip()
            acct_name = str(detail.get("AccountRef", {}).get("name", "")).strip()
            if not customer or not acct_name:
                continue
            amt = _line_amount(line)
            if amt == 0:
                continue
            _add(result, acct_name, customer, m_key, amt if posting == "credit" else -amt)
            je_count += 1

    log.info(f"Customer breakdown found {len(result)} accounts ({je_count} JE lines): {list(result.keys())[:20]}")
    return _as_floats(result)


def parse_reforecast_pl_actuals(report: dict, *, budget_year: int, months: list[str]) -> dict[str, list[dict]]:
    """Validated account/month identity for reforecast, preserving explicit zero."""
    from .driver_calculation import BudgetPlanError
    header = report.get("Header", {})
    if (header.get("StartPeriod") != f"{budget_year}-01-01"
            or header.get("EndPeriod") != f"{budget_year}-12-31"
            or str(header.get("ReportBasis", "")).lower() != "accrual"):
        raise BudgetPlanError("P&L date/basis metadata is unavailable or differs from the requested year.")
    if any(report.get(k) for k in ("Fault", "Warnings", "Truncated", "ContinuationToken")):
        raise BudgetPlanError("P&L actuals report is incomplete.")
    columns = report.get("Columns", {}).get("Column", [])
    result = {}
    for month in months:
        matches = [c for c in columns if c.get("ColType") == "Money"
                   and str(c.get("ColTitle", "")).lower().startswith(month)
                   and "total" not in str(c.get("ColTitle", "")).lower()]
        if len(matches) != 1:
            raise BudgetPlanError(f"P&L has no unique complete {month} column.")
        try:
            result[month] = _parse_pl_single_month_entries(report, month, strict=True)
        except (ValueError, TypeError, KeyError) as exc:
            raise BudgetPlanError("P&L account actuals could not be verified.") from exc
    return result

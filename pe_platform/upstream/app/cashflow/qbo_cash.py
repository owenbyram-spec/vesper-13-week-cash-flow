from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..services.errors import safe_error_detail


logger = logging.getLogger(__name__)


def _parse_amt(s) -> float:
    try:
        text = str(s).replace(",", "").replace("$", "").strip()
        if not text:
            return 0.0
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1].strip()
        if text.endswith("-"):
            negative = True
            text = text[:-1].strip()
        value = float(text)
        return -abs(value) if negative else value
    except (ValueError, TypeError):
        return 0.0


def _report_total_amount_index(report: Dict[str, Any]) -> Optional[int]:
    """Return the QBO report column index for the total money amount."""
    columns = report.get("Columns", {}).get("Column", []) or []
    money_columns: List[tuple[int, str]] = []
    for idx, column in enumerate(columns):
        title = (column.get("ColTitle") or "").strip()
        col_type = (column.get("ColType") or "").strip().lower()
        if col_type == "money":
            money_columns.append((idx, title))

    if not money_columns:
        return None

    for idx, title in money_columns:
        normalized = " ".join(title.lower().split())
        if not normalized or normalized == "total" or normalized.startswith("total "):
            return idx
    return money_columns[-1][0]


def _report_amount_column_title(report: Dict[str, Any], amount_idx: Optional[int]) -> Optional[str]:
    if amount_idx is None:
        return None
    columns = report.get("Columns", {}).get("Column", []) or []
    if amount_idx >= len(columns):
        return None
    return (columns[amount_idx].get("ColTitle") or "").strip() or None


def _amount_from_col_data(
    col_data: List[Dict[str, Any]],
    amount_idx: Optional[int] = None,
) -> Optional[float]:
    if not col_data:
        return None
    cell = col_data[amount_idx] if amount_idx is not None and amount_idx < len(col_data) else col_data[-1]
    value = cell.get("value")
    if value in (None, ""):
        return None
    return _parse_amt(value)


def _label_from_col_data(col_data: List[Dict[str, Any]]) -> str:
    if not col_data:
        return ""
    return (col_data[0].get("value") or "").strip()


def _is_bank_accounts_section(label: str) -> bool:
    normalized = " ".join(label.lower().replace("/", " ").split())
    return normalized in {"bank accounts", "checking savings"} or "bank accounts" in normalized


def _canonical_account_label(value: Optional[str]) -> str:
    normalized = " ".join((value or "").lower().replace("/", " ").split())
    normalized = re.sub(r"^\d+\s+", "", normalized)
    normalized = normalized.replace(" - ", " ")
    return " ".join(normalized.split())


def _is_clearing_account_name(value: Optional[str]) -> bool:
    normalized = _canonical_account_label(value)
    return "clearing" in normalized or "bill.com" in normalized or "billcom" in normalized


def _fetch_active_qbo_accounts(client) -> List[Dict[str, Any]]:
    """Fetch active QBO accounts with pagination so cash account discovery cannot truncate."""
    all_accounts: List[Dict[str, Any]] = []
    start_position = 1
    page_size = 1000
    while True:
        resp = client.query(
            "SELECT * FROM Account WHERE Active = true "
            f"STARTPOSITION {start_position} MAXRESULTS {page_size}"
        )
        batch = resp.get("QueryResponse", {}).get("Account", [])
        if not batch:
            break
        all_accounts.extend(batch)
        start_position += len(batch)
        if len(batch) < page_size:
            break
    return all_accounts


def _cash_account_universe(
    client,
    company_id: str,
    bank_account_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build the shared cash-account universe used by both the Balance Sheet anchor
    and weekly actuals.
    """
    bank_account_rows = bank_account_rows or []
    row_lookup = {
        _canonical_account_label(row.get("label")): row
        for row in bank_account_rows
        if row.get("label")
    }
    unmatched_rows = dict(row_lookup)

    try:
        accounts = _fetch_active_qbo_accounts(client)
    except Exception as exc:
        logger.warning("[CashForecast] Cash account universe account fetch failed for %s: %s", company_id, exc)
        accounts = []

    included_accounts: List[Dict[str, Any]] = []
    included_ids: set[str] = set()
    for account in accounts:
        account_id = str(account.get("Id") or "")
        name = account.get("Name") or ""
        fqn = account.get("FullyQualifiedName") or name
        account_type = account.get("AccountType") or ""
        account_sub_type = account.get("AccountSubType") or ""
        canonical_names = {
            _canonical_account_label(name),
            _canonical_account_label(fqn),
            _canonical_account_label(str(fqn).split(":")[-1]),
        }
        matched_key = next((key for key in canonical_names if key in row_lookup), None)
        include_from_account_type = account_type == "Bank"
        include_from_balance_sheet = matched_key is not None
        if not include_from_account_type and not include_from_balance_sheet:
            continue

        if account_id:
            included_ids.add(account_id)
        if matched_key:
            unmatched_rows.pop(matched_key, None)
        source = "balance_sheet_and_qbo_account" if include_from_balance_sheet else "qbo_account_type_bank"
        included_accounts.append(
            {
                "id": account_id or None,
                "name": name,
                "fully_qualified_name": fqn,
                "account_type": account_type,
                "account_sub_type": account_sub_type,
                "source": source,
                "balance_sheet_amount": row_lookup.get(matched_key, {}).get("amount") if matched_key else None,
                "is_clearing": _is_clearing_account_name(name) or _is_clearing_account_name(fqn),
            }
        )

    return {
        "account_ids": sorted(included_ids),
        "accounts": included_accounts,
        "included_count": len(included_accounts),
        "clearing_count": sum(1 for account in included_accounts if account["is_clearing"]),
        "unmatched_balance_sheet_rows": list(unmatched_rows.values()),
        "unmatched_balance_sheet_row_count": len(unmatched_rows),
    }


def _cash_account_universe_for_date(client, company_id: str, as_of_date: date) -> Dict[str, Any]:
    """Build cash account universe using Balance Sheet Bank Accounts rows when available."""
    bank_rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    params = {
        "start_date": as_of_date.isoformat(),
        "end_date": as_of_date.isoformat(),
        "accounting_method": "Accrual",
        "summarize_column_by": "Total",
    }
    try:
        report = client.get_report("BalanceSheet", params)
        header = report.get("Header", {}) or {}
        qbo_end_period = header.get("EndPeriod")
        if qbo_end_period and qbo_end_period != as_of_date.isoformat():
            warnings.append(
                f"QBO Balance Sheet returned EndPeriod {qbo_end_period} for requested {as_of_date.isoformat()}"
            )
        amount_idx = _report_total_amount_index(report)
        detail = _bank_accounts_section_detail(report.get("Rows", {}).get("Row", []), amount_idx)
        if detail is not None:
            bank_rows = detail["rows"]
        else:
            warnings.append("QBO Balance Sheet did not include a Bank Accounts section")
    except Exception as exc:
        detail = safe_error_detail(
            error_code="cashflow_balance_sheet_cash_rows_unavailable",
            user_message="Balance Sheet cash rows could not be loaded from QuickBooks.",
            exc=exc,
            retryable=True,
            log_context="cashflow_balance_sheet_cash_rows_unavailable",
        )
        warnings.append(f"{detail['user_message']} Reference: {detail['trace_id']}.")

    universe = _cash_account_universe(client, company_id, bank_rows)
    universe["balance_sheet_as_of"] = as_of_date.isoformat()
    universe["warnings"] = warnings
    return universe


def _report_data_row_amounts(
    rows: List[Dict[str, Any]],
    amount_idx: Optional[int] = None,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for row in rows:
        sub_rows = row.get("Rows", {}).get("Row", [])
        if sub_rows:
            child_entries = _report_data_row_amounts(sub_rows, amount_idx)
            if child_entries:
                entries.extend(child_entries)
                continue

        if row.get("type") == "Data":
            col_data = row.get("ColData", [])
            label = _label_from_col_data(col_data)
            amount = _amount_from_col_data(col_data, amount_idx)
            if label and amount is not None:
                entries.append({"label": label, "amount": round(amount, 2)})
    return entries


def _sum_report_data_rows(rows: List[Dict[str, Any]], amount_idx: Optional[int] = None) -> Optional[float]:
    entries = _report_data_row_amounts(rows, amount_idx)
    if not entries:
        return None
    return sum(float(entry["amount"] or 0.0) for entry in entries)


def _bank_accounts_section_detail(
    rows: List[Dict[str, Any]],
    amount_idx: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    for row in rows:
        header_label = _label_from_col_data((row.get("Header", {}) or {}).get("ColData", []))
        if _is_bank_accounts_section(header_label):
            summary_amount = _amount_from_col_data((row.get("Summary", {}) or {}).get("ColData", []), amount_idx)
            child_entries = _report_data_row_amounts(row.get("Rows", {}).get("Row", []), amount_idx)
            child_total = (
                round(sum(float(entry["amount"] or 0.0) for entry in child_entries), 2)
                if child_entries
                else None
            )
            selected_total = child_total if child_total is not None else summary_amount
            if selected_total is not None:
                return {
                    "section_label": header_label,
                    "total": round(selected_total, 2),
                    "summary_amount": round(summary_amount, 2) if summary_amount is not None else None,
                    "row_total": child_total,
                    "rows": child_entries,
                }
        sub_rows = row.get("Rows", {}).get("Row", [])
        if sub_rows:
            nested = _bank_accounts_section_detail(sub_rows, amount_idx)
            if nested is not None:
                return nested
    return None


def _bank_accounts_section_total(rows: List[Dict[str, Any]], amount_idx: Optional[int] = None) -> Optional[float]:
    detail = _bank_accounts_section_detail(rows, amount_idx)
    return float(detail["total"]) if detail is not None else None


def _historical_cash_balance_detail(
    client,
    company_id: str,
    as_of_date: date,
    db: Session,
) -> tuple[float, str, Dict[str, Any]]:
    """Return historical Balance Sheet Bank Accounts total plus report diagnostics."""
    params = {
        "start_date": as_of_date.isoformat(),
        "end_date": as_of_date.isoformat(),
        "accounting_method": "Accrual",
        "summarize_column_by": "Total",
    }
    report = client.get_report("BalanceSheet", params)
    amount_idx = _report_total_amount_index(report)
    amount_title = _report_amount_column_title(report, amount_idx)
    header = report.get("Header", {}) or {}
    diagnostics: Dict[str, Any] = {
        "requested_report": "BalanceSheet",
        "requested_end_date": as_of_date.isoformat(),
        "requested_params": params,
        "qbo_header_start_period": header.get("StartPeriod"),
        "qbo_header_end_period": header.get("EndPeriod"),
        "qbo_header_report_basis": header.get("ReportBasis"),
        "qbo_header_time": header.get("Time"),
        "amount_column_index": amount_idx,
        "amount_column_title": amount_title,
    }
    qbo_end_period = header.get("EndPeriod")
    if qbo_end_period and qbo_end_period != as_of_date.isoformat():
        logger.error(
            "[CashForecast] QBO Balance Sheet date mismatch company=%s requested_end_date=%s "
            "qbo_end_period=%s params=%s",
            company_id,
            as_of_date.isoformat(),
            qbo_end_period,
            params,
        )
        raise ValueError(
            f"QBO Balance Sheet returned EndPeriod {qbo_end_period} for requested end_date {as_of_date.isoformat()}"
        )

    bank_section = _bank_accounts_section_detail(report.get("Rows", {}).get("Row", []), amount_idx)
    if bank_section is not None:
        cash_universe = _cash_account_universe(client, company_id, bank_section["rows"])
        diagnostics.update(
            {
                "bank_accounts_section_label": bank_section["section_label"],
                "bank_accounts_total": bank_section["total"],
                "bank_accounts_summary_amount": bank_section["summary_amount"],
                "bank_accounts_row_total": bank_section["row_total"],
                "bank_accounts_row_count": len(bank_section["rows"]),
                "bank_accounts_rows": bank_section["rows"],
                "cash_account_universe": {
                    "included_count": cash_universe["included_count"],
                    "clearing_count": cash_universe["clearing_count"],
                    "unmatched_balance_sheet_row_count": cash_universe["unmatched_balance_sheet_row_count"],
                    "accounts": cash_universe["accounts"],
                    "unmatched_balance_sheet_rows": cash_universe["unmatched_balance_sheet_rows"],
                },
            }
        )
        summary_amount = bank_section["summary_amount"]
        row_total = bank_section["row_total"]
        if summary_amount is not None and row_total is not None and abs(summary_amount - row_total) >= 0.01:
            logger.warning(
                "[CashForecast] QBO Balance Sheet Bank Accounts summary/row mismatch company=%s requested_end_date=%s "
                "qbo_end_period=%s summary=%.2f row_total=%.2f using=row_total amount_col=%s",
                company_id,
                as_of_date.isoformat(),
                header.get("EndPeriod"),
                summary_amount,
                row_total,
                amount_idx if amount_idx is not None else "last",
            )
        logger.info(
            "[CashForecast] QBO Balance Sheet Bank Accounts anchor resolved company=%s requested_end_date=%s "
            "qbo_end_period=%s balance=%.2f rows=%s cash_accounts=%s unmatched_bs_rows=%s amount_col=%s",
            company_id,
            as_of_date.isoformat(),
            header.get("EndPeriod"),
            bank_section["total"],
            len(bank_section["rows"]),
            cash_universe["included_count"],
            cash_universe["unmatched_balance_sheet_row_count"],
            amount_idx if amount_idx is not None else "last",
        )
        return float(bank_section["total"]), "balance_sheet_accounts", diagnostics

    logger.error(
        "[CashForecast] No Bank Accounts section found in QBO Balance Sheet company=%s requested_end_date=%s "
        "qbo_end_period=%s amount_col=%s",
        company_id,
        as_of_date.isoformat(),
        header.get("EndPeriod"),
        amount_idx if amount_idx is not None else "last",
    )
    raise ValueError("No Bank Accounts section or Bank Accounts total found in balance sheet report")


def _historical_cash_balance(
    client,
    company_id: str,
    as_of_date: date,
    db: Session,
) -> tuple[float, str]:
    """Sum historical cash accounts from the BalanceSheet report as of a Friday."""
    balance, source, _diagnostics = _historical_cash_balance_detail(client, company_id, as_of_date, db)
    return balance, source

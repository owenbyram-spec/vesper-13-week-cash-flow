"""
Weekly bank-actuals pull: QBO bank transactions → ``CashForecastActual`` rows.

One company-week at a time: Deposits, Payments, Purchases, BillPayments,
Transfers and JournalEntries that hit a cash account in the Monday–Friday window
are saved for categorization, with entity-based auto-categorization and internal
bank-to-bank transfers auto-excluded.

No authorization happens here. Callers authorize first: the HTTP endpoint runs
``check_tenant_access("EDITOR", ...)``, and the transaction_review agent objective
runs under ``tenant_scope_for_realm`` for its own realm. Every QBO read is
paginated; a source that fails or exceeds the page bound marks the pull partial
(``data_quality`` + the persisted ``pull_partial`` flag) instead of looking
complete.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..models import CashForecastActual, CashForecastWeekStatus
from ..qbo_client import get_qbo_client_from_db
from ..services.redaction import redact_to_string
from .qbo_cash import _cash_account_universe_for_date
from .qbo_paging import query_all_pages, txn_date_where
from .versioning import _next_friday

logger = logging.getLogger(__name__)

WEEK_STATUS_NOT_PULLED = "not_pulled"
WEEK_STATUS_PENDING = "pending_categorization"
WEEK_STATUS_PULLED = "pulled"
WEEK_STATUS_CLOSED = "closed"
WEEK_STATUS_RECONCILED = "reconciled"
CASCADE_READY_WEEK_STATUSES = {
    WEEK_STATUS_PULLED,
    WEEK_STATUS_CLOSED,
    WEEK_STATUS_RECONCILED,
}

# Page bound per QBO source for one Monday–Friday window (1000 rows/page). A busier
# week is pulled up to the bound and flagged partial, never silently cut.
ACTUALS_MAX_PAGES = 20


class NoCashAccountsError(Exception):
    """The realm has no cash account to pull bank activity for.

    ``detail`` is the user-facing explanation built here from the cash-account universe
    (never provider exception text), so the HTTP route can return it as-is.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _actuals_counts_for_week(db: Session, company_id: str, week_ending: date) -> Dict[str, int]:
    records = db.query(CashForecastActual).filter(
        CashForecastActual.company_id == company_id,
        CashForecastActual.week_ending == week_ending,
        CashForecastActual.parent_id == None,  # noqa: E711
    ).all()
    return {
        "transaction_count": len(records),
        "categorized_count": sum(1 for r in records if r.category == "categorized"),
        "uncategorized_count": sum(1 for r in records if r.category == "uncategorized"),
        "excluded_count": sum(1 for r in records if r.category == "excluded"),
        "decomposed_count": sum(1 for r in records if r.category == "decomposed"),
    }


def _sync_week_status(
    db: Session,
    company_id: str,
    week_ending: date,
    *,
    explicit_status: Optional[str] = None,
    note: Optional[str] = None,
    pull_partial: bool | None = None,
    commit: bool = False,
) -> CashForecastWeekStatus:
    counts = _actuals_counts_for_week(db, company_id, week_ending)
    row = db.query(CashForecastWeekStatus).filter(
        CashForecastWeekStatus.company_id == company_id,
        CashForecastWeekStatus.week_ending == week_ending,
    ).first()
    if not row:
        row = CashForecastWeekStatus(
            company_id=company_id,
            week_ending=week_ending,
            status=WEEK_STATUS_NOT_PULLED,
        )
        db.add(row)

    if explicit_status:
        status = explicit_status
    elif counts["transaction_count"] == 0 and not row.pulled_at:
        status = WEEK_STATUS_NOT_PULLED
    elif counts["uncategorized_count"] > 0:
        status = WEEK_STATUS_PENDING
    elif row.status in {WEEK_STATUS_CLOSED, WEEK_STATUS_RECONCILED}:
        status = row.status
    else:
        status = WEEK_STATUS_PULLED

    row.status = status  # type: ignore[assignment]
    row.transaction_count = counts["transaction_count"]  # type: ignore[assignment]
    row.uncategorized_count = counts["uncategorized_count"]  # type: ignore[assignment]
    if note is not None:
        row.note = note  # type: ignore[assignment]
    if pull_partial is not None:
        row.pull_partial = pull_partial  # type: ignore[assignment]
    if status in {WEEK_STATUS_PULLED, WEEK_STATUS_PENDING, WEEK_STATUS_CLOSED, WEEK_STATUS_RECONCILED} and not row.pulled_at:
        row.pulled_at = datetime.now(timezone.utc)  # type: ignore[assignment]
    if status in {WEEK_STATUS_CLOSED, WEEK_STATUS_RECONCILED} and not row.reviewed_at:
        row.reviewed_at = datetime.now(timezone.utc)  # type: ignore[assignment]
    if status == WEEK_STATUS_RECONCILED and not row.reconciled_at:
        row.reconciled_at = datetime.now(timezone.utc)  # type: ignore[assignment]
    if commit:
        db.commit()
        db.refresh(row)
    return row


def _is_billcom_transaction(txn: Dict) -> bool:
    """Detect if a transaction is a Bill.com payment by payee name."""
    # Check various name fields for Bill.com indicators
    for field in ["EntityRef", "VendorRef"]:
        ref = txn.get(field) or {}
        name = (ref.get("name") or "").lower()
        if "bill.com" in name or "billcom" in name:
            return True
    # Check the memo/description
    memo = (txn.get("PrivateNote") or "").lower()
    if "bill.com" in memo:
        return True
    return False


def _actual_record_payload(record: CashForecastActual) -> Dict[str, Any]:
    return {
        "id": record.id,
        "qbo_txn_id": record.qbo_txn_id,
        "qbo_txn_type": record.qbo_txn_type,
        "qbo_account_id": record.qbo_account_id,
        "txn_date": record.txn_date.isoformat(),
        "amount": float(record.amount),
        "direction": record.direction,
        "description": record.description,
        "account_name": record.account_name,
        "entity_name": record.entity_name,
        "entity_type": record.entity_type,
        "category": record.category,
        "is_billcom": record.is_billcom,
        "parent_id": record.parent_id,
    }


def _cash_account_universe_response(universe: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "balance_sheet_as_of": universe.get("balance_sheet_as_of"),
        "included_count": universe.get("included_count", 0),
        "clearing_count": universe.get("clearing_count", 0),
        "unmatched_balance_sheet_row_count": universe.get("unmatched_balance_sheet_row_count", 0),
        "warnings": universe.get("warnings", []),
        "accounts": universe.get("accounts", []),
        "unmatched_balance_sheet_rows": universe.get("unmatched_balance_sheet_rows", []),
    }


def _fetch_source(
    client: Any,
    source: str,
    where: str,
    partial_errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """All rows of one QBO source for the window; failures/truncation → partial_errors."""
    key = source.lower()
    try:
        rows, truncated = query_all_pages(client, source, where, max_pages=ACTUALS_MAX_PAGES)
    except Exception as e:
        logger.warning("[Actuals] %s fetch failed: %s", source, redact_to_string(str(e), max_length=500))
        partial_errors.append({"error_code": f"actuals_{key}_unavailable", "section": source})
        return []
    if truncated:
        logger.warning("[Actuals] %s fetch hit the %s-page bound; pull is partial", source, ACTUALS_MAX_PAGES)
        partial_errors.append({"error_code": f"actuals_{key}_truncated", "section": source})
    return rows


def pull_week_actuals(
    db: Session,
    company_id: str,
    week_ending: Optional[date] = None,
    *,
    client: Any = None,
) -> Dict[str, Any]:
    """Pull one week's bank transactions from QBO and save them as actuals.

    ``week_ending`` is the Friday of the target week (default: last week's
    Friday). ``client`` is the realm's QBO client; resolved here when omitted.
    A week that already has actuals is returned as ``already_pulled`` without a
    re-pull. Raises ``NoCashAccountsError`` when the realm has no cash account.
    """
    if client is None:
        client = get_qbo_client_from_db(db, company_id)

    if not week_ending:
        # Default to last week's Friday
        week_ending = _next_friday(date.today()) - timedelta(weeks=1)

    week_start = week_ending - timedelta(days=4)  # Monday
    since = week_start.isoformat()
    until = week_ending.isoformat()
    cash_universe = _cash_account_universe_for_date(client, company_id, week_ending)

    # Check if we already have actuals for this week
    existing = db.query(CashForecastActual).filter(
        CashForecastActual.company_id == company_id,
        CashForecastActual.week_ending == week_ending,
    ).count()
    if existing > 0:
        # Return existing records instead of re-pulling
        week_status = _sync_week_status(db, company_id, week_ending)
        db.commit()
        records = db.query(CashForecastActual).filter(
            CashForecastActual.company_id == company_id,
            CashForecastActual.week_ending == week_ending,
            CashForecastActual.parent_id == None,  # noqa: E711
        ).all()
        return {
            "week_ending": week_ending.isoformat(),
            "week_start": week_start.isoformat(),
            "status": "already_pulled",
            "review_status": week_status.status,
            "count": len(records),
            "cash_account_universe": _cash_account_universe_response(cash_universe),
            "transactions": [_actual_record_payload(r) for r in records],
            "data_quality": {
                "is_partial": bool(week_status.pull_partial),
                "unavailable_sections": ["actuals_pull_partial"] if week_status.pull_partial else [],
            },
        }

    cash_account_ids = set(cash_universe["account_ids"])
    if not cash_account_ids:
        detail = "No cash accounts found in QBO"
        warnings = cash_universe.get("warnings") or []
        if warnings:
            detail = f"{detail}: {'; '.join(warnings)}"
        raise NoCashAccountsError(detail)

    where = txn_date_where(since, until)
    pulled = []
    # Per-source fetch failures and page-bound truncation, so a partial pull is
    # surfaced in the response (data_quality) + persisted on the week-status row
    # instead of a silent-complete success.
    partial_errors: list[dict[str, str]] = []

    # ── Deposits (cash in) ────────────────────────────────────────────────
    # Track Payment IDs that are linked to Deposits so we can skip them
    # in the Payment query (prevents double counting).
    deposit_linked_payment_ids: set = set()

    for dep in _fetch_source(client, "Deposit", where, partial_errors):
        # Only include if deposited into a bank account
        dep_acct = (dep.get("DepositToAccountRef") or {}).get("value", "")
        if dep_acct not in cash_account_ids:
            continue

        total = float(dep.get("TotalAmt", 0) or 0)
        txn_date_raw = dep.get("TxnDate", "")
        try:
            txn_dt = date.fromisoformat(txn_date_raw)
        except ValueError:
            continue

        # Collect linked Payment/SalesReceipt IDs from deposit lines
        for line in dep.get("Line", []):
            for lt in line.get("LinkedTxn", []):
                if lt.get("TxnType") in ("Payment", "SalesReceipt"):
                    deposit_linked_payment_ids.add(lt.get("TxnId", ""))

        # Try to find entity name from deposit lines
        entity_name = None
        for line in dep.get("Line", []):
            dld = line.get("DepositLineDetail") or {}
            entity_ref = dld.get("Entity") or {}
            ref = entity_ref.get("EntityRef") or {}
            name = ref.get("name")
            if name:
                entity_name = name
                break

        acct_name = (dep.get("DepositToAccountRef") or {}).get("name", "")

        pulled.append({
            "qbo_txn_id": dep.get("Id", ""),
            "qbo_txn_type": "Deposit",
            "txn_date": txn_dt,
            "amount": abs(total),
            "direction": "in",
            "description": dep.get("PrivateNote", "") or entity_name or "",
            "qbo_account_id": dep_acct,
            "account_name": acct_name,
            "entity_name": entity_name,
            "entity_type": "customer" if entity_name else None,
            "is_billcom": False,
        })

    logger.info(f"[Actuals] {len(deposit_linked_payment_ids)} payment IDs linked to deposits (will be skipped)")

    # ── Payments (customer AR collections — cash in) ──────────────────────
    for pmt in _fetch_source(client, "Payment", where, partial_errors):
        # Skip Payments already captured by a Deposit
        pmt_id = pmt.get("Id", "")
        if pmt_id in deposit_linked_payment_ids:
            logger.debug(f"[Actuals] Skipping Payment {pmt_id} — already linked to a Deposit")
            continue

        total = float(pmt.get("TotalAmt", 0) or 0)
        txn_date_raw = pmt.get("TxnDate", "")
        try:
            txn_dt = date.fromisoformat(txn_date_raw)
        except ValueError:
            continue

        # Only include if deposited to a bank account
        dep_acct = (pmt.get("DepositToAccountRef") or {}).get("value", "")
        if not dep_acct or dep_acct not in cash_account_ids:
            continue

        customer_name = (pmt.get("CustomerRef") or {}).get("name", "")

        pulled.append({
            "qbo_txn_id": pmt.get("Id", ""),
            "qbo_txn_type": "Payment",
            "txn_date": txn_dt,
            "amount": abs(total),
            "direction": "in",
            "description": customer_name or pmt.get("PrivateNote", ""),
            "qbo_account_id": dep_acct,
            "account_name": (pmt.get("DepositToAccountRef") or {}).get("name", ""),
            "entity_name": customer_name or None,
            "entity_type": "customer" if customer_name else None,
            "is_billcom": False,
        })

    # ── Purchases (vendor payments — cash out) ────────────────────────────
    for p in _fetch_source(client, "Purchase", where, partial_errors):
        # Only include Cash/Check purchases (not CC charges)
        pay_type = p.get("PaymentType", "")
        if pay_type == "CreditCard":
            continue  # Skip individual CC charges

        # Verify it hit a bank account
        acct_ref = (p.get("AccountRef") or {}).get("value", "")
        if not acct_ref or acct_ref not in cash_account_ids:
            continue

        total = float(p.get("TotalAmt", 0) or 0)
        txn_date_raw = p.get("TxnDate", "")
        try:
            txn_dt = date.fromisoformat(txn_date_raw)
        except ValueError:
            continue

        entity_ref = p.get("EntityRef") or {}
        entity_name = entity_ref.get("name", "")
        is_billcom = _is_billcom_transaction(p)

        pulled.append({
            "qbo_txn_id": p.get("Id", ""),
            "qbo_txn_type": "Purchase",
            "txn_date": txn_dt,
            "amount": abs(total),
            "direction": "out",
            "description": entity_name or p.get("PrivateNote", ""),
            "qbo_account_id": acct_ref,
            "account_name": (p.get("AccountRef") or {}).get("name", ""),
            "entity_name": entity_name or None,
            "entity_type": "vendor" if entity_name else None,
            "is_billcom": is_billcom,
        })

    # ── Bill Payments (vendor payments through AP — cash out) ─────────────
    for bp in _fetch_source(client, "BillPayment", where, partial_errors):
        # Only include check payments (not CC)
        pay_type = bp.get("PayType", "")
        if pay_type == "CreditCard":
            continue
        bank_ref = (bp.get("CheckPayment") or {}).get("BankAccountRef") or {}
        bank_account_id = bank_ref.get("value", "")
        if not bank_account_id or bank_account_id not in cash_account_ids:
            continue

        total = float(bp.get("TotalAmt", 0) or 0)
        txn_date_raw = bp.get("TxnDate", "")
        try:
            txn_dt = date.fromisoformat(txn_date_raw)
        except ValueError:
            continue

        vendor_name = (bp.get("VendorRef") or {}).get("name", "")
        is_billcom = _is_billcom_transaction(bp)

        pulled.append({
            "qbo_txn_id": bp.get("Id", ""),
            "qbo_txn_type": "BillPayment",
            "txn_date": txn_dt,
            "amount": abs(total),
            "direction": "out",
            "description": vendor_name or bp.get("PrivateNote", ""),
            "qbo_account_id": bank_account_id,
            "account_name": bank_ref.get("name", "") or (bp.get("APAccountRef") or {}).get("name", ""),
            "entity_name": vendor_name or None,
            "entity_type": "vendor" if vendor_name else None,
            "is_billcom": is_billcom,
        })

    # ── Transfers (between accounts — track as cash out from source) ──────
    for t in _fetch_source(client, "Transfer", where, partial_errors):
        total = float(t.get("Amount", 0) or 0)
        txn_date_raw = t.get("TxnDate", "")
        try:
            txn_dt = date.fromisoformat(txn_date_raw)
        except ValueError:
            continue

        from_acct = (t.get("FromAccountRef") or {}).get("value", "")
        to_acct = (t.get("ToAccountRef") or {}).get("value", "")
        from_name = (t.get("FromAccountRef") or {}).get("name", "")
        to_name = (t.get("ToAccountRef") or {}).get("name", "")

        # If BOTH accounts are bank accounts, this is an internal transfer
        # (net $0 to consolidated cash). Auto-exclude.
        is_internal = from_acct in cash_account_ids and to_acct in cash_account_ids

        # If transferring FROM a bank account → cash out
        if from_acct in cash_account_ids:
            pulled.append({
                "qbo_txn_id": t.get("Id", "") + "_out",
                "qbo_txn_type": "Transfer",
                "txn_date": txn_dt,
                "amount": abs(total),
                "direction": "out",
                "description": f"Transfer to {to_name}",
                "qbo_account_id": from_acct,
                "account_name": from_name,
                "entity_name": f"Transfer → {to_name}",
                "entity_type": "vendor",
                "is_billcom": False,
                "_auto_exclude": is_internal,
            })
        # If transferring TO a bank account → cash in
        if to_acct in cash_account_ids:
            pulled.append({
                "qbo_txn_id": t.get("Id", "") + "_in",
                "qbo_txn_type": "Transfer",
                "txn_date": txn_dt,
                "amount": abs(total),
                "direction": "in",
                "description": f"Transfer from {from_name}",
                "qbo_account_id": to_acct,
                "account_name": to_name,
                "entity_name": f"Transfer ← {from_name}",
                "entity_type": "customer",
                "is_billcom": False,
                "_auto_exclude": is_internal,
            })

    # ── JournalEntries (payroll, adjustments — may hit bank accounts) ─────
    for je in _fetch_source(client, "JournalEntry", where, partial_errors):
        txn_date_raw = je.get("TxnDate", "")
        try:
            txn_dt = date.fromisoformat(txn_date_raw)
        except ValueError:
            continue

        bank_lines = []
        has_non_bank_nonzero = False

        for line in je.get("Line", []):
            jed = line.get("JournalEntryLineDetail") or {}
            acct_ref = (jed.get("AccountRef") or {}).get("value", "")
            line_amt = abs(float(line.get("Amount", 0) or 0))
            if line_amt == 0:
                continue
            posting_type = jed.get("PostingType", "")
            acct_name = (jed.get("AccountRef") or {}).get("name", "")
            if acct_ref in cash_account_ids and posting_type in {"Credit", "Debit"}:
                bank_lines.append({
                    "account_id": acct_ref,
                    "account_name": acct_name,
                    "posting_type": posting_type,
                    "amount": line_amt,
                    "line_id": line.get("Id", ""),
                })
            else:
                has_non_bank_nonzero = True

        is_internal_bank_journal = (
            len(bank_lines) >= 2
            and not has_non_bank_nonzero
            and any(line["posting_type"] == "Credit" for line in bank_lines)
            and any(line["posting_type"] == "Debit" for line in bank_lines)
            and round(
                sum(line["amount"] for line in bank_lines if line["posting_type"] == "Debit")
                - sum(line["amount"] for line in bank_lines if line["posting_type"] == "Credit"),
                2,
            ) == 0
        )

        # JournalEntries have Line items. We look for lines that
        # CREDIT a bank account (cash out) or DEBIT a bank account (cash in).
        for line in je.get("Line", []):
            jed = line.get("JournalEntryLineDetail") or {}
            acct_ref = (jed.get("AccountRef") or {}).get("value", "")
            if acct_ref not in cash_account_ids:
                continue  # This line doesn't hit a bank account

            line_amt = abs(float(line.get("Amount", 0) or 0))
            if line_amt == 0:
                continue

            posting_type = jed.get("PostingType", "")  # "Credit" or "Debit"
            entity_ref = jed.get("Entity") or {}
            entity_name_ref = (entity_ref.get("EntityRef") or {}).get("name", "")

            # Credit to bank = money leaving (cash out)
            # Debit to bank = money entering (cash in)
            if posting_type == "Credit":
                direction = "out"
                e_type = "vendor"
            elif posting_type == "Debit":
                direction = "in"
                e_type = "customer"
            else:
                continue

            acct_name = (jed.get("AccountRef") or {}).get("name", "")
            memo = je.get("PrivateNote", "") or je.get("DocNumber", "") or ""
            counterpart_accounts = [
                item["account_name"]
                for item in bank_lines
                if item["account_id"] != acct_ref
            ]
            counterpart_label = ", ".join(dict.fromkeys(counterpart_accounts))

            if is_internal_bank_journal and counterpart_label:
                if direction == "out":
                    description = f"Transfer to {counterpart_label}"
                    entity_name = f"Transfer → {counterpart_label}"
                else:
                    description = f"Transfer from {counterpart_label}"
                    entity_name = f"Transfer ← {counterpart_label}"
                qbo_txn_type = "Transfer"
            else:
                description = entity_name_ref or memo or "Journal Entry"
                entity_name = entity_name_ref or None
                qbo_txn_type = "JournalEntry"

            pulled.append({
                "qbo_txn_id": je.get("Id", "") + f"_L{line.get('Id', '')}",
                "qbo_txn_type": qbo_txn_type,
                "txn_date": txn_dt,
                "amount": line_amt,
                "direction": direction,
                "description": description,
                "qbo_account_id": acct_ref,
                "account_name": acct_name,
                "entity_name": entity_name,
                "entity_type": e_type if (entity_name_ref or is_internal_bank_journal) else None,
                "is_billcom": False,
                "_auto_exclude": is_internal_bank_journal,
            })

    # Deduplicate by qbo_txn_id + type
    seen = set()
    deduped = []
    for item in pulled:
        key = (item["qbo_txn_id"], item["qbo_txn_type"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    pulled = deduped

    # Save to database
    saved = []
    for item in pulled:
        # Auto-exclude internal bank-to-bank transfers
        if item.get("_auto_exclude"):
            category = "excluded"
        elif item["entity_name"]:
            category = "categorized"
        else:
            category = "uncategorized"
        record = CashForecastActual(
            company_id=company_id,
            qbo_txn_id=item["qbo_txn_id"],
            qbo_txn_type=item["qbo_txn_type"],
            txn_date=item["txn_date"],
            amount=item["amount"],
            direction=item["direction"],
            description=item["description"],
            qbo_account_id=item.get("qbo_account_id"),
            account_name=item["account_name"],
            entity_name=item["entity_name"],
            entity_type=item["entity_type"],
            category=category,
            is_billcom=item["is_billcom"],
            week_ending=week_ending,
        )
        db.add(record)
        saved.append(record)

    db.commit()
    for r in saved:
        db.refresh(r)

    explicit_status = (
        WEEK_STATUS_PENDING
        if any(r.category == "uncategorized" for r in saved)
        else WEEK_STATUS_PULLED
    )
    week_status = _sync_week_status(
        db,
        company_id,
        week_ending,
        explicit_status=explicit_status,
        pull_partial=bool(partial_errors),
    )
    db.commit()

    logger.info(f"[Actuals] Pulled {len(saved)} bank transactions for week ending {week_ending}")

    return {
        "week_ending": week_ending.isoformat(),
        "week_start": week_start.isoformat(),
        "status": "pulled",
        "review_status": week_status.status,
        "count": len(saved),
        "cash_account_universe": _cash_account_universe_response(cash_universe),
        "transactions": [_actual_record_payload(r) for r in saved],
        "errors": partial_errors,
        "data_quality": {
            "is_partial": bool(partial_errors),
            "unavailable_sections": [e["error_code"] for e in partial_errors],
        },
    }

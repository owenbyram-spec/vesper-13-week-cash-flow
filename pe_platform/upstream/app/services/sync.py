import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Dict, Any, Optional
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import (
    QBOAccount,
    QBOVendor,
    QBOCustomer,
    QBOItem,
    QBOTransaction,
    QBOCompanyInfo,
    QBOReport,
    QBOJournalEntry,
    ReconciliationMatch,
)
from ..qbo_client import QBOClient
from .account_balances import record_balance_snapshots
from .errors import safe_error_detail
from .finance_search import index_qbo_entity
from .qbo_gl_cache import upsert_gl_report_lines
from .redaction import redact_to_string

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000


def _org_id_for_realm(db: Session, realm_id: str) -> int | None:
    """Owning org id for a realm.

    The org-keyed tables (qbo_accounts/qbo_vendors/qbo_reports) MUST carry
    ``organization_id`` or the live-RLS WITH CHECK denies the INSERT even under a
    pinned org GUC — a NULL ``organization_id`` never matches ``current_org_id``
    (INCIDENT-qbo-tokens-rls.md §B). Resolved via the RLS-excluded org tables, so
    it works under any scope. ``None`` when the realm has no org mapping (the row
    then only writes successfully under a bypass scope, which is correct).
    """
    from .tenant_companies import organization_for_company

    org = organization_for_company(db, str(realm_id))
    return org.id if org else None


def _sync_error_message(stage: str, exc: Exception, *, log_context: str) -> str:
    detail = safe_error_detail(
        error_code=f"sync_{stage}_failed",
        user_message=f"{stage} sync failed.",
        exc=exc,
        retryable=True,
        log_context=log_context,
    )
    return f"{stage}: {detail['user_message']} Reference: {detail['trace_id']}."


def _upsert_rows(
    db: Session,
    model: type,
    rows: list[dict[str, Any]],
    *,
    identity: list[str],
    update_columns: list[str],
    extra_set: dict[str, Any] | None = None,
) -> int:
    """Upsert mirror rows keyed by the table's DATABASE unique identity; returns
    the number of distinct rows written.

    The writers used to SELECT the existing rows into a dict and insert the misses,
    so two overlapping syncs of one realm both missed a new record and both inserted
    it. ``INSERT ... ON CONFLICT DO UPDATE`` lets the unique constraint decide.

    Executemany form only (``execute(stmt, rows)``): a multi-VALUES insert suffixes
    every parameter per row and the realm-bound statement gate cannot see the realm
    on any of them (see ``sync_accounts``). A key repeated within ``rows`` keeps its
    last occurrence: Postgres refuses to let one batched ON CONFLICT statement
    update the same row twice.
    """
    rows = list({tuple(row[column] for column in identity): row for row in rows}.values())
    if not rows:
        return 0
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    stmt = insert(model)
    set_ = {column: stmt.excluded[column] for column in update_columns}
    # updated_at carries an ORM-level onupdate that Core ON CONFLICT does not apply.
    set_["updated_at"] = func.now()
    set_.update(extra_set or {})
    db.execute(stmt.on_conflict_do_update(index_elements=identity, set_=set_), rows)
    return len(rows)


def _money(value: Any) -> Decimal | None:
    """QBO JSON numbers (float/int/str) as exact Decimals for Numeric columns."""
    return Decimal(str(value)) if value is not None else None


def _fetch_all(client: QBOClient, select_query: str, entity_name: str) -> List[Dict[str, Any]]:
    """
    Helper to fetch all records using pagination.
    select_query should NOT include STARTPOSITION or MAXRESULTS.
    """
    all_records = []
    start_position = 1
    
    while True:
        # Construct paginated query
        query = f"{select_query} STARTPOSITION {start_position} MAXRESULTS {BATCH_SIZE}"
        response = client.query(query)
        query_response = response.get("QueryResponse", {})
        batch = query_response.get(entity_name, [])
        
        if not batch:
            break
            
        all_records.extend(batch)
        start_position += len(batch)
        
        if len(batch) < BATCH_SIZE:
            break
            
    return all_records


def sync_company_info(db: Session, client: QBOClient):
    """
    Syncs basic CompanyInfo.
    """
    logger.info("Syncing CompanyInfo...")
    info = client.get_company_info()
    company_info = info.get("CompanyInfo", {})
    
    if not company_info:
        return

    # Check if exists
    existing = db.query(QBOCompanyInfo).filter(QBOCompanyInfo.realm_id == client.realm_id).first()
    
    if not existing:
        existing = QBOCompanyInfo(realm_id=client.realm_id)
        db.add(existing)

    existing.company_name = company_info.get("CompanyName")
    existing.legal_name = company_info.get("LegalName")
    existing.country = company_info.get("Country")
    
    # Nested fields often refer to name/value pairs, simplifying here
    # StartMonth is often e.g. "January"
    existing.fiscal_year_start_month = company_info.get("FiscalYearStartMonth")
    
    db.commit()


def sync_accounts(db: Session, client: QBOClient):
    """Sync the Chart of Accounts.

    Identity is ``(realm_id, qbo_id)`` and is resolved by the DATABASE via
    ``uq_qbo_account_identity`` + ``ON CONFLICT DO UPDATE``. This previously
    deduplicated against a Python dict built from one SELECT, so two concurrent syncs
    of the same realm both missed and both inserted duplicate accounts
    (plan 20260910-consolidator-prerequisites AC-2).
    """
    logger.info("Syncing Accounts...")
    records = _fetch_all(client, "SELECT * FROM Account", "Account")
    org_id = _org_id_for_realm(db, client.realm_id)

    balances_by_qbo_id = {}  # 0D: {qbo_id: current_balance} for the period snapshot
    rows = []
    for rec in records:
        qbo_id = rec.get("Id")
        if not qbo_id:
            continue
        rows.append({
            "realm_id": client.realm_id,
            "organization_id": org_id,
            "qbo_id": qbo_id,
            "name": rec.get("Name"),
            "fully_qualified_name": rec.get("FullyQualifiedName"),
            "acct_num": rec.get("AcctNum"),   # e.g. "1005"
            "account_type": rec.get("AccountType"),
            "account_sub_type": rec.get("AccountSubType"),
            "classification": rec.get("Classification"),
            # `or {}` not `.get(k, {})`: QBO sends an explicit null for an absent ref.
            "parent_ref": (rec.get("ParentRef") or {}).get("value"),
            "currency": (rec.get("CurrencyRef") or {}).get("value"),
            "current_balance": rec.get("CurrentBalance"),
            "active": rec.get("Active", True),
        })
        balances_by_qbo_id[qbo_id] = rec.get("CurrentBalance")

    if rows:
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        # NO .values(rows) HERE - the executemany form is required, not stylistic.
        # A values-LIST insert compiles to one multi-VALUES statement whose parameters are
        # suffixed per row (realm_id_m0, realm_id_m1, ...) with no bare `realm_id` key, so
        # the realm-bound statement gate cannot see the realm bound on any row and denies
        # the write. That made EVERY Chart-of-Accounts sync fail: the OAuth callback's
        # initial sync, the nightly sync_all_data task, and POST /sync/accounts.
        # Passing `rows` to execute() instead gives one parameter dict per row keyed by
        # bare column names, which the gate reads. Verified both shapes against the gate.
        stmt = insert(QBOAccount)
        # `updated_at` carries an ORM-level onupdate that Core ON CONFLICT does NOT
        # apply, so it is set explicitly — the dedupe migration breaks ties on it.
        stmt = stmt.on_conflict_do_update(
            index_elements=["realm_id", "qbo_id"],
            set_={
                "organization_id": stmt.excluded.organization_id,
                "name": stmt.excluded.name,
                "fully_qualified_name": stmt.excluded.fully_qualified_name,
                "acct_num": stmt.excluded.acct_num,
                "account_type": stmt.excluded.account_type,
                "account_sub_type": stmt.excluded.account_sub_type,
                "classification": stmt.excluded.classification,
                "parent_ref": stmt.excluded.parent_ref,
                "currency": stmt.excluded.currency,
                "current_balance": stmt.excluded.current_balance,
                "active": stmt.excluded.active,
                "updated_at": func.now(),
            },
        )
        db.execute(stmt, rows)

    db.commit()

    # 0D: stamp a period-keyed balance snapshot per account for the current month so
    # period-over-period deltas (e.g. a debt account's principal reduction, the
    # trailing-DSCR term) become derivable. Idempotent: a same-month re-sync updates.
    period = date.today().strftime("%Y-%m")
    record_balance_snapshots(db, client.realm_id, org_id, balances_by_qbo_id, period)
    db.commit()


def _ref_value(rec: dict[str, Any], key: str) -> Any:
    # `or {}`, not `.get(k, {})`: QBO sends an explicit null for an absent ref.
    return (rec.get(key) or {}).get("value")


def _email(rec: dict[str, Any]) -> Any:
    return (rec.get("PrimaryEmailAddr") or {}).get("Address")


def _vendor_row(realm_id: str, org_id: int | None, rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "realm_id": realm_id,
        "organization_id": org_id,
        "qbo_id": rec["Id"],
        "display_name": rec.get("DisplayName"),
        "company_name": rec.get("CompanyName"),
        "email": _email(rec),
        "balance": _money(rec.get("Balance")),
        "active": rec.get("Active", True),
    }


def _customer_row(realm_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "realm_id": realm_id,
        "qbo_id": rec["Id"],
        "display_name": rec.get("DisplayName"),
        "company_name": rec.get("CompanyName"),
        "email": _email(rec),
        "balance": _money(rec.get("Balance")),
        "active": rec.get("Active", True),
    }


_VENDOR_UPDATE_COLUMNS = ["organization_id", "display_name", "company_name", "email", "balance", "active"]
_CUSTOMER_UPDATE_COLUMNS = ["display_name", "company_name", "email", "balance", "active"]


def _upsert_vendors(db: Session, realm_id: str, records: list[dict[str, Any]]) -> None:
    org_id = _org_id_for_realm(db, realm_id)
    rows = [_vendor_row(realm_id, org_id, rec) for rec in records if rec.get("Id")]
    _upsert_rows(db, QBOVendor, rows, identity=["realm_id", "qbo_id"], update_columns=_VENDOR_UPDATE_COLUMNS)


def _upsert_customers(db: Session, realm_id: str, records: list[dict[str, Any]]) -> None:
    rows = [_customer_row(realm_id, rec) for rec in records if rec.get("Id")]
    _upsert_rows(db, QBOCustomer, rows, identity=["realm_id", "qbo_id"], update_columns=_CUSTOMER_UPDATE_COLUMNS)


def sync_vendors(db: Session, client: QBOClient):
    """Sync the vendor list; identity ``(realm_id, qbo_id)`` resolved by the database."""
    logger.info("Syncing Vendors...")
    _upsert_vendors(db, client.realm_id, _fetch_all(client, "SELECT * FROM Vendor", "Vendor"))
    db.commit()


def sync_customers(db: Session, client: QBOClient):
    """Sync the customer list; identity ``(realm_id, qbo_id)`` resolved by the database."""
    logger.info("Syncing Customers...")
    _upsert_customers(db, client.realm_id, _fetch_all(client, "SELECT * FROM Customer", "Customer"))
    db.commit()


def sync_items(db: Session, client: QBOClient):
    """Sync products and services; identity ``(realm_id, qbo_id)`` resolved by the database."""
    logger.info("Syncing Items...")
    records = _fetch_all(client, "SELECT * FROM Item", "Item")
    rows = [
        {
            "realm_id": client.realm_id,
            "qbo_id": rec["Id"],
            "name": rec.get("Name"),
            "type": rec.get("Type"),
            "unit_price": _money(rec.get("UnitPrice")),
            "expense_account_ref": _ref_value(rec, "ExpenseAccountRef"),
            "income_account_ref": _ref_value(rec, "IncomeAccountRef"),
            "active": rec.get("Active", True),
        }
        for rec in records
        if rec.get("Id")
    ]
    _upsert_rows(
        db, QBOItem, rows, identity=["realm_id", "qbo_id"],
        update_columns=["name", "type", "unit_price", "expense_account_ref", "income_account_ref", "active"],
    )
    db.commit()


_LIST_ENTITIES = {"Vendor": QBOVendor, "Customer": QBOCustomer}


def remove_list_entity(db: Session, realm_id: str, entity_name: str, qbo_id: str) -> int:
    """Drop a vendor/customer cache row QBO deleted or merged away. Returns rows removed."""
    model = _LIST_ENTITIES.get(entity_name)
    if model is None or not qbo_id:
        return 0
    removed = (
        db.query(model)
        .filter(model.realm_id == realm_id, model.qbo_id == str(qbo_id))
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed


def refresh_list_entity(db: Session, client: QBOClient, entity_name: str, qbo_id: str) -> bool:
    """Re-read ONE vendor/customer and upsert it, or drop it when QBO no longer returns
    it. Returns True when the cache row was written. Fetch failures raise."""
    if entity_name not in _LIST_ENTITIES or not qbo_id:
        return False
    record = fetch_entity_by_id(client, entity_name, qbo_id)
    if record is None:
        remove_list_entity(db, client.realm_id, entity_name, qbo_id)
        return False
    if entity_name == "Vendor":
        _upsert_vendors(db, client.realm_id, [record])
    else:
        _upsert_customers(db, client.realm_id, [record])
    db.commit()
    return True


# QBO PaymentType -> the exact lowercase transaction_type strings the bank-recon
# matcher recognizes (match.py:_MONEY_OUT_TYPES). Storing the raw enum (e.g. bare
# "CreditCard") would normalize OUTSIDE the matcher's set and make the row
# direction-unknown forever (LOCK-1/AC-2, plan 20260703-recon-book-side-rebuild).
_PURCHASE_PAYMENT_TYPE_MAP: dict[str, str] = {
    "Cash": "cash",
    "Check": "check",
    "CreditCard": "creditcardexpense",
}


def _purchase_account_ref(rec: dict[str, Any]) -> str | None:  # CRAFT-OK: dynamic QBO Purchase payload boundary
    """LOCK-2 classification-flood guard for a Purchase record.

    Populate ``account_ref`` (the category column) ONLY when the purchase has
    EXACTLY ONE line and that line's detail is ``AccountBasedExpenseLineDetail``
    carrying an ``AccountRef.value``. Any ``ItemBasedExpenseLineDetail`` line, or
    more than one line, leaves the row NULL — genuinely needs human review. Never
    derived from the bank/CC posting account (``AccountRef`` at the header level).
    """
    lines = rec.get("Line") or []
    if len(lines) != 1:
        return None
    detail = lines[0].get("AccountBasedExpenseLineDetail")
    if detail is None:
        return None
    if lines[0].get("ItemBasedExpenseLineDetail") is not None:
        return None
    return detail.get("AccountRef", {}).get("value")


def _deposit_account_ref(rec: dict[str, Any]) -> str | None:  # CRAFT-OK: dynamic QBO Deposit payload boundary
    """LOCK-2 classification-flood guard for a Deposit record.

    Same rule as ``_purchase_account_ref`` over ``DepositLineDetail`` lines:
    exactly one line, categorized, carrying an ``AccountRef.value`` -> populate;
    otherwise (multi-line, or no ``DepositLineDetail``) leave NULL.
    """
    lines = rec.get("Line") or []
    if len(lines) != 1:
        return None
    detail = lines[0].get("DepositLineDetail")
    if detail is None:
        return None
    return detail.get("AccountRef", {}).get("value")


def _purchase_description(rec: dict[str, Any]) -> str:  # CRAFT-OK: dynamic QBO Purchase/Deposit payload boundary
    private_note = rec.get("PrivateNote")
    if private_note:
        return private_note
    lines = rec.get("Line") or []
    if lines:
        return lines[0].get("Description", "") or ""
    return ""


def _fetch_all_ordered(client: QBOClient, entity_name: str, where_str: str) -> list[dict[str, Any]]:  # CRAFT-OK: dynamic QBO record list boundary
    """``_fetch_all`` for one QBO entity, with ``ORDERBY Id`` for stable pagination.

    ``ORDERBY`` MUST precede the STARTPOSITION/MAXRESULTS ``_fetch_all`` appends,
    or QBO pagination can skip/duplicate rows across pages (AC-2 pagination
    hardening).
    """
    query = f"SELECT * FROM {entity_name}"
    if where_str:
        query += f" WHERE {where_str}"
    query += " ORDERBY Id"
    return _fetch_all(client, query, entity_name)


# ── Phase-2 bank-line extractors (BillPayment / Transfer / JournalEntry / Payment) ──
# Every transaction LINE that posts to the reconciled bank account becomes a signed
# book row (plan 20260703-recon-book-side-phase2). The polarity convention is BANK
# polarity: money-in `+`, money-out `−` (match.py:97-99). Two shapes:
#   - fixed-direction (BillPayment→out, Payment→in): store the UNSIGNED TotalAmt
#     magnitude + a transaction_type IN the matcher sets (_book_direction resolves
#     the sign from the type — LOCK-A). Mirrors Phase-1 Purchase/Deposit.
#   - per-line/leg (Transfer, JournalEntry): store a SIGNED amount in bank polarity
#     + transaction_type="transfer"/"journalentry" (deliberately NOT in the matcher
#     sets); match.py:180's signed-equal branch resolves them BEFORE _book_direction.
# These are READ-side ingest extractors — they parse QBO records into book rows;
# none POSTs/writes to QBO (no JE balance / idempotency concern applies here).


def _bank_account_id_set(client: QBOClient) -> set[str]:  # CRAFT-OK: dynamic QBO Account payload boundary
    """The Bank + Credit-Card ``Account.Id`` set for this realm (AC-7).

    ONE paginated ``SELECT Id, AccountType FROM Account`` read (minorversion-pinned
    inside ``client.query``), computed ONCE per sync run and passed to the
    Transfer/JE extractors — no per-record query (no N+1). Paginated because an
    unbounded QBO query returns only its first 100 rows, which would silently drop
    the bank accounts of a larger chart. The fixed-direction entities
    (BillPayment/Payment) do NOT consult it; they carry an explicit bank field.
    """
    accounts = _fetch_all(client, "SELECT Id, AccountType FROM Account", "Account")
    bank_types = {"Bank", "Credit Card"}
    return {
        acct.get("Id")
        for acct in accounts
        if acct.get("AccountType") in bank_types and acct.get("Id")
    }


def _billpayment_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:  # CRAFT-OK: dynamic QBO BillPayment payload boundary
    """AC-1: one row per ``PayType=="Check"`` BillPayment posting to a bank account.

    ``bank_account_ref = CheckPayment.BankAccountRef.value``; unsigned ``TotalAmt``
    magnitude + ``transaction_type="billpayment"`` (∈ ``_MONEY_OUT_TYPES``). Absent
    ``CheckPayment.BankAccountRef.value`` → skip (unmatchable without a bank key).
    The CreditCard PayType (``CreditCardPayment.CCAccountRef``) is GATED (AC-8):
    NOT built until its field path is live-confirmed.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        qbo_id = rec.get("Id")  # SA-OK: dict access on an in-memory QBO record, not an ORM query
        if not qbo_id or rec.get("PayType") != "Check":
            continue
        bank_ref = rec.get("CheckPayment", {}).get("BankAccountRef", {}).get("value")
        if not bank_ref:
            continue
        rows.append(
            {
                "qbo_id": qbo_id,
                "entity_type": "BillPayment",
                "bank_account_ref": bank_ref,
                "transaction_type": "billpayment",
                "amount": rec.get("TotalAmt"),
                "date": rec.get("TxnDate"),
                "description": rec.get("PrivateNote") or "",
            }
        )
    return rows


def _transfer_rows(  # CRAFT-OK: dynamic QBO Transfer payload boundary
    records: list[dict[str, Any]], bank_ids: set[str]
) -> list[dict[str, Any]]:
    """AC-2: one row per bank-typed Transfer leg, SIGNED in bank polarity.

    ``FromAccountRef.value`` (money out) → ``amount = −Amount``;
    ``ToAccountRef.value`` (money in) → ``amount = +Amount``. Only legs whose
    account id ∈ ``bank_ids`` become rows: a bank↔non-bank transfer yields ONE
    row, bank↔bank yields TWO (different accounts → different ``bank_account_ref``
    → the upsert identity never collides). ``qbo_id`` is the raw Transfer Id.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        qbo_id = rec.get("Id")  # SA-OK: dict access on an in-memory QBO record, not an ORM query
        if not qbo_id:
            continue
        amount = rec.get("Amount")
        if amount is None:
            continue
        txn_date = rec.get("TxnDate")
        description = rec.get("PrivateNote") or ""
        from_ref = rec.get("FromAccountRef", {}).get("value")
        to_ref = rec.get("ToAccountRef", {}).get("value")
        if from_ref in bank_ids:
            rows.append(
                {
                    "qbo_id": qbo_id,
                    "entity_type": "Transfer",
                    "bank_account_ref": from_ref,
                    "transaction_type": "transfer",
                    "amount": -amount,
                    "date": txn_date,
                    "description": description,
                }
            )
        if to_ref in bank_ids:
            rows.append(
                {
                    "qbo_id": qbo_id,
                    "entity_type": "Transfer",
                    "bank_account_ref": to_ref,
                    "transaction_type": "transfer",
                    "amount": amount,
                    "date": txn_date,
                    "description": description,
                }
            )
    return rows


# QBO-OK: read-side ingest extractor below — parses JournalEntry records into book
# rows; it does NOT POST/write a JournalEntry to QBO, so no debit==credit balance
# assertion or write-idempotency ledger applies (the AP-QBO-06 concern is write-only).
def _journalentry_rows(  # CRAFT-OK: dynamic QBO JournalEntry payload boundary
    records: list[dict[str, Any]], bank_ids: set[str]
) -> list[dict[str, Any]]:
    """AC-3: one row PER bank-typed JE line, SIGNED in bank polarity (NO summing).

    For each line whose ``JournalEntryLineDetail.AccountRef.value`` ∈ ``bank_ids``:
    ``amount = +line.Amount`` if ``PostingType=="Debit"`` else ``−line.Amount``
    (JE ``TotalAmt`` is 0 — never use it), per-line. ``qbo_id = f"{JE_Id}:{line.Id}"``
    (line-disambiguated) so two same-account bank lines in one JE clear as TWO
    independent statement lines (Rev B, refuter Finding 4) and re-runs stay
    idempotent. A JE with no bank-typed line yields zero rows.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        je_id = rec.get("Id")  # SA-OK: dict access on an in-memory QBO record, not an ORM query
        if not je_id:
            continue
        txn_date = rec.get("TxnDate")
        description = rec.get("PrivateNote") or ""
        for line in rec.get("Line") or []:
            detail = line.get("JournalEntryLineDetail")
            if detail is None:
                continue
            account_ref = detail.get("AccountRef", {}).get("value")
            if account_ref not in bank_ids:
                continue
            line_amount = line.get("Amount")
            if line_amount is None:
                continue
            posting = detail.get("PostingType")
            signed = line_amount if posting == "Debit" else -line_amount
            line_id = line.get("Id")
            rows.append(
                {
                    "qbo_id": f"{je_id}:{line_id}",
                    "entity_type": "JournalEntry",
                    "bank_account_ref": account_ref,
                    "transaction_type": "journalentry",
                    "amount": signed,
                    "date": txn_date,
                    "description": description,
                }
            )
    return rows


def _payment_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:  # CRAFT-OK: dynamic QBO Payment payload boundary
    """AC-4: one row per Payment with a present ``DepositToAccountRef``.

    ``bank_account_ref = DepositToAccountRef.value`` (read ``.get("value")`` — the
    ref may carry only ``value``, no ``name``); unsigned ``TotalAmt`` magnitude +
    ``transaction_type="payment"`` (∈ ``_MONEY_IN_TYPES``). Absent
    ``DepositToAccountRef`` → SKIP (the money went to Undeposited Funds, not a
    bank account; it lands on the bank later as a Deposit).
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        qbo_id = rec.get("Id")  # SA-OK: dict access on an in-memory QBO record, not an ORM query
        if not qbo_id:
            continue
        bank_ref = rec.get("DepositToAccountRef", {}).get("value")
        if not bank_ref:
            continue
        rows.append(
            {
                "qbo_id": qbo_id,
                "entity_type": "Payment",
                "bank_account_ref": bank_ref,
                "transaction_type": "payment",
                "amount": rec.get("TotalAmt"),
                "date": rec.get("TxnDate"),
                "description": rec.get("PrivateNote") or "",
            }
        )
    return rows


def _purchase_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One money-out book row per Purchase posted to a bank/CC account.

    A Purchase without a bank/CC posting account (``AccountRef``) cannot key the
    recon filter (``bank_account_ref``) and is skipped rather than written as an
    unmatchable row.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        qbo_id = rec.get("Id")
        bank_account_ref = _ref_value(rec, "AccountRef")
        if not qbo_id or not bank_account_ref:
            continue
        rows.append({
            "qbo_id": qbo_id,
            "entity_type": "Purchase",
            "bank_account_ref": bank_account_ref,
            "transaction_type": _PURCHASE_PAYMENT_TYPE_MAP.get(rec.get("PaymentType"), "expense"),
            "amount": rec.get("TotalAmt"),
            "date": rec.get("TxnDate"),
            "description": _purchase_description(rec),
            "account_ref": _purchase_account_ref(rec),
            "entity_ref": _ref_value(rec, "EntityRef"),
            "currency_ref": _ref_value(rec, "CurrencyRef"),
            "exchange_rate": rec.get("ExchangeRate", 1.0),
        })
    return rows


def _deposit_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One money-in book row per Deposit to a bank account.

    Stored SIGNED as returned (no abs()) — a negative-TotalAmt refund deposit then
    self-corrects via _book_direction's negative-amount branch (match.py:157),
    matching a bank withdrawal instead of never matching (crit-eval amendment 7).
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        qbo_id = rec.get("Id")
        bank_account_ref = _ref_value(rec, "DepositToAccountRef")
        if not qbo_id or not bank_account_ref:
            continue
        lines = rec.get("Line") or []
        entity_ref = None
        if lines:
            entity_ref = ((lines[0].get("DepositLineDetail") or {}).get("Entity") or {}).get("value")
        rows.append({
            "qbo_id": qbo_id,
            "entity_type": "Deposit",
            "bank_account_ref": bank_account_ref,
            "transaction_type": "deposit",
            "amount": rec.get("TotalAmt"),
            "date": rec.get("TxnDate"),
            "description": _purchase_description(rec),
            "account_ref": _deposit_account_ref(rec),
            "entity_ref": entity_ref,
            "currency_ref": _ref_value(rec, "CurrencyRef"),
            "exchange_rate": rec.get("ExchangeRate", 1.0),
        })
    return rows


# Every entity type sync_transactions writes, and the extractor for each. Transfer
# and JournalEntry also need the realm's Bank/CC account-id set.
BOOK_SIDE_ENTITY_TYPES = ("Purchase", "Deposit", "BillPayment", "Transfer", "JournalEntry", "Payment")
_BANK_SET_KEYED_TYPES = ("Transfer", "JournalEntry")


def _absence_proves_removal(row: Any, bank_ids: set[str]) -> bool:
    """A Transfer/JournalEntry row exists only for a leg or line on an account in
    the fetched Bank/CC set. A row on an account outside it (the account was made
    inactive since) could not have been produced, so its absence proves nothing
    and it must not be retired."""
    return row.entity_type not in _BANK_SET_KEYED_TYPES or row.bank_account_ref in bank_ids


def _book_rows(entity_type: str, records: list[dict[str, Any]], bank_ids: set[str]) -> list[dict[str, Any]]:
    if entity_type == "Purchase":
        return _purchase_rows(records)
    if entity_type == "Deposit":
        return _deposit_rows(records)
    if entity_type == "BillPayment":
        return _billpayment_rows(records)
    if entity_type == "Transfer":
        return _transfer_rows(records, bank_ids)
    if entity_type == "JournalEntry":
        return _journalentry_rows(records, bank_ids)
    if entity_type == "Payment":
        return _payment_rows(records)
    raise ValueError(f"not a book-side entity type: {entity_type}")


_BOOK_UPDATE_COLUMNS = [
    "transaction_type", "date", "amount", "description",
    "account_ref", "entity_ref", "currency_ref", "exchange_rate",
]


def _upsert_book_rows(db: Session, realm_id: str, rows: list[dict[str, Any]]) -> int:
    """Upsert extracted book rows on the LOCK-1 identity. Returns rows written.

    Never touches what a reconciliation or a reviewer recorded on the row
    (``cleared``, ``cleared_session_id``, ``classification_dismissed_at``,
    ``vendor_name``); a row QBO returns again is no longer removed.
    """
    params = []
    for row in rows:
        if not row.get("date"):
            # date is NOT NULL and keys the sync window; QBO always sends TxnDate.
            logger.warning("Skipping %s %s without a TxnDate", row["entity_type"], row["qbo_id"])
            continue
        params.append({
            "realm_id": realm_id,
            "qbo_id": row["qbo_id"],
            "entity_type": row["entity_type"],
            "bank_account_ref": row["bank_account_ref"],
            "transaction_type": row["transaction_type"],
            "date": date.fromisoformat(row["date"]),
            "amount": _money(row["amount"]),
            "description": row["description"],
            "account_ref": row.get("account_ref"),
            "entity_ref": row.get("entity_ref"),
            "currency_ref": row.get("currency_ref"),
            "exchange_rate": _money(row.get("exchange_rate", 1.0)),
            "source": "qbo",
        })
    return _upsert_rows(
        db, QBOTransaction, params,
        identity=["realm_id", "entity_type", "qbo_id", "bank_account_ref"],
        update_columns=_BOOK_UPDATE_COLUMNS,
        extra_set={"qbo_removed_at": None},
    )


_ID_CHUNK = 500


def _retire_book_rows(db: Session, realm_id: str, stale: list[Any]) -> dict[str, int]:
    """Retire book rows QBO no longer returns (deleted, voided out, re-pointed).

    An uncleared row is deleted, so the bank-recon candidate and outstanding
    readers never see it. A row a reconciliation already cleared (or holds an
    APPROVED match claim on) is kept for that reconciliation's history and stamped
    ``qbo_removed_at`` instead. ``stale`` rows carry ``id``, ``cleared`` and
    ``cleared_session_id``.
    """
    if not stale:
        return {"deleted": 0, "marked_removed": 0}
    ids = [row.id for row in stale]
    protected = {row.id for row in stale if row.cleared or row.cleared_session_id is not None}
    for offset in range(0, len(ids), _ID_CHUNK):
        chunk = ids[offset:offset + _ID_CHUNK]
        protected |= {
            claim_id
            for (claim_id,) in db.query(ReconciliationMatch.book_txn_local_id).filter(
                ReconciliationMatch.realm_id == realm_id,
                ReconciliationMatch.status == "APPROVED",
                ReconciliationMatch.book_txn_local_id.in_(chunk),
            )
        }
    doomed = [row_id for row_id in ids if row_id not in protected]
    kept = [row_id for row_id in ids if row_id in protected]
    for offset in range(0, len(doomed), _ID_CHUNK):
        db.query(QBOTransaction).filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.id.in_(doomed[offset:offset + _ID_CHUNK]),
        ).delete(synchronize_session=False)
    now = datetime.now(timezone.utc)
    for offset in range(0, len(kept), _ID_CHUNK):
        db.query(QBOTransaction).filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.id.in_(kept[offset:offset + _ID_CHUNK]),
            QBOTransaction.qbo_removed_at.is_(None),
        ).update({QBOTransaction.qbo_removed_at: now}, synchronize_session=False)
    return {"deleted": len(doomed), "marked_removed": len(kept)}


def _local_book_rows(db: Session, realm_id: str):
    """The realm's synced book rows: QBO-sourced and keyed by a bank account, i.e.
    exactly the rows ``sync_transactions`` writes under the LOCK-1 identity."""
    return db.query(
        QBOTransaction.id,
        QBOTransaction.entity_type,
        QBOTransaction.qbo_id,
        QBOTransaction.bank_account_ref,
        QBOTransaction.cleared,
        QBOTransaction.cleared_session_id,
    ).filter(
        QBOTransaction.realm_id == realm_id,
        QBOTransaction.source == "qbo",
        QBOTransaction.bank_account_ref.isnot(None),
    )


def sync_transactions(
    db: Session,
    client: QBOClient,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, int]:
    """
    Syncs bank-reconciliation book-side transactions: every QBO transaction LINE
    that posts to the reconciled bank account becomes a signed book row in
    ``qbo_transactions``, regardless of entity type.

    Phase 1 covers Purchase (money-out) + Deposit (money-in). Phase 2
    (plan 20260703-recon-book-side-phase2) extends the SAME writer with the four
    confirmed bank-posting entities via the ``_*_rows`` extractors above (see each
    extractor's docstring), closing the "0 candidates" gap for customers whose
    bank activity is booked as those types. The three gated entities (AC-8) are
    NOT built until their field paths are live-confirmed.

    ``SELECT * FROM Transaction`` is not a real QBO entity — the previous body
    queried it and silently synced zero rows DB-wide (root cause of the empty
    book side in bank reconciliation). Defaults to the last 90 days when no dates
    are given (matches ``sync_gl_lines``'s convention).

    Upsert identity is ``(realm_id, entity_type, qbo_id, bank_account_ref)``
    (LOCK-1), enforced by ``uq_qbo_transaction_identity`` and resolved by the
    database (``ON CONFLICT DO UPDATE``), so overlapping syncs cannot duplicate a
    row. QBO ids are unique per ENTITY TYPE only, and ``entity_type`` (not
    ``transaction_type``, which drifts with ``PaymentType``) carries identity.

    The window is then reconciled against QBO: a book row dated inside
    ``[start_date, end_date]`` whose key QBO did not return (deleted, voided out of
    the account, or moved to another bank account) is retired by
    ``_retire_book_rows``. Every fetch completes before anything is written, so a
    failed fetch retires nothing.
    """
    logger.info("Syncing bank-recon transactions (all bank-posting entity types)...")

    if not start_date:
        start_date = date.today() - timedelta(days=90)
    if not end_date:
        end_date = date.today()

    where_str = f"TxnDate >= '{start_date.isoformat()}' AND TxnDate <= '{end_date.isoformat()}'"

    # The Bank/CC account-id set is fetched ONCE (AC-7, no N+1) and threaded into
    # the Transfer/JE extractors; the fixed-direction entities carry a bank field.
    records = {entity_type: _fetch_all_ordered(client, entity_type, where_str) for entity_type in ("Purchase", "Deposit")}
    bank_ids = _bank_account_id_set(client)
    for entity_type in ("BillPayment", "Transfer", "JournalEntry", "Payment"):
        records[entity_type] = _fetch_all_ordered(client, entity_type, where_str)

    rows: list[dict[str, Any]] = []
    for entity_type in BOOK_SIDE_ENTITY_TYPES:
        rows += _book_rows(entity_type, records[entity_type], bank_ids)

    count = _upsert_book_rows(db, client.realm_id, rows)

    returned = {(row["entity_type"], row["qbo_id"], row["bank_account_ref"]) for row in rows}
    stale = [
        row
        for row in _local_book_rows(db, client.realm_id).filter(
            QBOTransaction.entity_type.in_(BOOK_SIDE_ENTITY_TYPES),
            QBOTransaction.date >= start_date,
            QBOTransaction.date <= end_date,
        )
        if (row.entity_type, row.qbo_id, row.bank_account_ref) not in returned
        and _absence_proves_removal(row, bank_ids)
    ]
    retired = _retire_book_rows(db, client.realm_id, stale)
    db.commit()
    logger.info(
        "Synced %d bank-recon transactions; retired %d deleted / %d marked removed.",
        count, retired["deleted"], retired["marked_removed"],
    )
    return {"upserted": count, **retired}


def _book_rows_of_entity(db: Session, realm_id: str, entity_type: str, qbo_id: str):
    """Local book rows that belong to one QBO transaction (JE rows are per line)."""
    query = _local_book_rows(db, realm_id).filter(QBOTransaction.entity_type == entity_type)
    if entity_type == "JournalEntry":
        return query.filter(QBOTransaction.qbo_id.startswith(f"{qbo_id}:", autoescape=True))
    return query.filter(QBOTransaction.qbo_id == str(qbo_id))


def book_transaction_is_mirrored(db: Session, realm_id: str, entity_type: str, qbo_id: str) -> bool:
    """True when the windowed sync mirrored this QBO transaction as book rows."""
    if entity_type not in BOOK_SIDE_ENTITY_TYPES or not qbo_id:
        return False
    return _book_rows_of_entity(db, realm_id, entity_type, qbo_id).first() is not None


def retire_book_transaction(db: Session, realm_id: str, entity_type: str, qbo_id: str) -> dict[str, int]:
    """QBO deleted this transaction: retire every book row it produced."""
    if entity_type not in BOOK_SIDE_ENTITY_TYPES or not qbo_id:
        return {"deleted": 0, "marked_removed": 0}
    retired = _retire_book_rows(db, realm_id, _book_rows_of_entity(db, realm_id, entity_type, qbo_id).all())
    db.commit()
    return retired


def refresh_book_transaction(db: Session, client: QBOClient, entity_type: str, qbo_id: str) -> dict[str, int]:
    """Re-read one mirrored transaction after QBO changed it (Update / Void).

    Only a transaction already in the local mirror is refreshed: the windowed sync
    owns which transactions are mirrored. Its rows are rewritten from the current
    record; keys it no longer produces (re-pointed to another bank account, voided
    out) are retired, and a transaction QBO no longer returns is retired whole.
    """
    if entity_type not in BOOK_SIDE_ENTITY_TYPES or not qbo_id:
        return {"upserted": 0, "deleted": 0, "marked_removed": 0}
    local = _book_rows_of_entity(db, client.realm_id, entity_type, qbo_id).all()
    if not local:
        return {"upserted": 0, "deleted": 0, "marked_removed": 0}
    record = fetch_entity_by_id(client, entity_type, qbo_id)
    rows: list[dict[str, Any]] = []
    stale = local
    if record is not None:
        bank_ids = _bank_account_id_set(client) if entity_type in _BANK_SET_KEYED_TYPES else set()
        rows = _book_rows(entity_type, [record], bank_ids)
        keys = {(row["entity_type"], row["qbo_id"], row["bank_account_ref"]) for row in rows}
        stale = [
            row for row in local
            if (row.entity_type, row.qbo_id, row.bank_account_ref) not in keys
            and _absence_proves_removal(row, bank_ids)
        ]
    count = _upsert_book_rows(db, client.realm_id, rows)
    retired = _retire_book_rows(db, client.realm_id, stale)
    db.commit()
    return {"upserted": count, **retired}


def fetch_entity_by_id(client: QBOClient, entity_name: str, entity_id: str) -> dict[str, Any] | None:
    """One QBO entity by Id through the query endpoint, or None when QBO no longer
    returns it (deleted). Transport and auth failures raise."""
    safe_id = str(entity_id).replace("'", "")
    response = client.query(f"SELECT * FROM {entity_name} WHERE Id = '{safe_id}'")
    records = (response.get("QueryResponse") or {}).get(entity_name) or []
    return records[0] if records else None


def sync_journal_entries(
    db: Session,
    client: QBOClient,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None
):
    """
    Syncs Journal Entries (QBO "JournalEntry" entity) into qbo_journal_entries.
    Identity ``(realm_id, qbo_id)`` is resolved by the database (ON CONFLICT).
    """
    logger.info("Syncing Journal Entries...")

    where_clauses = []
    if start_date:
        where_clauses.append(f"TxnDate >= '{start_date.isoformat()}'")
    if end_date:
        where_clauses.append(f"TxnDate <= '{end_date.isoformat()}'")

    where_str = " AND ".join(where_clauses)
    query = "SELECT * FROM JournalEntry"
    if where_str:
        query += f" WHERE {where_str}"

    records = _fetch_all(client, query, "JournalEntry")
    rows = [
        {
            "realm_id": client.realm_id,
            "qbo_id": rec["Id"],
            "doc_number": rec.get("DocNumber"),
            "txn_date": date.fromisoformat(rec["TxnDate"]),
            "total_amt": _money(rec.get("TotalAmt")),
            "private_note": rec.get("PrivateNote"),
            "adjustment": rec.get("Adjustment", False),
            # Store full lines as JSON
            "lines": rec.get("Line", []),
        }
        for rec in records
        if rec.get("Id") and rec.get("TxnDate")
    ]
    _upsert_rows(
        db, QBOJournalEntry, rows, identity=["realm_id", "qbo_id"],
        update_columns=["doc_number", "txn_date", "total_amt", "private_note", "adjustment", "lines"],
    )
    db.commit()
    logger.info(f"Synced {len(rows)} Journal Entries.")


def sync_search_index(
    db: Session,
    client: QBOClient,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> int:
    """
    Populate the normalized QBO search index used by the assistant.

    This keeps chat-time lookups on deterministic database rows instead of
    repeated ad hoc scans over cached QBO JSON or live report payloads.
    """
    logger.info("Syncing normalized QBO search index...")
    entities = [
        ("Customer", "Customer", False),
        ("Vendor", "Vendor", False),
        ("Account", "Account", False),
        ("Class", "Class", False),
        ("Department", "Department", False),  # QBO's location dimension
        ("Invoice", "Invoice", True),
        ("Bill", "Bill", True),
        ("Payment", "Payment", True),
        ("Deposit", "Deposit", True),
        ("JournalEntry", "JournalEntry", True),
    ]

    total = 0
    errors = []
    for qbo_entity, response_key, uses_txn_date in entities:
        query = f"SELECT * FROM {qbo_entity}"
        where_clauses = []
        if uses_txn_date and start_date:
            where_clauses.append(f"TxnDate >= '{start_date.isoformat()}'")
        if uses_txn_date and end_date:
            where_clauses.append(f"TxnDate <= '{end_date.isoformat()}'")
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        try:
            records = _fetch_all(client, query, response_key)
        except Exception as exc:
            logger.warning(
                "Search index sync skipped %s: %s",
                qbo_entity,
                redact_to_string(str(exc), max_length=500),
            )
            errors.append(_sync_error_message(qbo_entity, exc, log_context="sync_search_index_entity_failed"))
            continue

        for rec in records:
            if index_qbo_entity(db, client.realm_id, qbo_entity, rec):
                total += 1
            if total and total % 100 == 0:
                db.commit()
        db.commit()

    if errors:
        logger.warning("Normalized QBO search index completed with skips: %s", "; ".join(errors))
    logger.info("Synced %s normalized QBO search records.", total)
    return total


def index_single_entity(
    db: Session,
    client: QBOClient,
    qbo_entity: str,
    entity_id: str,
) -> bool:
    """Fetch ONE QBO entity by Id and route it through the canonical search indexer.

    Used by the webhook handler so a freshly created/updated Invoice/Bill becomes
    findable via ``search_finance_records`` without waiting for the next full
    ``sync_search_index`` pass. The fetch uses the SAME ``client.query`` SQL path
    ``sync_search_index`` / ``_fetch_all`` already use (minorversion pinned in the
    shared QBO client), and the row is upserted through the ONE canonical indexer
    ``index_qbo_entity`` — no forked indexer. Idempotent: ``index_qbo_entity`` ->
    ``upsert_search_record`` upserts by (realm_id, entity_type, qbo_id), so a
    redelivered webhook updates the same row rather than duplicating it.

    Returns True when a record was indexed, False when the entity was not found
    (already-deleted, or a query that returned no rows).
    """
    if not entity_id:
        return False
    # Single-row fetch via the canonical QBO SQL query path (client.query pins
    # minorversion in the shared client, mirroring _fetch_all in this module).
    safe_id = str(entity_id).replace("'", "")
    response = client.query(f"SELECT * FROM {qbo_entity} WHERE Id = '{safe_id}'")
    records = response.get("QueryResponse", {}).get(qbo_entity, [])
    if not records:
        logger.info(
            "Search index single-fetch found no %s with Id=%s for realm=%s",
            qbo_entity,
            entity_id,
            client.realm_id,
        )
        return False
    indexed = index_qbo_entity(db, client.realm_id, qbo_entity, records[0])
    if indexed:
        db.commit()
        return True
    return False


def sync_report(
    db: Session,
    client: QBOClient,
    report_name: str,
    params: Optional[Dict[str, Any]] = None
):
    """
    Fetches a report from QBO and stores it as a JSON snapshot.
    """
    logger.info(f"Syncing Report: {report_name}...")
    
    # Fetch data
    report_data = client.get_report(report_name, params=params)
    
    # Create snapshot
    # We don't overwrite history to allow trending analysis over time.
    # We just add a new snapshot.
    # OR we can update the "latest" for a given period.
    
    # Logic: If params contain date range, we might want to deduplicate based on range.
    # But QBO reports are complex. Simplest MVP: Store every fetch as a snapshot.
    
    # Extract date info if available in params
    start_date = None
    end_date = None
    as_of_date = None
    
    if params:
        if "start_date" in params:
            try:
                start_date = date.fromisoformat(params["start_date"])
            except ValueError:
                pass
        if "end_date" in params:
            try:
                end_date = date.fromisoformat(params["end_date"])
            except ValueError:
                pass
        if "date_macro" in params:
            # We don't easily know the exact dates from macro without parsing the result
            # But the result usually has "Header" -> "StartPeriod", "EndPeriod"
            pass
            
    # Try to refine dates from Response Header
    header = report_data.get("Header", {})
    if not start_date and "StartPeriod" in header:
        try:
            start_date = date.fromisoformat(header["StartPeriod"])
        except ValueError:
            pass
            
    if not end_date and "EndPeriod" in header:
        try:
            end_date = date.fromisoformat(header["EndPeriod"])
        except ValueError:
            pass
            
    if not as_of_date and "Time" in header:
        # Balance Sheet often has "Time" or we just use today
        pass
        
    # Serialize to JSON string
    import json
    data_json = json.dumps(report_data)
    
    snapshot = QBOReport(
        realm_id=client.realm_id,
        organization_id=_org_id_for_realm(db, client.realm_id),
        report_name=report_name,
        start_date=start_date,
        end_date=end_date,
        as_of_date=as_of_date,
        data=data_json
    )
    
    db.add(snapshot)
    db.commit()
    return snapshot


def sync_gl_lines(
    db: Session,
    client: QBOClient,
    start_date: date,
    end_date: date,
) -> int:
    """Sync normalized GeneralLedger lines for fast assistant/drilldown reads."""
    logger.info("Syncing normalized GeneralLedger lines...")
    report = client.get_report("GeneralLedger", params={
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "accounting_method": "Accrual",
    })
    count = upsert_gl_report_lines(
        db,
        realm_id=client.realm_id,
        report=report,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    logger.info("Synced %s normalized GeneralLedger lines.", count)
    return count


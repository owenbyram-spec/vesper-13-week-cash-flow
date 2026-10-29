from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    Organization,
    OrganizationCompany,
    QBOAccount,
    QBOCustomer,
    QBOJournalEntry,
    QBOSearchRecord,
    QBOTransaction,
    QBOVendor,
)
from ..services.tenant_companies import organization_company_for_realm, organization_for_company

logger = logging.getLogger(__name__)


SUPPORTED_ENTITY_TYPES = {
    "customer",
    "vendor",
    "invoice",
    "bill",
    "payment",
    "deposit",
    "journal_entry",
    "account",
    "class",
    "location",
}


def _finance_search_scope(
    db: Session,
    realm_id: str,
) -> Tuple[Optional[Organization], Optional[OrganizationCompany]]:
    organization = organization_for_company(db, realm_id)
    if not organization:
        return None, None
    organization_company = organization_company_for_realm(
        db,
        organization_id=organization.id,
        realm_id=realm_id,
    )
    return organization, organization_company


def _apply_search_record_scope(query_obj: Any, organization: Optional[Organization]) -> Any:
    if organization:
        return query_obj.filter(
            or_(QBOSearchRecord.organization_id == organization.id, QBOSearchRecord.organization_id.is_(None))
        )
    return query_obj.filter(QBOSearchRecord.organization_id.is_(None))

_ENTITY_SYNONYMS = {
    "customer": {"customer", "customers", "client", "clients"},
    "vendor": {"vendor", "vendors", "supplier", "suppliers", "payee", "payees"},
    "invoice": {"invoice", "invoices", "ar", "receivable", "receivables"},
    "bill": {"bill", "bills", "ap", "payable", "payables"},
    "payment": {"payment", "payments", "receipt", "receipts"},
    "deposit": {"deposit", "deposits", "cash receipt", "cash receipts"},
    "journal_entry": {"journal entry", "journal entries", "journal", "journals", "je", "jes"},
    "account": {"account", "accounts", "coa", "chart of accounts"},
    "class": {"class", "classes"},
    "location": {"location", "locations", "department", "departments"},
}

_QBO_ENTITY_TO_CANONICAL = {
    "Customer": "customer",
    "Vendor": "vendor",
    "Invoice": "invoice",
    "Bill": "bill",
    "Payment": "payment",
    "Deposit": "deposit",
    "JournalEntry": "journal_entry",
    "Account": "account",
    "Class": "class",
    "Department": "location",
    "Location": "location",
}

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "last",
    "me",
    "month",
    "of",
    "on",
    "open",
    "over",
    "paid",
    "past",
    "show",
    "the",
    "this",
    "to",
    "under",
    "unpaid",
    "with",
}

_MONTHS = {name.lower(): idx for idx, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): idx for idx, name in enumerate(calendar.month_abbr) if name})


@dataclass
class ParsedFinanceQuery:
    original_query: str
    entity_types: List[str] = field(default_factory=list)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    min_amount: Optional[float] = None
    max_amount: Optional[float] = None
    status: Optional[str] = None
    customer_name: Optional[str] = None
    vendor_name: Optional[str] = None
    customer_vendor_name: Optional[str] = None
    account: Optional[str] = None
    class_name: Optional[str] = None
    location: Optional[str] = None
    overdue_as_of: Optional[date] = None
    terms: List[str] = field(default_factory=list)

    def to_filters(self) -> Dict[str, Any]:
        return {
            "entity_types": self.entity_types,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "min_amount": self.min_amount,
            "max_amount": self.max_amount,
            "status": self.status,
            "customer_name": self.customer_name,
            "vendor_name": self.vendor_name,
            "customer_vendor_name": self.customer_vendor_name,
            "account": self.account,
            "class_name": self.class_name,
            "location": self.location,
            "overdue_as_of": self.overdue_as_of.isoformat() if self.overdue_as_of else None,
            "terms": self.terms,
        }


def _canonical_entity_type(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).strip()
    if raw in _QBO_ENTITY_TO_CANONICAL:
        return _QBO_ENTITY_TO_CANONICAL[raw]
    normalized = raw.lower().replace("-", "_").replace(" ", "_")
    if normalized in SUPPORTED_ENTITY_TYPES:
        return normalized
    for entity_type, words in _ENTITY_SYNONYMS.items():
        if raw.lower() in words:
            return entity_type
    return normalized


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _compact_text(parts: Iterable[Any]) -> str:
    seen = set()
    output = []
    for part in parts:
        if part in (None, "", [], {}):
            continue
        text = str(part).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return " ".join(output)


def _walk_strings(value: Any, *, limit: int = 250) -> Iterable[str]:
    count = 0

    def visit(node: Any) -> Iterable[str]:
        nonlocal count
        if count >= limit:
            return
        if isinstance(node, dict):
            for child in node.values():
                yield from visit(child)
        elif isinstance(node, list):
            for child in node:
                yield from visit(child)
        elif isinstance(node, str):
            text = node.strip()
            if text:
                count += 1
                yield text

    yield from visit(value)


def _add_ref(refs: Dict[str, List[Tuple[Optional[str], str]]], kind: str, ref: Any) -> None:
    if not isinstance(ref, dict):
        return
    name = ref.get("name") or ref.get("Name")
    value = ref.get("value") or ref.get("Value")
    if not name and not value:
        return
    refs.setdefault(kind, [])
    item = (str(value) if value is not None else None, str(name or value))
    if item not in refs[kind]:
        refs[kind].append(item)


def _collect_refs(raw: Dict[str, Any]) -> Dict[str, List[Tuple[Optional[str], str]]]:
    refs: Dict[str, List[Tuple[Optional[str], str]]] = {}

    def visit(node: Any, parent_key: Optional[str] = None) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "CustomerRef":
                    _add_ref(refs, "customer", value)
                elif key == "VendorRef":
                    _add_ref(refs, "vendor", value)
                elif key in {"AccountRef", "DepositToAccountRef", "APAccountRef", "ARAccountRef", "ItemAccountRef", "ExpenseAccountRef", "IncomeAccountRef"}:
                    _add_ref(refs, "account", value)
                elif key == "ClassRef":
                    _add_ref(refs, "class", value)
                elif key == "DepartmentRef":
                    _add_ref(refs, "location", value)
                elif key == "Entity" and isinstance(value, dict):
                    entity_type = str(value.get("Type") or "").lower()
                    entity_ref = value.get("EntityRef") or {}
                    if entity_type == "customer":
                        _add_ref(refs, "customer", entity_ref)
                    elif entity_type == "vendor":
                        _add_ref(refs, "vendor", entity_ref)
                    else:
                        _add_ref(refs, "entity", entity_ref)
                visit(value, key)
        elif isinstance(node, list):
            for item in node:
                visit(item, parent_key)

    visit(raw)
    return refs


def _first_ref(refs: Dict[str, List[Tuple[Optional[str], str]]], kind: str) -> Tuple[Optional[str], Optional[str]]:
    values = refs.get(kind) or []
    if not values:
        return None, None
    ref_id, name = values[0]
    return ref_id, name


def _ref_names(refs: Dict[str, List[Tuple[Optional[str], str]]], kind: str) -> List[str]:
    return [name for _, name in refs.get(kind, []) if name]


def _memo_from_raw(raw: Dict[str, Any]) -> Optional[str]:
    customer_memo = raw.get("CustomerMemo")
    if isinstance(customer_memo, dict):
        customer_memo = customer_memo.get("value")
    parts = [
        raw.get("PrivateNote"),
        raw.get("Memo"),
        customer_memo,
    ]
    for line in raw.get("Line") or []:
        if isinstance(line, dict):
            parts.append(line.get("Description"))
    return _compact_text(parts) or None


def _status_for_entity(entity_type: str, raw: Dict[str, Any]) -> Optional[str]:
    active = raw.get("Active")
    if active is not None and entity_type in {"customer", "vendor", "account", "class", "location"}:
        return "active" if bool(active) else "inactive"

    if entity_type in {"invoice", "bill"}:
        balance = _as_float(raw.get("Balance"))
        if balance is not None:
            return "open" if balance > 0 else "paid"
        linked = raw.get("LinkedTxn")
        if isinstance(linked, list) and linked:
            return "paid"
        return "open"

    if entity_type == "journal_entry":
        return "adjustment" if raw.get("Adjustment") else "posted"

    if entity_type in {"payment", "deposit"}:
        return "posted"

    return None


def normalize_qbo_entity(realm_id: str, entity_type: str, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    canonical_type = _canonical_entity_type(entity_type)
    if not canonical_type:
        return None
    if canonical_type == "transaction":
        canonical_type = _canonical_entity_type(raw.get("TxnType")) or "transaction"

    qbo_id = raw.get("Id") or raw.get("id")
    if not qbo_id:
        return None
    qbo_id = str(qbo_id)

    refs = _collect_refs(raw)
    customer_id, customer_name = _first_ref(refs, "customer")
    vendor_id, vendor_name = _first_ref(refs, "vendor")
    account_id, account_name = _first_ref(refs, "account")
    class_id, class_name = _first_ref(refs, "class")
    location_id, location_name = _first_ref(refs, "location")

    doc_number = raw.get("DocNumber") or raw.get("AcctNum")
    txn_date = _parse_date(raw.get("TxnDate") or raw.get("MetaData", {}).get("CreateTime"))
    due_date = _parse_date(raw.get("DueDate"))
    amount = _as_float(
        raw.get("TotalAmt")
        if raw.get("TotalAmt") is not None
        else raw.get("Amount")
        if raw.get("Amount") is not None
        else raw.get("CurrentBalance")
        if raw.get("CurrentBalance") is not None
        else raw.get("Balance")
    )
    open_balance = _as_float(raw.get("Balance")) if canonical_type in {"invoice", "bill", "customer", "vendor"} else None
    memo = _memo_from_raw(raw)
    status = _status_for_entity(canonical_type, raw)

    if canonical_type == "customer":
        display_name = raw.get("DisplayName") or raw.get("FullyQualifiedName") or raw.get("CompanyName") or qbo_id
        customer_id = qbo_id
        customer_name = display_name
        amount = _as_float(raw.get("Balance"))
        open_balance = amount
    elif canonical_type == "vendor":
        display_name = raw.get("DisplayName") or raw.get("CompanyName") or qbo_id
        vendor_id = qbo_id
        vendor_name = display_name
        amount = _as_float(raw.get("Balance"))
        open_balance = amount
    elif canonical_type == "account":
        display_name = raw.get("FullyQualifiedName") or raw.get("Name") or qbo_id
        account_id = qbo_id
        account_name = display_name
        amount = _as_float(raw.get("CurrentBalance"))
    elif canonical_type == "class":
        display_name = raw.get("FullyQualifiedName") or raw.get("Name") or qbo_id
        class_id = qbo_id
        class_name = display_name
    elif canonical_type == "location":
        display_name = raw.get("FullyQualifiedName") or raw.get("Name") or qbo_id
        location_id = qbo_id
        location_name = display_name
    elif canonical_type == "invoice":
        display_name = f"Invoice {doc_number or qbo_id}"
        if customer_name:
            display_name += f" - {customer_name}"
    elif canonical_type == "bill":
        display_name = f"Bill {doc_number or qbo_id}"
        if vendor_name:
            display_name += f" - {vendor_name}"
    elif canonical_type == "payment":
        display_name = f"Payment {doc_number or qbo_id}"
        if customer_name:
            display_name += f" - {customer_name}"
    elif canonical_type == "deposit":
        display_name = f"Deposit {doc_number or qbo_id}"
        entity_name = customer_name or vendor_name
        if entity_name:
            display_name += f" - {entity_name}"
    elif canonical_type == "journal_entry":
        display_name = f"Journal Entry {doc_number or qbo_id}"
    else:
        display_name = raw.get("DisplayName") or raw.get("Name") or doc_number or qbo_id

    source_updated_at = None
    metadata = raw.get("MetaData") if isinstance(raw.get("MetaData"), dict) else {}
    if metadata:
        source_updated_at = _parse_datetime(metadata.get("LastUpdatedTime") or metadata.get("CreateTime"))

    searchable_parts = [
        canonical_type,
        qbo_id,
        doc_number,
        display_name,
        status,
        txn_date.isoformat() if txn_date else None,
        due_date.isoformat() if due_date else None,
        amount,
        open_balance,
        customer_name,
        vendor_name,
        account_name,
        class_name,
        location_name,
        memo,
        raw.get("Name"),
        raw.get("FullyQualifiedName"),
        raw.get("CompanyName"),
        raw.get("PrintOnCheckName"),
        raw.get("PrimaryEmailAddr", {}).get("Address") if isinstance(raw.get("PrimaryEmailAddr"), dict) else None,
        *_ref_names(refs, "customer"),
        *_ref_names(refs, "vendor"),
        *_ref_names(refs, "account"),
        *_ref_names(refs, "class"),
        *_ref_names(refs, "location"),
        *_walk_strings(raw),
    ]

    return {
        "realm_id": realm_id,
        "entity_type": canonical_type,
        "qbo_id": qbo_id,
        "doc_number": str(doc_number) if doc_number not in (None, "") else None,
        "display_name": display_name,
        "txn_date": txn_date,
        "due_date": due_date,
        "amount": amount,
        "open_balance": open_balance,
        "status": status,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "account_id": account_id,
        "account_name": account_name,
        "class_id": class_id,
        "class_name": class_name,
        "location_id": location_id,
        "location_name": location_name,
        "memo": memo,
        "search_text": _compact_text(searchable_parts),
        "raw_data": raw,
        "source_updated_at": source_updated_at,
    }


def upsert_search_record(db: Session, data: Dict[str, Any]) -> Optional[QBOSearchRecord]:  # CRAFT-OK: `data` is the dynamic normalized-QBO-entity payload (search-record column values from normalize_qbo_entity)
    organization, organization_company = _finance_search_scope(db, data["realm_id"])

    def _identity_query():
        # QBO-OK: exact unique-key (realm_id, entity_type, qbo_id) identity lookup for unique-constraint conflict resolution, NOT an FTS/user-facing match; _assert_owned applies the cross-tenant guard before any write
        return db.query(QBOSearchRecord).filter(
            QBOSearchRecord.realm_id == data["realm_id"],
            QBOSearchRecord.entity_type == data["entity_type"],
            QBOSearchRecord.qbo_id == data["qbo_id"],
        )

    def _assert_owned(existing: QBOSearchRecord) -> None:
        # Cross-tenant guard: never write over a record owned by another organization/entity.
        if existing.organization_id not in (None, organization.id if organization else None):
            raise PermissionError("QBO search record belongs to another organization")
        if organization_company and existing.organization_company_id not in (None, organization_company.id):
            raise PermissionError("QBO search record belongs to another entity")

    record = _apply_search_record_scope(_identity_query(), organization).first()
    if record is None:
        # The scoped read missed it — but the row may still exist (a concurrent insert, an
        # RLS-scoped read, or a row left under a different org by a prior tenant move).
        record = _identity_query().first()

    if record is None:
        # No visible existing row → INSERT under a SAVEPOINT. The realm-level unique
        # constraint (realm_id, entity_type, qbo_id) still fires for a row that exists but
        # was invisible to the reads above; catching it here lets the conflict resolve to
        # the existing row instead of aborting the whole transaction — which is what turned
        # one duplicate into a permanent UniqueViolation poison-loop in the rebuild.
        new_record = QBOSearchRecord(
            organization_id=organization.id if organization else None,
            organization_company_id=organization_company.id if organization_company else None,
            realm_id=data["realm_id"],
            entity_type=data["entity_type"],
            qbo_id=data["qbo_id"],
        )
        db.add(new_record)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            db.expunge(new_record)
            record = _apply_search_record_scope(_identity_query(), organization).first() or _identity_query().first()
            if record is None:
                # The row exists at the DB level but is not visible/updatable under the
                # current scope (e.g. left under another org by a prior move). Skip it
                # rather than crash; a re-index under the correct scope heals it.
                logger.warning(
                    "QBO search record conflict unresolved under current scope; skipping: "
                    "realm_id=%s entity_type=%s qbo_id=%s",
                    data["realm_id"],
                    data["entity_type"],
                    data["qbo_id"],
                )
                return None
        else:
            record = new_record

    _assert_owned(record)
    if organization is not None:
        record.organization_id = organization.id
        if organization_company:
            record.organization_company_id = organization_company.id

    for key, value in data.items():
        setattr(record, key, value)
    return record


def index_qbo_entity(db: Session, realm_id: str, entity_type: str, raw: Dict[str, Any]) -> Optional[QBOSearchRecord]:
    data = normalize_qbo_entity(realm_id, entity_type, raw)
    if not data:
        return None
    return upsert_search_record(db, data)


def rebuild_search_index_from_mirror(db: Session, realm_id: str) -> int:
    """Backfill the normalized search index from already-mirrored QBO tables."""
    count = 0
    customers = {c.qbo_id: c.display_name for c in db.query(QBOCustomer).filter(QBOCustomer.realm_id == realm_id).all()}
    vendors = {v.qbo_id: v.display_name for v in db.query(QBOVendor).filter(QBOVendor.realm_id == realm_id).all()}
    accounts = {a.qbo_id: a.fully_qualified_name or a.name for a in db.query(QBOAccount).filter(QBOAccount.realm_id == realm_id).all()}

    for customer in db.query(QBOCustomer).filter(QBOCustomer.realm_id == realm_id).all():
        raw = {
            "Id": customer.qbo_id,
            "DisplayName": customer.display_name,
            "CompanyName": customer.company_name,
            "PrimaryEmailAddr": {"Address": customer.email} if customer.email else None,
            "Balance": _as_float(customer.balance),
            "Active": customer.active,
        }
        if index_qbo_entity(db, realm_id, "Customer", raw):
            count += 1

    for vendor in db.query(QBOVendor).filter(QBOVendor.realm_id == realm_id).all():
        raw = {
            "Id": vendor.qbo_id,
            "DisplayName": vendor.display_name,
            "CompanyName": vendor.company_name,
            "PrimaryEmailAddr": {"Address": vendor.email} if vendor.email else None,
            "Balance": _as_float(vendor.balance),
            "Active": vendor.active,
        }
        if index_qbo_entity(db, realm_id, "Vendor", raw):
            count += 1

    for account in db.query(QBOAccount).filter(QBOAccount.realm_id == realm_id).all():
        raw = {
            "Id": account.qbo_id,
            "Name": account.name,
            "FullyQualifiedName": account.fully_qualified_name,
            "AcctNum": account.acct_num,
            "Active": account.active,
            "Classification": account.classification,
            "AccountType": account.account_type,
            "AccountSubType": account.account_sub_type,
            "CurrentBalance": _as_float(account.current_balance),
        }
        if index_qbo_entity(db, realm_id, "Account", raw):
            count += 1

    for txn in db.query(QBOTransaction).filter(QBOTransaction.realm_id == realm_id).all():
        entity_type = _canonical_entity_type(txn.transaction_type) or "transaction"
        entity_name = customers.get(txn.entity_ref) or vendors.get(txn.entity_ref)
        raw = {
            "Id": txn.qbo_id,
            "TxnType": txn.transaction_type,
            "TxnDate": txn.date.isoformat() if txn.date else None,
            "TotalAmt": _as_float(txn.amount),
            "PrivateNote": txn.description,
            "AccountRef": {"value": txn.account_ref, "name": accounts.get(txn.account_ref)} if txn.account_ref else None,
            "EntityRef": {"value": txn.entity_ref, "name": entity_name} if txn.entity_ref else None,
            "CustomerRef": {"value": txn.entity_ref, "name": customers.get(txn.entity_ref)} if txn.entity_ref in customers else None,
            "VendorRef": {"value": txn.entity_ref, "name": vendors.get(txn.entity_ref) or txn.vendor_name} if txn.entity_ref in vendors or txn.vendor_name else None,
        }
        if index_qbo_entity(db, realm_id, entity_type, raw):
            count += 1

    for journal_entry in db.query(QBOJournalEntry).filter(QBOJournalEntry.realm_id == realm_id).all():
        raw = {
            "Id": journal_entry.qbo_id,
            "DocNumber": journal_entry.doc_number,
            "TxnDate": journal_entry.txn_date.isoformat() if journal_entry.txn_date else None,
            "TotalAmt": _as_float(journal_entry.total_amt),
            "PrivateNote": journal_entry.private_note,
            "Adjustment": journal_entry.adjustment,
            "Line": journal_entry.lines or [],
        }
        if index_qbo_entity(db, realm_id, "JournalEntry", raw):
            count += 1

    db.commit()
    return count


def ensure_search_index(db: Session, realm_id: str) -> int:
    organization, _organization_company = _finance_search_scope(db, realm_id)
    count_query = db.query(func.count(QBOSearchRecord.id)).filter(QBOSearchRecord.realm_id == realm_id)
    existing = _apply_search_record_scope(count_query, organization).scalar() or 0
    if existing:
        return int(existing)
    rebuild_search_index_from_mirror(db, realm_id)
    count_query = db.query(func.count(QBOSearchRecord.id)).filter(QBOSearchRecord.realm_id == realm_id)
    return int(_apply_search_record_scope(count_query, organization).scalar() or 0)


def _month_bounds(year: int, month: int) -> Tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _quarter_bounds(today: date, offset: int = 0) -> Tuple[date, date]:
    quarter = (today.month - 1) // 3 + 1 + offset
    year = today.year
    while quarter < 1:
        quarter += 4
        year -= 1
    while quarter > 4:
        quarter -= 4
        year += 1
    start_month = (quarter - 1) * 3 + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    _, end_day = calendar.monthrange(year, end_month)
    return start, date(year, end_month, end_day)


def _parse_amount(text: str) -> float:
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?", text)
    if not match:
        return 0.0
    value = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        value *= 1000
    elif suffix == "m":
        value *= 1_000_000
    return value


def _strip_known_phrases(value: str) -> str:
    text = value
    phrase_patterns = [
        r"\blast month\b",
        r"\bthis month\b",
        r"\blast year\b",
        r"\bthis year\b",
        r"\bytd\b",
        r"\blast quarter\b",
        r"\bthis quarter\b",
        r"\bover\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?\b",
        r"\babove\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?\b",
        r"\bunder\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?\b",
        r"\bbelow\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?\b",
        r"\bbetween\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?\s+and\s+\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?\b",
    ]
    for pattern in phrase_patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    for words in _ENTITY_SYNONYMS.values():
        for word in sorted(words, key=len, reverse=True):
            text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.IGNORECASE)
    for word in ["open", "unpaid", "outstanding", "paid", "closed", "overdue", "active", "inactive"]:
        text = re.sub(rf"\b{word}\b", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,.;:")


def _extract_named_filter(lower_query: str, keywords: Sequence[str]) -> Optional[str]:
    keyword_pattern = "|".join(re.escape(keyword) for keyword in keywords)
    match = re.search(rf"\b(?:{keyword_pattern})\s+(.+)$", lower_query, flags=re.IGNORECASE)
    if not match:
        return None
    candidate = _strip_known_phrases(match.group(1))
    candidate = re.split(r"\b(?:and|where|that|with)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0]
    candidate = candidate.strip(" ,.;:")
    if not candidate or candidate in _STOP_WORDS:
        return None
    return candidate


def parse_finance_query(query: str, reference_date: Optional[date] = None) -> ParsedFinanceQuery:
    today = reference_date or date.today()
    parsed = ParsedFinanceQuery(original_query=query or "")
    lower_query = (query or "").lower()

    for entity_type, words in _ENTITY_SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(word)}\b", lower_query) for word in sorted(words, key=len, reverse=True)):
            if entity_type not in parsed.entity_types:
                parsed.entity_types.append(entity_type)

    transactional_types = {"invoice", "bill", "payment", "deposit", "journal_entry"}
    if any(entity_type in transactional_types for entity_type in parsed.entity_types):
        parsed.entity_types = [
            entity_type
            for entity_type in parsed.entity_types
            if entity_type in transactional_types
        ]

    if re.search(r"\b(open|unpaid|outstanding|uncollected)\b", lower_query):
        parsed.status = "open"
    if re.search(r"\b(paid|closed)\b", lower_query):
        parsed.status = "paid"
    if re.search(r"\b(overdue|past due)\b", lower_query):
        parsed.status = "open"
        parsed.overdue_as_of = today
    if re.search(r"\binactive\b", lower_query):
        parsed.status = "inactive"
    elif re.search(r"\bactive\b", lower_query):
        parsed.status = "active"

    if "last month" in lower_query:
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        parsed.start_date, parsed.end_date = _month_bounds(last_month_end.year, last_month_end.month)
    elif "this month" in lower_query or "current month" in lower_query:
        parsed.start_date, parsed.end_date = _month_bounds(today.year, today.month)
    elif "last year" in lower_query:
        parsed.start_date, parsed.end_date = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif "this year" in lower_query or "current year" in lower_query or "ytd" in lower_query:
        parsed.start_date, parsed.end_date = date(today.year, 1, 1), today
    elif "last quarter" in lower_query:
        parsed.start_date, parsed.end_date = _quarter_bounds(today, offset=-1)
    elif "this quarter" in lower_query or "current quarter" in lower_query:
        parsed.start_date, parsed.end_date = _quarter_bounds(today, offset=0)

    for match in re.finditer(r"\b(" + "|".join(_MONTHS.keys()) + r")\s+(\d{4})\b", lower_query):
        month = _MONTHS[match.group(1)]
        year = int(match.group(2))
        parsed.start_date, parsed.end_date = _month_bounds(year, month)

    iso_dates = [date.fromisoformat(m.group(0)) for m in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", lower_query)]
    if len(iso_dates) >= 2:
        parsed.start_date, parsed.end_date = min(iso_dates), max(iso_dates)
    elif len(iso_dates) == 1:
        parsed.start_date = parsed.end_date = iso_dates[0]

    between = re.search(
        r"\bbetween\s+(\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?)\s+and\s+(\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?)",
        lower_query,
    )
    if between:
        low = _parse_amount(between.group(1))
        high = _parse_amount(between.group(2))
        parsed.min_amount, parsed.max_amount = sorted([low, high])
    else:
        over = re.search(r"\b(?:over|above|greater than|more than|at least)\s+(\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?)", lower_query)
        under = re.search(r"\b(?:under|below|less than|at most)\s+(\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*[kKmM]?)", lower_query)
        if over:
            parsed.min_amount = _parse_amount(over.group(1))
        if under:
            parsed.max_amount = _parse_amount(under.group(1))

    parsed.customer_name = _extract_named_filter(lower_query, ["customer", "client"])
    parsed.vendor_name = _extract_named_filter(lower_query, ["vendor", "supplier", "payee"])
    parsed.account = _extract_named_filter(lower_query, ["account", "accounts", "coa"])
    parsed.class_name = _extract_named_filter(lower_query, ["class", "classes"])
    parsed.location = _extract_named_filter(lower_query, ["location", "locations", "department", "departments"])
    generic_name = _extract_named_filter(lower_query, ["for", "from", "with"])
    if generic_name and not any([parsed.customer_name, parsed.vendor_name, parsed.account, parsed.class_name, parsed.location]):
        parsed.customer_vendor_name = generic_name

    normalized_terms = _normalize_text(_strip_known_phrases(lower_query)).split()
    parsed.terms = [
        term
        for term in normalized_terms
        if term not in _STOP_WORDS and not term.isdigit() and len(term) > 1
    ][:12]
    return parsed


def _merge_filters(parsed: ParsedFinanceQuery, filters: Optional[Dict[str, Any]]) -> ParsedFinanceQuery:
    if not filters:
        return parsed
    entity_type = filters.get("entity_type")
    entity_types = filters.get("entity_types")
    if entity_type:
        parsed.entity_types = [_canonical_entity_type(entity_type) or str(entity_type)]
    if entity_types:
        parsed.entity_types = [_canonical_entity_type(value) or str(value) for value in entity_types]
    for attr in ["status", "customer_name", "vendor_name", "customer_vendor_name", "account", "class_name", "location"]:
        if filters.get(attr):
            setattr(parsed, attr, str(filters[attr]))
    if filters.get("start_date"):
        parsed.start_date = _parse_date(filters["start_date"])
    if filters.get("end_date"):
        parsed.end_date = _parse_date(filters["end_date"])
    if filters.get("min_amount") is not None:
        parsed.min_amount = _as_float(filters["min_amount"])
    if filters.get("max_amount") is not None:
        parsed.max_amount = _as_float(filters["max_amount"])
    return parsed


def _similarity(a: str, b: str) -> float:
    a_norm = _normalize_text(a)
    b_norm = _normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm in b_norm or b_norm in a_norm:
        return 1.0
    ratios = [SequenceMatcher(None, a_norm, b_norm).ratio()]
    b_tokens = b_norm.split()
    a_tokens = a_norm.split()
    for a_token in a_tokens:
        for b_token in b_tokens:
            ratios.append(SequenceMatcher(None, a_token, b_token).ratio())
    return max(ratios)


def _name_match_score(needle: Optional[str], values: Sequence[Optional[str]]) -> Tuple[bool, int, Optional[str]]:
    if not needle:
        return True, 0, None
    best_score = 0
    best_value = None
    for value in values:
        if not value:
            continue
        ratio = _similarity(needle, value)
        if ratio >= 1.0:
            score = 160
        elif ratio >= 0.82:
            score = 110
        elif ratio >= 0.68:
            score = 70
        else:
            score = 0
        if score > best_score:
            best_score = score
            best_value = str(value)
    return best_score > 0, best_score, best_value


def _highlight_text(value: Optional[str], needles: Sequence[str], *, fuzzy_whole: bool = False) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value)
    highlighted = text
    did_highlight = False
    for needle in needles:
        needle = str(needle or "").strip()
        if not needle:
            continue
        pattern = re.compile(re.escape(needle), flags=re.IGNORECASE)
        highlighted, count = pattern.subn(lambda m: f"[[{m.group(0)}]]", highlighted)
        did_highlight = did_highlight or count > 0
    if fuzzy_whole and not did_highlight:
        return f"[[{text}]]"
    return highlighted if did_highlight else None


def _record_fields(record: QBOSearchRecord) -> Dict[str, Optional[str]]:
    return {
        "qbo_id": record.qbo_id,
        "doc_number": record.doc_number,
        "display_name": record.display_name,
        "customer_name": record.customer_name,
        "vendor_name": record.vendor_name,
        "account_name": record.account_name,
        "class_name": record.class_name,
        "location_name": record.location_name,
        "memo": record.memo,
        "search_text": record.search_text,
    }


def _score_record(record: QBOSearchRecord, parsed: ParsedFinanceQuery) -> Optional[Tuple[int, Dict[str, str], List[str]]]:
    score = 0
    highlights: Dict[str, str] = {}
    matched_fields: List[str] = []
    fields = _record_fields(record)
    query_norm = _normalize_text(parsed.original_query)
    terms = parsed.terms

    name_checks = [
        (parsed.customer_name, [record.customer_name, record.display_name], "customer_name"),
        (parsed.vendor_name, [record.vendor_name, record.display_name], "vendor_name"),
        (parsed.customer_vendor_name, [record.customer_name, record.vendor_name, record.display_name], "display_name"),
        (parsed.account, [record.account_name, record.display_name], "account_name"),
        (parsed.class_name, [record.class_name, record.display_name], "class_name"),
        (parsed.location, [record.location_name, record.display_name], "location_name"),
    ]
    for needle, values, field_name in name_checks:
        ok, add_score, best_value = _name_match_score(needle, values)
        if not ok:
            return None
        if add_score:
            if field_name == "display_name":
                if best_value == record.customer_name:
                    field_name = "customer_name"
                elif best_value == record.vendor_name:
                    field_name = "vendor_name"
            score += add_score
            matched_fields.append(field_name)
            highlight_value = _highlight_text(best_value, [needle or ""], fuzzy_whole=True)
            if highlight_value:
                highlights.setdefault(field_name, highlight_value)

    for field_name in ["qbo_id", "doc_number"]:
        value = fields.get(field_name)
        value_norm = _normalize_text(value)
        if value_norm and query_norm and (query_norm == value_norm or value_norm in query_norm):
            score += 1000 if query_norm == value_norm else 700
            matched_fields.append(field_name)
            highlighted = _highlight_text(value, [value or ""])
            if highlighted:
                highlights[field_name] = highlighted

    phrase_needles = [parsed.original_query.strip()]
    for field_name, value in fields.items():
        if not value:
            continue
        value_norm = _normalize_text(value)
        if query_norm and query_norm in value_norm and field_name != "search_text":
            score += 220
            matched_fields.append(field_name)
            highlighted = _highlight_text(value, phrase_needles)
            if highlighted:
                highlights.setdefault(field_name, highlighted)

    term_hits = 0
    for term in terms:
        hit_for_term = False
        for field_name, value in fields.items():
            if not value:
                continue
            value_norm = _normalize_text(value)
            if term in value_norm:
                hit_for_term = True
                weight = 90 if field_name in {"qbo_id", "doc_number", "display_name"} else 45
                if field_name == "memo":
                    weight = 70
                score += weight
                if field_name != "search_text":
                    matched_fields.append(field_name)
                    highlighted = _highlight_text(value, [term])
                    if highlighted:
                        highlights.setdefault(field_name, highlighted)
        if not hit_for_term:
            name_values = [
                record.display_name,
                record.customer_name,
                record.vendor_name,
                record.account_name,
                record.class_name,
                record.location_name,
            ]
            ok, add_score, best_value = _name_match_score(term, name_values)
            if ok:
                score += max(add_score - 30, 20)
                hit_for_term = True
                if best_value:
                    matched_fields.append("display_name")
                    highlights.setdefault("display_name", f"[[{best_value}]]")
        if hit_for_term:
            term_hits += 1

    if parsed.entity_types and record.entity_type in parsed.entity_types:
        score += 80
        matched_fields.append("entity_type")
    if parsed.status and record.status == parsed.status:
        score += 60
        matched_fields.append("status")
    if parsed.start_date or parsed.end_date:
        score += 30
        matched_fields.append("txn_date")
    if parsed.min_amount is not None or parsed.max_amount is not None:
        score += 30
        matched_fields.append("amount")

    requires_text_match = bool(terms or parsed.customer_vendor_name or parsed.customer_name or parsed.vendor_name or parsed.account or parsed.class_name or parsed.location)
    if requires_text_match and score <= 0:
        return None
    if terms and term_hits == 0 and not any([parsed.customer_vendor_name, parsed.customer_name, parsed.vendor_name, parsed.account, parsed.class_name, parsed.location]):
        return None

    return score, highlights, sorted(set(matched_fields))


def _to_float(value: Any) -> Optional[float]:
    parsed = _as_float(value)
    return round(parsed, 2) if parsed is not None else None


def _record_to_result(record: QBOSearchRecord, score: int, highlights: Dict[str, str], matched_fields: List[str]) -> Dict[str, Any]:
    return {
        "id": record.id,
        "entity_type": record.entity_type,
        "qbo_id": record.qbo_id,
        "doc_number": record.doc_number,
        "display_name": record.display_name,
        "txn_date": record.txn_date.isoformat() if record.txn_date else None,
        "due_date": record.due_date.isoformat() if record.due_date else None,
        "amount": _to_float(record.amount),
        "open_balance": _to_float(record.open_balance),
        "status": record.status,
        "customer_name": record.customer_name,
        "vendor_name": record.vendor_name,
        "account_name": record.account_name,
        "class_name": record.class_name,
        "location_name": record.location_name,
        "memo": record.memo,
        "score": score,
        "matched_fields": matched_fields,
        "highlights": highlights,
        "source": "qbo_search_records",
    }


def _apply_text_prefilter(db: Session, query_obj: Any, parsed: ParsedFinanceQuery) -> Any:
    bind = db.get_bind()
    dialect_name = getattr(bind.dialect, "name", "")
    if dialect_name != "postgresql" or not parsed.terms:
        return query_obj
    if any([parsed.customer_vendor_name, parsed.customer_name, parsed.vendor_name, parsed.account, parsed.class_name, parsed.location]):
        return query_obj
    ts_query = func.plainto_tsquery("english", " ".join(parsed.terms))
    return query_obj.filter(func.to_tsvector("english", func.coalesce(QBOSearchRecord.search_text, "")).op("@@")(ts_query))


def search_finance_records(
    db: Session,
    realm_id: str,
    query: str,
    *,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 20,
    reference_date: Optional[date] = None,
    ensure_index: bool = True,
) -> Dict[str, Any]:
    if ensure_index:
        ensure_search_index(db, realm_id)

    parsed = _merge_filters(parse_finance_query(query, reference_date=reference_date), filters)
    organization, _organization_company = _finance_search_scope(db, realm_id)
    candidate_query = _apply_search_record_scope(
        db.query(QBOSearchRecord).filter(QBOSearchRecord.realm_id == realm_id),
        organization,
    )

    if parsed.entity_types:
        candidate_query = candidate_query.filter(QBOSearchRecord.entity_type.in_(parsed.entity_types))
    if parsed.status:
        candidate_query = candidate_query.filter(QBOSearchRecord.status == parsed.status)
    if parsed.start_date:
        candidate_query = candidate_query.filter(QBOSearchRecord.txn_date >= parsed.start_date)
    if parsed.end_date:
        candidate_query = candidate_query.filter(QBOSearchRecord.txn_date <= parsed.end_date)
    if parsed.overdue_as_of:
        candidate_query = candidate_query.filter(QBOSearchRecord.due_date != None)  # noqa: E711
        candidate_query = candidate_query.filter(QBOSearchRecord.due_date < parsed.overdue_as_of)
    if parsed.min_amount is not None:
        candidate_query = candidate_query.filter(QBOSearchRecord.amount >= parsed.min_amount)
    if parsed.max_amount is not None:
        candidate_query = candidate_query.filter(QBOSearchRecord.amount <= parsed.max_amount)

    candidate_query = _apply_text_prefilter(db, candidate_query, parsed)
    candidates = candidate_query.limit(max(limit * 50, 250)).all()

    scored: List[Tuple[int, QBOSearchRecord, Dict[str, str], List[str]]] = []
    for record in candidates:
        score_payload = _score_record(record, parsed)
        if not score_payload:
            continue
        score, highlights, matched_fields = score_payload
        scored.append((score, record, highlights, matched_fields))

    scored.sort(
        key=lambda item: (
            item[0],
            item[1].txn_date or date.min,
            float(item[1].amount or 0),
        ),
        reverse=True,
    )

    results = [
        _record_to_result(record, score, highlights, matched_fields)
        for score, record, highlights, matched_fields in scored[:limit]
    ]
    return {
        "query": query,
        "filters": parsed.to_filters(),
        "count": len(results),
        "results": results,
        "deterministic": True,
        "source": "qbo_search_records",
    }

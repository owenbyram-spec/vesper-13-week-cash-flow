"""Journal-entry line checks against the realm's synced chart, shared by every
JE lane (propose, edit, approve, execute) and the accrual drafters.

QuickBooks rejects a JournalEntry line whose ``AccountRef`` is empty or unknown,
and an Accounts Receivable / Accounts Payable line that carries no
``JournalEntryLineDetail.Entity`` (a Customer on A/R, a Vendor on A/P). The
checks here refuse those drafts deterministically, before any approval or post,
and resolve each line's entity to a stored id so the JE writer can attach it.

Simplified line shape (``{date, memo, lines}`` payloads)::

    {account_id, account_name, debit, credit, description,
     vendor_name?, customer_name?, entity_type?, entity_id?}

``entity_type`` ("Customer" | "Vendor") + ``entity_id`` are the stored
resolution. A ``vendor_name`` / ``customer_name`` (the JE composer's Name
column) is resolved to an id by exact, case-insensitive display-name match
against the synced vendor/customer list, never guessed. On an A/R or A/P line a
missing or ambiguous name is refused; on any other line it is refused only when
a person just typed it (``resolve_je_lines``). The A/R / A/P rule is the shared
exact-match predicate in
``services/bank_recon/offset_inference._is_ar_ap_control``.
"""
from __future__ import annotations

import copy
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import QBOAccount, QBOCustomer, QBOTransaction, QBOVendor, VendorProfile
from ..services.bank_recon.offset_inference import _is_ar_ap_control
from .refusals import QBOActionRefused

ENTITY_TYPES = ("Customer", "Vendor")
_EXPENSE_ACCOUNT_TYPES = ("Expense", "Other Expense", "Cost of Goods Sold")


def _required_entity_type(account: QBOAccount) -> str | None:
    """"Customer" for an A/R control, "Vendor" for an A/P control, else None."""
    if not _is_ar_ap_control(account):
        return None
    if account.account_sub_type == "AccountsReceivable" or account.account_type == "Accounts Receivable":
        return "Customer"
    return "Vendor"


def _accounts_by_id(db: Session, realm_id: str, account_ids: set[str]) -> dict[str, QBOAccount]:
    if not account_ids:
        return {}
    rows = (
        db.query(QBOAccount)
        .filter(
            QBOAccount.realm_id == realm_id,
            QBOAccount.qbo_id.in_(account_ids),
            QBOAccount.active.is_(True),
        )
        .all()
    )
    return {str(row.qbo_id): row for row in rows}


def _normalized_name(name: str) -> str:
    return " ".join(str(name or "").split()).lower()


def _matching_ids(db: Session, realm_id: str, entity_type: str, name: str) -> list[str]:
    """Synced ids whose display name matches exactly (case-insensitive, trimmed)."""
    model = QBOVendor if entity_type == "Vendor" else QBOCustomer
    rows = (
        db.query(model.qbo_id)
        .filter(
            model.realm_id == realm_id,
            model.active.isnot(False),
            func.lower(func.trim(model.display_name)) == _normalized_name(name),
        )
        .all()
    )
    return sorted({str(row[0]) for row in rows})


def _resolve_name(db: Session, realm_id: str, entity_type: str, name: str) -> str:
    """Exact (case-insensitive, trimmed) display-name match; refuse 0 or >1 hits."""
    ids = _matching_ids(db, realm_id, entity_type, name)
    if len(ids) != 1:
        problem = "is not in the synced list" if not ids else "matches more than one record"
        raise QBOActionRefused(
            f"je_line_unknown_{entity_type.lower()}",
            f"{entity_type} '{name}' {problem}. Pick the {entity_type.lower()} from the list.",
        )
    return ids[0]


def _entity_exists(db: Session, realm_id: str, entity_type: str, entity_id: str) -> bool:
    model = QBOVendor if entity_type == "Vendor" else QBOCustomer
    return (
        db.query(model.id)
        .filter(model.realm_id == realm_id, model.qbo_id == entity_id, model.active.isnot(False))
        .first()
        is not None
    )


def realm_has_synced_chart(db: Session, realm_id: str) -> bool:
    """True once any account of this realm's chart has been synced."""
    return db.query(QBOAccount.id).filter(QBOAccount.realm_id == realm_id).first() is not None


def resolve_je_lines(
    db: Session,
    realm_id: str,
    lines: list[dict],
    *,
    require_synced_chart: bool = True,
    strict_names: bool = True,
    carried_names: Collection[str] = (),
) -> list[dict]:
    """Validate simplified JE lines and return copies carrying resolved entities.

    Refuses (``QBOActionRefused``) an empty account id, an account that is not an
    active account of this realm, an unknown stored entity id, and an A/R or A/P
    line without a matching Customer/Vendor.

    An empty account id is refused unconditionally. The chart checks need the
    realm's synced chart: with ``require_synced_chart=False`` (the propose,
    approve and execute lanes) a realm with NO synced account at all is checked
    for empty ids only and QuickBooks remains the validator (an unknown account
    or a missing entity comes back as a confirmed rejection; nothing posts). The
    edit lane keeps ``require_synced_chart=True``: it refuses ids it cannot find.

    A Name on a line that needs no entity is attribution. It is attached when it
    matches exactly one synced record. An unknown or ambiguous one is refused
    only when a person has just typed it (``strict_names`` and not among the
    draft's ``carried_names``: the propose lane, a new name in the edit lane);
    otherwise it stays on the stored line as a label and is not sent. System
    drafters write labels there (the schedule engine derives one from the
    schedule name), and refusing them would block every such entry.
    """
    carried = {_normalized_name(name) for name in carried_names if name}
    resolved = [dict(line or {}) for line in lines or []]
    missing = [i + 1 for i, line in enumerate(resolved) if not str(line.get("account_id") or "").strip()]
    if missing:
        raise QBOActionRefused(
            "je_line_missing_account",
            f"Journal entry line(s) {', '.join(map(str, missing))} have no account. "
            "Pick an account for every line before approving.",
        )
    if not resolved:
        return resolved
    if not require_synced_chart and not realm_has_synced_chart(db, realm_id):
        return resolved
    account_ids = {str(line["account_id"]).strip() for line in resolved}
    accounts = _accounts_by_id(db, realm_id, account_ids)
    unknown = sorted(account_ids - set(accounts))
    if unknown:
        raise QBOActionRefused(
            "je_line_unknown_account",
            f"Unknown or inactive account id(s) for this company: {', '.join(unknown)}. "
            "Pick an account from the list.",
        )

    for index, line in enumerate(resolved, start=1):
        account_id = str(line["account_id"]).strip()
        line["account_id"] = account_id
        required = _required_entity_type(accounts[account_id])
        entity_type = line.get("entity_type")
        entity_id = str(line.get("entity_id") or "").strip()
        if entity_id:
            if entity_type not in ENTITY_TYPES:
                raise QBOActionRefused(
                    "je_line_unknown_entity",
                    f"Line {index} names an entity without a valid type (Customer or Vendor).",
                )
            if not _entity_exists(db, realm_id, entity_type, entity_id):
                raise QBOActionRefused(
                    "je_line_unknown_entity",
                    f"Line {index} names a {entity_type.lower()} that is not in the synced list.",
                )
        else:
            vendor_name = str(line.get("vendor_name") or "").strip()
            customer_name = str(line.get("customer_name") or "").strip()
            if required == "Customer" and customer_name:
                name_type, name = "Customer", customer_name
            elif vendor_name and required != "Customer":
                name_type, name = "Vendor", vendor_name
            elif customer_name and required is None:
                name_type, name = "Customer", customer_name
            else:
                name_type, name = None, ""
            entity_type, entity_id = None, ""
            if name_type is not None:
                if required is None and (not strict_names or _normalized_name(name) in carried):
                    ids = _matching_ids(db, realm_id, name_type, name)
                    if len(ids) == 1:
                        entity_type, entity_id = name_type, ids[0]
                else:
                    entity_type, entity_id = name_type, _resolve_name(db, realm_id, name_type, name)
        if required is not None and (not entity_id or entity_type != required):
            label = "Accounts Receivable" if required == "Customer" else "Accounts Payable"
            raise QBOActionRefused(
                "je_line_entity_required",
                f"Line {index} posts to {label}; QuickBooks requires a {required.lower()} on that "
                f"line. Choose the {required.lower()} (Name) for it.",
            )
        if entity_id:
            line["entity_type"] = entity_type
            line["entity_id"] = entity_id
    return resolved


def validate_raw_je_lines(db: Session, realm_id: str, lines: list[dict]) -> None:
    """The same account / entity rule over QBO-native ``Line`` payloads (the
    execute lane's scope: chart checks only once the realm's chart is synced)."""
    account_ids: list[str] = []
    for line in lines:
        detail = (line or {}).get("JournalEntryLineDetail") or {}
        account_ids.append(str((detail.get("AccountRef") or {}).get("value") or "").strip())
    if any(not account_id for account_id in account_ids):
        raise QBOActionRefused(
            "je_line_missing_account", "A journal entry line has no account. Pick an account for every line."
        )
    if not account_ids or not realm_has_synced_chart(db, realm_id):
        return
    accounts = _accounts_by_id(db, realm_id, set(account_ids))
    unknown = sorted(set(account_ids) - set(accounts))
    if unknown:
        raise QBOActionRefused(
            "je_line_unknown_account",
            f"Unknown or inactive account id(s) for this company: {', '.join(unknown)}.",
        )
    for index, (line, account_id) in enumerate(zip(lines, account_ids), start=1):
        required = _required_entity_type(accounts[account_id])
        if required is None:
            continue
        entity = ((line or {}).get("JournalEntryLineDetail") or {}).get("Entity") or {}
        if entity.get("Type") != required or not (entity.get("EntityRef") or {}).get("value"):
            raise QBOActionRefused(
                "je_line_entity_required",
                f"Line {index} posts to an A/R or A/P account without the required {required.lower()}.",
            )


def prepare_journal_entry_payload(db: Session, realm_id: str, payload: dict) -> dict:
    """The executor's pre-post check: a copy of ``payload`` with resolved lines."""
    payload = payload or {}
    if "Line" in payload:
        validate_raw_je_lines(db, realm_id, payload.get("Line") or [])
        return payload
    prepared = copy.deepcopy(payload)
    prepared["lines"] = resolve_je_lines(
        db, realm_id, payload.get("lines") or [], require_synced_chart=False, strict_names=False
    )
    return prepared


# ── Accrual drafting (missing recurring-vendor accruals) ─────────────────────

@dataclass(frozen=True)
class AccrualAccounts:
    expense_account_id: str
    expense_account_name: str
    liability_account_id: str
    liability_account_name: str


def _account_label(account: QBOAccount) -> str:
    return account.fully_qualified_name or account.name


def resolve_accrued_liability_account(db: Session, realm_id: str) -> QBOAccount | None:
    """The realm's one accrued-liabilities account, or None when absent/ambiguous.

    Candidates are active Other Current Liability accounts. The QBO detail type
    ``AccruedLiabilities`` wins when exactly one account carries it; otherwise
    exactly one account named "Accrued Liabilities" or "Accrued Expenses" (the
    balance-sheet caption) qualifies. Two plausible accounts are never guessed
    between.
    """
    pool = (
        db.query(QBOAccount)
        .filter(
            QBOAccount.realm_id == realm_id,
            QBOAccount.active.is_(True),
            QBOAccount.account_type == "Other Current Liability",
        )
        .all()
    )
    names = {"accrued liabilities", "accrued expenses"}

    def _named(account: QBOAccount) -> bool:
        return " ".join((account.name or "").split()).lower() in names

    typed = [a for a in pool if a.account_sub_type == "AccruedLiabilities"]
    if len(typed) == 1:
        return typed[0]
    candidates = [a for a in typed if _named(a)] if typed else [a for a in pool if _named(a)]
    return candidates[0] if len(candidates) == 1 else None


def _expense_account(db: Session, realm_id: str, account_id: str | None) -> QBOAccount | None:
    if not account_id:
        return None
    return (
        db.query(QBOAccount)
        .filter(
            QBOAccount.realm_id == realm_id,
            QBOAccount.qbo_id == str(account_id),
            QBOAccount.active.is_(True),
            QBOAccount.account_type.in_(_EXPENSE_ACCOUNT_TYPES),
        )
        .first()
    )


def resolve_vendor_expense_account(db: Session, realm_id: str, vendor: VendorProfile) -> QBOAccount | None:
    """The vendor's historical expense account: its profile's typical account,
    else the category of its most recent categorized transaction. Only an active
    Expense / Other Expense / COGS account of this realm qualifies."""
    account = _expense_account(db, realm_id, vendor.typical_account_id)
    if account is not None:
        return account
    latest = (
        db.query(QBOTransaction.account_ref)
        .filter(
            QBOTransaction.realm_id == realm_id,
            QBOTransaction.vendor_name == vendor.vendor_name,
            QBOTransaction.account_ref.isnot(None),
        )
        .order_by(QBOTransaction.date.desc(), QBOTransaction.id.desc())
        .first()
    )
    return _expense_account(db, realm_id, latest[0] if latest else None)


def accrual_unresolved_reason(expense: QBOAccount | None, liability: QBOAccount | None) -> str | None:
    if expense is None:
        return "no_expense_account"
    if liability is None:
        return "no_accrued_liability_account"
    return None


ACCRUAL_UNRESOLVED_MESSAGES = {
    "no_expense_account": (
        "no active expense account could be resolved from the vendor's history"
    ),
    "no_accrued_liability_account": (
        "the company has no single active Accrued Liabilities (Other Current Liability) account"
    ),
}


def accrual_journal_lines(
    *, vendor_name: str, amount: Decimal, expense: QBOAccount, liability: QBOAccount
) -> list[dict]:
    """Debit the vendor's expense account, credit Accrued Liabilities."""
    value = float(amount.quantize(Decimal("0.01")))
    description = f"Accrual: {vendor_name}"
    return [
        {
            "account_id": str(expense.qbo_id),
            "account_name": _account_label(expense),
            "debit": value,
            "credit": 0,
            "description": description,
        },
        {
            "account_id": str(liability.qbo_id),
            "account_name": _account_label(liability),
            "debit": 0,
            "credit": value,
            "description": description,
        },
    ]


def unresolved_accrual_finding(
    *, vendor_name: str, period: str, amount: Decimal, reason: str
) -> dict:
    """The review-only REVIEW_CHECK payload for an accrual that was not drafted."""
    detail = ACCRUAL_UNRESOLVED_MESSAGES.get(reason, reason)
    return {
        "check_id": "missing_accrual_accounts_unresolved",
        "period": period,
        "severity": "warning",
        "summary": f"Missing accrual not drafted: {vendor_name}",
        "message": (
            f"No {vendor_name} transaction was found in {period} (expected about "
            f"${amount:,.2f}). No accrual entry was drafted because {detail}. "
            "Draft the accrual manually or record the vendor's usual expense account."
        ),
        "finding": {"vendor_name": vendor_name, "expected_amount": float(amount), "reason": reason},
    }

"""Offset-account inference for a bank-rec "Create entry" (plan 20260715-je-offset-inference).

Given an unmatched statement line, suggest the OFFSETTING account for the balancing JE leg by
running the shared classifier (``LLMClassifier.classify_fields``) over the FULL active chart of
accounts for the realm, then VALIDATING the returned id against a real active ``QBOAccount``.

Safety posture (agentic-writeback-safety): the suggestion is ADVISORY only. It is surfaced to
the human in the confirm-at-create modal, which the human accepts or overrides before any draft
exists — nothing here writes. A low-confidence / invalid / zero-amount line returns ``None`` so
the modal simply leaves the picker for the user (DEC-JE-2 / DEC-JE-3); we never fabricate or
pass an unvalidated id (which would draft a balanced-but-unpostable JE).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, func
from sqlalchemy.orm import Session

from ...models import BankFeedItem, QBOAccount
from ..llm_classifier import LLMClassifier
from ..redaction import redact_to_string

logger = logging.getLogger(__name__)

# Suggestions below this confidence are withheld (the modal keeps the picker for the user).
CONFIDENCE_THRESHOLD = 0.70

# The AR/AP CONTROL-account identifiers (DEC-PG-1 — AR/AP-EXACT-ONLY). An active realm
# QBOAccount is a NON-postable JE offset target iff it is an AR or AP control account, matched
# by EXACT equality / IN-membership ONLY — NEVER a substring / ilike / lower-contains match:
# QBO liability sub_types that CONTAIN "Payable" (LoanPayable, NotesPayable, SalesTaxPayable,
# PayrollTaxPayable, …) are LEGITIMATE JE offsets (a loan repayment / tax remittance) and MUST
# stay postable. QBO stores the AccountType WITH spaces ('Accounts Receivable') and the
# AccountSubType as the camelCase Intuit enum ('AccountsReceivable'); sync.py persists both raw.
_AR_AP_CONTROL_SUB_TYPES: tuple[str, ...] = ("AccountsReceivable", "AccountsPayable")
_AR_AP_CONTROL_TYPES: tuple[str, ...] = ("Accounts Receivable", "Accounts Payable")


def _is_ar_ap_control(account: QBOAccount) -> bool:
    """True iff ``account`` is an AR/AP control account (a NON-postable JE offset target).

    THE one shared predicate (DEC-PG-3): the offset-candidate exclusion (via
    ``ar_ap_control_exclusion_filter``), the ``suggest_offset`` validation, and the router's
    ``propose_entry`` 422 all resolve postability through this exact-match rule. A JE line to an
    AR/AP control needs an ``Entity`` ref, so a balanced draft to one FAILS at the QBO post —
    excluding it keeps "Create entry" to accounts it can actually post. ``None`` (either field
    absent) is NOT a control → postable (mirrored NULL-safe by the query form below).
    """
    return (
        account.account_sub_type in _AR_AP_CONTROL_SUB_TYPES
        or account.account_type in _AR_AP_CONTROL_TYPES
    )


def ar_ap_control_exclusion_filter() -> ColumnElement[bool]:
    """A SQLAlchemy WHERE expression that KEEPS only postable rows — i.e. excludes AR/AP control
    accounts. The query-form of ``_is_ar_ap_control`` (DEC-PG-3, one shared rule), applied by
    ``LLMClassifier._get_coa_context`` when ``postable_only=True``.

    NULL-safe via ``coalesce``: a row whose ``account_type``/``account_sub_type`` is NULL is NOT
    an AR/AP control, so it is KEPT — exactly mirroring the Python predicate (which treats
    ``None`` as "not a control"). A naive ``not_(or_(in_, in_))`` would DROP an all-NULL row
    under SQL three-valued logic (``not_(NULL)`` is not-true) — this De-Morgan+coalesce form
    does not, so the query and the predicate agree on every row.
    """
    return and_(
        func.coalesce(QBOAccount.account_sub_type, "").notin_(_AR_AP_CONTROL_SUB_TYPES),
        func.coalesce(QBOAccount.account_type, "").notin_(_AR_AP_CONTROL_TYPES),
    )


@dataclass(frozen=True)
class OffsetSuggestion:
    """The validated offsetting-account suggestion the /suggest-offset route returns."""

    account_id: str
    account_name: str
    classification: str | None
    confidence: float
    reason: str | None


def suggest_offset(
    db: Session, *, realm_id: str, item: BankFeedItem
) -> OffsetSuggestion | None:
    """Infer + validate the offsetting account for one unmatched statement line, or ``None``.

    Returns ``None`` (no confident/valid suggestion) when the amount is zero/absent, the model
    returns nothing, the confidence is below ``CONFIDENCE_THRESHOLD`` (0.70), or the returned
    account id is not an ACTIVE account in this realm.
    """
    amount = item.amount
    if amount is None or amount == 0:
        return None

    # Direction from the statement sign (ingest convention: positive = deposit / money in,
    # negative = withdrawal / money out). A deposit frames toward income, a withdrawal over the
    # full CoA — the model reasons across all classes either way; the sign only frames the ask.
    framing = "income" if amount > 0 else "expense_full"

    try:
        result = LLMClassifier(db).classify_fields(
            item.description,
            item.amount,
            item.txn_date,
            realm_id,
            allowed_coa=None,  # the FULL active chart — Income/Asset/Liability/Equity reachable
            framing=framing,
            postable_only=True,  # exclude AR/AP control accounts from the candidate set (DEC-PG-1)
        )
    except Exception as e:
        # Fail SOFT (DEC-PG-4): the offset suggestion is ADVISORY — the modal keeps the picker
        # for the user on a null result — so any LLM/provider/budget error degrades to "no
        # suggestion" (None → /suggest-offset all-null), never a 500. Mirrors the sibling
        # llm_suggest.suggest_pairings honest-empty contract; the cause is redacted at the sink
        # (log-redaction) so a token/payload in the exception never reaches the log.
        logger.warning("offset suggestion LLM call failed: %s", redact_to_string(e))
        return None
    if not result:
        return None

    account_id = result.get("account_id")
    if not account_id:
        return None
    account_id = str(account_id)

    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError):
        return None
    if confidence < CONFIDENCE_THRESHOLD:
        return None

    # VALIDATE the returned id against a real, ACTIVE account in THIS realm — never trust the
    # model's id (a hallucinated/inactive id → a balanced-but-unpostable JE). Realm-scoped so a
    # foreign-tenant account can never satisfy the check (multi-tenancy AP-004).
    account = (
        db.query(QBOAccount)
        .filter(
            QBOAccount.realm_id == realm_id,
            QBOAccount.qbo_id == account_id,
            QBOAccount.active == True,  # noqa: E712
        )
        .first()
    )
    if account is None:
        return None

    # Postability guard (DEC-PG-1): even a valid, active, in-realm account is WITHHELD when it is
    # an AR/AP CONTROL account — a JE line to AR/AP needs an Entity ref, so a balanced draft to
    # one would FAIL at the QBO post. Same shared predicate the propose_entry 422 uses.
    if _is_ar_ap_control(account):
        return None

    return OffsetSuggestion(
        account_id=account.qbo_id,
        account_name=account.fully_qualified_name or account.name,
        classification=account.classification,
        confidence=confidence,
        reason=result.get("reason"),
    )

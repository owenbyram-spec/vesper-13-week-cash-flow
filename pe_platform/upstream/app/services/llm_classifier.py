import logging
from datetime import date as date_type
from decimal import Decimal
from typing import TypedDict

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models import QBOTransaction, QBOAccount, SystemSettings
from .llm import LLMService

logger = logging.getLogger(__name__)

# The sweep-preserving default: classify(txn) delegates with the expense-only chart of
# accounts + expense framing so the categorization sweep (app/routers/classification.py,
# app/services/categorization_sweep.py) sends the byte-identical prompt it always did.
_EXPENSE_COA: list[str] = ["Expense", "CostOfGoodsSold"]

# A Deposit is money IN: its category is an income account or a balance-sheet account
# (a transfer in, a loan, an owner contribution). QBO's Classification values are Asset,
# Equity, Expense, Liability and Revenue; Income and Other Income accounts classify as
# Revenue. Expense is excluded so a deposit can never be proposed as negative expense.
_DEPOSIT_COA: list[str] = ["Revenue", "Asset", "Liability", "Equity"]

# The original triple-quoted prompts were indented 8 spaces (the method body indent), so every
# prompt line — including the "blank" separator lines — carried 8 leading spaces. We rebuild the
# prompts from explicit line lists rather than a triple-quoted string so that byte-identity to the
# pre-extraction prompt is GUARANTEED (whitespace-strippers cannot silently drop the 8-space
# separator lines) and directly testable (crit-eval RC-3 — the sweep prompt must not drift).
_IND = " " * 8

# Direction/target framings for the shared classifier. "expense" is BYTE-PRESERVED from the
# pre-extraction classify() (the goal + unsure sentences are the original literals). "income"
# frames a deposit toward income / balance-sheet; "expense_full" frames a withdrawal over the
# WHOLE chart of accounts (a bank line may be a transfer, a loan, a card payment, an asset).
_FRAMINGS: dict[str, dict[str, str]] = {
    "expense": {
        "goal": "classify bank transactions into the correct Expense Account.",
        "unsure": "If unsure, enable 'Uncategorized Expense' or return low confidence.",
    },
    "income": {
        "goal": (
            "classify this deposit (money in) into the correct account. It is most often an "
            "Income account, but it may be a transfer in, a loan, or an owner contribution, "
            "so consider the full chart of accounts."
        ),
        "unsure": (
            "If unsure, prefer an Income or 'Uncategorized Income' account, or return low "
            "confidence."
        ),
    },
    "expense_full": {
        "goal": (
            "classify this withdrawal (money out) into the correct account. It is most often "
            "an Expense account, but it may be a transfer out, a credit-card payment, a loan "
            "repayment, or an asset purchase, so consider the full chart of accounts."
        ),
        "unsure": "If unsure, return low confidence rather than guessing.",
    },
}


class ClassificationResult(TypedDict, total=False):
    """The (LLM-produced) classification shape the sweep + offset-inference consume."""

    account_id: str
    account_name: str
    reason: str
    confidence: float


def llm_failure_reason(classifier: "LLMClassifier") -> str:
    """Why the classifier's last classify()/classify_fields() returned None.

    AC-C2 (plan 20260716-batch5-remediation): the generation loops COUNT every
    None instead of silently skipping it — this accessor reads the reason the
    classifier recorded. The isinstance guard keeps a monkeypatched/mocked
    classifier (the test seam) from leaking a non-string into the counters.
    """
    reason = getattr(classifier, "last_failure_reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return "unknown"


def _build_system_prompt(goal: str) -> str:
    """The classify system prompt. For goal=the expense literal this is byte-identical to the
    pre-extraction prompt (each line + the blank separators carry the original 8-space indent)."""
    lines = [
        "",
        f"{_IND}You are an expert accountant for a business using QuickBooks Online.",
        f"{_IND}Your goal is to {goal}",
        _IND,
        f"{_IND}Output must be Valid JSON:",
        f"{_IND}{{",
        f'{_IND}    "account_id": "string (QBO Id)",',
        f'{_IND}    "account_name": "string",',
        f'{_IND}    "reason": "string (short explanation)",',
        f'{_IND}    "confidence": float (0.0 to 1.0)',
        f"{_IND}}}",
        _IND,
    ]
    return "\n".join(lines)


def _build_user_prompt(
    date_str: str, vendor: str, amount_val: float, history_text: str, coa_text: str, unsure: str
) -> str:
    """The classify user prompt. For unsure=the expense literal this is byte-identical to the
    pre-extraction prompt (the 8-space blank separators are preserved as explicit lines)."""
    lines = [
        "",
        f"{_IND}Classify this transaction:",
        f"{_IND}- Date: {date_str}",
        f'{_IND}- Description: "{vendor}"',
        f"{_IND}- Amount: {amount_val}",
        _IND,
        f"{_IND}Here is the relevant historical context (how we categorized similar vendors before):",
        f"{_IND}{history_text}",
        _IND,
        f"{_IND}Here is the Chart of Accounts (ID: Name):",
        f"{_IND}{coa_text}",
        _IND,
        f"{_IND}Instructions:",
        f"{_IND}1. Prioritize consistency with historical context.",
        f"{_IND}2. If no history, choose the most logical account from the list.",
        f"{_IND}3. {unsure}",
        _IND,
    ]
    return "\n".join(lines)


class LLMClassifier:
    def __init__(self, db: Session):
        self.db = db
        self.llm = LLMService()
        # AC-C2 (plan 20260716-batch5-remediation): why the LAST classify call
        # returned None — "empty_coa" | "llm_failed" | "invalid_llm_response",
        # or None after a success. Read via llm_failure_reason() so the
        # generation loops can count and surface failures instead of silently
        # skipping them (the R-Dim-4 silent-None funnel).
        self.last_failure_reason: str | None = None

    def _get_historical_context(
        self, vendor_name: str, realm_id: str | None, limit: int = 5
    ) -> str:
        """
        Retrieves recent transactions for similar vendors to provide few-shot examples.
        Scoped to the realm (multi-tenancy AP-004) so history never crosses tenants.
        """
        if not vendor_name:
            return ""

        # Simple wildcard match for now. in Phase 3 we use vector search.
        # Clean the vendor name a bit
        safe_name = vendor_name.replace("'", "")

        txns = (
            self.db.query(QBOTransaction)
            .filter(QBOTransaction.description.ilike(f"%{safe_name}%"))
            .filter(QBOTransaction.account_ref.isnot(None))
            .filter(QBOTransaction.realm_id == realm_id)
            .order_by(desc(QBOTransaction.date))
            .limit(limit)
            .all()
        )

        if not txns:
            return "No previous transactions found for this vendor."

        history = []
        for t in txns:
            history.append(f"- {t.date}: '{t.description}' classified as Account ID {t.account_ref} ({t.amount})")

        return "\n".join(history)

    def _get_coa_context(
        self,
        realm_id: str | None,
        allowed_coa: list[str] | None,
        *,
        postable_only: bool = False,
    ) -> str:
        """
        Returns a summarized Chart of Accounts for the realm. When allowed_coa is given the
        list is filtered to those classifications (the sweep passes Expense/COGS to save
        tokens AND preserve its behavior); when None the FULL active chart is returned (the
        offset-inference path needs Income/Asset/Liability/Equity too). Realm-scoped so the
        context never leaks another tenant's accounts (multi-tenancy AP-004).

        When postable_only is True (the offset-inference path only) the candidate query also
        drops AR/AP CONTROL accounts — non-postable JE offset targets (DEC-PG-1/DEC-PG-2). The
        default (False) adds NO extra WHERE clause and NO extra query, so the categorization
        sweep issues the byte-identical single query it always did.
        """
        query = self.db.query(QBOAccount)
        if allowed_coa:
            query = query.filter(QBOAccount.classification.in_(allowed_coa))
        query = query.filter(QBOAccount.active == True)  # noqa: E712
        query = query.filter(QBOAccount.realm_id == realm_id)
        if postable_only:
            # Function-local import breaks the offset_inference ↔ llm_classifier module cycle
            # (offset_inference imports LLMClassifier at module load, so llm_classifier cannot
            # import offset_inference at module load). Only reached on the offset path.
            from .bank_recon.offset_inference import ar_ap_control_exclusion_filter

            query = query.filter(ar_ap_control_exclusion_filter())
        accounts = query.all()

        lines = [f"{acc.qbo_id}: {acc.fully_qualified_name} ({acc.account_sub_type})" for acc in accounts]
        return "\n".join(lines)

    def _is_candidate_account(self, realm_id: str | None, account_id: str, allowed_coa: list[str]) -> bool:
        """True iff ``account_id`` is an active, postable realm account inside ``allowed_coa``
        — the same candidate set ``_get_coa_context(..., postable_only=True)`` offered."""
        if not account_id:
            return False
        from .bank_recon.offset_inference import ar_ap_control_exclusion_filter

        return (
            self.db.query(QBOAccount.id)
            .filter(
                QBOAccount.realm_id == realm_id,
                QBOAccount.qbo_id == account_id,
                QBOAccount.active == True,  # noqa: E712
                QBOAccount.classification.in_(allowed_coa),
                ar_ap_control_exclusion_filter(),
            )
            .first()
            is not None
        )

    def classify_fields(
        self,
        description: str | None,
        amount: Decimal | float | int | None,
        date: date_type | None,
        realm_id: str | None,
        *,
        allowed_coa: list[str] | None,
        framing: str,
        postable_only: bool = False,
    ) -> ClassificationResult | None:
        """
        The shared field-based classifier: ask the LLM to map a single line (description,
        amount, date) to a chart-of-accounts id + confidence. The sweep calls this via
        classify(txn) with allowed_coa=Expense/COGS + framing="expense" (byte-preserved);
        the offset-inference service calls it over the FULL CoA with a direction framing and
        postable_only=True (drop AR/AP control accounts from the candidate set — DEC-PG-1).

        Every None return records its reason on ``last_failure_reason`` (AC-C2,
        plan 20260716-batch5-remediation) so callers can surface it. An EMPTY
        realm chart of accounts short-circuits BEFORE any LLM call — asking a
        model to pick an account from an empty list is guaranteed dead spend,
        re-billed daily by the sweep; the honest refusal stops it.
        """
        self.last_failure_reason = None

        # AC-C2: the empty-CoA short-circuit runs FIRST — no LLM call, no
        # history query; the realm simply has no synced accounts to pick from.
        coa_text = self._get_coa_context(realm_id, allowed_coa, postable_only=postable_only)
        if not coa_text:
            self.last_failure_reason = "empty_coa"
            logger.warning(
                "[llm_classifier] no synced chart of accounts for this realm "
                "(realm_id=%s, allowed_coa=%s) — refusing to classify without an LLM call",
                realm_id,
                allowed_coa,
            )
            return None

        # Fetch system settings to determine model
        settings_row = self.db.query(SystemSettings).filter(SystemSettings.realm_id == realm_id).first()
        model = settings_row.default_model if settings_row else "gpt-4o"

        vendor = description or "Unknown"
        amount_val = float(amount) if amount else 0.0

        # 1. Gather Context (RAG)
        history_text = self._get_historical_context(vendor, realm_id)

        frame = _FRAMINGS.get(framing, _FRAMINGS["expense"])

        system_prompt = _build_system_prompt(frame["goal"])
        user_prompt = _build_user_prompt(
            str(date), vendor, amount_val, history_text, coa_text, frame["unsure"]
        )

        # AC-2 (plan 20260716-classification-hardening): the lane stamps its own
        # task_type so llm_usage_events can see classification spend.
        result = self.llm.predict_json(
            task_type="classification",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
        )

        if result and "account_id" in result and "confidence" in result:
            return result

        # Distinguish "the LLM lane failed / was denied" (predict_json returns
        # None on provider error, budget denial, or a JSON-parse failure — the
        # LLM layer already logged the redacted cause) from "the LLM answered
        # but the shape is unusable" — the counters surface each distinctly.
        self.last_failure_reason = "llm_failed" if result is None else "invalid_llm_response"
        return None

    def classify(self, transaction: QBOTransaction) -> ClassificationResult | None:
        """
        Classify a QBOTransaction (the categorization-sweep entry point).

        A Deposit is classified with the income framing over the income and
        balance-sheet chart (AR/AP control accounts excluded), and the reply is
        accepted only if it names one of those candidate accounts — a deposit is
        never proposed against an expense account. Every other entity (Purchase)
        delegates with the expense-only chart + expense framing, so its prompt
        stays byte-identical to the pre-extraction implementation.
        """
        if (transaction.entity_type or "").lower() == "deposit":
            result = self.classify_fields(
                transaction.description,
                transaction.amount,
                transaction.date,
                transaction.realm_id,
                allowed_coa=_DEPOSIT_COA,
                framing="income",
                postable_only=True,
            )
            if result is None:
                return None
            if not self._is_candidate_account(
                transaction.realm_id, str(result.get("account_id") or ""), _DEPOSIT_COA
            ):
                # The model named an account outside the deposit chart (an
                # expense account, an AR/AP control, or an unknown id): refuse
                # rather than draft a wrong-direction category.
                self.last_failure_reason = "suggestion_outside_allowed_chart"
                return None
            return result
        return self.classify_fields(
            transaction.description,
            transaction.amount,
            transaction.date,
            transaction.realm_id,
            allowed_coa=_EXPENSE_COA,
            framing="expense",
        )

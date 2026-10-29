"""
app/fpa/uaf/schema.py
─────────────────────
Universal Accounting Fabric (UAF) Canonical Schema.

Provides the vendor-agnostic intermediate representation for Charts of Accounts
and Trial Balances. Populated today from CSV/Excel uploads (excel_ingest) and
structured UniversalTrialBalance JSON; no ERP connector feeds it.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Optional


class UniversalClassification(str, enum.Enum):
    """Standardized financial statement classification."""
    REVENUE = "revenue"
    COGS = "cogs"
    OPEX = "opex"
    OTHER_INCOME = "other_income"
    OTHER_EXPENSE = "other_expense"
    CURRENT_ASSET = "current_asset"
    NON_CURRENT_ASSET = "non_current_asset"
    CURRENT_LIABILITY = "current_liability"
    NON_CURRENT_LIABILITY = "non_current_liability"
    EQUITY = "equity"
    UNKNOWN = "unknown"


class NormalBalance(str, enum.Enum):
    """Normal accounting balance side."""
    DEBIT = "debit"
    CREDIT = "credit"


@dataclass
class UniversalAccount:
    """A standardized Chart of Accounts entry."""
    account_id: str
    name: str
    account_number: Optional[str] = None
    classification: UniversalClassification = UniversalClassification.UNKNOWN
    sub_type: Optional[str] = None
    normal_balance: NormalBalance = NormalBalance.DEBIT
    description: Optional[str] = None
    source_system: str = "generic"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_income_statement(self) -> bool:
        return self.classification in (
            UniversalClassification.REVENUE,
            UniversalClassification.COGS,
            UniversalClassification.OPEX,
            UniversalClassification.OTHER_INCOME,
            UniversalClassification.OTHER_EXPENSE,
        )

    def is_balance_sheet(self) -> bool:
        return self.classification in (
            UniversalClassification.CURRENT_ASSET,
            UniversalClassification.NON_CURRENT_ASSET,
            UniversalClassification.CURRENT_LIABILITY,
            UniversalClassification.NON_CURRENT_LIABILITY,
            UniversalClassification.EQUITY,
        )


@dataclass
class UniversalPeriodBalance:
    """Balance for an account over or as-of a specific period."""
    account_id: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    debit_balance: float = 0.0
    credit_balance: float = 0.0
    net_balance: float = 0.0

    def calculate_net(self, normal_side: NormalBalance = NormalBalance.DEBIT) -> float:
        """Returns net balance formatted according to normal balance side."""
        if normal_side == NormalBalance.DEBIT:
            return round(self.debit_balance - self.credit_balance, 2)
        else:
            return round(self.credit_balance - self.debit_balance, 2)


@dataclass
class UniversalTrialBalance:
    """Canonical Trial Balance representation with exact balance validation."""
    entity_id: str
    as_of_date: date
    currency: str = "USD"
    accounts: Dict[str, UniversalAccount] = field(default_factory=dict)
    balances: Dict[str, UniversalPeriodBalance] = field(default_factory=dict)
    source_metadata: Dict[str, Any] = field(default_factory=dict)

    def add_entry(
        self,
        account: UniversalAccount,
        debit: float = 0.0,
        credit: float = 0.0,
        net: Optional[float] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
    ) -> None:
        """Add an account and its balance to the trial balance."""
        self.accounts[account.account_id] = account
        if net is None:
            # Derive net balance
            if debit > 0 or credit > 0:
                net = (
                    round(debit - credit, 2)
                    if account.normal_balance == NormalBalance.DEBIT
                    else round(credit - debit, 2)
                )
            else:
                net = 0.0

        self.balances[account.account_id] = UniversalPeriodBalance(
            account_id=account.account_id,
            period_start=period_start,
            period_end=period_end or self.as_of_date,
            debit_balance=round(debit, 2),
            credit_balance=round(credit, 2),
            net_balance=round(net, 2),
        )

    def validate_balance(self, tolerance: float = 0.01) -> tuple[bool, float, float, float]:
        """
        Validates double-entry invariant: sum(Debits) == sum(Credits).
        Returns (is_balanced, total_debits, total_credits, discrepancy).
        """
        if not self.balances:
            return False, 0.0, 0.0, 0.0

        total_debits = sum(b.debit_balance for b in self.balances.values())
        total_credits = sum(b.credit_balance for b in self.balances.values())

        # If trial balance provided net balances without separate debit/credit columns.
        # Accept both conventions: signed (debit-positive/credit-negative) sums to zero,
        # and normal-side-positive balances when debits and credits are netted per side.
        if total_debits == 0.0 and total_credits == 0.0:
            signed_sum = sum(b.net_balance for b in self.balances.values())
            normal_sum = 0.0
            for acct_id, bal in self.balances.items():
                acct = self.accounts.get(acct_id)
                if acct is not None and acct.normal_balance == NormalBalance.CREDIT:
                    normal_sum -= bal.net_balance
                else:
                    normal_sum += bal.net_balance
            discrepancy = min(abs(signed_sum), abs(normal_sum))
            return discrepancy <= tolerance, 0.0, 0.0, round(discrepancy, 2)

        discrepancy = round(abs(total_debits - total_credits), 2)
        is_balanced = discrepancy <= tolerance
        return is_balanced, round(total_debits, 2), round(total_credits, 2), discrepancy

    def get_financial_totals(self) -> Dict[str, float]:
        """Calculates summarized statement lines from the canonical trial balance."""
        totals = {
            "revenue": 0.0,
            "cogs": 0.0,
            "gross_profit": 0.0,
            "opex": 0.0,
            "operating_income": 0.0,
            "other_income": 0.0,
            "other_expense": 0.0,
            "net_income": 0.0,
            "current_assets": 0.0,
            "non_current_assets": 0.0,
            "total_assets": 0.0,
            "current_liabilities": 0.0,
            "non_current_liabilities": 0.0,
            "total_liabilities": 0.0,
            "equity": 0.0,
        }

        for acct_id, acct in self.accounts.items():
            bal = self.balances.get(acct_id)
            if not bal:
                continue

            amount = bal.net_balance

            if acct.classification == UniversalClassification.REVENUE:
                totals["revenue"] += amount
            elif acct.classification == UniversalClassification.COGS:
                totals["cogs"] += amount
            elif acct.classification == UniversalClassification.OPEX:
                totals["opex"] += amount
            elif acct.classification == UniversalClassification.OTHER_INCOME:
                totals["other_income"] += amount
            elif acct.classification == UniversalClassification.OTHER_EXPENSE:
                totals["other_expense"] += amount
            elif acct.classification == UniversalClassification.CURRENT_ASSET:
                totals["current_assets"] += amount
            elif acct.classification == UniversalClassification.NON_CURRENT_ASSET:
                totals["non_current_assets"] += amount
            elif acct.classification == UniversalClassification.CURRENT_LIABILITY:
                totals["current_liabilities"] += amount
            elif acct.classification == UniversalClassification.NON_CURRENT_LIABILITY:
                totals["non_current_liabilities"] += amount
            elif acct.classification == UniversalClassification.EQUITY:
                totals["equity"] += amount

        totals["revenue"] = round(totals["revenue"], 2)
        totals["cogs"] = round(totals["cogs"], 2)
        totals["gross_profit"] = round(totals["revenue"] - totals["cogs"], 2)
        totals["opex"] = round(totals["opex"], 2)
        totals["operating_income"] = round(totals["gross_profit"] - totals["opex"], 2)
        totals["other_income"] = round(totals["other_income"], 2)
        totals["other_expense"] = round(totals["other_expense"], 2)
        totals["net_income"] = round(
            totals["operating_income"] + totals["other_income"] - totals["other_expense"], 2
        )
        totals["total_assets"] = round(totals["current_assets"] + totals["non_current_assets"], 2)
        totals["total_liabilities"] = round(totals["current_liabilities"] + totals["non_current_liabilities"], 2)
        totals["equity"] = round(totals["equity"], 2)

        return totals

    def to_dict(self) -> Dict[str, Any]:
        """Serialize trial balance to dictionary."""
        return {
            "entity_id": self.entity_id,
            "as_of_date": self.as_of_date.isoformat(),
            "currency": self.currency,
            "source_metadata": self.source_metadata,
            "accounts": [
                {
                    "account_id": a.account_id,
                    "name": a.name,
                    "account_number": a.account_number,
                    "classification": a.classification.value,
                    "sub_type": a.sub_type,
                    "normal_balance": a.normal_balance.value,
                    "description": a.description,
                    "source_system": a.source_system,
                    "metadata": a.metadata,
                }
                for a in self.accounts.values()
            ],
            "balances": [
                {
                    "account_id": b.account_id,
                    "period_start": b.period_start.isoformat() if b.period_start else None,
                    "period_end": b.period_end.isoformat() if b.period_end else None,
                    "debit_balance": b.debit_balance,
                    "credit_balance": b.credit_balance,
                    "net_balance": b.net_balance,
                }
                for b in self.balances.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UniversalTrialBalance:
        """Deserialize trial balance from dictionary."""
        tb = cls(
            entity_id=data["entity_id"],
            as_of_date=date.fromisoformat(data["as_of_date"]),
            currency=data.get("currency", "USD"),
            source_metadata=data.get("source_metadata", {}),
        )
        for a_data in data.get("accounts", []):
            acct = UniversalAccount(
                account_id=a_data["account_id"],
                name=a_data["name"],
                account_number=a_data.get("account_number"),
                classification=UniversalClassification(a_data.get("classification", "unknown")),
                sub_type=a_data.get("sub_type"),
                normal_balance=NormalBalance(a_data.get("normal_balance", "debit")),
                description=a_data.get("description"),
                source_system=a_data.get("source_system", "generic"),
                metadata=a_data.get("metadata", {}),
            )
            tb.accounts[acct.account_id] = acct

        for b_data in data.get("balances", []):
            tb.balances[b_data["account_id"]] = UniversalPeriodBalance(
                account_id=b_data["account_id"],
                period_start=date.fromisoformat(b_data["period_start"]) if b_data.get("period_start") else None,
                period_end=date.fromisoformat(b_data["period_end"]) if b_data.get("period_end") else None,
                debit_balance=b_data.get("debit_balance", 0.0),
                credit_balance=b_data.get("credit_balance", 0.0),
                net_balance=b_data.get("net_balance", 0.0),
            )
        return tb

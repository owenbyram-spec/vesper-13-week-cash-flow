"""Typed, explicitly accepted workbench inputs; no financial assumptions are defaults."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ContractError(ValueError):
    def __init__(self, code: str, fields: list[str], message: str):
        super().__init__(message)
        self.code, self.fields = code, sorted(set(fields))

    def detail(self):
        return {"code": self.code, "fields": self.fields, "message": str(self)}


class TypedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


Bucket = Literal[
    "revenue", "contra_revenue", "cogs", "opex", "depreciation", "amortization",
    "other_income", "other_expense", "interest_expense", "income_tax_expense",
    "cash", "accounts_receivable", "inventory", "other_current_assets",
    "gross_fixed_assets", "accumulated_depreciation", "intangibles", "goodwill", "other_assets",
    "accounts_payable", "accrued_expenses", "other_current_liabilities",
    "short_term_debt", "long_term_debt", "common_equity", "retained_earnings",
]
BUCKETS = list(Bucket.__args__)


class DebtInput(TypedInput):
    facility_id: str = Field(min_length=1)
    account_id: str | None
    tranche_name: str = Field(min_length=1)
    opening_balance: float = Field(ge=0)
    funded_amount: float = Field(ge=0)
    rate_type: Literal["fixed", "floating"]
    interest_rate: float = Field(ge=0, le=1)
    base_rate: float = Field(ge=0, le=1)
    spread: float = Field(ge=0, le=1)
    amort_years: float | None = Field(ge=0)
    amort_basis: float = Field(ge=0)
    term_years: int | None = Field(ge=1)
    bullet_pct: float = Field(ge=0, le=1)
    revolver_flag: bool
    commitment: float = Field(ge=0)
    commitment_fee_pct: float = Field(ge=0, le=1)
    sweep_participating: bool
    seniority_rank: int = Field(ge=1)
    pik_flag: bool
    fees_pct: float = Field(ge=0, le=1)


class AssetInput(TypedInput):
    category: str = Field(min_length=1)
    depreciation_method: str
    capex_pct_revenue: float = Field(ge=0, le=1)
    useful_life_years: int = Field(ge=1, le=100)
    salvage_value_pct: float = Field(ge=0, le=1)
    beginning_gross: float = Field(ge=0)
    beginning_accum_depr: float = Field(ge=0)
    # Years left to depreciate the opening net book value. Required when the
    # category carries opening accumulated depreciation (never inferred).
    remaining_life_years: int | None = Field(default=None, ge=1, le=100)


class TransactionInput(TypedInput):
    shape: str
    valuation_basis: Literal["fixed_enterprise_value", "ebitda_multiple"]
    entry_enterprise_value: float | None = Field(ge=0)
    entry_ebitda_multiple: float | None = Field(ge=0)
    transaction_fees_pct: float = Field(ge=0, le=1)
    equity_funding_policy: Literal["residual_cash_equity"]
    # How the target's book cash is priced (app/lbo/sources_uses.resolve_deal_basis):
    # cash-free/debt-free by default (the seller keeps the cash; the sponsor funds
    # minimum_operating_cash as a Use), or an equity purchase (EV + cash − debt).
    deal_basis: Literal["cash_free_debt_free", "equity_purchase"] = "cash_free_debt_free"
    minimum_operating_cash: float = Field(default=0.0, ge=0)


class WorkbenchModelInputs(TypedInput):
    # Optional means UNKNOWN, never a zero or accepted business policy.
    tb_presentation: Literal["closed", "unclosed"] | None = None
    account_mappings: dict[str, Bucket] | None = None
    operating_cost_policy: Literal["aggregate_ratio"] | None = None
    other_income_expense_policy: Literal["hold_source_amounts"] | None = None
    ar_dso: float | None = Field(default=None, ge=0, le=3650)
    ap_dpo: float | None = Field(default=None, ge=0, le=3650)
    inventory_dio: float | None = Field(default=None, ge=0, le=3650)
    other_ca_pct: float | None = Field(default=None, ge=0)
    accrued_pct: float | None = Field(default=None, ge=0)
    other_liab_pct: float | None = Field(default=None, ge=0)
    tax_rate: float | None = Field(default=None, ge=0, le=1)
    distribution_pct_of_fcf: float | None = Field(default=None, ge=0, le=1)
    sweep_pct: float | None = Field(default=None, ge=0, le=1)
    sweep_pct_by_year: list[float] | None = None
    debt_facilities: list[DebtInput] | None = None
    assets: list[AssetInput] | None = None
    intangible_amort_years: int | None = Field(default=None, ge=0, le=100)
    goodwill_book_amort_years: int | None = Field(default=None, ge=0, le=10)
    goodwill_tax_amort_years: int | None = Field(default=None, ge=1, le=100)
    section_197_election: bool | None = None
    # Current transaction engine cannot represent debt payoff / retained vs new
    # funding without ambiguity. These shapes are refused, never approximated.
    transaction: TransactionInput | None = None

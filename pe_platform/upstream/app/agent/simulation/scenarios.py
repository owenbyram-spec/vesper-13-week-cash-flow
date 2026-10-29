"""Comprehensive Synthetic Financial Scenarios & Ground-Truth Oracles.

Provides parameterized financial environments with exact mathematical solutions
for stress-testing frontier and economy models on the Race Track Harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .oracle import ExpectedJournalEntry, ScenarioOracle


@dataclass(frozen=True)
class SimulationScenario:
    id: str
    name: str
    category: str
    task_prompt: str
    context_data: Dict[str, Any]
    oracle: ScenarioOracle
    # Names of registered assistant tools (app/agent/runtime/tool_registry.py) only.
    expected_tools: List[str] = field(default_factory=list)
    risk_tier: str = "high"
    version: str = "2026-09-26.2"
    metric_schema: Dict[str, str] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Multi-Entity Intercompany Elimination Scenario
# ─────────────────────────────────────────────────────────────────────────────
ORACLE_INTERCOMPANY = ScenarioOracle(
    scenario_id="intercompany_elimination_mismatch",
    expected_journal_entries=[
        ExpectedJournalEntry(
            account_number="2100",
            account_name="Intercompany Accounts Payable",
            debit=Decimal("150000.00"),
            credit=Decimal("0.00"),
            entity_id="entity-opco-east",
        ),
        ExpectedJournalEntry(
            account_number="1150",
            account_name="Intercompany Accounts Receivable",
            debit=Decimal("0.00"),
            credit=Decimal("150000.00"),
            entity_id="entity-parent-corp",
        ),
    ],
    expected_numeric_metrics={
        "elimination_amount": Decimal("150000.00"),
        "pre_elimination_imbalance": Decimal("150000.00"),
        "post_elimination_imbalance": Decimal("0.00"),
    },
)

SCENARIO_INTERCOMPANY = SimulationScenario(
    id="intercompany_elimination_mismatch",
    name="Intercompany Management Fee Elimination & Cutoff Reconciliation",
    category="intercompany_consolidation",
    task_prompt=(
        "Review the September cross-entity transactions between Parent Corp (HoldCo) and OpCo East. "
        "Parent Corp billed OpCo East $150,000 for Q3 shared services. OpCo East booked the invoice in AP, "
        "but Parent Corp recorded the receivable in October due to billing cutoff. "
        "Identify the uneliminated intercompany imbalance and propose the exact balancing topside elimination journal entry."
    ),
    context_data={
        "entities": [
            {"id": "entity-parent-corp", "name": "Apex Holdings Parent Corp", "currency": "USD"},
            {"id": "entity-opco-east", "name": "Apex Services OpCo East", "currency": "USD"},
        ],
        "trial_balance_opco": {
            "2100": {"name": "Intercompany Accounts Payable", "balance": -150000.00},
            "6050": {"name": "Shared Management Fee Expense", "balance": 150000.00},
        },
        "trial_balance_parent": {
            "1150": {"name": "Intercompany Accounts Receivable", "balance": 0.00},  # unrecorded in Sep
        },
        "cutoff_date": "2026-09-30",
        "consolidation_assumption": "A separate authorized cutoff correction posts the missing parent receivable before this elimination. Propose ONLY AP/AR elimination against the corrected balances, not that separate correction.",
    },
    metric_schema={"elimination_amount": "Amount of AP/AR eliminated", "pre_elimination_imbalance": "Absolute September counterparty mismatch before cutoff correction", "post_elimination_imbalance": "Remaining AP/AR after separately posted cutoff correction and this elimination"},
    oracle=ORACLE_INTERCOMPANY,
    expected_tools=["get_financial_data", "create_journal_entry"],
    risk_tier="high",
)


# ─────────────────────────────────────────────────────────────────────────────
# 2. ASC 606 Enterprise Multi-Year SaaS Revenue Recognition
# ─────────────────────────────────────────────────────────────────────────────
ORACLE_ASC606 = ScenarioOracle(
    scenario_id="asc606_enterprise_saas_schedule",
    expected_journal_entries=[
        ExpectedJournalEntry(
            account_number="2300",
            account_name="Deferred Revenue - Software",
            debit=Decimal("35000.00"),
            credit=Decimal("0.00"),
        ),
        ExpectedJournalEntry(
            account_number="4010",
            account_name="Subscription Revenue",
            debit=Decimal("0.00"),
            credit=Decimal("33333.33"),
        ),
        ExpectedJournalEntry(
            account_number="4020",
            account_name="Implementation Revenue",
            debit=Decimal("0.00"),
            credit=Decimal("1666.67"),
        ),
    ],
    expected_numeric_metrics={
        "monthly_subscription_revenue": Decimal("33333.33"),
        "monthly_implementation_revenue": Decimal("1666.67"),
        "total_monthly_recognized_revenue": Decimal("35000.00"),
        "remaining_deferred_revenue": Decimal("1225000.00"),
    },
)

SCENARIO_ASC606 = SimulationScenario(
    id="asc606_enterprise_saas_schedule",
    name="ASC 606 Multi-Element Enterprise SaaS & Implementation Schedule",
    category="revenue_recognition_asc606",
    task_prompt=(
        "Customer 'Global Corp' executed a 36-month enterprise subscription for $1,200,000 paid upfront on Sep 1, 2026, "
        "plus a $60,000 non-refundable implementation fee. Under ASC 606, implementation does not have standalone value "
        "and must be recognized ratably over the 36-month subscription term. "
        "Calculate the monthly recognized revenue for September 2026 and draft the adjusting journal entry from Deferred Revenue."
    ),
    context_data={
        "chart_of_accounts": {"2300": "Deferred Revenue - Software", "4010": "Subscription Revenue", "4020": "Implementation Revenue"},
        "rounding": "Round each monthly component to cents; this fixture considers the first month only.",
        "customer": "Global Corp",
        "contract_term_months": 36,
        "subscription_fee_total": 1200000.00,
        "implementation_fee_total": 60000.00,
        "start_date": "2026-09-01",
        "end_date": "2029-08-31",
        "current_deferred_revenue_balance": 1260000.00,
    },
    metric_schema={"monthly_subscription_revenue": "Subscription fee divided by term, rounded to cents", "monthly_implementation_revenue": "Implementation fee divided by term, rounded to cents", "total_monthly_recognized_revenue": "Total recognized in first month", "remaining_deferred_revenue": "Opening deferred less first month revenue"},
    oracle=ORACLE_ASC606,
    # No registered tool computes a revenue schedule from supplied terms; the
    # calculation is the answer, and the adjusting entry is drafted for review.
    expected_tools=["create_journal_entry"],
    risk_tier="high",
)


# ─────────────────────────────────────────────────────────────────────────────
# 3. LBO Debt Waterfall & Financial Covenant Compliance
# ─────────────────────────────────────────────────────────────────────────────
ORACLE_COVENANTS = ScenarioOracle(
    scenario_id="lbo_covenant_headroom",
    expected_journal_entries=[],  # Read-only calculation
    expected_numeric_metrics={
        "total_debt": Decimal("16000000.00"),
        "net_debt": Decimal("14500000.00"),
        "ttm_ebitda": Decimal("4200000.00"),
        "leverage_ratio": Decimal("3.45"),
        "senior_leverage_ratio": Decimal("2.50"),
        "total_annual_interest": Decimal("1500000.00"),
        "covenant_headroom": Decimal("0.30"),
    },
)

SCENARIO_COVENANTS = SimulationScenario(
    id="lbo_covenant_headroom",
    name="LBO Debt Schedule, SOFR Interest, & Covenant Headroom Analysis",
    category="debt_covenants_lbo",
    task_prompt=(
        "Evaluate compliance and headroom for the Senior Credit Agreement as of Q3 2026. "
        "Debt structure: $12,000,000 Term Loan A at 8.50% annual interest, and $4,000,000 Subordinated Debt at 12.00%. "
        "Cash balance is $1,500,000. TTM Adjusted EBITDA is $4,200,000. "
        "Covenant specifies Maximum Total Net Leverage of 3.75x. "
        "Compute Total Debt, Net Debt, Net Leverage Ratio, Annual Total Interest, and Covenant Headroom."
    ),
    context_data={
        "term_loan_a": {"principal": 12000000.00, "rate": 0.085},
        "sub_debt": {"principal": 4000000.00, "rate": 0.12},
        "cash": 1500000.00,
        "ttm_adjusted_ebitda": 4200000.00,
        "covenants": {
            "max_net_leverage": 3.75,
            "min_interest_coverage": 2.50,
        },
    },
    metric_schema={"total_debt": "All debt principal", "net_debt": "Total debt less cash", "ttm_ebitda": "Given adjusted EBITDA", "leverage_ratio": "Net debt divided by EBITDA, rounded to 2 decimals", "senior_leverage_ratio": "Term Loan A less all cash, divided by EBITDA, rounded to 2 decimals", "total_annual_interest": "Sum principal times annual rate", "covenant_headroom": "Maximum net leverage less computed net leverage, rounded to 2 decimals"},
    oracle=ORACLE_COVENANTS,
    # No registered tool computes covenant ratios from supplied terms; the
    # calculation is the answer.
    expected_tools=["get_financial_data"],
    risk_tier="high",
)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Noisy Bank Feed Reconciliation & Fee Netting
# ─────────────────────────────────────────────────────────────────────────────
ORACLE_BANK_RECON = ScenarioOracle(
    scenario_id="noisy_bank_feed_reconciliation",
    expected_journal_entries=[
        ExpectedJournalEntry(
            account_number="6010",
            account_name="Cloud Infrastructure & Hosting",
            debit=Decimal("14850.00"),
            credit=Decimal("0.00"),
        ),
        ExpectedJournalEntry(
            account_number="6080",
            account_name="Bank & Merchant Processing Fees",
            debit=Decimal("150.00"),
            credit=Decimal("0.00"),
        ),
        ExpectedJournalEntry(
            account_number="1010",
            account_name="Operating Checking Account",
            debit=Decimal("0.00"),
            credit=Decimal("15000.00"),
        ),
    ],
    expected_numeric_metrics={
        "gross_bill_amount": Decimal("14850.00"),
        "wire_processing_fee": Decimal("150.00"),
        "total_cash_outflow": Decimal("15000.00"),
    },
)

SCENARIO_BANK_RECON = SimulationScenario(
    id="noisy_bank_feed_reconciliation",
    name="Noisy Wire Feed Reconciliation with Merchant Fee Split",
    category="bank_reconciliation",
    task_prompt=(
        "Reconcile messy bank transaction 'WIRE OUT INT'L SVC CHARGE - AMZN CLOUD CORP 09-28-26 REF#48192'. "
        "Total debited from Operating Checking is $15,000.00. "
        "Invoice analysis reveals AWS Cloud usage was $14,850.00, and the bank deducted a $150.00 international wire fee. "
        "Draft the balanced three-legged journal entry splitting hosting expense and bank fees."
    ),
    context_data={
        "chart_of_accounts": {"6010": "Cloud Infrastructure & Hosting", "6080": "Bank & Merchant Processing Fees", "1010": "Operating Checking Account"},
        "bank_transaction": {
            "date": "2026-09-28",
            "raw_text": "WIRE OUT INT'L SVC CHARGE - AMZN CLOUD CORP 09-28-26 REF#48192",
            "amount": -15000.00,
            "bank_account": "1010 Operating Checking",
        },
        "vendor_invoice": {
            "vendor": "Amazon Web Services",
            "invoice_number": "INV-AWS-9921",
            "amount": 14850.00,
            "fee": 150.00,
        },
    },
    metric_schema={"gross_bill_amount": "Hosting charge", "wire_processing_fee": "Bank fee", "total_cash_outflow": "Combined cash paid"},
    oracle=ORACLE_BANK_RECON,
    # The registered reconciliation tools act on an open reconciliation session;
    # this scenario supplies the bank line directly, so only the entry draft is expected.
    expected_tools=["create_journal_entry"],
    risk_tier="standard",
)


ALL_SIMULATION_SCENARIOS = [
    SCENARIO_INTERCOMPANY,
    SCENARIO_ASC606,
    SCENARIO_COVENANTS,
    SCENARIO_BANK_RECON,
]


# Absolute tolerances are explicit per output field, not a universal 1% pass.
for _scenario in ALL_SIMULATION_SCENARIOS:
    _scenario.oracle.metric_tolerances = {key: Decimal("0.005") for key in _scenario.metric_schema}


def scenario_by_id(scenario_id: str) -> Optional[SimulationScenario]:
    for s in ALL_SIMULATION_SCENARIOS:
        if s.id == scenario_id:
            return s
    return None

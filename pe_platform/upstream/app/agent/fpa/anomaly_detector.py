"""
app/agent/fpa/anomaly_detector.py
─────────────────────────────────
Deterministic and heuristic anomaly scanner for General Ledgers & Trial Balances.
Detects out-of-balance entries, abnormal account balances, classification ambiguities,
and candidate EBITDA add-backs for PE deal underwriting.
"""
from __future__ import annotations

from typing import List

from ...fpa.uaf.schema import (
    NormalBalance,
    UniversalClassification,
    UniversalTrialBalance,
)
from .interview_state import AnomalyRecord

# Patterns indicating potential EBITDA add-backs / pro-forma adjustments
ADDBACK_KEYWORDS = [
    ("executive aircraft", "Executive aircraft / flight expenses (owner perk)"),
    ("airplane", "Aircraft / aviation expense (owner perk)"),
    ("perk", "Discretionary executive perquisite"),
    ("owner compensation", "Above-market owner compensation adjustment"),
    ("shareholder personal", "Shareholder personal expenses"),
    ("transaction advisory", "Transaction / M&A advisory fees (non-recurring)"),
    ("acquisition legal", "Deal-related acquisition legal expenses"),
    ("severance", "One-time executive / employee severance restructuring"),
    ("restructuring", "Non-recurring organizational restructuring charges"),
    ("litigation settlement", "One-time legal / litigation settlement"),
    ("lawsuit", "Non-recurring litigation expenses"),
    ("management fee", "Sponsor / management monitoring fee (normalized at close)"),
]

# Patterns indicating classification ambiguity (revenue vs opex vs cogs)
AMBIGUOUS_KEYWORDS = [
    ("reimbursable", "Reimbursable expenses: Could be Gross Revenue or Contra-Expense"),
    ("shipping pass", "Shipping / freight pass-through: Gross Revenue vs Net Contra-COGS"),
    ("freight out", "Outbound freight: COGS vs Selling/Marketing OpEx"),
    ("contractor software", "Contractor software labor: Operating expense vs Capitalized Capex"),
    ("consulting services", "Consulting services: Direct COGS vs General Overhead"),
    ("royalty income", "Royalty income: Core Operating Revenue vs Non-Operating Other Income"),
]

# Patterns indicating debt facilities requiring capital structure resolution
DEBT_KEYWORDS = [
    ("seller note", "Subordinated Seller Note: Refinance at close vs Rollover into capital structure"),
    ("shareholder loan", "Shareholder Loan / Note: Equity rollover vs Cash paydown"),
    ("promissory note", "Promissory note facility"),
    ("line of credit", "Existing Revolving Line of Credit: Payoff vs Roll"),
    ("term loan", "Senior Term Loan: Existing debt to refinance"),
    ("mezzanine", "Mezzanine debt tranche"),
]


def detect_tb_anomalies(tb: UniversalTrialBalance) -> List[AnomalyRecord]:
    """
    Scans UniversalTrialBalance for accounting anomalies and deal-modeling decision points.
    Returns a structured list of AnomalyRecord objects.
    """
    anomalies: List[AnomalyRecord] = []

    # 1. Double-Entry Balance Invariant
    is_balanced, debits, credits, discrepancy = tb.validate_balance()
    if not is_balanced:
        anomalies.append(AnomalyRecord(
            anomaly_id="anomaly_tb_unbalanced",
            anomaly_type="unbalanced_trial_balance",
            account_id="system_trial_balance",
            account_name="General Ledger Invariant",
            dollar_amount=discrepancy,
            description=f"Trial balance is out of balance by ${discrepancy:,.2f} (Debits: ${debits:,.2f}, Credits: ${credits:,.2f})",
            severity="critical",
        ))

    for acct_id, acct in tb.accounts.items():
        bal = tb.balances.get(acct_id)
        if not bal:
            continue

        net = bal.net_balance
        name_lower = acct.name.lower()
        amount_mag = abs(net) if net != 0 else max(bal.debit_balance, bal.credit_balance)

        # 2. Abnormal Balance Side Check
        if acct.normal_balance == NormalBalance.DEBIT and net < -100.0:
            anomalies.append(AnomalyRecord(
                anomaly_id=f"anomaly_abnormal_{acct_id}",
                anomaly_type="abnormal_balance",
                account_id=acct_id,
                account_name=acct.name,
                dollar_amount=abs(net),
                description=f"Account '{acct.name}' normally carries a debit balance but shows a net credit balance of ${abs(net):,.2f}",
                severity="medium" if abs(net) > 5000 else "low",
            ))
        elif acct.normal_balance == NormalBalance.CREDIT and net < -100.0:
            anomalies.append(AnomalyRecord(
                anomaly_id=f"anomaly_abnormal_{acct_id}",
                anomaly_type="abnormal_balance",
                account_id=acct_id,
                account_name=acct.name,
                dollar_amount=abs(net),
                description=f"Account '{acct.name}' normally carries a credit balance but shows a net debit balance of ${abs(net):,.2f}",
                severity="medium" if abs(net) > 5000 else "low",
            ))

        # 3. Unknown Classification
        if acct.classification == UniversalClassification.UNKNOWN and amount_mag > 0:
            anomalies.append(AnomalyRecord(
                anomaly_id=f"anomaly_unknown_class_{acct_id}",
                anomaly_type="unknown_classification",
                account_id=acct_id,
                account_name=acct.name,
                dollar_amount=amount_mag,
                description=f"Account '{acct.name}' has an unassigned financial statement classification (${amount_mag:,.2f})",
                severity="high" if amount_mag > 10000 else "medium",
            ))

        # 4. Candidate EBITDA Add-Backs
        for kw, desc in ADDBACK_KEYWORDS:
            if kw in name_lower and amount_mag > 500.0:
                anomalies.append(AnomalyRecord(
                    anomaly_id=f"anomaly_addback_{acct_id}",
                    anomaly_type="candidate_addback",
                    account_id=acct_id,
                    account_name=acct.name,
                    dollar_amount=amount_mag,
                    description=f"{desc}: '{acct.name}' (${amount_mag:,.2f})",
                    severity="high",
                ))
                break

        # 5. Classification Ambiguities
        for kw, desc in AMBIGUOUS_KEYWORDS:
            if kw in name_lower and amount_mag > 1000.0:
                anomalies.append(AnomalyRecord(
                    anomaly_id=f"anomaly_ambiguous_{acct_id}",
                    anomaly_type="ambiguous_classification",
                    account_id=acct_id,
                    account_name=acct.name,
                    dollar_amount=amount_mag,
                    description=f"{desc}: '{acct.name}' (${amount_mag:,.2f})",
                    severity="medium",
                ))
                break

        # 6. Existing Debt Tranches
        for kw, desc in DEBT_KEYWORDS:
            if kw in name_lower and amount_mag > 5000.0:
                anomalies.append(AnomalyRecord(
                    anomaly_id=f"anomaly_debt_{acct_id}",
                    anomaly_type="debt_tranche",
                    account_id=acct_id,
                    account_name=acct.name,
                    dollar_amount=amount_mag,
                    description=f"{desc}: '{acct.name}' (${amount_mag:,.2f})",
                    severity="high",
                ))
                break

    return anomalies

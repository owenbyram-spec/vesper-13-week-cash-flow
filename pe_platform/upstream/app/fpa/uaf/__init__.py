"""
app/fpa/uaf/__init__.py
───────────────────────
Universal Accounting Fabric (UAF) module.
Provides the vendor-agnostic trial balance representation and the strict
CSV / Excel trial-balance ingestor the FP&A workbench uses. Trial balances also
arrive as structured UniversalTrialBalance JSON (POST /fpa/workbench/sessions/ingest-tb).
"""
from .schema import (
    NormalBalance,
    UniversalAccount,
    UniversalClassification,
    UniversalPeriodBalance,
    UniversalTrialBalance,
)
from .excel_ingest import (
    TrialBalanceParseError,
    TrialBalancePeriodError,
    ingest_csv_trial_balance,
    ingest_excel_trial_balance,
    parse_amount,
    parse_tabular_rows,
)

__all__ = [
    "NormalBalance",
    "UniversalAccount",
    "UniversalClassification",
    "UniversalPeriodBalance",
    "UniversalTrialBalance",
    "TrialBalanceParseError",
    "TrialBalancePeriodError",
    "ingest_csv_trial_balance",
    "ingest_excel_trial_balance",
    "parse_amount",
    "parse_tabular_rows",
]

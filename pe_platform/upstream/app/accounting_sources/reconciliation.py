"""Deterministic dataset qualification; balanced postings alone never qualify.

The trusted persisted writer must reconstruct these inputs from retained pages
and independent reports. This calculation authenticates no caller-supplied check.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext
import re
from typing import Iterable
from itertools import islice

from .contracts import SourceContractError, SourceLine, SourceScope, canonical_digest, exact_decimal


@dataclass(frozen=True)
class AccountTieOut:
    account_id: str
    opening: Decimal
    closing: Decimal
    report_debit: Decimal
    report_credit: Decimal
    report_digest: str
    scope_digest: str

    def __post_init__(self) -> None:
        if not self.account_id or not re.fullmatch(r"[a-f0-9]{64}", self.report_digest) or not re.fullmatch(r"[a-f0-9]{64}", self.scope_digest):
            raise SourceContractError("independent scoped report identity required")
        for value in (self.opening, self.closing, self.report_debit, self.report_credit):
            if not isinstance(value, Decimal):
                raise SourceContractError("tie-outs require Decimal, never float")
            exact_decimal(value)
        if self.report_debit < 0 or self.report_credit < 0:
            raise SourceContractError("report debit/credit cannot be negative")


@dataclass(frozen=True)
class QualificationChecks:
    pagination_complete: bool = False
    mapping_complete: bool = False
    required_history_complete: bool = False
    required_datasets_complete: bool = False
    independent_reports_complete: bool = False
    drift_checked: bool = False
    source_changed: bool = True
    successful_zero_report: bool = False
    point_in_time_required: bool = False
    point_in_time_proven: bool = False
    observed_start: datetime | None = None
    observed_end: datetime | None = None
    reported_count: int | None = None
    reported_debit: Decimal | None = None
    reported_credit: Decimal | None = None
    rounding_quantum: Decimal = Decimal("0")
    additional_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualificationResult:
    qualified: bool
    failures: tuple[str, ...]
    debit: Decimal
    credit: Decimal
    record_count: int
    reported_count: int | None
    reconciliation_digest: str
    residuals: tuple[tuple[str, Decimal, Decimal, Decimal], ...] = ()


def reconcile(scope: SourceScope, lines: Iterable[SourceLine], accounts: tuple[AccountTieOut, ...],
              checks: QualificationChecks) -> QualificationResult:
    rows = tuple(islice(lines, 100001))
    if len(rows) > 100000 or len(accounts) > 100000:
        raise SourceContractError("unbounded qualification input")
    if not isinstance(checks, QualificationChecks) or any(not isinstance(row, SourceLine) for row in rows):
        raise SourceContractError("typed qualification observations required")
    if type(accounts) is not tuple or any(not isinstance(item, AccountTieOut) for item in accounts):
        raise SourceContractError("typed immutable account reports required")
    boolean_names = ("pagination_complete", "mapping_complete", "required_history_complete",
                     "required_datasets_complete", "independent_reports_complete", "drift_checked",
                     "source_changed", "successful_zero_report", "point_in_time_required", "point_in_time_proven")
    if any(type(getattr(checks, key)) is not bool for key in boolean_names):
        raise SourceContractError("qualification flags must be explicit booleans")
    failures = list(checks.additional_failures)
    for name in boolean_names[:6]:
        if not getattr(checks, name):
            failures.append(name + "_unproven")
    if checks.source_changed:
        failures.append("source_changed_mid_read")
    if checks.point_in_time_required and not checks.point_in_time_proven:
        failures.append("required_point_in_time_unproven")
    start, end = checks.observed_start, checks.observed_end
    if start is None or end is None or start.tzinfo is None or end.tzinfo is None or start > end:
        failures.append("source_observation_interval_missing")
    if checks.reported_count is None or type(checks.reported_count) is not int or checks.reported_count < 0:
        failures.append("reported_count_missing")
    elif checks.reported_count != len(rows):
        failures.append("reported_count_mismatch")
    if not rows and not checks.successful_zero_report:
        failures.append("empty_is_not_proven_zero")
    if any(not scope.period_start <= row.posting_date <= scope.period_end or row.currency != scope.currency for row in rows):
        failures.append("period_or_currency_mismatch")
    identities = [(row.source_id, row.occurrence) for row in rows]
    if len(set(identities)) != len(identities):
        failures.append("duplicate_source_identity")
    account_map = {item.account_id: item for item in accounts}
    if len(account_map) != len(accounts):
        failures.append("ambiguous_account_mapping")
    expected_scope_digest = canonical_digest(scope)
    if any(item.scope_digest != expected_scope_digest for item in accounts):
        failures.append("report_scope_mismatch")
    if rows and not accounts:
        failures.append("independent_account_report_missing")
    if any(row.account_id not in account_map for row in rows):
        failures.append("missing_account_mapping")
    quantum = exact_decimal(checks.rounding_quantum)
    if quantum < 0 or quantum > Decimal("0.01"):
        raise SourceContractError("rounding exceeds this reviewed exact reconciliation profile")
    residuals = []
    with localcontext(Context(prec=60)):
        debit = sum((row.debit for row in rows), Decimal("0"))
        credit = sum((row.credit for row in rows), Decimal("0"))
        account_totals: dict[str, tuple[Decimal, Decimal]] = {}
        for row in rows:
            prior_debit, prior_credit = account_totals.get(row.account_id, (Decimal("0"), Decimal("0")))
            account_totals[row.account_id] = (prior_debit + row.debit, prior_credit + row.credit)
        for name, observed in (("debit", debit), ("credit", credit)):
            expected = getattr(checks, "reported_" + name)
            if expected is None:
                failures.append("independent_total_" + name + "_missing")
            elif abs(observed - exact_decimal(expected)) > quantum:
                failures.append("independent_total_" + name + "_mismatch")
        for account in accounts:
            account_debit, account_credit = account_totals.get(account.account_id, (Decimal("0"), Decimal("0")))
            close_residual = account.opening + account_debit - account_credit - account.closing
            debit_residual, credit_residual = account_debit - account.report_debit, account_credit - account.report_credit
            residuals.append((account.account_id, close_residual, debit_residual, credit_residual))
            if any(abs(value) > quantum for value in (close_residual, debit_residual, credit_residual)):
                failures.append("account_tieout:" + account.account_id)
    # Deterministic evidence retains actual residuals; never write a balancing plug.
    unique_failures = tuple(sorted(set(failures)))
    digest = canonical_digest({"scope": scope, "records": rows, "accounts": accounts,
                               "checks": checks, "residuals": residuals, "failures": unique_failures})
    return QualificationResult(not unique_failures, unique_failures, debit, credit, len(rows),
                               checks.reported_count, digest, tuple(residuals))

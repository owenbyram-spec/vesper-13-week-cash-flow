"""Strict scorer for synthetic fixtures; fixture truth still needs domain review."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from typing import Any


@dataclass(frozen=True)
class ExpectedJournalEntry:
    account_number: str
    account_name: str
    debit: Decimal
    credit: Decimal
    entity_id: str | None = None


@dataclass
class OracleVerificationResult:
    scenario_id: str
    passed: bool
    score: float
    debit_credit_balanced: bool
    numeric_accuracy: float
    account_precision: float
    invariant_violations: list[str] = field(default_factory=list)
    detailed_discrepancies: list[str] = field(default_factory=list)


def _number(value: Any) -> Decimal:
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 128:
        raise ValueError("A bounded finite number is required")
    result = Decimal(str(value))
    if not result.is_finite() or abs(result.as_tuple().exponent) > 100 or (result and abs(result.adjusted()) > 50):
        raise ValueError("A bounded finite number is required")
    return result


@dataclass
class ScenarioOracle:
    scenario_id: str
    expected_journal_entries: list[ExpectedJournalEntry] = field(default_factory=list)
    expected_numeric_metrics: dict[str, Decimal] = field(default_factory=dict)
    materiality_threshold: Decimal = Decimal("0.01")
    metric_tolerances: dict[str, Decimal] = field(default_factory=dict)

    def verify_proposal(self, proposed_entries: list[dict[str, Any]], proposed_metrics: dict[str, Any] | None = None) -> OracleVerificationResult:
        # Bounds admit at most 128 digits per value; this precision also preserves
        # cancellations and cent differences across all 128 journal legs.
        with localcontext() as context:
            context.prec = 256
            try:
                return self._verify_bounded(proposed_entries, proposed_metrics)
            except DecimalException:
                return OracleVerificationResult(self.scenario_id, False, 0.0, False, 0.0, 0.0, ["Invalid numeric operation"], [])

    def _verify_bounded(self, proposed_entries, proposed_metrics):
        violations: list[str] = []
        discrepancies: list[str] = []
        parsed = []
        try:
            if type(proposed_entries) is not list or len(proposed_entries) > 128:
                raise ValueError("entries must be a bounded list")
            metrics = {} if proposed_metrics is None else proposed_metrics
            if type(metrics) is not dict or len(metrics) > 256 or any(type(k) is not str for k in metrics):
                raise ValueError("metrics must be a bounded object")
            metrics = {key: _number(value) for key, value in metrics.items()}
            for entry in proposed_entries:
                if type(entry) is not dict or not {"account_number", "debit", "credit"} <= entry.keys():
                    raise ValueError("every leg requires account_number, debit and credit")
                if set(entry) - {"account_number", "account_name", "debit", "credit", "entity_id"}:
                    raise ValueError("unexpected journal leg fields")
                account = entry["account_number"]
                entity = entry.get("entity_id")
                if type(account) is not str or not account or len(account) > 128 or (entity is not None and type(entity) is not str):
                    raise ValueError("account and entity IDs must be strings")
                debit, credit = _number(entry["debit"]), _number(entry["credit"])
                if debit < 0 or credit < 0 or (debit == 0) == (credit == 0):
                    raise ValueError("each leg requires exactly one positive debit or credit")
                leg = (account, entity, debit, credit)
                if leg in parsed:
                    raise ValueError("duplicate journal leg")
                parsed.append(leg)
        except (ValueError, TypeError, InvalidOperation) as exc:
            return OracleVerificationResult(self.scenario_id, False, 0.0, False, 0.0, 0.0, [str(exc)], [])

        total_debits = sum((leg[2] for leg in parsed), Decimal(0))
        total_credits = sum((leg[3] for leg in parsed), Decimal(0))
        balanced = abs(total_debits - total_credits) <= self.materiality_threshold
        if not balanced:
            violations.append("Debits and credits do not balance")
        unmatched = list(parsed)
        matches = 0
        for expected in self.expected_journal_entries:
            index = next((i for i, leg in enumerate(unmatched)
                if leg[0] == expected.account_number
                and (expected.entity_id is None or leg[1] == expected.entity_id)
                and abs(leg[2] - expected.debit) <= self.materiality_threshold
                and abs(leg[3] - expected.credit) <= self.materiality_threshold), None)
            if index is None:
                discrepancies.append(f"Missing or wrong leg: {expected.account_number}, entity={expected.entity_id}")
            else:
                matches += 1
                unmatched.pop(index)
        if unmatched:
            violations.append(f"Unexpected journal legs: {len(unmatched)}")
        metric_matches = 0
        for key, expected in self.expected_numeric_metrics.items():
            tolerance = self.metric_tolerances.get(key, self.materiality_threshold)
            if key not in metrics:
                discrepancies.append(f"Missing required metric: {key}")
            elif abs(metrics[key] - expected) > tolerance:
                discrepancies.append(f"Metric mismatch: {key}")
            else:
                metric_matches += 1
        if not self.expected_journal_entries and not self.expected_numeric_metrics:
            violations.append("Oracle has no required observations")
        account_precision = matches / max(len(parsed), len(self.expected_journal_entries), 1) if parsed or self.expected_journal_entries else 1.0
        numeric_accuracy = metric_matches / len(self.expected_numeric_metrics) if self.expected_numeric_metrics else 1.0
        score = round(account_precision * 0.6 + numeric_accuracy * 0.4, 3)
        return OracleVerificationResult(self.scenario_id, balanced and not violations and not discrepancies,
            score if balanced else min(score, 0.5), balanced, numeric_accuracy, account_precision, violations, discrepancies)

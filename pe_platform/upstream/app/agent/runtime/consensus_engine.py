"""Optional dual-model comparison utility, not the live chat execution loop.

Agreement measures consistency between supplied model outputs; it does not prove
source truth, accounting correctness or authorization. No universal financial
write gate invokes this utility.

Executes two independent models concurrently, normalizes their financial proposals
and calculated metrics, and computes deterministic consensus or structured
discrepancies. Journal legs pair only on account number AND entity; every other
asserted field, nested values included, is compared; non-finite numbers never agree.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.agent.performance import estimate_model_cost_usd

logger = logging.getLogger(__name__)


class ConsensusDiscrepancyType(str, Enum):
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_LEG = "MISSING_LEG"
    DIRECTION_MISMATCH = "DIRECTION_MISMATCH"
    METRIC_MISMATCH = "METRIC_MISMATCH"
    TOOL_MISMATCH = "TOOL_MISMATCH"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"


class DiscrepancyMateriality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class ConsensusDiscrepancy:
    field_name: str
    discrepancy_type: ConsensusDiscrepancyType
    materiality: DiscrepancyMateriality
    model_a_name: str
    model_a_value: Any
    model_b_name: str
    model_b_value: Any
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field_name": self.field_name,
            "discrepancy_type": self.discrepancy_type.value,
            "materiality": self.materiality.value,
            "model_a_name": self.model_a_name,
            "model_a_value": str(self.model_a_value),
            "model_b_name": self.model_b_name,
            "model_b_value": str(self.model_b_value),
            "explanation": self.explanation,
        }


class ConsensusStatus(str, Enum):
    UNANIMOUS = "UNANIMOUS"
    DIVERGENT = "DIVERGENT"
    MODEL_A_FAILED = "MODEL_A_FAILED"
    MODEL_B_FAILED = "MODEL_B_FAILED"
    BOTH_FAILED = "BOTH_FAILED"


@dataclass
class ConsensusResult:
    status: ConsensusStatus
    agreement_score: float  # 0.0 to 1.0
    discrepancies: List[ConsensusDiscrepancy] = field(default_factory=list)
    model_a_name: str = ""
    model_b_name: str = ""
    model_a_output: Any = None
    model_b_output: Any = None
    unified_proposal: Optional[Dict[str, Any]] = None
    execution_telemetry: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "agreement_score": round(self.agreement_score, 4),
            "discrepancies": [d.to_dict() for d in self.discrepancies],
            "model_a_name": self.model_a_name,
            "model_b_name": self.model_b_name,
            "model_a_output": self.model_a_output,
            "model_b_output": self.model_b_output,
            "unified_proposal": self.unified_proposal,
            "execution_telemetry": self.execution_telemetry,
        }


def _try_decimal(val: Any) -> Optional[Decimal]:
    """Parse a numeric value. Returns None when the value is not a number."""
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return None


# Decimal arithmetic outside this exponent range overflows the default context, so
# such a value is as unusable for comparison as NaN or Infinity.
_MAX_DECIMAL_EXPONENT = 1000


def _usable_decimal(dec: Decimal) -> bool:
    return dec.is_finite() and abs(dec.adjusted()) <= _MAX_DECIMAL_EXPONENT


def _finite_decimal(val: Any) -> Optional[Decimal]:
    """Parse a finite, in-range number; NaN/sNaN/Infinity are not numbers here, never zero."""
    dec = _try_decimal(val)
    return dec if dec is not None and _usable_decimal(dec) else None


_LEG_LIST_KEYS = ("entries", "lines", "journal_entries", "legs")

# Leg keys compared as the leg's entity and amounts. Of the account keys, only the
# one that supplied the account number (account_number, else account_id) and the
# one that supplied the label (account_name, else account) are the leg's identity;
# every other leg key, an unused account alias included, is an asserted field
# compared between paired legs.
_LEG_AMOUNT_ENTITY_KEYS = ("debit", "credit", "entity_id")


def _leg_list_key(raw_proposal: Any) -> Optional[str]:
    """The key whose list holds the journal legs (first non-empty alias), if any."""
    if not isinstance(raw_proposal, dict):
        return None
    for key in _LEG_LIST_KEYS:
        value = raw_proposal.get(key)
        if value:
            return key if isinstance(value, list) else None
    return None


def _normalize_entries(raw_proposal: Any) -> List[Dict[str, Any]]:
    """Extract and normalize journal entry legs from proposal."""
    key = _leg_list_key(raw_proposal)
    if key is None:
        return []

    normalized = []
    for item in raw_proposal[key]:
        if not isinstance(item, dict):
            # An unreadable leg is never dropped: it pairs with nothing, so it is
            # always reported as a disagreement.
            normalized.append({
                "account_number": "",
                "account_name": "",
                "debit": Decimal("0.00"),
                "credit": Decimal("0.00"),
                "amounts_parsed": False,
                "debit_raw": None,
                "credit_raw": None,
                "description": "",
                "entity_id": None,
                "extra": {},
                "unreadable": item,
            })
            continue
        number_key = "account_number" if item.get("account_number") else "account_id"
        name_key = "account_name" if item.get("account_name") else "account"
        acct_num = str(item.get(number_key) or "").strip()
        acct_name = str(item.get(name_key) or "").strip()
        raw_deb = item.get("debit")
        raw_cred = item.get("credit")
        deb = _finite_decimal(raw_deb)
        cred = _finite_decimal(raw_cred)
        # A non-numeric or non-finite amount is not zero; flag it so it can never match silently.
        amounts_parsed = not (
            (raw_deb not in (None, "") and deb is None)
            or (raw_cred not in (None, "") and cred is None)
        )
        desc = str(item.get("description") or "").strip()
        entity_id = str(item.get("entity_id") or "").strip() or None
        identity_keys = {number_key, name_key, *_LEG_AMOUNT_ENTITY_KEYS}
        extra: Dict[str, Any] = {}
        for field_key, value in item.items():
            if field_key not in identity_keys:
                _flatten(str(field_key), value, extra)
        if acct_name:
            # The account name labels the GL account; compared without regard to case.
            extra["account_name"] = acct_name.casefold()

        normalized.append({
            "account_number": acct_num,
            "account_name": acct_name,
            "debit": deb if deb is not None else Decimal("0.00"),
            "credit": cred if cred is not None else Decimal("0.00"),
            "amounts_parsed": amounts_parsed,
            "debit_raw": raw_deb,
            "credit_raw": raw_cred,
            "description": desc,
            "entity_id": entity_id,
            "extra": extra,
            "unreadable": None,
        })
    return normalized


def _leg_label(leg: Dict[str, Any]) -> str:
    label = leg["account_number"] or leg["account_name"] or "<no account>"
    return f"{label}@{leg['entity_id']}" if leg["entity_id"] else label


def _leg_value(leg: Dict[str, Any]) -> str:
    if leg["unreadable"] is not None:
        return f"<unreadable leg {leg['unreadable']!r}>"
    if not leg["amounts_parsed"]:
        return f"{_leg_label(leg)} {leg['account_name']} Dr:{leg['debit_raw']!r} Cr:{leg['credit_raw']!r}"
    return f"{_leg_label(leg)} {leg['account_name']} Dr:{leg['debit']} Cr:{leg['credit']}"


# Execution trace, not part of the asserted answer. ``tool_name`` is compared on
# its own (TOOL_MISMATCH).
_TRACE_KEYS = ("tool_events", "tool_calls", "token_usage", "tool_name")

# Containers whose members are compared as if they were top-level fields.
_METRIC_CONTAINER_KEYS = ("metrics", "calculated_metrics")


def _skip_top_level_key(key: Any) -> bool:
    return key in _TRACE_KEYS or (isinstance(key, str) and key.startswith("_"))


def _flatten(path: str, value: Any, out: Dict[str, Any]) -> None:
    """Flatten nested dicts/lists into dotted/indexed leaf paths.

    Lists compare by position, so a reordered or resized list is a disagreement.
    None/"" leaves are absent values (an absence on one side only is still reported).
    """
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(f"{path}.{key}", child, out)
    elif isinstance(value, (list, tuple)):
        for idx, child in enumerate(value):
            _flatten(f"{path}[{idx}]", child, out)
    elif value is not None and value != "":
        out[path] = value


def _comparable_leaves(raw_output: Any) -> Dict[str, Any]:
    """Every scalar a model asserted, keyed by path, outside the journal legs.

    Only the leg list actually compared as legs is excluded; any other leg-alias
    key is compared as a field. Members of a ``metrics``/``calculated_metrics``
    container keep their own key as the path; one whose path is already taken
    keeps the container prefix. Top-level fields are compared alongside the
    container, never shadowed by it.
    """
    if not isinstance(raw_output, dict):
        return {}
    leg_key = _leg_list_key(raw_output)
    containers = [k for k in _METRIC_CONTAINER_KEYS if isinstance(raw_output.get(k), dict)]
    leaves: Dict[str, Any] = {}
    for key, value in raw_output.items():
        if _skip_top_level_key(key) or key == leg_key or key in containers:
            continue
        _flatten(str(key), value, leaves)
    for container in containers:
        for key, value in raw_output[container].items():
            if _skip_top_level_key(key):
                continue
            member: Dict[str, Any] = {}
            _flatten(str(key), value, member)
            if key in raw_output or any(path in leaves for path in member):
                member = {}
                _flatten(f"{container}.{key}", value, member)
            leaves.update(member)
    return leaves


def _split_leaves(
    leaves: Dict[str, Any],
) -> Tuple[Dict[str, Decimal], Dict[str, str], Dict[str, str]]:
    """Split leaves into numeric metrics, non-numeric values and unusable numbers.

    A value that will not parse as a number is never coerced to 0.00; it is
    compared as a value in its own right. A non-finite or out-of-range number
    (NaN, Infinity) is returned in the third bag and can never agree with anything.
    """
    metrics: Dict[str, Decimal] = {}
    values: Dict[str, str] = {}
    non_finite: Dict[str, str] = {}
    for path, v in leaves.items():
        dec = _try_decimal(v)
        if dec is None:
            values[path] = str(v)
        elif _usable_decimal(dec):
            metrics[path] = dec
        else:
            non_finite[path] = str(v)
    return metrics, values, non_finite


def _split_comparable_fields(
    raw_output: Any,
) -> Tuple[Dict[str, Decimal], Dict[str, str], Dict[str, str]]:
    """``_split_leaves`` over every asserted field of an output."""
    return _split_leaves(_comparable_leaves(raw_output))


def _extract_metrics(raw_output: Any) -> Dict[str, Decimal]:
    """Extract numeric metrics from output dictionary."""
    return _split_comparable_fields(raw_output)[0]


class DualModelConsensusEngine:
    """Evaluates consensus between two AI model executions on financial tasks."""

    def __init__(
        self,
        *,
        tolerance: Decimal = Decimal("0.01"),
        materiality_high_threshold: Decimal = Decimal("10000.00"),
        materiality_medium_threshold: Decimal = Decimal("500.00"),
    ):
        self.tolerance = tolerance
        self.materiality_high_threshold = materiality_high_threshold
        self.materiality_medium_threshold = materiality_medium_threshold

    def compare_proposals(
        self,
        *,
        model_a_name: str,
        output_a: Any,
        model_b_name: str,
        output_b: Any,
    ) -> ConsensusResult:
        """Compare outputs of Model A and Model B for consensus."""
        discrepancies: List[ConsensusDiscrepancy] = []

        # Extract tool names if tool execution
        tool_a = output_a.get("tool_name") if isinstance(output_a, dict) else None
        tool_b = output_b.get("tool_name") if isinstance(output_b, dict) else None
        # A tool named by one model only is a disagreement, not an uncompared field.
        if (tool_a or tool_b) and tool_a != tool_b:
            discrepancies.append(
                ConsensusDiscrepancy(
                    field_name="tool_name",
                    discrepancy_type=ConsensusDiscrepancyType.TOOL_MISMATCH,
                    materiality=DiscrepancyMateriality.HIGH,
                    model_a_name=model_a_name,
                    model_a_value=tool_a,
                    model_b_name=model_b_name,
                    model_b_value=tool_b,
                    explanation=f"Models selected different tools: '{tool_a}' vs '{tool_b}'",
                )
            )

        # 1. Compare Journal Entry Legs
        entries_a = _normalize_entries(output_a)
        entries_b = _normalize_entries(output_b)

        total_legs_expected = max(len(entries_a), len(entries_b))
        matched_legs = 0

        if entries_a or entries_b:
            matched_b_indices: set = set()
            unpaired_a: List[int] = []

            # Pass 1: a leg's identity is its account number AND entity. Account
            # names are labels, not GL identity, so a name alone never pairs legs.
            for idx_a, leg_a in enumerate(entries_a):
                idx_b = self._same_identity_counterpart(leg_a, entries_b, matched_b_indices)
                if idx_b is None:
                    unpaired_a.append(idx_a)
                    continue
                matched_b_indices.add(idx_b)
                leg_discrepancies = self._compare_paired_legs(
                    idx_a, leg_a, entries_b[idx_b], model_a_name, model_b_name
                )
                if leg_discrepancies:
                    discrepancies.extend(leg_discrepancies)
                else:
                    matched_legs += 1

            # Pass 2: an unpaired leg posting the same amounts as an unpaired leg of
            # the other model is the same posting to a different account or entity.
            for idx_a in unpaired_a:
                leg_a = entries_a[idx_a]
                idx_b = self._same_amount_counterpart(leg_a, entries_b, matched_b_indices)
                if idx_b is not None:
                    matched_b_indices.add(idx_b)
                    discrepancies.append(
                        self._account_mismatch(idx_a, leg_a, entries_b[idx_b], model_a_name, model_b_name)
                    )
                    continue
                discrepancies.append(
                    ConsensusDiscrepancy(
                        field_name=f"entry[{idx_a}]",
                        discrepancy_type=ConsensusDiscrepancyType.MISSING_LEG,
                        materiality=self._leg_materiality(leg_a),
                        model_a_name=model_a_name,
                        model_a_value=_leg_value(leg_a),
                        model_b_name=model_b_name,
                        model_b_value="<missing>",
                        explanation=f"Model {model_b_name} did not include account {_leg_label(leg_a)}.",
                    )
                )

            for idx_b, leg_b in enumerate(entries_b):
                if idx_b not in matched_b_indices:
                    discrepancies.append(
                        ConsensusDiscrepancy(
                            field_name=f"entry_b[{idx_b}]",
                            discrepancy_type=ConsensusDiscrepancyType.MISSING_LEG,
                            materiality=self._leg_materiality(leg_b),
                            model_a_name=model_a_name,
                            model_a_value="<missing>",
                            model_b_name=model_b_name,
                            model_b_value=_leg_value(leg_b),
                            explanation=f"Model {model_a_name} did not include account {_leg_label(leg_b)}.",
                        )
                    )

        # 2. Compare every other asserted field, nested values included
        field_evaluations, matched_fields, field_discrepancies = self._compare_leaves(
            _comparable_leaves(output_a),
            _comparable_leaves(output_b),
            model_a_name=model_a_name,
            model_b_name=model_b_name,
        )
        discrepancies.extend(field_discrepancies)

        # 3. Calculate Agreement Score
        total_evaluations = total_legs_expected + field_evaluations
        is_identical = output_a == output_b
        if total_evaluations == 0:
            agreement_score = 1.0 if is_identical else 0.5
            status = ConsensusStatus.UNANIMOUS if is_identical else ConsensusStatus.DIVERGENT
        else:
            agreement_score = (matched_legs + matched_fields) / total_evaluations
            status = (
                ConsensusStatus.UNANIMOUS
                if len(discrepancies) == 0
                else ConsensusStatus.DIVERGENT
            )

        # Only stamp verification when something was actually compared.
        comparison_performed = total_evaluations > 0 or is_identical
        unified_proposal = None
        if status == ConsensusStatus.UNANIMOUS and comparison_performed:
            unified_proposal = dict(output_a) if isinstance(output_a, dict) else {"result": output_a}
            unified_proposal["_consensus_verified"] = True
            unified_proposal["_consensus_models"] = [model_a_name, model_b_name]

        return ConsensusResult(
            status=status,
            agreement_score=agreement_score,
            discrepancies=discrepancies,
            model_a_name=model_a_name,
            model_b_name=model_b_name,
            model_a_output=output_a,
            model_b_output=output_b,
            unified_proposal=unified_proposal,
        )

    def _compare_leaves(
        self,
        leaves_a: Dict[str, Any],
        leaves_b: Dict[str, Any],
        *,
        model_a_name: str,
        model_b_name: str,
        path_prefix: str = "",
    ) -> Tuple[int, int, List[ConsensusDiscrepancy]]:
        """Compare two path -> scalar maps; returns (evaluations, matched, discrepancies)."""
        discrepancies: List[ConsensusDiscrepancy] = []
        metrics_a, values_a, non_finite_a = _split_leaves(leaves_a)
        metrics_b, values_b, non_finite_b = _split_leaves(leaves_b)
        all_metric_keys = set(metrics_a.keys()) | set(metrics_b.keys())
        matched_metrics = 0

        for key in sorted(all_metric_keys):
            name = f"{path_prefix}{key}"
            if key in metrics_a and key in metrics_b:
                val_a = metrics_a[key]
                val_b = metrics_b[key]
                delta = abs(val_a - val_b)
                if delta <= self.tolerance:
                    matched_metrics += 1
                else:
                    mat = (
                        DiscrepancyMateriality.HIGH
                        if delta >= self.materiality_high_threshold
                        else DiscrepancyMateriality.MEDIUM
                    )
                    discrepancies.append(
                        ConsensusDiscrepancy(
                            field_name=f"metric.{name}",
                            discrepancy_type=ConsensusDiscrepancyType.METRIC_MISMATCH,
                            materiality=mat,
                            model_a_name=model_a_name,
                            model_a_value=val_a,
                            model_b_name=model_b_name,
                            model_b_value=val_b,
                            explanation=f"Metric '{name}' differs: {model_a_name}={val_a} vs {model_b_name}={val_b} (delta={delta})",
                        )
                    )
            else:
                # Numeric on one side only: the other side omitted it, or gave a
                # non-numeric / non-finite value that cannot agree with a number.
                in_a = key in metrics_a
                other = values_b if in_a else values_a
                other_non_finite = non_finite_b if in_a else non_finite_a
                other_value = other.get(key, other_non_finite.get(key))
                present, absent = (model_a_name, model_b_name) if in_a else (model_b_name, model_a_name)
                discrepancies.append(
                    ConsensusDiscrepancy(
                        field_name=f"metric.{name}",
                        discrepancy_type=ConsensusDiscrepancyType.METRIC_MISMATCH,
                        materiality=(
                            DiscrepancyMateriality.LOW if other_value is None else DiscrepancyMateriality.HIGH
                        ),
                        model_a_name=model_a_name,
                        model_a_value=metrics_a[key] if in_a else (other_value if other_value is not None else "<missing>"),
                        model_b_name=model_b_name,
                        model_b_value=(other_value if other_value is not None else "<missing>") if in_a else metrics_b[key],
                        explanation=(
                            f"Metric '{name}' present in {present} but omitted by {absent}"
                            if other_value is None
                            else f"Metric '{name}' is numeric in {present} but {other_value!r} in {absent}"
                        ),
                    )
                )

        # Non-finite or out-of-range numbers (NaN, Infinity) are rejected on either
        # side, even when both models produced the same one.
        all_non_finite_keys = (set(non_finite_a.keys()) | set(non_finite_b.keys())) - all_metric_keys
        for key in sorted(all_non_finite_keys):
            name = f"{path_prefix}{key}"
            discrepancies.append(
                ConsensusDiscrepancy(
                    field_name=f"metric.{name}",
                    discrepancy_type=ConsensusDiscrepancyType.METRIC_MISMATCH,
                    materiality=DiscrepancyMateriality.HIGH,
                    model_a_name=model_a_name,
                    model_a_value=non_finite_a.get(key, values_a.get(key, "<missing>")),
                    model_b_name=model_b_name,
                    model_b_value=non_finite_b.get(key, values_b.get(key, "<missing>")),
                    explanation=f"Field '{name}' holds a non-finite or out-of-range number; it cannot be compared or verified.",
                )
            )

        # Non-numeric values are compared as values, never collapsed to a number.
        all_value_keys = (set(values_a.keys()) | set(values_b.keys())) - all_metric_keys - all_non_finite_keys
        matched_values = 0

        for key in sorted(all_value_keys):
            name = f"{path_prefix}{key}"
            in_a = key in values_a
            in_b = key in values_b
            if in_a and in_b and values_a[key] == values_b[key]:
                matched_values += 1
                continue
            discrepancies.append(
                ConsensusDiscrepancy(
                    field_name=f"field.{name}",
                    discrepancy_type=ConsensusDiscrepancyType.METRIC_MISMATCH,
                    materiality=(
                        DiscrepancyMateriality.HIGH if in_a and in_b else DiscrepancyMateriality.LOW
                    ),
                    model_a_name=model_a_name,
                    model_a_value=values_a.get(key, "<missing>"),
                    model_b_name=model_b_name,
                    model_b_value=values_b.get(key, "<missing>"),
                    explanation=(
                        f"Non-numeric field '{name}' differs: {model_a_name}="
                        f"{values_a.get(key, '<missing>')!r} vs {model_b_name}={values_b.get(key, '<missing>')!r}"
                    ),
                )
            )

        evaluations = len(all_metric_keys) + len(all_non_finite_keys) + len(all_value_keys)
        return evaluations, matched_metrics + matched_values, discrepancies

    def _leg_materiality(self, leg: Dict[str, Any]) -> DiscrepancyMateriality:
        return (
            DiscrepancyMateriality.HIGH
            if not leg["amounts_parsed"] or max(leg["debit"], leg["credit"]) >= self.materiality_high_threshold
            else DiscrepancyMateriality.MEDIUM
        )

    def _amounts_agree(self, leg_a: Dict[str, Any], leg_b: Dict[str, Any]) -> bool:
        return (
            leg_a["amounts_parsed"]
            and leg_b["amounts_parsed"]
            and abs(leg_a["debit"] - leg_b["debit"]) <= self.tolerance
            and abs(leg_a["credit"] - leg_b["credit"]) <= self.tolerance
        )

    def _same_identity_counterpart(
        self,
        leg_a: Dict[str, Any],
        entries_b: List[Dict[str, Any]],
        matched_b_indices: set,
    ) -> Optional[int]:
        """Index of an unpaired leg in B with the same account number and entity,
        preferring one that also posts the same amounts."""
        if not leg_a["account_number"]:
            return None
        candidates = [
            i
            for i, leg_b in enumerate(entries_b)
            if i not in matched_b_indices
            and leg_a["account_number"] == leg_b["account_number"]
            and leg_a["entity_id"] == leg_b["entity_id"]
        ]
        for i in candidates:
            if self._amounts_agree(leg_a, entries_b[i]):
                return i
        return candidates[0] if candidates else None

    def _compare_paired_legs(
        self,
        idx_a: int,
        leg_a: Dict[str, Any],
        leg_b: Dict[str, Any],
        model_a_name: str,
        model_b_name: str,
    ) -> List[ConsensusDiscrepancy]:
        """Compare two legs with the same account and entity; empty means they agree.

        Every other field either model asserted on the leg (account name, memo,
        dates, dimensions) is compared too.
        """
        discrepancies: List[ConsensusDiscrepancy] = []
        amount = self._compare_leg_amounts(idx_a, leg_a, leg_b, model_a_name, model_b_name)
        if amount is not None:
            discrepancies.append(amount)
        discrepancies.extend(
            self._compare_leaves(
                leg_a["extra"],
                leg_b["extra"],
                model_a_name=model_a_name,
                model_b_name=model_b_name,
                path_prefix=f"entry[{idx_a}].",
            )[2]
        )
        return discrepancies

    def _compare_leg_amounts(
        self,
        idx_a: int,
        leg_a: Dict[str, Any],
        leg_b: Dict[str, Any],
        model_a_name: str,
        model_b_name: str,
    ) -> Optional[ConsensusDiscrepancy]:
        if not (leg_a["amounts_parsed"] and leg_b["amounts_parsed"]):
            return ConsensusDiscrepancy(
                field_name=f"entry[{idx_a}].amount",
                discrepancy_type=ConsensusDiscrepancyType.AMOUNT_MISMATCH,
                materiality=DiscrepancyMateriality.HIGH,
                model_a_name=model_a_name,
                model_a_value=f"Dr:{leg_a['debit_raw']!r} Cr:{leg_a['credit_raw']!r}",
                model_b_name=model_b_name,
                model_b_value=f"Dr:{leg_b['debit_raw']!r} Cr:{leg_b['credit_raw']!r}",
                explanation=f"Non-numeric or non-finite amount on account {_leg_label(leg_a)}: amounts are not comparable.",
            )

        deb_diff = abs(leg_a["debit"] - leg_b["debit"])
        cred_diff = abs(leg_a["credit"] - leg_b["credit"])
        if deb_diff <= self.tolerance and cred_diff <= self.tolerance:
            return None

        if (
            abs(leg_a["debit"] - leg_b["credit"]) <= self.tolerance
            and abs(leg_a["credit"] - leg_b["debit"]) <= self.tolerance
        ):
            return ConsensusDiscrepancy(
                field_name=f"entry[{idx_a}].direction",
                discrepancy_type=ConsensusDiscrepancyType.DIRECTION_MISMATCH,
                materiality=DiscrepancyMateriality.HIGH,
                model_a_name=model_a_name,
                model_a_value=f"Dr:{leg_a['debit']} Cr:{leg_a['credit']}",
                model_b_name=model_b_name,
                model_b_value=f"Dr:{leg_b['debit']} Cr:{leg_b['credit']}",
                explanation=f"Account {_leg_label(leg_a)} has inverted debit/credit direction.",
            )

        delta = max(deb_diff, cred_diff)
        mat = (
            DiscrepancyMateriality.HIGH
            if delta >= self.materiality_high_threshold
            else (
                DiscrepancyMateriality.MEDIUM
                if delta >= self.materiality_medium_threshold
                else DiscrepancyMateriality.LOW
            )
        )
        return ConsensusDiscrepancy(
            field_name=f"entry[{idx_a}].amount",
            discrepancy_type=ConsensusDiscrepancyType.AMOUNT_MISMATCH,
            materiality=mat,
            model_a_name=model_a_name,
            model_a_value=f"Dr:{leg_a['debit']} Cr:{leg_a['credit']}",
            model_b_name=model_b_name,
            model_b_value=f"Dr:{leg_b['debit']} Cr:{leg_b['credit']}",
            explanation=f"Amount mismatch for account {_leg_label(leg_a)}: delta=${delta:.2f}",
        )

    def _same_amount_counterpart(
        self,
        leg_a: Dict[str, Any],
        entries_b: List[Dict[str, Any]],
        matched_b_indices: set,
    ) -> Optional[int]:
        """Index of an unpaired leg in B with the same parsed amounts, preferring one
        that shares the account number, entity or account name with ``leg_a``."""
        candidates = [
            i
            for i, leg_b in enumerate(entries_b)
            if i not in matched_b_indices and self._amounts_agree(leg_a, leg_b)
        ]
        for i in candidates:
            leg_b = entries_b[i]
            if (
                (leg_a["account_number"] and leg_a["account_number"] == leg_b["account_number"])
                or (leg_a["entity_id"] and leg_a["entity_id"] == leg_b["entity_id"])
                or (leg_a["account_name"] and leg_a["account_name"].lower() == leg_b["account_name"].lower())
            ):
                return i
        return candidates[0] if candidates else None

    def _account_mismatch(
        self,
        idx_a: int,
        leg_a: Dict[str, Any],
        leg_b: Dict[str, Any],
        model_a_name: str,
        model_b_name: str,
    ) -> ConsensusDiscrepancy:
        differences = []
        if not leg_a["account_number"] or not leg_b["account_number"]:
            differences.append("an account number is missing, so the GL account cannot be verified")
        elif leg_a["account_number"] != leg_b["account_number"]:
            differences.append(f"account {leg_a['account_number']} vs {leg_b['account_number']}")
        if leg_a["entity_id"] != leg_b["entity_id"]:
            differences.append(f"entity {leg_a['entity_id'] or '<none>'} vs {leg_b['entity_id'] or '<none>'}")
        return ConsensusDiscrepancy(
            field_name=f"entry[{idx_a}].account",
            discrepancy_type=ConsensusDiscrepancyType.ACCOUNT_MISMATCH,
            materiality=DiscrepancyMateriality.HIGH,
            model_a_name=model_a_name,
            model_a_value=_leg_value(leg_a),
            model_b_name=model_b_name,
            model_b_value=_leg_value(leg_b),
            explanation=f"Same amounts posted to a different account or entity: {'; '.join(differences)}.",
        )

    def run_parallel(
        self,
        *,
        runner_a: Callable[[], Any],
        runner_b: Callable[[], Any],
        model_a_name: str,
        model_b_name: str,
        timeout_seconds: float = 30.0,
    ) -> ConsensusResult:
        """Run runner_a and runner_b concurrently and compare results."""
        t0 = time.perf_counter()
        res_a: Any = None
        res_b: Any = None
        err_a: Optional[str] = None
        err_b: Optional[str] = None
        latency_ms_a = 0
        latency_ms_b = 0

        def _timed_exec(runner: Callable[[], Any]) -> Tuple[Any, int, Optional[str]]:
            start = time.perf_counter()
            try:
                out = runner()
                elapsed = int((time.perf_counter() - start) * 1000)
                return out, elapsed, None
            except Exception as exc:
                elapsed = int((time.perf_counter() - start) * 1000)
                logger.error("Runner execution failed: %s", exc, exc_info=True)
                return None, elapsed, str(exc)

        # Each runner sees the caller's bound tenant/task context, so its model calls
        # stay attributed to, and budget-gated for, the caller's tenant.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(contextvars.copy_context().run, _timed_exec, runner_a)
            future_b = executor.submit(contextvars.copy_context().run, _timed_exec, runner_b)

            try:
                res_a, latency_ms_a, err_a = future_a.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError:
                err_a = f"Model {model_a_name} timed out after {timeout_seconds}s"
                latency_ms_a = int(timeout_seconds * 1000)

            try:
                res_b, latency_ms_b, err_b = future_b.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError:
                err_b = f"Model {model_b_name} timed out after {timeout_seconds}s"
                latency_ms_b = int(timeout_seconds * 1000)

        total_latency_ms = int((time.perf_counter() - t0) * 1000)

        if err_a and err_b:
            return ConsensusResult(
                status=ConsensusStatus.BOTH_FAILED,
                agreement_score=0.0,
                discrepancies=[
                    ConsensusDiscrepancy(
                        field_name="execution",
                        discrepancy_type=ConsensusDiscrepancyType.EXECUTION_FAILURE,
                        materiality=DiscrepancyMateriality.HIGH,
                        model_a_name=model_a_name,
                        model_a_value=err_a,
                        model_b_name=model_b_name,
                        model_b_value=err_b,
                        explanation=f"Both models failed execution: A='{err_a}', B='{err_b}'",
                    )
                ],
                model_a_name=model_a_name,
                model_b_name=model_b_name,
                model_a_output={"error": err_a},
                model_b_output={"error": err_b},
                execution_telemetry={
                    "latency_ms_a": latency_ms_a,
                    "latency_ms_b": latency_ms_b,
                    "total_latency_ms": total_latency_ms,
                },
            )
        if err_a:
            return ConsensusResult(
                status=ConsensusStatus.MODEL_A_FAILED,
                agreement_score=0.0,
                discrepancies=[
                    ConsensusDiscrepancy(
                        field_name="execution",
                        discrepancy_type=ConsensusDiscrepancyType.EXECUTION_FAILURE,
                        materiality=DiscrepancyMateriality.HIGH,
                        model_a_name=model_a_name,
                        model_a_value=err_a,
                        model_b_name=model_b_name,
                        model_b_value="success",
                        explanation=f"Model {model_a_name} failed: '{err_a}'",
                    )
                ],
                model_a_name=model_a_name,
                model_b_name=model_b_name,
                model_a_output={"error": err_a},
                model_b_output=res_b,
                execution_telemetry={
                    "latency_ms_a": latency_ms_a,
                    "latency_ms_b": latency_ms_b,
                    "total_latency_ms": total_latency_ms,
                },
            )
        if err_b:
            return ConsensusResult(
                status=ConsensusStatus.MODEL_B_FAILED,
                agreement_score=0.0,
                discrepancies=[
                    ConsensusDiscrepancy(
                        field_name="execution",
                        discrepancy_type=ConsensusDiscrepancyType.EXECUTION_FAILURE,
                        materiality=DiscrepancyMateriality.HIGH,
                        model_a_name=model_a_name,
                        model_a_value="success",
                        model_b_name=model_b_name,
                        model_b_value=err_b,
                        explanation=f"Model {model_b_name} failed: '{err_b}'",
                    )
                ],
                model_a_name=model_a_name,
                model_b_name=model_b_name,
                model_a_output=res_a,
                model_b_output={"error": err_b},
                execution_telemetry={
                    "latency_ms_a": latency_ms_a,
                    "latency_ms_b": latency_ms_b,
                    "total_latency_ms": total_latency_ms,
                },
            )

        tokens_a = getattr(res_a, "token_usage", None) or (res_a.get("token_usage") if isinstance(res_a, dict) else None)
        tokens_b = getattr(res_b, "token_usage", None) or (res_b.get("token_usage") if isinstance(res_b, dict) else None)

        cost_a = estimate_model_cost_usd(model_a_name, tokens_a) if tokens_a else None
        cost_b = estimate_model_cost_usd(model_b_name, tokens_b) if tokens_b else None
        total_cost = None
        if cost_a is not None or cost_b is not None:
            total_cost = (cost_a or Decimal("0")) + (cost_b or Decimal("0"))

        comparison = self.compare_proposals(
            model_a_name=model_a_name,
            output_a=res_a,
            model_b_name=model_b_name,
            output_b=res_b,
        )

        comparison.execution_telemetry = {
            "latency_ms_a": latency_ms_a,
            "latency_ms_b": latency_ms_b,
            "total_latency_ms": total_latency_ms,
            "tokens_a": tokens_a,
            "tokens_b": tokens_b,
            "cost_usd_a": float(cost_a) if cost_a is not None else None,
            "cost_usd_b": float(cost_b) if cost_b is not None else None,
            "total_cost_usd": float(total_cost) if total_cost is not None else None,
        }

        return comparison

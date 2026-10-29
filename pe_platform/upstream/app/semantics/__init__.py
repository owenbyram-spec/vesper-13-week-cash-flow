"""Vesper Bot semantic core — the single canonical metric layer.

Converged (plan 20260712-semantic-layer-convergence) from the former FOS
``app/semantics`` value-objects/money/compat + the former ``app/services/semantics``
catalog/evidence vocabulary. Public surface:
- ``refs``     — Basis / Scenario / ScenarioKind / Period / Unit / Statement /
                 ExpectedSign / MetricRef value objects;
- ``registry`` — MetricKind (== value-kind) / MetricDef / METRIC_REGISTRY
                 (== CANONICAL_METRICS) + get_metric / resolve_alias / validate_catalog;
- ``compat``   — assert_comparable / check_comparable (deployed covenant/budget
                 surface) + comparability_violations / Violation / V_* (catalog lane);
- ``money``    — the Decimal money policy.
"""
from __future__ import annotations

from .compat import (
    REASON_BASIS_MISMATCH,
    REASON_CURRENCY_MISMATCH,
    REASON_DIMENSION_MISMATCH,
    REASON_ENTITY_MISMATCH,
    REASON_METRIC_MISMATCH_KIND,
    REASON_PARTIAL_VS_FULL_PERIOD,
    REASON_SCENARIO_MISMATCH,
    REASON_UNIT_MISMATCH,
    REASON_UNKNOWN_METRIC,
    SemanticCompatibilityError,
    V_BASIS,
    V_CURRENCY,
    V_DIMENSIONS,
    V_ENTITY,
    V_GRAIN,
    V_METRIC,
    V_PARTIAL_PERIOD,
    V_SCENARIO,
    V_UNIT,
    V_UNKNOWN_METRIC,
    V_VALUE_KIND,
    Violation,
    assert_comparable,
    check_comparable,
    comparability_violations,
)
from .money import (
    FOUR_PLACES,
    TWO_PLACES,
    ZERO,
    quantize_money,
    quantize_ratio,
    sum_money,
    to_display_float,
    to_money,
)
from .refs import (
    Basis,
    ExpectedSign,
    MetricRef,
    Period,
    PeriodGrain,
    Scenario,
    ScenarioKind,
    Statement,
    Unit,
)
from .registry import (
    METRIC_REGISTRY,
    MetricDef,
    MetricKind,
    get_metric,
    resolve_alias,
    validate_catalog,
)

# The registry IS the canonical catalog — the former ``CANONICAL_METRICS`` name is
# kept as an alias for the W2 evidence lane (facts.py / semantic.py).
CANONICAL_METRICS = METRIC_REGISTRY
# ``value_kind`` is a spelling of ``kind`` (STOCK/FLOW); ``ValueKind`` aliases
# ``MetricKind`` so the former W2 import ``from ...semantics import ValueKind`` resolves.
ValueKind = MetricKind

__all__ = [
    "CANONICAL_METRICS",
    "FOUR_PLACES",
    "METRIC_REGISTRY",
    "REASON_BASIS_MISMATCH",
    "REASON_CURRENCY_MISMATCH",
    "REASON_DIMENSION_MISMATCH",
    "REASON_ENTITY_MISMATCH",
    "REASON_METRIC_MISMATCH_KIND",
    "REASON_PARTIAL_VS_FULL_PERIOD",
    "REASON_SCENARIO_MISMATCH",
    "REASON_UNIT_MISMATCH",
    "REASON_UNKNOWN_METRIC",
    "TWO_PLACES",
    "ZERO",
    "Basis",
    "ExpectedSign",
    "MetricDef",
    "MetricKind",
    "MetricRef",
    "Period",
    "PeriodGrain",
    "Scenario",
    "ScenarioKind",
    "SemanticCompatibilityError",
    "Statement",
    "Unit",
    "V_BASIS",
    "V_CURRENCY",
    "V_DIMENSIONS",
    "V_ENTITY",
    "V_GRAIN",
    "V_METRIC",
    "V_PARTIAL_PERIOD",
    "V_SCENARIO",
    "V_UNIT",
    "V_UNKNOWN_METRIC",
    "V_VALUE_KIND",
    "ValueKind",
    "Violation",
    "assert_comparable",
    "check_comparable",
    "comparability_violations",
    "get_metric",
    "quantize_money",
    "quantize_ratio",
    "resolve_alias",
    "sum_money",
    "to_display_float",
    "to_money",
    "validate_catalog",
]

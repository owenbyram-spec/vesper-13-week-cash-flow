"""Semantic comparability guard — refuse ACCIDENTAL mixing of incomparable numbers.

The forbidden comparisons this module exists to catch (each is a real financial
error, not a style nit):

- cash-basis vs accrual-basis (``basis_mismatch``) — different recognition rules,
  different numbers;
- actual vs budget/forecast/stress UNFLAGGED (``scenario_mismatch``) — budget-vs-
  actual variance IS a legitimate product feature, so it is allowed when the
  caller explicitly passes ``allow_cross_scenario=True`` for the SAME metric; the
  guard is against silently summing a budget line into an actuals total;
- partial vs full period (``partial_vs_full_period``) — July 1-11 MTD against a
  full June reads as a collapse that never happened;
- balance vs activity (``metric_mismatch_kind``) — a STOCK (AR total, cash) never
  compares against a FLOW (revenue, EBITDA);
- plus currency, unit, entity, and dimension mixing.

``assert_comparable`` is the fail-closed gate (raises ``SemanticCompatibilityError``);
``check_comparable`` is the non-raising variant for data-quality reporting — it
returns EVERY failed reason code, not just the first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .refs import Basis, MetricRef, Period, PeriodGrain
from .registry import METRIC_REGISTRY, MetricDef, MetricKind, resolve_alias

__all__ = [
    "REASON_BASIS_MISMATCH",
    "REASON_CURRENCY_MISMATCH",
    "REASON_DIMENSION_MISMATCH",
    "REASON_ENTITY_MISMATCH",
    "REASON_METRIC_MISMATCH_KIND",
    "REASON_PARTIAL_VS_FULL_PERIOD",
    "REASON_SCENARIO_MISMATCH",
    "REASON_UNIT_MISMATCH",
    "REASON_UNKNOWN_METRIC",
    "SemanticCompatibilityError",
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
    "Violation",
    "assert_comparable",
    "check_comparable",
    "comparability_violations",
]

# Reason codes (stable strings — they land in data_quality payloads and logs).
REASON_BASIS_MISMATCH = "basis_mismatch"
REASON_SCENARIO_MISMATCH = "scenario_mismatch"
REASON_CURRENCY_MISMATCH = "currency_mismatch"
REASON_UNIT_MISMATCH = "unit_mismatch"
REASON_ENTITY_MISMATCH = "entity_mismatch"
REASON_METRIC_MISMATCH_KIND = "metric_mismatch_kind"
REASON_PARTIAL_VS_FULL_PERIOD = "partial_vs_full_period"
REASON_DIMENSION_MISMATCH = "dimension_mismatch"
# Supplementary (beyond the minimum set): a metric_id that resolves in neither the
# registry nor its aliases cannot be kind-graded — fail closed, never assume.
REASON_UNKNOWN_METRIC = "unknown_metric"


class SemanticCompatibilityError(ValueError):
    """Two MetricRefs are not semantically comparable.

    ``reason_code`` is the first failed code (stable, machine-readable);
    ``reason_codes`` carries ALL failed codes for reporting.
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        reason_codes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.reason_codes = reason_codes or (reason_code,)


def _resolve_def(ref: MetricRef, registry: dict[str, MetricDef]) -> MetricDef | None:
    """The registry row for a ref's metric_id (canonical id, alias, or prefix), or None."""
    try:
        return resolve_alias(ref.metric_id, registry=registry)
    except KeyError:
        return None


def _describe(ref: MetricRef) -> str:
    """Compact one-line identity for error messages."""
    scen = ref.scenario.kind.value
    if ref.scenario.stress_name:
        scen = f"{scen}:{ref.scenario.stress_name}"
    dims = ""
    if ref.dimensions:
        dims = " {" + ", ".join(f"{k}={v}" for k, v in ref.dimensions) + "}"
    return (
        f"{ref.metric_id}[entity={ref.entity_id} "
        f"{ref.period.start.isoformat()}..{ref.period.end.isoformat()}/"
        f"{ref.period.grain.value} {ref.basis.value}/{scen} "
        f"{ref.currency} {ref.unit.value}{dims}]"
    )


def _periods_partial_vs_full(a: MetricRef, b: MetricRef, as_of: date | None) -> bool:
    """Grain-aware partial-vs-full detection for SAME-grain periods.

    Three prongs (any one flags):
    1. one period exactly spans its grain and the other is truncated
       (full June vs July 1-11, both grain MONTH);
    2. CUSTOM-grain windows of unequal length (CUSTOM has no canonical span, so
       "shorter" is judged by day count);
    3. with an ``as_of``: one period ends inside a not-yet-closed month while the
       other is fully closed (full-July-vs-full-June caught mid-July — the July
       column is still accumulating even though its window LOOKS complete).

    Different grains are NOT flagged here: cross-grain comparisons (a month
    against its TTM) are deliberate analytical moves (run-rate vs as-tested is a
    covenant product feature), not accidental mixing.
    """
    pa, pb = a.period, b.period
    if pa.grain != pb.grain:
        return False
    full_a, full_b = pa.spans_full_grain(), pb.spans_full_grain()
    if full_a != full_b:
        return True
    if pa.grain is PeriodGrain.CUSTOM and pa.span_days() != pb.span_days():
        return True
    if as_of is not None and pa.is_partial(as_of) != pb.is_partial(as_of):
        return True
    return False


def check_comparable(
    a: MetricRef,
    b: MetricRef,
    *,
    registry: dict[str, MetricDef] | None = None,
    allow_cross_scenario: bool = False,
    as_of: date | None = None,
) -> list[str]:
    """All failed comparability reason codes for (a, b); empty list = comparable.

    Non-raising — built for data_quality reporting. ``as_of`` sharpens the
    partial-period check (an in-flight month is detected even when its window
    looks complete); without it only structural truncation is caught.
    """
    reg = METRIC_REGISTRY if registry is None else registry
    failures: list[str] = []

    def_a = _resolve_def(a, reg)
    def_b = _resolve_def(b, reg)
    if def_a is None or def_b is None:
        # Fail closed: an unregistered metric cannot be kind-graded, and assuming
        # comparability for an unknown metric is exactly the accident this module
        # exists to prevent.
        failures.append(REASON_UNKNOWN_METRIC)
    elif def_a.kind is not def_b.kind:
        # Balance vs activity (STOCK vs FLOW) — AR total against revenue, cash
        # against net income. Same-kind DIFFERENT metrics are allowed: revenue-vs-
        # opex, actual-vs-budget-line variance etc. are legitimate comparisons.
        failures.append(REASON_METRIC_MISMATCH_KIND)

    if a.basis is not b.basis:
        failures.append(REASON_BASIS_MISMATCH)

    if a.scenario != b.scenario:
        same_metric = (
            def_a is not None and def_b is not None and def_a.metric_id == def_b.metric_id
        ) or a.metric_id == b.metric_id
        # Budget-vs-actual (or any cross-scenario read) of the SAME metric is a
        # legitimate product feature — but only when the caller SAYS SO. The flag
        # never licenses cross-scenario mixing of different metrics.
        if not (allow_cross_scenario and same_metric):
            failures.append(REASON_SCENARIO_MISMATCH)

    if a.currency != b.currency:
        failures.append(REASON_CURRENCY_MISMATCH)

    if a.unit is not b.unit:
        failures.append(REASON_UNIT_MISMATCH)

    if a.entity_id != b.entity_id:
        failures.append(REASON_ENTITY_MISMATCH)

    if _periods_partial_vs_full(a, b, as_of):
        failures.append(REASON_PARTIAL_VS_FULL_PERIOD)

    if (a.dimensions or None) != (b.dimensions or None):
        failures.append(REASON_DIMENSION_MISMATCH)

    return failures


def assert_comparable(
    a: MetricRef,
    b: MetricRef,
    *,
    registry: dict[str, MetricDef] | None = None,
    allow_cross_scenario: bool = False,
    as_of: date | None = None,
) -> None:
    """Fail-closed comparability gate: raises ``SemanticCompatibilityError`` listing
    every failed reason code when (a, b) are not semantically comparable."""
    failures = check_comparable(
        a,
        b,
        registry=registry,
        allow_cross_scenario=allow_cross_scenario,
        as_of=as_of,
    )
    if failures:
        raise SemanticCompatibilityError(
            failures[0],
            f"metric refs are not comparable ({', '.join(failures)}): "
            f"{_describe(a)} vs {_describe(b)}",
            reason_codes=tuple(failures),
        )


# ─────────────────────────────────────────────────────────────────────────────
# comparability_violations — the CLOSED violation vocabulary (W1 catalog lane),
# ported from the former app/services/semantics/types.py at convergence. A richer,
# metadata-driven variant of check_comparable: it grades against per-definition
# metadata (value_kind / basis_sensitive / unit) and returns typed Violation objects
# with a closed code vocabulary that fixtures assert on exactly. ``check_comparable``
# (above) remains the deployed covenant/budget comparability surface; this is the
# catalog/evidence-facing surface.
# ─────────────────────────────────────────────────────────────────────────────

# Closed violation vocabulary (stable strings — fixtures assert on them exactly).
V_METRIC = "metric_mismatch"
V_ENTITY = "entity_mismatch"
V_BASIS = "basis_mismatch"
V_SCENARIO = "scenario_mismatch"
V_CURRENCY = "currency_mismatch"
V_UNIT = "unit_mismatch"
V_VALUE_KIND = "stock_flow_mismatch"
V_GRAIN = "grain_mismatch"
V_PARTIAL_PERIOD = "partial_vs_full_period"
V_DIMENSIONS = "dimension_mismatch"
V_UNKNOWN_METRIC = "unknown_metric"


@dataclass(frozen=True)
class Violation:
    """One reason two MetricRefs are not directly comparable."""

    code: str
    detail: str


def comparability_violations(
    a: MetricRef,
    b: MetricRef,
    *,
    definitions: dict[str, MetricDef],
    allow_cross_metric: bool = False,
    allow_cross_scenario: bool = False,
) -> list[Violation]:
    """Every reason ``a`` and ``b`` may not be directly compared (empty ⇒ comparable).

    Cross-scenario (actual vs budget) and cross-metric comparisons are legitimate
    ANALYSES (variance, ratio) but never silent — the caller must opt in explicitly,
    which is itself the disclosure record.
    """
    violations: list[Violation] = []
    def_a = definitions.get(a.metric_id)
    def_b = definitions.get(b.metric_id)
    if def_a is None:
        violations.append(Violation(V_UNKNOWN_METRIC, f"{a.metric_id} not in catalog"))
    if def_b is None:
        violations.append(Violation(V_UNKNOWN_METRIC, f"{b.metric_id} not in catalog"))
    if def_a is None or def_b is None:
        return violations

    if a.metric_id != b.metric_id and not allow_cross_metric:
        violations.append(Violation(V_METRIC, f"{a.metric_id} vs {b.metric_id}"))
    if a.entity_id != b.entity_id:
        violations.append(Violation(V_ENTITY, f"{a.entity_id} vs {b.entity_id}"))

    basis_insensitive = not (def_a.basis_sensitive or def_b.basis_sensitive)
    bases = {a.basis, b.basis} - {Basis.BASIS_INDEPENDENT}
    if len(bases) > 1 and not basis_insensitive:
        violations.append(Violation(V_BASIS, f"{a.basis.value} vs {b.basis.value}"))

    if a.scenario != b.scenario and not allow_cross_scenario:
        violations.append(
            Violation(V_SCENARIO, f"{a.scenario.kind.value} vs {b.scenario.kind.value}")
        )
    if a.currency != b.currency:
        violations.append(Violation(V_CURRENCY, f"{a.currency} vs {b.currency}"))
    if def_a.unit != def_b.unit:
        violations.append(Violation(V_UNIT, f"{def_a.unit.value} vs {def_b.unit.value}"))
    if def_a.value_kind != def_b.value_kind:
        violations.append(
            Violation(V_VALUE_KIND, f"{def_a.value_kind.value} vs {def_b.value_kind.value}")
        )
    if (a.dimensions or None) != (b.dimensions or None):
        violations.append(Violation(V_DIMENSIONS, f"{a.dimensions} vs {b.dimensions}"))

    violations.extend(_period_violations(a.period, b.period, def_a, def_b))
    return violations


def _period_violations(
    pa: Period, pb: Period, def_a: MetricDef, def_b: MetricDef
) -> list[Violation]:
    out: list[Violation] = []
    stocks = {def_a.value_kind, def_b.value_kind} == {MetricKind.STOCK}
    if pa.grain != pb.grain:
        # A point-in-time read of a STOCK metric is comparable across grains; flows are not.
        if not (stocks and PeriodGrain.POINT_IN_TIME in {pa.grain, pb.grain}):
            out.append(Violation(V_GRAIN, f"{pa.grain.value} vs {pb.grain.value}"))
    elif pa.grain is PeriodGrain.MONTH and pa.spans_full_grain() != pb.spans_full_grain():
        out.append(
            Violation(
                V_PARTIAL_PERIOD,
                f"{pa.start}–{pa.end} ({pa.span_days()}d) vs {pb.start}–{pb.end} ({pb.span_days()}d)",
            )
        )
    elif pa.grain == pb.grain and not stocks:
        # Same grain, both flows: a materially different day-count is a partial period.
        longer, shorter = max(pa.span_days(), pb.span_days()), min(pa.span_days(), pb.span_days())
        if longer and shorter / longer < 0.9 and pa.grain is PeriodGrain.CUSTOM:
            out.append(Violation(V_PARTIAL_PERIOD, f"{shorter}d vs {longer}d spans"))
    return out

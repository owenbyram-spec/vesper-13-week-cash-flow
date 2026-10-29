# Plan 20260712-agentic-finance-os W2 (GAP-2) — typed-fact emission at the tool seam.
"""Deterministic typed facts emitted by finance tool handlers.

A ``TypedFact`` is a number WITH its semantic coordinates (W1 axes: metric_id /
entity / period / basis / scenario / currency), emitted by handler code at the
point where the value is computed — the same variables that render the tool's
text, never re-parsed from prose and NEVER produced by a model (the model does
not verify itself). The model loop wraps each tool invocation in
``capture_facts()`` and attaches the collected facts to the tool event; the W2
semantic verifier then binds answer-prose claims to these facts.

Emission is a per-context sink (ContextVar): ``emit_fact`` outside an active
capture is a silent no-op, so handlers stay callable from routers/tests/scripts
unchanged. Values are Decimal end-to-end (financial-modeling-correctness).
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterator

from app.semantics import CANONICAL_METRICS, Basis, Period, PeriodGrain, ScenarioKind

_FACT_SINK: ContextVar[list["TypedFact"] | None] = ContextVar("evidence_fact_sink", default=None)


@dataclass(frozen=True)
class TypedFact:
    metric_id: str
    value: Decimal
    entity_id: str
    period_start: date
    period_end: date
    period_grain: PeriodGrain
    basis: Basis
    scenario: ScenarioKind
    currency: str = "USD"
    source_tool: str = ""
    as_of: str | None = None

    @property
    def fact_id(self) -> str:
        """Deterministic id over the semantic axes + value (stable across runs)."""
        key = "|".join(
            [
                self.metric_id,
                str(self.value),
                self.entity_id,
                self.period_start.isoformat(),
                self.period_end.isoformat(),
                self.period_grain.value,
                self.basis.value,
                self.scenario.value,
                self.currency,
            ]
        )
        return "fact_" + hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def period(self) -> Period:
        return Period(start=self.period_start, end=self.period_end, grain=self.period_grain)

    def to_payload(self) -> dict:  # CRAFT-OK: JSON tool-event attachment — the loop telemetry shape
        return {
            "fact_id": self.fact_id,
            "metric_id": self.metric_id,
            "value": str(self.value),
            "entity_id": self.entity_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "period_grain": self.period_grain.value,
            "basis": self.basis.value,
            "scenario": self.scenario.value,
            "currency": self.currency,
            "source_tool": self.source_tool,
            "as_of": self.as_of,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "TypedFact | None":  # CRAFT-OK: JSON tool-event attachment — the loop telemetry shape
        """Rehydrate from a tool-event attachment; None on any malformed field
        (facts are evidence — a corrupt fact is DROPPED, never guessed at)."""
        try:
            return cls(
                metric_id=str(payload["metric_id"]),
                value=Decimal(str(payload["value"])),
                entity_id=str(payload["entity_id"]),
                period_start=date.fromisoformat(payload["period_start"]),
                period_end=date.fromisoformat(payload["period_end"]),
                period_grain=PeriodGrain(payload["period_grain"]),
                basis=Basis(payload["basis"]),
                scenario=ScenarioKind(payload["scenario"]),
                currency=str(payload.get("currency") or "USD"),
                source_tool=str(payload.get("source_tool") or ""),
                as_of=payload.get("as_of"),
            )
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return None


@contextmanager
def capture_facts() -> Iterator[list[TypedFact]]:
    """Open a fact-capture scope; yields the (live) list facts are collected into."""
    sink: list[TypedFact] = []
    token = _FACT_SINK.set(sink)
    try:
        yield sink
    finally:
        _FACT_SINK.reset(token)


def emit_fact(
    *,
    metric_id: str,
    value: Decimal | int | float | str,
    entity_id: str,
    period_start: date,
    period_end: date,
    period_grain: PeriodGrain,
    basis: Basis,
    scenario: ScenarioKind,
    currency: str = "USD",
    source_tool: str = "",
    as_of: str | None = None,
) -> None:
    """Record one typed fact into the active capture scope (no-op when none).

    Unregistered metric_ids are refused loudly — a fact must key into the
    canonical catalog or it is not a fact (add the metric to catalog.py first).
    """
    sink = _FACT_SINK.get()
    if sink is None:
        return
    if metric_id not in CANONICAL_METRICS:
        raise ValueError(f"emit_fact: '{metric_id}' is not in the canonical metric catalog")
    sink.append(
        TypedFact(
            metric_id=metric_id,
            value=Decimal(str(value)),
            entity_id=entity_id,
            period_start=period_start,
            period_end=period_end,
            period_grain=period_grain,
            basis=basis,
            scenario=scenario,
            currency=currency,
            source_tool=source_tool,
            as_of=as_of,
        )
    )


def facts_from_events(tool_events: list[dict]) -> list[TypedFact]:  # CRAFT-OK: tool-event dicts are the established loop telemetry shape
    """Rehydrate every well-formed fact attached to the run's tool events."""
    facts: list[TypedFact] = []
    for event in tool_events or []:
        payloads = (event or {}).get("facts") or []  # SA-OK: dict-key access on in-memory tool events, not a DB query
        for payload in payloads:
            fact = TypedFact.from_payload(payload)
            if fact is not None:
                facts.append(fact)
    return facts

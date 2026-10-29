# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
"""Pure validation gate for model-authored agent-plan DAGs (Wave 3.1, decision 5).

The DAG is MODEL OUTPUT — adversarial input, never authority. ``validate_plan``
refuses over trusting, lists EVERY failure (never first-fail), and derives every
security-relevant property (``is_write``, risk, role) from the REGISTRY, never from
model-declared fields. No HTTP, no DB session, no side effects: the caller passes
the built registry + the trusted ContextPacket + the raw DAG.

The decision-5 checks, all enforced here:
  * structural sanity (list of step objects, integer unique step_index, dict input)
  * cycle check over dep edges (self-loops included)
  * <= MAX_PLAN_STEPS steps; <= MAX_WRITE_STEPS write steps
  * tool exists in the registry
  * MODEL_HIDDEN_TOOLS / send_to_model=False rejected (the model can never plan them)
  * HANDLER_LANE_SELF_DRAFTERS rejected as write steps (E3 — their handlers draft
    internally with no plan_step_id seam: NULL-FK, undeduped, unadoptable proposals)
  * confirmation-token tools rejected with the recorded-decision message
    (the plan lane mints no tokens — decision 5)
  * write steps RESTRICTED to the internal-DB PROPOSAL_ONLY_WRITE_TOOLS class (E3)
  * per-step ToolPolicyGate.check(preflight=True) (intent / role / risk ceiling /
    output mode — the same gate chat selection clears)
  * input-schema validation vs ToolDefinition.input_schema (required keys,
    declared property types, enums)
  * the six scope keys stripped from every step input AND refused when present
    (model-supplied scope is never trusted; execution-time strip+re-inject in
    tool_executor._prepare_input is unchanged)

Plus the tamper seal: ``canonical_steps_serialization`` (sorted keys, volatile
fields dropped, deterministic JSON) + ``steps_content_hash`` (SHA-256). The
AGENT_PLAN approval handler re-hashes the persisted step rows and REFUSES on
mismatch (synthesis E3 binding) — canonicalization mirrors the
``app/qbo_actions/proposal_fingerprint.py`` precedent (pattern copied, not
imported, to keep this module free of the models/DB import chain).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .tool_policy import ToolPolicyGate
from .tool_registry import (
    HANDLER_LANE_SELF_DRAFTERS,
    MODEL_HIDDEN_TOOLS,
    PROPOSAL_ONLY_WRITE_TOOLS,
    ToolRegistry,
)
from .types import ContextPacket, ToolDefinition

# Decision-5 bounds.
MAX_PLAN_STEPS = 20
MAX_WRITE_STEPS = 5

# The six scope keys — MUST stay byte-aligned with the strip set in
# tool_executor._prepare_input and the override-guard keys in
# tool_policy._check_scope_override (AWB-04: the strip set and the guard set
# are one contract).
SCOPE_KEYS = (
    "realm_id",
    "company_id",
    "tenant_id",
    "qbo_realm_id",
    "organization_id",
    "organization_company_id",
)

# Volatile identity keys excluded from the canonical form (the
# proposal_fingerprint.VOLATILE_PAYLOAD_KEYS precedent): they identify WHICH
# run/turn authored the step, not WHAT the step does — re-authoring the same
# plan must hash identically.
VOLATILE_INPUT_KEYS = frozenset(
    {
        "source_data",
        "source_metadata",
        "conversation_id",
        "agent_run_id",
        "run_id",
        "request_id",
        "timestamp",
        "ts",
        "nonce",
        "created_at",
    }
)

_JSON_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (Mapping,),
}


@dataclass(frozen=True)
class ValidatedStep:
    """One gate-cleared DAG node, ready to persist as an AgentPlanStep row.

    ``tool_input`` is the model's input with the six scope keys stripped;
    ``is_write`` is derived from the registry flags, never model-declared.
    """

    step_index: int
    tool_name: str
    tool_input: dict[str, object]
    depends_on: tuple[int, ...]
    is_write: bool


@dataclass
class PlanValidationResult:
    ok: bool
    refusals: list[str] = field(default_factory=list)
    steps: list[ValidatedStep] = field(default_factory=list)
    steps_hash: str | None = None


def _canonicalize(value: object) -> object:
    """Sorted keys, volatile + scope keys dropped, integral floats -> ints,
    whitespace-normalized strings, bools preserved (the fingerprint precedent)."""
    if isinstance(value, Mapping):
        return {
            str(k): _canonicalize(value[k])
            for k in sorted(value, key=str)
            if str(k) not in VOLATILE_INPUT_KEYS and str(k) not in SCOPE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def _normalize_step(step: object) -> dict[str, object]:
    """Reduce a step (ValidatedStep, AgentPlanStep row, or mapping) to its four
    hash-covered DEFINITION fields (E4: tool name, input payload, dep edges, order).
    Execution-state fields (status/attempts/linkage/evidence) never enter the hash."""
    if isinstance(step, Mapping):
        raw = dict(step)
    else:  # duck-typed ORM row / dataclass
        raw = {
            "step_index": getattr(step, "step_index"),
            "tool_name": getattr(step, "tool_name"),
            "tool_input": getattr(step, "tool_input"),
            "depends_on": getattr(step, "depends_on"),
        }
    depends_on = raw["depends_on"] if raw.get("depends_on") else []
    tool_input = raw["tool_input"] if raw.get("tool_input") else {}
    return {
        "step_index": int(raw["step_index"]),
        "tool_name": str(raw["tool_name"] if "tool_name" in raw else ""),
        "tool_input": _canonicalize(tool_input),
        "depends_on": sorted({int(d) for d in depends_on}),
    }


def canonical_steps_serialization(steps: Sequence[object]) -> str:
    """Deterministic JSON over the DEFINITION fields, ordered by step_index —
    byte-stable across authoring, persistence, and reload."""
    normalized = sorted((_normalize_step(s) for s in steps), key=lambda s: s["step_index"])
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)


def steps_content_hash(steps: Sequence[object]) -> str:
    """SHA-256 over the canonical serialization — AgentPlan.steps_hash."""
    return hashlib.sha256(canonical_steps_serialization(steps).encode("utf-8")).hexdigest()


def steps_hash_matches(steps: Sequence[object], expected_hash: str) -> bool:
    """The approval-time tamper check (synthesis E3): re-hash the persisted rows
    and compare against the stored seal. A single mutated definition field flips it."""
    return steps_content_hash(steps) == str(expected_hash or "")


def _schema_violations(schema: Mapping[str, object], value: Mapping[str, object]) -> list[str]:
    """JSON-schema-lite check vs ToolDefinition.input_schema: required keys,
    declared top-level property types, enums. Unknown keys follow the JSON-schema
    default (allowed) unless the schema sets additionalProperties=False."""
    violations: list[str] = []
    properties = schema["properties"] if isinstance(schema.get("properties"), Mapping) else {}
    required = schema.get("required") or []
    for key in required:
        if key not in value:
            violations.append(f"missing required field '{key}'")
    for key, spec in properties.items():
        if key not in value or not isinstance(spec, Mapping):
            continue
        supplied = value[key]
        declared = spec["type"] if "type" in spec else None
        expected = (
            _JSON_TYPE_MAP[declared]
            if isinstance(declared, str) and declared in _JSON_TYPE_MAP
            else None
        )
        if expected is not None:
            ok = isinstance(supplied, expected)
            # bool is an int subclass — never satisfies integer/number.
            if ok and declared in ("integer", "number") and isinstance(supplied, bool):
                ok = False
            if not ok:
                violations.append(f"field '{key}' must be of type {declared}")
                continue
        enum = spec["enum"] if "enum" in spec else None
        if isinstance(enum, (list, tuple)) and supplied not in enum:
            violations.append(f"field '{key}' must be one of {list(enum)}")
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in properties:
                violations.append(f"unknown field '{key}'")
    return violations


def _cycle_members(nodes: set[int], edges: dict[int, set[int]]) -> list[int]:
    """Kahn's algorithm over the dep graph; returns the step indexes left on a
    cycle (empty list == acyclic). ``edges`` must carry an entry (possibly empty)
    for every node; an edge dep -> idx means idx waits on dep."""
    indegree = {n: len(edges[n]) for n in nodes}
    dependents: dict[int, list[int]] = {n: [] for n in nodes}
    for idx in nodes:
        for dep in edges[idx]:
            dependents[dep].append(idx)
    queue = [n for n, d in indegree.items() if d == 0]
    processed = 0
    while queue:
        node = queue.pop()
        processed += 1
        for follower in dependents[node]:
            indegree[follower] -= 1
            if indegree[follower] == 0:
                queue.append(follower)
    if processed == len(nodes):
        return []
    return sorted(n for n, d in indegree.items() if d > 0)


def validate_plan(
    raw_steps: object,
    *,
    registry: ToolRegistry,
    context: ContextPacket,
    policy_gate: ToolPolicyGate | None = None,
) -> PlanValidationResult:
    """Gate a model-authored DAG. Refusals list EVERY failure; ok only when empty.

    ``registry`` is the built assistant ToolRegistry (``.get(name)`` ->
    ToolDefinition | None); ``context`` is the TRUSTED ContextPacket (scope, role,
    intent, risk ceiling) — never anything the model supplied.
    """
    gate = policy_gate or ToolPolicyGate()
    refusals: list[str] = []

    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        return PlanValidationResult(ok=False, refusals=["plan steps must be a non-empty list"])

    if len(raw_steps) > MAX_PLAN_STEPS:
        refusals.append(f"plan has {len(raw_steps)} steps — exceeds the {MAX_PLAN_STEPS}-step limit")

    # ── structural pass ──────────────────────────────────────────────────────
    seen_indexes: set[int] = set()
    parsed: list[dict[str, object]] = []
    for position, raw in enumerate(raw_steps):
        if not isinstance(raw, Mapping):
            refusals.append(f"step at position {position} is not an object")
            continue
        idx = raw["step_index"] if "step_index" in raw else None
        if isinstance(idx, bool) or not isinstance(idx, int):
            refusals.append(f"step at position {position}: step_index must be an integer")
            continue
        if idx in seen_indexes:
            refusals.append(f"duplicate step_index {idx}")
            continue
        seen_indexes.add(idx)
        name = raw["tool_name"] if "tool_name" in raw else None
        if not isinstance(name, str) or not name.strip():
            refusals.append(f"step {idx}: tool_name must be a non-empty string")
            continue
        tool_input = raw["tool_input"] if "tool_input" in raw else None
        if tool_input is None:
            tool_input = {}
        if not isinstance(tool_input, Mapping):
            refusals.append(f"step {idx}: tool_input must be an object")
            continue
        deps_raw = raw["depends_on"] if "depends_on" in raw else None
        if deps_raw is None:
            deps_raw = []
        if not isinstance(deps_raw, (list, tuple)) or any(
            isinstance(d, bool) or not isinstance(d, int) for d in deps_raw
        ):
            refusals.append(f"step {idx}: depends_on must be a list of integer step_indexes")
            continue
        parsed.append(
            {
                "step_index": idx,
                "tool_name": name.strip(),
                "tool_input": dict(tool_input),
                "depends_on": sorted(set(deps_raw)),
            }
        )

    # ── dep-edge pass (unknown refs, self-loops, cycles) ─────────────────────
    known = {s["step_index"] for s in parsed}
    edges: dict[int, set[int]] = {}
    for step in parsed:
        idx = step["step_index"]
        kept: set[int] = set()
        for dep in step["depends_on"]:
            if dep == idx:
                refusals.append(f"step {idx}: depends_on references itself")
            elif dep not in known:
                refusals.append(f"step {idx}: depends_on references unknown step index {dep}")
            else:
                kept.add(dep)
        edges[idx] = kept
    cycle = _cycle_members(known, edges)
    if cycle:
        refusals.append(f"dependency cycle among steps {cycle}")

    # ── per-step tool pass ───────────────────────────────────────────────────
    validated: list[ValidatedStep] = []
    write_count = 0
    for step in parsed:
        idx = step["step_index"]
        name = step["tool_name"]
        stripped = dict(step["tool_input"])
        for key in SCOPE_KEYS:
            if key in stripped:
                refusals.append(
                    f"step {idx}: scope key '{key}' in tool input — model-supplied scope is never trusted; stripped and refused"
                )
                stripped.pop(key, None)

        tool: ToolDefinition | None = registry.get(name)  # SA-OK: ToolRegistry.get is an in-memory dict lookup (self._tools.get), not a DB query — no N+1
        if tool is None:
            refusals.append(f"step {idx}: unknown tool '{name}' — not in the registry")
            continue

        if name in MODEL_HIDDEN_TOOLS or not tool.send_to_model:
            refusals.append(
                f"step {idx}: tool '{name}' is model-hidden (send_to_model=False) and can never be planned"
            )

        # is_write is DERIVED from registry flags — a model-declared "is_write"
        # field on the step is ignored entirely.
        is_write = bool(tool.mutates_state or tool.creates_proposed_action)
        if is_write:
            write_count += 1
            if name in HANDLER_LANE_SELF_DRAFTERS:
                refusals.append(
                    f"step {idx}: tool '{name}' is a HANDLER_LANE self-drafter — its handler drafts "
                    f"internally with no plan_step_id seam (NULL-FK, undeduped, unadoptable proposals); "
                    f"rejected as a plan write step (decision 5 E3)"
                )
            elif tool.requires_confirmation:
                refusals.append(
                    f"step {idx}: tool '{name}' requires a backend confirmation token — "
                    f"confirmation-token tools are excluded by recorded decision: the plan lane "
                    f"mints no tokens (plan 20260714-wave3-1-agent-plans decision 5)"
                )
            elif name not in PROPOSAL_ONLY_WRITE_TOOLS:
                refusals.append(
                    f"step {idx}: write tool '{name}' is not in PROPOSAL_ONLY_WRITE_TOOLS — plan "
                    f"write steps are restricted to the internal-DB proposal-only class (decision 5 E3)"
                )

        decision = gate.check(tool, {}, context, preflight=True)
        if not decision.allowed:
            refusals.append(f"step {idx}: policy denies tool '{name}': {decision.reason}")

        for violation in _schema_violations(tool.input_schema or {}, stripped):
            refusals.append(f"step {idx}: input schema violation for '{name}': {violation}")

        validated.append(
            ValidatedStep(
                step_index=idx,
                tool_name=name,
                tool_input=stripped,
                depends_on=tuple(sorted(edges[idx])),
                is_write=is_write,
            )
        )

    if write_count > MAX_WRITE_STEPS:
        refusals.append(
            f"plan has {write_count} write steps — exceeds the {MAX_WRITE_STEPS}-write limit"
        )

    ok = not refusals
    validated.sort(key=lambda s: s.step_index)
    return PlanValidationResult(
        ok=ok,
        refusals=refusals,
        steps=validated if ok else [],
        steps_hash=steps_content_hash(validated) if ok else None,
    )

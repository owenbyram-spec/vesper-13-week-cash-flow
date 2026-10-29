# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — registry integrity enforcement
# (user-directed hardening pass; non-interactive session, ratification unavailable — audited doorway).
"""Machine-enforced tool-registry integrity (plan AC-1, SYNTHESIS GAP-A1).

The registry derives every tool's risk metadata from name-set membership in
``tool_registry.py``. Before this module, a tool registered in ``ASSISTANT_TOOLS`` or
``TOOL_HANDLERS`` but forgotten in every classification set silently fell through the
``_metadata_for_tool`` else-branch to READ_ONLY / VIEWER / mutates_state=False — the exact
"mutating tool classified as read-only" failure a financial agent cannot tolerate.

``ensure_registry_integrity`` is invoked on every registry build (cached on the name-set
signature, so per-request rebuilds are free) and raises ``RegistryIntegrityError`` listing
EVERY violation — never a warning, never first-only. Tests that build the registry
exercise this gate, including ``tests/test_registry_integrity.py``. This is a
runtime structural check, not evidence of repository CI or complete tool behavior.
"""
from __future__ import annotations

from functools import lru_cache
from typing import AbstractSet, Iterable


class RegistryIntegrityError(RuntimeError):
    """The tool registry violates a structural invariant. Fail closed — never serve."""


def _classification_sets() -> dict[str, AbstractSet[str]]:
    # Imported lazily to avoid a circular import (tool_registry imports this module).
    from . import tool_registry as reg

    return {
        "READ_ONLY_TOOLS": reg.READ_ONLY_TOOLS,
        "ANALYSIS_TOOLS": reg.ANALYSIS_TOOLS,
        "PROPOSAL_TOOLS": reg.PROPOSAL_TOOLS,
        "SAFE_INTERNAL_WRITE_TOOLS": reg.SAFE_INTERNAL_WRITE_TOOLS,
        "PROPOSAL_ONLY_WRITE_TOOLS": reg.PROPOSAL_ONLY_WRITE_TOOLS,
    }


def validate_registry_integrity(
    schema_names: Iterable[str],
    handler_names: Iterable[str],
) -> list[str]:
    """Return ALL structural violations (empty list == healthy registry)."""
    from . import tool_registry as reg
    from ...services.internal_action_execution import ACTION_TYPE_TO_TOOL

    schemas = {str(n) for n in schema_names if n}
    handlers = {str(n) for n in handler_names if n}
    registered = schemas | handlers
    sets = _classification_sets()
    classified = set().union(*sets.values())
    violations: list[str] = []

    # 1. Every registered tool is explicitly classified — the READ_ONLY default is abolished.
    for name in sorted(registered - classified):
        violations.append(
            f"unclassified tool '{name}': every registered tool must be a member of exactly one "
            "classification set in tool_registry.py (a forgotten write tool would otherwise "
            "silently default to READ_ONLY/VIEWER)"
        )

    # 2. No phantom set members (a classification for a tool that no longer exists).
    for name in sorted(classified - registered):
        violations.append(
            f"phantom classification '{name}': named in a classification set but registered in "
            "neither ASSISTANT_TOOLS nor TOOL_HANDLERS"
        )

    # 3. Exactly-one membership — double classification makes risk metadata order-dependent.
    for name in sorted(classified):
        members = [label for label, s in sets.items() if name in s]
        if len(members) > 1:
            violations.append(f"tool '{name}' is classified in multiple sets: {members}")

    # 4. Every schema must have a handler and vice versa. A schema with no handler yields
    #    "Unknown tool" at runtime; a handler with no schema yields a synthetic legacy
    #    definition whose input schema validates nothing.
    for name in sorted(schemas - handlers):
        violations.append(f"tool '{name}' has a model schema but no registered handler")
    for name in sorted(handlers - schemas):
        violations.append(f"tool '{name}' has a handler but no model schema in ASSISTANT_TOOLS")

    # 5. Proposal/executor lockstep, both directions. Every internal-DB proposal tool must
    #    resolve to an approval-time executor row, and every executor row must correspond to
    #    a proposal-gated tool — a divergence means a proposal that can never execute, or an
    #    executor reachable with no drafted proposal shape.
    executor_tools = set(ACTION_TYPE_TO_TOOL.values())
    for name in sorted(reg.PROPOSAL_ONLY_WRITE_TOOLS - executor_tools):
        violations.append(
            f"proposal tool '{name}' (PROPOSAL_ONLY_WRITE_TOOLS) has no ACTION_TYPE_TO_TOOL "
            "executor row — its approved proposals could never execute"
        )
    for name in sorted(executor_tools - reg.PROPOSAL_ONLY_WRITE_TOOLS):
        violations.append(
            f"executor row for '{name}' (ACTION_TYPE_TO_TOOL) is not a PROPOSAL_ONLY_WRITE_TOOLS "
            "member — an executor without a proposal-gated drafting tool"
        )

    # 6. Handler-lane self-drafters draft their own PENDING proposal inside the handler; they
    #    must be PROPOSAL_TOOLS members (DRAFT_ONLY + creates_proposed_action) and must NOT
    #    also have an internal-DB executor row (their execution lane is the QBO/JE lane).
    for name in sorted(reg.HANDLER_LANE_SELF_DRAFTERS):
        if name not in reg.PROPOSAL_TOOLS:
            violations.append(
                f"handler-lane self-drafter '{name}' must be a PROPOSAL_TOOLS member"
            )
        if name in executor_tools:
            violations.append(
                f"handler-lane self-drafter '{name}' must not also carry an "
                "ACTION_TYPE_TO_TOOL executor row"
            )

    # 7. Specialist specs (plan 20260712-agentic-finance-os W5, GAP-5): each formal
    #    specialist must bind to a real INTENT_BUNDLES key (1:1), carry a valid risk
    #    class + non-empty mandate/criteria, and prohibit only really-registered tools.
    #    Enforced here so the §8 specialist metadata is a startup/CI gate, not decoration.
    from .specialist_specs import validate_specialist_specs

    violations.extend(
        validate_specialist_specs(frozenset(reg.INTENT_BUNDLES.keys()), frozenset(registered))
    )

    # 8. SideEffectTier map integrity (plan 20260716-five-domain-remediation AC-B1):
    #    every known action_type must be classified in exactly one tier — an
    #    UNCLASSIFIED internal type fails the build (startup + CI), never a silent
    #    approval-semantics drift. The tier checks live with the map
    #    (app/services/action_tiers.py); this wiring makes the registry build the
    #    startup gate, mirroring how the classification sets above are enforced.
    from ...services.action_tiers import validate_action_tier_integrity

    violations.extend(validate_action_tier_integrity())

    return violations


@lru_cache(maxsize=8)
def _ensure_cached(schema_sig: frozenset[str], handler_sig: frozenset[str]) -> bool:
    violations = validate_registry_integrity(schema_sig, handler_sig)
    if violations:
        detail = "\n  - ".join(violations)
        raise RegistryIntegrityError(
            f"tool registry failed integrity validation ({len(violations)} violation(s)):\n"
            f"  - {detail}\n"
            "Fix the classification sets in app/agent/runtime/tool_registry.py — do not ship a "
            "registry whose risk metadata is untrue."
        )
    return True


def ensure_registry_integrity(
    schema_names: Iterable[str], handler_names: Iterable[str]
) -> None:
    """Raise RegistryIntegrityError unless the registry is structurally sound (cached)."""
    _ensure_cached(frozenset(str(n) for n in schema_names if n), frozenset(str(n) for n in handler_names if n))

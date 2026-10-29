# Plan 20260712-agentic-finance-os W6 (GAP-6, directive §16) — model capability registry.
"""A versioned, code-defined registry of model capabilities + a router that RECORDS
its selection reason and REFUSES a fallback to an unapproved class.

The directive's zero-tolerance gate (§21): "0 silent provider fallbacks to
unapproved model classes." So routing is not a bare string map — it consults this
registry, which declares which task classes and risk classes each model is APPROVED
for, and ``select_model`` raises ``NoApprovedModelError`` rather than quietly using
a model outside its approval. Every selection carries a human-readable ``reason`` and
its ``fallback_from`` chain for the observability trace.

Model ids here MUST also appear in MODEL_PRICING_PER_MILLION (app/agent/performance.py)
so per-call cost attribution never silently drops (AP-LAO-03) — validate_model_capabilities
enforces that parity.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Task classes a call can request.
TASK_CLASSIFY = "classify"        # cheap intent/routing/extraction
TASK_ANALYSIS = "analysis"        # accounting/financial reasoning
TASK_NARRATIVE = "narrative"      # drafting a user-facing answer
TASK_LONG_CONTEXT = "long_context"  # long-document review
TASK_CRITIQUE = "critique"        # independent review / verification

_VALID_TASKS = frozenset({TASK_CLASSIFY, TASK_ANALYSIS, TASK_NARRATIVE, TASK_LONG_CONTEXT, TASK_CRITIQUE})

# Risk classes (materiality of the workflow the call serves).
RISK_LOW = "low"
RISK_STANDARD = "standard"
RISK_HIGH = "high"   # material financial output — only high-reliability models approved
_VALID_RISKS = frozenset({RISK_LOW, RISK_STANDARD, RISK_HIGH})

# Structured-output reliability tiers.
REL_HIGH = "high"
REL_MEDIUM = "medium"
_VALID_RELIABILITY = frozenset({REL_HIGH, REL_MEDIUM})

# The provider-conformance behaviours every supported provider must satisfy (directive
# §16). The live cross-provider run is a keyed/CI concern; this is the fixture SPEC.
CONFORMANCE_BEHAVIORS: tuple[str, ...] = (
    "intent_classification", "tool_selection", "typed_arguments", "multiple_tool_calls",
    "malformed_arguments", "tool_result_consumption", "structured_claims", "proposal_generation",
    "refusal", "token_truncation", "streaming", "provider_timeout", "rate_limiting", "fallback",
)


class NoApprovedModelError(RuntimeError):
    """Raised when no registered model is approved for a (task_class, risk_class) —
    the router refuses rather than silently using an unapproved model (§21)."""


@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model: str
    approved_tasks: frozenset[str]
    approved_risks: frozenset[str]
    structured_output_reliability: str
    context_window: int
    supports_tools: bool
    cost_tier: str  # economy | standard | frontier
    version: int = 1


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str
    task_class: str
    risk_class: str
    reason: str
    fallback_from: tuple[str, ...] = field(default_factory=tuple)


def _cap(provider, model, tasks, risks, reliability, ctx, tools, cost) -> ModelCapability:
    return ModelCapability(
        provider=provider, model=model, approved_tasks=frozenset(tasks),
        approved_risks=frozenset(risks), structured_output_reliability=reliability,
        context_window=ctx, supports_tools=tools, cost_tier=cost,
    )


# The registry. Model ids match MODEL_PRICING_PER_MILLION keys (parity-enforced).
MODEL_CAPABILITIES: tuple[ModelCapability, ...] = (
    _cap("anthropic", "claude-sonnet-4-5",
         [TASK_ANALYSIS, TASK_NARRATIVE, TASK_LONG_CONTEXT, TASK_CRITIQUE, TASK_CLASSIFY],
         [RISK_LOW, RISK_STANDARD, RISK_HIGH], REL_HIGH, 200_000, True, "frontier"),
    _cap("anthropic", "claude-haiku-4-5",
         [TASK_CLASSIFY, TASK_NARRATIVE],
         [RISK_LOW, RISK_STANDARD], REL_HIGH, 200_000, True, "economy"),
    _cap("openai", "gpt-4o",
         [TASK_ANALYSIS, TASK_NARRATIVE, TASK_CRITIQUE, TASK_CLASSIFY],
         [RISK_LOW, RISK_STANDARD, RISK_HIGH], REL_HIGH, 128_000, True, "standard"),
    _cap("openai", "gpt-4o-mini",
         [TASK_CLASSIFY],
         [RISK_LOW], REL_MEDIUM, 128_000, True, "economy"),
    _cap("google", "gemini-2.5-pro",
         [TASK_ANALYSIS, TASK_LONG_CONTEXT],
         [RISK_LOW, RISK_STANDARD], REL_MEDIUM, 1_000_000, True, "standard"),
    _cap("google", "gemini-2.5-flash",
         [TASK_CLASSIFY, TASK_LONG_CONTEXT],
         [RISK_LOW, RISK_STANDARD], REL_HIGH, 1_000_000, True, "economy"),
    _cap("openai", "o3-mini",
         [TASK_ANALYSIS, TASK_CRITIQUE],
         [RISK_LOW, RISK_STANDARD, RISK_HIGH], REL_HIGH, 200_000, True, "frontier"),
    _cap("openai_compatible", "deepseek-chat",
         [TASK_CLASSIFY, TASK_ANALYSIS],
         [RISK_LOW, RISK_STANDARD], REL_HIGH, 128_000, True, "economy"),
)

# Preferred pick order when several models are approved (frontier reliability first
# for high-risk; economy first for cheap classify). Keyed by (task, risk) → order is
# derived, not hardcoded per pair, in select_model.
_COST_RANK = {"economy": 0, "standard": 1, "frontier": 2}


def approved_models(task_class: str, risk_class: str) -> list[ModelCapability]:
    """Every registered model APPROVED for this (task, risk)."""
    return [
        c for c in MODEL_CAPABILITIES
        if task_class in c.approved_tasks and risk_class in c.approved_risks
    ]


def select_model(
    task_class: str,
    risk_class: str,
    *,
    prefer: str | None = None,
    unavailable: frozenset[str] = frozenset(),
) -> ModelSelection:
    """Pick an APPROVED model for (task, risk), recording the reason. High-risk work
    prefers the highest structured-output reliability then frontier tier; low-risk
    classify prefers the cheapest. ``prefer`` is honored only if it is itself
    approved. ``unavailable`` models (a provider outage) are skipped — but a skip
    NEVER drops to an unapproved model; if nothing approved remains, it refuses."""
    if task_class not in _VALID_TASKS:
        raise ValueError(f"unknown task_class '{task_class}'")
    if risk_class not in _VALID_RISKS:
        raise ValueError(f"unknown risk_class '{risk_class}'")

    candidates = [c for c in approved_models(task_class, risk_class) if c.model not in unavailable]
    if not candidates:
        raise NoApprovedModelError(
            f"no registered model is approved for task '{task_class}' at risk '{risk_class}' "
            f"(unavailable={sorted(unavailable)}) — refusing to fall back to an unapproved model"
        )

    fallback_from: list[str] = []
    if prefer is not None:
        preferred = [c for c in candidates if c.model == prefer]
        if preferred:
            chosen = preferred[0]
            return ModelSelection(
                provider=chosen.provider, model=chosen.model, task_class=task_class,
                risk_class=risk_class, reason=f"preferred model '{prefer}' is approved for this class",
            )
        fallback_from.append(prefer)  # preferred model was not approved/available → fall through

    # High-risk / critique → reliability-first then frontier tier. Else cheapest-first.
    if risk_class == RISK_HIGH or task_class == TASK_CRITIQUE:
        chosen = sorted(
            candidates,
            key=lambda c: (0 if c.structured_output_reliability == REL_HIGH else 1, -_COST_RANK[c.cost_tier]),
        )[0]
        reason = f"high-reliability pick for {task_class}/{risk_class}"
    else:
        chosen = sorted(candidates, key=lambda c: _COST_RANK[c.cost_tier])[0]
        reason = f"cheapest approved model for {task_class}/{risk_class}"

    if fallback_from:
        reason += f" (preferred '{fallback_from[0]}' was not approved/available)"
    return ModelSelection(
        provider=chosen.provider, model=chosen.model, task_class=task_class,
        risk_class=risk_class, reason=reason, fallback_from=tuple(fallback_from),
    )


def validate_model_capabilities(pricing_keys: frozenset[str]) -> list[str]:
    """Registry integrity (empty == healthy): valid task/risk/reliability vocabularies,
    every model in the pricing table (AP-LAO-03 cost-parity), no duplicate model, and
    every (task, risk) combination that is used has at least one approved model would
    be too strict — instead we assert the HIGH-risk analysis + narrative lanes are
    covered (a material answer must always have an approved model)."""
    violations: list[str] = []
    seen: set[str] = set()
    for c in MODEL_CAPABILITIES:
        if c.model in seen:
            violations.append(f"duplicate model capability '{c.model}'")
        seen.add(c.model)
        if not c.approved_tasks <= _VALID_TASKS:
            violations.append(f"model '{c.model}' has invalid task class(es): {sorted(c.approved_tasks - _VALID_TASKS)}")
        if not c.approved_risks <= _VALID_RISKS:
            violations.append(f"model '{c.model}' has invalid risk class(es): {sorted(c.approved_risks - _VALID_RISKS)}")
        if c.structured_output_reliability not in _VALID_RELIABILITY:
            violations.append(f"model '{c.model}' has invalid reliability '{c.structured_output_reliability}'")
        if c.cost_tier not in _COST_RANK:
            violations.append(f"model '{c.model}' has invalid cost_tier '{c.cost_tier}'")
        if c.model not in pricing_keys:
            violations.append(
                f"model '{c.model}' is not in MODEL_PRICING_PER_MILLION — its per-call cost "
                "would silently drop (AP-LAO-03)"
            )
    # A material (high-risk) analysis + narrative answer must always be routable.
    for task in (TASK_ANALYSIS, TASK_NARRATIVE):
        if not approved_models(task, RISK_HIGH):
            violations.append(f"no model approved for high-risk '{task}' — material answers would have no route")
    return violations

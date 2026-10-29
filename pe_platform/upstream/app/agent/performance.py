from __future__ import annotations

import logging  # PLAN-EXEMPT: live cost-attribution bug fix (gpt-4o-mini priced 16x as gpt-4o); single-file, no plan doc
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional


MODEL_PRICING_PER_MILLION: Dict[str, Dict[str, Decimal]] = {
    "claude-sonnet-4-6": {"input": Decimal("3.00"), "output": Decimal("15.00")},
    "claude-sonnet-4-5": {"input": Decimal("3.00"), "output": Decimal("15.00")},
    "claude-3-7-sonnet": {"input": Decimal("3.00"), "output": Decimal("15.00")},
    "claude-3-5-sonnet": {"input": Decimal("3.00"), "output": Decimal("15.00")},
    # Haiku 4.5 — the default fast-first model for tiered routing. Pricing is
    # approximate and used only for cost estimation/observability, not billing.
    "claude-haiku-4-5": {"input": Decimal("1.00"), "output": Decimal("5.00")},
    "claude-3-5-haiku": {"input": Decimal("0.80"), "output": Decimal("4.00")},
    "gpt-4o": {"input": Decimal("2.50"), "output": Decimal("10.00")},
    "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
    "o1": {"input": Decimal("15.00"), "output": Decimal("60.00")},
    "o3-mini": {"input": Decimal("1.10"), "output": Decimal("4.40")},
    "gemini-2.5-pro": {"input": Decimal("1.25"), "output": Decimal("10.00")},
    "gemini-2.5-flash": {"input": Decimal("0.30"), "output": Decimal("2.50")},
    "gemini-2.0-flash": {"input": Decimal("0.10"), "output": Decimal("0.40")},
    "deepseek-chat": {"input": Decimal("0.14"), "output": Decimal("0.28")},
    "deepseek-reasoner": {"input": Decimal("0.55"), "output": Decimal("2.19")},
    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 — embedding calls are
    # now metered (llm.py get_embedding); price the pinned embedding model so the
    # rows carry a cost instead of tripping the unpriced-model warning.
    "text-embedding-3-small": {"input": Decimal("0.02"), "output": Decimal("0.00")},
}

# PLAN-EXEMPT: live cost-attribution bug fix; module logger + one-time dedup for unpriced models
logger = logging.getLogger(__name__)

# Model names already warned about, so an unpriced lane logs once (not per call).
_warned_unpriced_models: set[str] = set()


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    lane: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "lane": self.lane,
            "reason": self.reason,
        }


def _pricing_key(model_name: str | None) -> str | None:
    # PLAN-EXEMPT: live cost-attribution bug fix — exact match first, else the LONGEST
    # priced key that is a substring, so "gpt-4o-mini" wins over "gpt-4o" regardless of
    # dict order. First-substring matching mispriced gpt-4o-mini ~16x (as gpt-4o).
    if not model_name:
        return None
    normalized = model_name.lower()
    if normalized in MODEL_PRICING_PER_MILLION:
        return normalized
    best: str | None = None
    for key in MODEL_PRICING_PER_MILLION:
        if key in normalized and (best is None or len(key) > len(best)):
            best = key
    return best


def is_model_priced(model_name: Optional[str]) -> bool:
    """True when ``model_name`` resolves to a row of MODEL_PRICING_PER_MILLION.

    An unpriced model's usage is recorded with no cost, so it can never count
    toward a spend budget; budget-enforcing callers refuse it instead.
    """
    return _pricing_key(model_name) is not None


def estimate_model_cost_usd(model_name: Optional[str], token_usage: Optional[Dict[str, Any]]) -> Optional[Decimal]:
    if not token_usage:
        return None
    key = _pricing_key(model_name)
    if not key:
        # PLAN-EXEMPT: live cost-attribution bug fix — never silently cost $0; warn once per
        # unpriced model so a lane pointed at an unlisted model id is visible, not invisible.
        if model_name and model_name not in _warned_unpriced_models:
            _warned_unpriced_models.add(model_name)
            logger.warning("No pricing entry for model %r; cost will be unattributed.", model_name)
        return None

    input_tokens = Decimal(str(token_usage.get("input_tokens") or 0))
    output_tokens = Decimal(str(token_usage.get("output_tokens") or 0))
    pricing = MODEL_PRICING_PER_MILLION[key]
    cost = (
        (input_tokens / Decimal(1_000_000)) * pricing["input"]
        + (output_tokens / Decimal(1_000_000)) * pricing["output"]
    )
    return cost.quantize(Decimal("0.000001"))


def summarize_steps(steps: Iterable[Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "model_call_count": 0,
        "tool_call_count": 0,
        "failed_step_count": 0,
        "total_step_latency_ms": 0,
        "tool_names": [],
    }
    tool_names: list[str] = []
    for step in steps:
        step_type = getattr(step, "step_type", None)
        if step_type == "model_call":
            summary["model_call_count"] += 1
        if step_type == "tool_call":
            summary["tool_call_count"] += 1
            tool_name = getattr(step, "tool_name", None)
            if tool_name:
                tool_names.append(tool_name)
        if getattr(step, "status", None) == "failed":
            summary["failed_step_count"] += 1
        latency_ms = getattr(step, "latency_ms", None)
        if latency_ms:
            summary["total_step_latency_ms"] += int(latency_ms)

    summary["tool_names"] = sorted(set(tool_names))
    return summary


def route_chat_model(
    *,
    provider: str,
    default_model: str,
    fast_model: Optional[str],
    reasoning_model: Optional[str],
    intent: Optional[str],
    requires_tools: bool,
) -> ModelRoute:
    normalized_provider = (provider or "claude").lower()
    default_model = default_model or ""
    fast_model = fast_model or default_model
    reasoning_model = reasoning_model or default_model

    if requires_tools and normalized_provider not in {"claude", "anthropic"}:
        return ModelRoute(
            provider=normalized_provider,
            model=default_model,
            lane="tool_runtime",
            reason="assistant_tool_loop_provider_neutral",
        )

    if intent in {"general", "conversational", "classification", "extraction"}:
        return ModelRoute(
            provider=normalized_provider,
            model=fast_model,
            lane="fast",
            reason=f"intent:{intent}",
        )

    if intent in {"deep_analysis", "alert_escalation"}:
        return ModelRoute(
            provider=normalized_provider,
            model=reasoning_model,
            lane="reasoning",
            reason=f"intent:{intent}",
        )

    return ModelRoute(
        provider=normalized_provider,
        model=default_model,
        lane="balanced",
        reason="default",
    )


def downgrade_route(
    route: ModelRoute,
    *,
    default_model: str,
    fast_model: str,
    reasoning_model: Optional[str] = None,
    reason: str = "",
) -> ModelRoute:
    """Bias a route one Claude tier cheaper under budget pressure (Phase B).

    Budget pressure is a BIAS toward cheaper routes, never a re-route across
    providers or a demotion of deep analysis into garbage:

    * Only steps DOWN within Claude/Anthropic. A non-Claude route (e.g. the
      provider-neutral tool runtime pinned by route_chat_model when tools are
      required on a non-Claude provider) is returned unchanged — we never switch
      the interactive tool loop off Claude.
    * reasoning (deep_analysis/alert_escalation) floors at ``default_model`` —
      one tier down (e.g. Opus→Sonnet), never down to the fast tier, so deep
      analysis keeps real reasoning capability.
    * balanced/default → ``fast_model`` (e.g. Sonnet→Haiku).
    * fast is already the cheapest tier; left unchanged.

    The chosen tier model is annotated into ``reason`` so the downgrade is
    auditable via model_routing_metadata.
    """
    provider = (route.provider or "").lower()
    if provider not in {"claude", "anthropic"}:
        return route

    if route.lane == "reasoning":
        target = default_model
    elif route.lane == "fast":
        target = route.model  # already cheapest
    else:  # balanced / default
        target = fast_model

    suffix = f"budget_downgrade:{reason}" if reason else "budget_downgrade"
    if not target or target == route.model:
        # Nothing cheaper to step to; keep the route but record that pressure was
        # applied (and had no cheaper tier to take).
        return ModelRoute(
            provider=route.provider,
            model=route.model,
            lane=route.lane,
            reason=f"{route.reason}|{suffix}_noop",
        )
    return ModelRoute(
        provider=route.provider,
        model=target,
        lane=route.lane,
        reason=f"{route.reason}|{suffix}",
    )

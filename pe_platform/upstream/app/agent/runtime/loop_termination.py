# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — honest terminal-reason handling
# (AP-LAO-02) + classification-gated cross-provider fallback (SYNTHESIS GAP-A2/GAP-B3).
"""Loop-termination policy for the provider-neutral tool loop.

Before this module, the loop treated ANY no-tool-call turn as a complete final answer —
a ``max_tokens``-truncated turn (half a variance analysis, a cut-off journal narrative) was
presented and persisted as if it were whole, and a provider content-filter refusal triggered
the same blind cross-provider fallback as a rate limit. Both are silent-failure classes a
financial agent cannot carry.

The loop (``model_loop.py``) stays the single caller; this module owns the POLICY so the
1300-line loop file only gains call sites:

* ``resolve_terminal_action`` — branch the adapter-normalized terminal reason into
  final / continue (pause_turn) / truncated (max_tokens) / refusal (content filter).
* ``truncated_answer`` / ``refusal_answer`` / ``stamp_*`` — the user-honest disclosure texts
  and telemetry stamps. The stamps land in the run's ``token_usage`` JSON (no migration;
  readers use ``.get``).
* ``fallback_eligible`` — cross-provider fallback fires only for error classes a different
  provider can plausibly cure (rate limit / timeout / parse / other). Auth and quota
  failures surface immediately: provider-A credentials say nothing about provider B being
  wanted, and quota is a billing decision, not a routing one.
"""
from __future__ import annotations

import logging
from typing import Any  # CRAFT-OK: dynamic provider SDK response/telemetry boundary module

from ...services.llm_usage import (
    ERROR_AUTH,
    ERROR_QUOTA_EXHAUSTED,
    classify_error,
)
from .model_adapters import (
    TERMINAL_MAX_TOKENS,
    TERMINAL_PAUSE_TURN,
    TERMINAL_REFUSAL,
    TERMINAL_UNKNOWN,
    ModelAdapter,
)

logger = logging.getLogger(__name__)

ACTION_FINAL = "final"
ACTION_CONTINUE = "continue"
ACTION_TRUNCATED = "truncated"
ACTION_REFUSAL = "refusal"

TRUNCATION_DISCLOSURE = (
    "\n\n[Note: this response was cut off by a length limit and may be incomplete. "
    "Ask me to continue for the rest.]"
)
REFUSAL_FALLBACK_TEXT = (
    "The model declined to complete this request (provider content filter). No financial "
    "data was read or changed by this refusal. Please rephrase the request, or contact "
    "support if this looks wrong."
)

# Error classes a DIFFERENT provider can plausibly cure. Auth/quota are deliberately
# excluded: retrying them on another provider hides a credential/billing problem.
_FALLBACK_INELIGIBLE = {ERROR_AUTH, ERROR_QUOTA_EXHAUSTED}


def terminal_reason_of(adapter: ModelAdapter, response: Any) -> str:  # CRAFT-OK: dynamic provider SDK response boundary
    """Read the adapter-normalized terminal reason; unknown-shaped doubles stay 'unknown'."""
    reader = getattr(adapter, "terminal_reason", None)
    if reader is None:
        return TERMINAL_UNKNOWN
    try:
        return str(reader(response))
    except Exception as exc:  # CRAFT-OK: best-effort telemetry read over an SDK double; logged, never raises into the loop
        logger.warning("[AgentRuntime] terminal_reason read failed: %s", exc)
        return TERMINAL_UNKNOWN


def resolve_terminal_action(adapter: ModelAdapter, response: Any) -> tuple[str, str]:  # CRAFT-OK: dynamic provider SDK response boundary
    """Map a no-tool-call turn to the loop action. Returns (action, normalized_reason).

    ``unknown`` and ``other:*`` reasons resolve to FINAL — backward-compatible with test
    doubles and future provider vocabularies; only the three explicitly-dangerous classes
    change behavior.
    """
    reason = terminal_reason_of(adapter, response)
    if reason == TERMINAL_MAX_TOKENS:
        return ACTION_TRUNCATED, reason
    if reason == TERMINAL_PAUSE_TURN:
        return ACTION_CONTINUE, reason
    if reason == TERMINAL_REFUSAL:
        return ACTION_REFUSAL, reason
    return ACTION_FINAL, reason


def append_paused_turn(messages: list[dict[str, Any]], response: Any) -> bool:  # CRAFT-OK: provider transcript dicts are the established loop shape
    """On pause_turn, append the paused assistant content so the resend continues it."""
    content = getattr(response, "content", None)
    if not content:
        return False
    messages.append({"role": "assistant", "content": content})
    return True


def truncated_answer(answer: str) -> str:
    """A max_tokens turn is TRUNCATED, not final — disclose it, never present as whole."""
    return (answer or "") + TRUNCATION_DISCLOSURE


def refusal_answer(answer: str) -> str:
    """A refusal turn surfaces a user-safe refusal; never retried blindly."""
    text = (answer or "").strip()
    return text if text else REFUSAL_FALLBACK_TEXT


def stamp_terminal_telemetry(
    token_usage: dict[str, Any],  # CRAFT-OK: run token_usage is the established JSON telemetry dict
    *,
    stop_reason: str,
    truncated: bool = False,
    refusal: bool = False,
) -> None:
    """Persist the decision dimensions into the run's token_usage JSON (GAP-C2 partial).

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A2A3 — unknown-reason stamp skip.
    An ``unknown`` reason (a legacy test double with no stop_reason attribute) is not
    stamped — it carries no information and real provider responses always carry one.
    """
    if stop_reason != TERMINAL_UNKNOWN:
        token_usage["stop_reason"] = stop_reason
    if truncated:
        token_usage["truncated"] = True
    if refusal:
        token_usage["refusal"] = True


def fallback_category(exc: BaseException) -> str:
    return classify_error(exc)


def fallback_eligible(category: str) -> bool:
    return category not in _FALLBACK_INELIGIBLE


def stamp_fallback_telemetry(
    token_usage: dict[str, Any],  # CRAFT-OK: run token_usage is the established JSON telemetry dict
    *,
    fallback_provider: str,
    category: str,
) -> None:
    token_usage["fallback_used"] = fallback_provider
    token_usage["fallback_reason"] = category

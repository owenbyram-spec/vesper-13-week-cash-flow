# Plan 20260712-agentic-finance-os W2 (GAP-2) — evidence package public surface.
"""Typed facts at the tool seam + semantic claim verification + evidence-derived
confidence. Handlers emit facts (facts.emit_fact) under the model loop's capture
scope; the assistant's answer compiler verifies prose claims against them."""
from .confidence import evidence_confidence
from .facts import TypedFact, capture_facts, emit_fact, facts_from_events
from .semantic import (
    S_PRESENCE_VERIFIED,
    S_SEMANTIC_MISMATCH,
    S_SEMANTIC_VERIFIED,
    S_UNVERIFIED,
    SemanticClaim,
    SemanticVerification,
    semantic_caveats,
    semantic_claims_block,
    verify_semantic_answer,
)

# The tools whose handlers emit typed facts today (decision D-6 per-tool cutover).
# Extend this roster ONLY together with the handler's emit_fact calls + its
# adversarial fixtures in tests/test_semantic_claim_verification.py.
FACT_EMITTING_TOOLS = frozenset(
    {
        "get_covenants_status",
        "get_ar_ap_summary",
        "get_cashflow_snapshot",
    }
)

__all__ = [
    "TypedFact",
    "capture_facts",
    "emit_fact",
    "facts_from_events",
    "evidence_confidence",
    "SemanticClaim",
    "SemanticVerification",
    "semantic_caveats",
    "semantic_claims_block",
    "verify_semantic_answer",
    "S_PRESENCE_VERIFIED",
    "S_SEMANTIC_MISMATCH",
    "S_SEMANTIC_VERIFIED",
    "S_UNVERIFIED",
    "FACT_EMITTING_TOOLS",
]

# Plan 20260712-agentic-finance-os W2 (GAP-2a) — evidence-derived confidence.
"""Multidimensional confidence computed from OBSERVABLES, never model self-report.

Deterministic function of the semantic-verification outcome: how much of the
answer's numeric surface was checked against typed facts (data), whether any
checked figure conflicted (calculation), and how much of the surface verified
at all (interpretation). Distinct from app/services/confidence_calibration.py,
which calibrates model confidence against a tenant's human-correction history
(decision D-9) — that axis is untouched; this one is per-answer evidence math.
Formulas are fixed and documented here; the W7 benchmark lanes calibrate them.
"""
from __future__ import annotations

from .semantic import S_SEMANTIC_MISMATCH, S_SEMANTIC_VERIFIED, SemanticVerification


def evidence_confidence(verification: SemanticVerification) -> dict:  # CRAFT-OK: explanation payload is the established metadata_json shape
    """{data, calculation, interpretation, overall} each in [0, 1].

    - data: share of claims checked against TYPED facts (semantic lane reached),
      floored at 0.4 — presence-only answers are weaker evidence, not zero.
    - calculation: 1.0 with no conflicts; each conflicting figure subtracts its
      share of the claim count, floored at 0.2 (a single conflict in a long
      answer degrades, it does not zero).
    - interpretation: plain verified share (semantic + presence).
    - overall: min of the three — one weak axis caps the answer.
    A claim-free answer carries no numeric evidence axis: all dimensions None.
    """
    total = verification.numeric_claim_count
    if total == 0:
        return {"data": None, "calculation": None, "interpretation": None, "overall": None}

    semantically_checked = sum(
        1 for c in verification.claims if c.status in (S_SEMANTIC_VERIFIED, S_SEMANTIC_MISMATCH)
    )
    mismatch_count = len(verification.mismatches)

    data = round(0.4 + 0.6 * (semantically_checked / total), 3)
    calculation = round(max(0.2, 1.0 - (mismatch_count / total)), 3) if mismatch_count else 1.0
    interpretation = round(verification.verified_count / total, 3)
    overall = min(data, calculation, interpretation)
    return {
        "data": data,
        "calculation": calculation,
        "interpretation": interpretation,
        "overall": overall,
    }

# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A5 — claim-grain evidence
# (SYNTHESIS GAP-C1; user-directed hardening pass, audited doorway).
"""Deterministic verification of numeric claims in user-visible AI answers.

Peregrine's provenance was tool-grain (which tools ran), never claim-grain (does THIS
dollar figure in the prose appear in ANY tool evidence). The eval scorer's
``numeric_correctness`` sub-score (weight 0.25) read a ``numeric_claim_count`` that had
no producer anywhere — always 0, always a perfect score. This module is the producer.

The model is never asked to verify itself: extraction and matching are pure regex/set
arithmetic over the final answer text and the actual tool-result texts from the run.
A claim VERIFIES when a source number matches within a rounding tolerance
(max($0.51, 0.5%) — answers legitimately round cents and thousands). Anything else is
UNVERIFIED and produces an explicit user-visible caveat — the honest state, never a
silent pass. Only material claim kinds are extracted (currency + percent); bare integers
are deliberately skipped (dates, counts, ids would drown the signal in noise).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SUFFIX_MULTIPLIERS = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0, "bn": 1_000_000_000.0}

# $120,000.50 · $1.2M · $(3,000) · $3k — currency claims in answer prose.
_CURRENCY_RE = re.compile(
    r"\$\s?\(?\s?(-?[\d][\d,]*(?:\.\d+)?)\s?\)?\s*(k|m|b|bn)?\b", re.IGNORECASE
)
# 12.5% · -3% — percent claims.
_PERCENT_RE = re.compile(r"(-?[\d][\d,]*(?:\.\d+)?)\s*%")
# Any numeric token in tool-result evidence (with optional $, parens-negative, suffix).
_SOURCE_NUMBER_RE = re.compile(
    r"\(?\s?\$?\s?(-?[\d][\d,]*(?:\.\d+)?)\s?\)?\s*(k|m|b|bn)?\b", re.IGNORECASE
)


@dataclass(frozen=True)
class NumericClaim:
    raw: str
    value: float
    kind: str  # "currency" | "percent"
    verified: bool = False


@dataclass(frozen=True)
class ClaimVerification:
    claims: list[NumericClaim] = field(default_factory=list)

    @property
    def numeric_claim_count(self) -> int:
        return len(self.claims)

    @property
    def verified_count(self) -> int:
        return sum(1 for claim in self.claims if claim.verified)

    @property
    def unverified(self) -> list[NumericClaim]:
        return [claim for claim in self.claims if not claim.verified]


def _to_value(number_text: str, suffix: str | None) -> float | None:
    try:
        value = float(number_text.replace(",", ""))
    except ValueError:
        return None
    if suffix:
        value *= _SUFFIX_MULTIPLIERS.get(suffix.lower(), 1.0)
    return value


def extract_numeric_claims(answer: str) -> list[NumericClaim]:
    """Extract material numeric claims (currency + percent) from answer prose."""
    text = answer or ""
    claims: list[NumericClaim] = []
    seen: set[tuple[str, float]] = set()
    for match in _CURRENCY_RE.finditer(text):
        value = _to_value(match.group(1), match.group(2))
        if value is None:
            continue
        key = ("currency", round(abs(value), 2))
        if key not in seen:
            seen.add(key)
            claims.append(NumericClaim(raw=match.group(0).strip(), value=value, kind="currency"))
    for match in _PERCENT_RE.finditer(text):
        value = _to_value(match.group(1), None)
        if value is None:
            continue
        key = ("percent", round(abs(value), 4))
        if key not in seen:
            seen.add(key)
            claims.append(NumericClaim(raw=match.group(0).strip(), value=value, kind="percent"))
    return claims


def collect_source_numbers(tool_events: list[dict]) -> list[float]:  # CRAFT-OK: tool-event dicts are the established loop telemetry shape
    """Every numeric value present in the run's actual tool-result evidence."""
    numbers: list[float] = []
    for event in tool_events or []:
        result_text = str((event or {}).get("result") or "")  # SA-OK: dict-key access on in-memory tool events, not a DB query
        for match in _SOURCE_NUMBER_RE.finditer(result_text):
            value = _to_value(match.group(1), match.group(2))
            if value is not None:
                numbers.append(value)
    return numbers


def _matches_source(value: float, source_numbers: list[float]) -> bool:
    tolerance = max(0.51, abs(value) * 0.005)
    target = abs(value)
    return any(abs(abs(source) - target) <= tolerance for source in source_numbers)


def verify_answer_claims(answer: str, tool_events: list[dict]) -> ClaimVerification:  # CRAFT-OK: tool-event dicts are the established loop telemetry shape
    """Cross-check every material numeric claim in ``answer`` against tool evidence.

    With NO tool events (a purely conversational answer), claims are still extracted but
    every one is unverified — the caller decides whether that warrants a caveat (a
    no-live-tools caveat already exists upstream).
    """
    claims = extract_numeric_claims(answer)
    if not claims:
        return ClaimVerification(claims=[])
    source_numbers = collect_source_numbers(tool_events)
    verified_claims = [
        NumericClaim(
            raw=claim.raw,
            value=claim.value,
            kind=claim.kind,
            verified=_matches_source(claim.value, source_numbers),
        )
        for claim in claims
    ]
    return ClaimVerification(claims=verified_claims)


def claims_explanation_block(verification: ClaimVerification) -> dict:  # CRAFT-OK: explanation payload is the established metadata_json shape
    """The explanation-payload block: counts + up to 5 unverified raws for the UI."""
    return {
        "numeric_claim_count": verification.numeric_claim_count,
        "verified_claim_count": verification.verified_count,
        "unverified_claims": [claim.raw for claim in verification.unverified][:5],
    }


def unverified_claims_caveat(verification: ClaimVerification) -> str | None:
    """The user-visible limitation when material claims lack tool evidence."""
    unverified = verification.unverified
    if not unverified:
        return None
    examples = ", ".join(claim.raw for claim in unverified[:3])
    return (
        f"{len(unverified)} numeric figure(s) in this answer ({examples}) could not be "
        "matched to retrieved data — verify before relying on them."
    )

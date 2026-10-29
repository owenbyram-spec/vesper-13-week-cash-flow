# Plan 20260712-agentic-finance-os W2 (GAP-2) — semantic claim verification.
"""Bind numeric claims in answer prose to typed facts and verify MEANING.

The presence lane (app/services/claim_verification.py) asks "does this number
appear in ANY tool output" — sign-blind and metric-blind by design. This lane
asks the semantic question: WHICH metric does the prose claim, and does a typed
fact for THAT metric match on value, sign, and period. Everything here is
regex/Decimal arithmetic over the answer text, the catalog, and handler-emitted
facts — the model never verifies itself.

Lane precedence per claim (decision D-6 — per-tool cutover, presence retained):
  1. claim binds to a catalog metric AND facts exist for it → SEMANTIC verdict
     (verified, or MISMATCH — a mismatch is a conflict, stronger than unverified);
  2. claim binds but no facts were emitted for that metric (tool not yet in the
     covered set) → presence lane;
  3. claim does not bind → presence lane.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ..claim_verification import collect_source_numbers
from app.semantics import CANONICAL_METRICS, Unit
from .facts import TypedFact, facts_from_events

_SUFFIX = {"k": Decimal(1_000), "m": Decimal(1_000_000), "b": Decimal(1_000_000_000), "bn": Decimal(1_000_000_000)}

# Currency claims incl. parenthesised negatives: $120,000.50 · $1.2M · $(3,000) · ($3,000)
_CURRENCY_SPAN_RE = re.compile(
    r"(\()?\s?\$\s?(\()?\s?(-?[\d][\d,]*(?:\.\d+)?)\s?\)?\s*(k|m|b|bn)?\b", re.IGNORECASE
)
# Percent claims: 12.5% · -3% · (3.2)%
_PERCENT_SPAN_RE = re.compile(r"(\()?\s?(-?[\d][\d,]*(?:\.\d+)?)\s?\)?\s*%")

# How far around the number we look for a metric name / period token.
_CONTEXT_BEFORE = 100
_CONTEXT_AFTER = 60

# Closed negation-cue vocabulary: prose legitimately states a negative flow as a
# positive token ("declined by $3,000"). A positive-token claim against a negative
# fact passes ONLY when one of these cues is in the context window; a bare positive
# against a negative fact is the sign-inversion error this lane exists to catch.
_NEGATION_CUES = (
    "decline", "decrease", "drop", "down", "negative", "loss", "deficit",
    "shortfall", "burn", "outflow", "owe", "reduction", "fell", "under water",
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_TOKEN_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?\s*(\d{4})?", re.IGNORECASE
)

S_SEMANTIC_VERIFIED = "semantic_verified"
S_SEMANTIC_MISMATCH = "semantic_mismatch"
S_PRESENCE_VERIFIED = "presence_verified"
S_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class SemanticClaim:
    raw: str
    kind: str  # "currency" | "percent"
    value: Decimal  # signed
    metric_id: str | None
    status: str
    reason: str | None = None
    fact_id: str | None = None

    @property
    def verified(self) -> bool:
        return self.status in (S_SEMANTIC_VERIFIED, S_PRESENCE_VERIFIED)


@dataclass(frozen=True)
class SemanticVerification:
    claims: list[SemanticClaim]

    @property
    def numeric_claim_count(self) -> int:
        return len(self.claims)

    @property
    def verified_count(self) -> int:
        return sum(1 for c in self.claims if c.verified)

    @property
    def unverified(self) -> list[SemanticClaim]:
        return [c for c in self.claims if not c.verified]

    @property
    def mismatches(self) -> list[SemanticClaim]:
        return [c for c in self.claims if c.status == S_SEMANTIC_MISMATCH]


@dataclass(frozen=True)
class _Span:
    raw: str
    kind: str
    value: Decimal
    start: int
    end: int


def _to_decimal(number_text: str, suffix: str | None, negative: bool) -> Decimal | None:
    try:
        value = Decimal(number_text.replace(",", ""))
    except ArithmeticError:
        return None
    if suffix:
        value *= _SUFFIX.get(suffix.lower(), Decimal(1))
    if negative and value > 0:
        value = -value
    return value


def _extract_spans(answer: str) -> list[_Span]:
    spans: list[_Span] = []
    seen: set[tuple[str, str]] = set()
    for match in _CURRENCY_SPAN_RE.finditer(answer):
        negative = bool(match.group(1) or match.group(2)) or match.group(3).startswith("-")
        value = _to_decimal(match.group(3), match.group(4), negative)
        if value is None:
            continue
        key = ("currency", str(abs(value)))
        if key in seen:
            continue
        seen.add(key)
        spans.append(_Span(match.group(0).strip(), "currency", value, match.start(), match.end()))
    for match in _PERCENT_SPAN_RE.finditer(answer):
        negative = bool(match.group(1)) or match.group(2).startswith("-")
        value = _to_decimal(match.group(2), None, negative)
        if value is None:
            continue
        key = ("percent", str(abs(value)))
        if key in seen:
            continue
        seen.add(key)
        spans.append(_Span(match.group(0).strip(), "percent", value, match.start(), match.end()))
    return spans


def _clause_window(answer: str, span: _Span) -> tuple[str, int]:
    """The claim's own clause (bounded by the nearest sentence/clause delimiters
    inside the ±context span) plus the number's offset within it. Shared by metric
    binding, period, and sign checks so a NEIGHBOURING clause's month token or
    negation cue can never leak in and produce a false conflict."""
    lo = max(0, span.start - _CONTEXT_BEFORE)
    hi = min(len(answer), span.end + _CONTEXT_AFTER)
    segment = answer[lo:hi]
    rel_start = span.start - lo
    rel_end = span.end - lo
    boundary_before = max((segment.rfind(ch, 0, rel_start) for ch in ".;!?\n"), default=-1)
    after = [idx for idx in (segment.find(ch, rel_end) for ch in ".;!?\n") if idx != -1]
    clause_lo = boundary_before + 1
    clause_hi = min(after) if after else len(segment)
    return segment[clause_lo:clause_hi], rel_start - clause_lo


def _metric_tokens() -> list[tuple[str, str, Unit]]:
    """(token, metric_id, unit) for every catalog label + alias, longest-first so
    'ebitda margin' outbinds 'ebitda' when both are in the window."""
    tokens: list[tuple[str, str, Unit]] = []
    for metric in CANONICAL_METRICS.values():
        names = (metric.label, *metric.aliases)
        for name in names:
            # Prefix-resolution slugs (``cash:`` / ``custom:``) are machine keys for
            # resolve_alias, never natural-language prose — a literal "cash:" in
            # "projected cash:" must not out-bind a real metric phrase.
            if name.endswith(":"):
                continue
            cleaned = name.lower().replace(" %", "").replace("%", "").replace(" ($)", "").replace(" (%)", "").strip()
            if cleaned:
                tokens.append((cleaned, metric.metric_id, metric.unit))
    tokens.sort(key=lambda t: len(t[0]), reverse=True)
    return tokens


def _bind_metric(answer: str, span: _Span) -> str | None:
    """The catalog metric this claim's surrounding prose names, or None.

    Unit-filtered (a % claim never binds a currency metric), longest-token-first,
    nearest-occurrence-to-the-number wins; a distance tie between two DIFFERENT
    metrics is ambiguous → unbound (presence lane, never a guess). The window is
    clamped to the claim's own clause — a metric named in the previous clause
    must not capture an unrelated number ("AR is $50,000; a $777 fee applies")."""
    clause, number_pos = _clause_window(answer, span)
    window = clause.lower()

    want_units = {Unit.PERCENT} if span.kind == "percent" else {Unit.CURRENCY}
    best: tuple[int, int, str] | None = None  # (distance, -token_len, metric_id)
    for token, metric_id, unit in _metric_tokens():
        if unit not in want_units:
            continue
        idx = window.find(token)
        while idx != -1:
            distance = abs(idx - number_pos)
            candidate = (distance, -len(token), metric_id)
            if best is None or candidate < best:
                best = candidate
            elif candidate[:2] == best[:2] and metric_id != best[2]:
                return None  # exact tie between different metrics — ambiguous
            idx = window.find(token, idx + 1)
    return best[2] if best else None


def _period_token_ok(answer: str, span: _Span, fact: TypedFact) -> bool:
    """False only when the prose names a month (and optionally year) the fact's
    period does not cover — the same-metric/different-period rejection. Scoped to
    the claim's own clause so a month in a NEIGHBOURING clause cannot false-reject."""
    window, _ = _clause_window(answer, span)
    fact_months = set()
    cursor = fact.period_start.replace(day=1)
    while cursor <= fact.period_end:
        fact_months.add((cursor.year, cursor.month))
        cursor = cursor.replace(year=cursor.year + (cursor.month == 12), month=(cursor.month % 12) + 1)
    for match in _MONTH_TOKEN_RE.finditer(window):
        month = _MONTHS[match.group(1).lower()]
        year_text = match.group(2)
        if year_text:
            if (int(year_text), month) not in fact_months:
                return False
        elif month not in {m for _, m in fact_months}:
            return False
    return True


def _sign_ok(answer: str, span: _Span, fact_value: Decimal) -> bool:
    if fact_value == 0 or span.value == 0:
        return True  # zero carries no sign information
    if (span.value > 0) == (fact_value > 0):
        return True
    if span.value > 0 > fact_value:
        clause, _ = _clause_window(answer, span)
        window = clause.lower()
        return any(cue in window for cue in _NEGATION_CUES)
    return False  # prose says negative, fact is positive — never sign-safe


def _tolerance(value: Decimal) -> Decimal:
    return max(Decimal("0.51"), abs(value) * Decimal("0.005"))


def verify_semantic_answer(answer: str, tool_events: list[dict]) -> SemanticVerification:  # CRAFT-OK: tool-event dicts are the established loop telemetry shape
    """Cross-check every material numeric claim in ``answer`` — semantically where
    facts exist, presence-grain otherwise (D-6 lane precedence)."""
    text = answer or ""
    spans = _extract_spans(text)
    if not spans:
        return SemanticVerification(claims=[])

    facts = facts_from_events(tool_events or [])
    facts_by_metric: dict[str, list[TypedFact]] = {}
    for fact in facts:
        facts_by_metric.setdefault(fact.metric_id, []).append(fact)
    source_numbers = [Decimal(str(n)) for n in collect_source_numbers(tool_events or [])]

    claims: list[SemanticClaim] = []
    for span in spans:
        metric_id = _bind_metric(text, span)
        metric_facts = facts_by_metric.get(metric_id or "", [])  # SA-OK: dict-index on the in-memory fact map built above, not a DB query
        if metric_id and metric_facts:
            claims.append(_judge_against_facts(text, span, metric_id, metric_facts))
        else:
            claims.append(_presence_judge(span, metric_id, source_numbers))
    return SemanticVerification(claims=claims)


def _judge_against_facts(
    text: str, span: _Span, metric_id: str, metric_facts: list[TypedFact]
) -> SemanticClaim:
    tol = _tolerance(span.value)
    value_matches = [f for f in metric_facts if abs(abs(f.value) - abs(span.value)) <= tol]
    for fact in value_matches:
        if _sign_ok(text, span, fact.value) and _period_token_ok(text, span, fact):
            return SemanticClaim(
                raw=span.raw, kind=span.kind, value=span.value, metric_id=metric_id,
                status=S_SEMANTIC_VERIFIED, fact_id=fact.fact_id,
            )
    if value_matches:
        fact = value_matches[0]
        reason = (
            f"sign conflict: answer states {span.raw} but the computed "
            f"{CANONICAL_METRICS[metric_id].label} is {fact.value}"
            if not _sign_ok(text, span, fact.value)
            else (
                f"period conflict: the computed {CANONICAL_METRICS[metric_id].label} value "
                f"{fact.value} is for {fact.period_start}–{fact.period_end}"
            )
        )
        return SemanticClaim(
            raw=span.raw, kind=span.kind, value=span.value, metric_id=metric_id,
            status=S_SEMANTIC_MISMATCH, reason=reason, fact_id=fact.fact_id,
        )
    nearest = min(metric_facts, key=lambda f: abs(abs(f.value) - abs(span.value)))
    return SemanticClaim(
        raw=span.raw, kind=span.kind, value=span.value, metric_id=metric_id,
        status=S_SEMANTIC_MISMATCH, fact_id=nearest.fact_id,
        reason=(
            f"value conflict: answer states {span.raw} for "
            f"{CANONICAL_METRICS[metric_id].label} but the computed value is {nearest.value}"
        ),
    )


def _presence_judge(
    span: _Span, metric_id: str | None, source_numbers: list[Decimal]
) -> SemanticClaim:
    tol = _tolerance(span.value)
    target = abs(span.value)
    present = any(abs(abs(n) - target) <= tol for n in source_numbers)
    return SemanticClaim(
        raw=span.raw, kind=span.kind, value=span.value, metric_id=metric_id,
        status=S_PRESENCE_VERIFIED if present else S_UNVERIFIED,
        reason=None if present else "not found in any tool evidence",
    )


def semantic_claims_block(verification: SemanticVerification) -> dict:  # CRAFT-OK: explanation payload is the established metadata_json shape
    """Explanation-payload block — a SUPERSET of the presence lane's keys so every
    existing consumer (eval exporter, UI) keeps working unchanged."""
    return {
        "numeric_claim_count": verification.numeric_claim_count,
        "verified_claim_count": verification.verified_count,
        "unverified_claims": [c.raw for c in verification.unverified][:5],
        "semantic": {
            "bound_count": sum(1 for c in verification.claims if c.metric_id),
            "semantic_verified_count": sum(
                1 for c in verification.claims if c.status == S_SEMANTIC_VERIFIED
            ),
            "mismatches": [
                {"raw": c.raw, "metric_id": c.metric_id, "reason": c.reason}
                for c in verification.mismatches
            ][:5],
        },
    }


def semantic_caveats(verification: SemanticVerification) -> list[str]:
    """User-visible limitations. A semantic MISMATCH is a conflict (the figure
    contradicts a deterministic computation) — worded harder than mere absence."""
    out: list[str] = []
    mismatches = verification.mismatches
    if mismatches:
        first = mismatches[0]
        out.append(
            f"{len(mismatches)} figure(s) in this answer CONFLICT with deterministically "
            f"computed values — e.g. {first.raw}: {first.reason}. Do not rely on them."
        )
    plain = [c for c in verification.unverified if c.status == S_UNVERIFIED]
    if plain:
        examples = ", ".join(c.raw for c in plain[:3])
        out.append(
            f"{len(plain)} numeric figure(s) in this answer ({examples}) could not be "
            "matched to retrieved data — verify before relying on them."
        )
    return out


def verification_status(verification: SemanticVerification) -> str:
    """Observable numeric-check coverage; presence matching is not semantic checking."""
    if verification.mismatches:
        return "conflict"
    if not verification.claims:
        return "no_numeric_claims"
    if all(claim.status == S_SEMANTIC_VERIFIED for claim in verification.claims):
        return "checked_numeric_claims"
    return "limited"

"""
In-process retrieval over the ASC knowledge map.

No DB and no embeddings — the corpus is 150 short, static records, so a scored
substring/token match is fast, deterministic, and good enough for triage and
issue-spotting. The agent calls this through the ``search_asc_guidance`` tool;
final accounting conclusions still require authoritative ASC retrieval.

Ranking signals (highest weight first):
    * exact ASC code in the query (e.g. "ASC 606", "606", "842")
    * route-tag name appearing in the query
    * token overlap with the ASC area label
    * token overlap with the common-trigger phrase
    * token overlap with the guidance text
"""
from __future__ import annotations

import re
from typing import Optional

from .data import ASC_TOPICS, ROUTE_TAGS

# Tokens that add no routing signal — dropped before matching so a query like
# "what does the lease standard say" keys on "lease"/"standard", not "what"/"the".
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "to", "is", "are", "do", "does", "what",
    "how", "should", "we", "i", "our", "my", "and", "or", "on", "in", "with",
    "about", "asc", "gaap", "rule", "rules", "standard", "standards",
    "accounting", "treatment", "treat", "account", "when", "this", "that",
    "it", "be", "can", "if", "under",
})

# Matches "ASC 606", "606-10", "842", "350-40" etc. so a code in the query is a
# strong, precise signal.
_CODE_RE = re.compile(r"\b(\d{3}(?:-\d{2,3})?)\b")


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", (text or "").lower())
    return {tok for tok in raw if tok not in _STOPWORDS and len(tok) > 1}


def _codes_in(text: str) -> set[str]:
    return set(_CODE_RE.findall(text or ""))


def _score(topic: dict, q_tokens: set[str], q_codes: set[str], q_lower: str) -> float:
    score = 0.0

    # Exact ASC code match — the strongest signal.
    topic_codes = _codes_in(topic["asc_code"])
    if q_codes and topic_codes & q_codes:
        score += 12.0

    # Route-tag named in the query (e.g. "leases", "revenue").
    tag = topic["route_tag"]
    if tag.replace("_", " ") in q_lower or tag in q_lower:
        score += 5.0

    if not q_tokens:
        return score

    area_tokens = _tokens(topic["asc_area"])
    trigger_tokens = _tokens(topic["common_trigger"])
    guidance_tokens = _tokens(topic["agent_guidance"])
    tag_tokens = _tokens(tag.replace("_", " "))

    score += 3.0 * len(q_tokens & area_tokens)
    score += 2.5 * len(q_tokens & tag_tokens)
    score += 2.0 * len(q_tokens & trigger_tokens)
    score += 1.0 * len(q_tokens & guidance_tokens)
    return score


def search_asc(query: str, *, limit: int = 6, route_tag: Optional[str] = None) -> list[dict]:
    """Return the most relevant ASC topics for a free-text query.

    Each hit is the topic dict plus a ``score``. Results are sorted by score
    (desc), then ascending ``id`` for stable ordering. ``route_tag``, when
    supplied and valid, restricts the search to that cluster.
    """
    q = (query or "").strip()
    q_lower = q.lower()
    q_tokens = _tokens(q)
    q_codes = _codes_in(q)

    candidates = ASC_TOPICS
    if route_tag and route_tag in ROUTE_TAGS:
        candidates = [t for t in candidates if t["route_tag"] == route_tag]

    scored = []
    for topic in candidates:
        s = _score(topic, q_tokens, q_codes, q_lower)
        if s > 0:
            scored.append((s, topic))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [{**topic, "score": round(score, 2)} for score, topic in scored[:limit]]


_RISK_LABEL = {
    "low": "Low",
    "medium": "Medium",
    "high": "HIGH — CPA review recommended",
    "specialist": "SPECIALIST — escalate to CPA / valuation / tax",
}

# Label used when a record's binding_status is "proposed-not-binding". It MUST be
# unmistakable — a proposal is never current GAAP, and the agent must never
# present it as mandatory. The literal substring "proposed-not-binding" is part
# of the contract (asserted in tests/test_asc_schema_currency.py).
_PROPOSED_LABEL = "PROPOSED — NOT BINDING (proposed-not-binding; no final ASU — do NOT treat as current/mandatory GAAP)"


def _currency_line(r: dict) -> str:
    """Render a record's regulatory-currency metadata into one readable line.

    Always surfaces binding status + last-reviewed date; adds the grounded
    effective date + citation when present, and the amended-by ASU pointers when
    any apply. A ``proposed-not-binding`` record is ALWAYS rendered with the
    explicit not-binding label so the agent can never mistake a proposal for
    live GAAP. A record with no grounded effective date says so plainly (a flag
    for the CPA content pass) rather than inventing one.
    """
    if r.get("binding_status") == "proposed-not-binding":
        status = _PROPOSED_LABEL
    else:
        status = "Binding (effective GAAP)"

    parts = [f"Status: {status}", f"Last reviewed: {r.get('last_reviewed') or 'n/a'}"]

    effective = r.get("effective_date")
    citation = r.get("source_citation")
    if effective and citation:
        parts.append(f"Effective: {effective}")
        parts.append(f"Citation: {citation}")
    else:
        parts.append("Effective date: not yet recorded (pending CPA content review — do not infer)")

    amended = r.get("amended_by") or []
    if amended:
        parts.append(f"Amended/affected by: {', '.join(amended)}")

    return "  |  ".join(parts)


def format_results(query: str, results: list[dict]) -> str:
    """Render search hits as an assistant-readable block.

    The trailing reminders keep the agent honest: this is a routing/triage
    layer, conclusions need authoritative retrieval, and the reporting basis
    must be established before applying any ASC guidance.
    """
    if not results:
        tags = ", ".join(sorted(ROUTE_TAGS))
        return (
            f"No ASC topics matched '{query}'. Try a more specific term, an ASC "
            f"code (e.g. 'ASC 606'), or a routing cluster: {tags}."
        )

    needs_review = any(r.get("requires_cpa_review") for r in results)
    lines = [f"=== ASC GUIDANCE — top {len(results)} match(es) for '{query}' ==="]
    for r in results:
        code = f"{r['asc_code']} · " if r["asc_code"] else ""
        risk = _RISK_LABEL.get(r["risk_level"], r["risk_level"])  # SA-OK: bounded-loop dict lookup on an in-memory result, not a DB query
        lines.append(f"\n[{r['id']}] {r['asc_area']}")
        lines.append(f"  Cluster: {r['route_tag']}  |  Risk: {risk}")
        lines.append(f"  {_currency_line(r)}")
        lines.append(f"  Triggered by: {r['common_trigger']}")
        lines.append(f"  Guidance: {code}{r['agent_guidance']}")

    # PLAN-EXEMPT: sub-agent executing ratified plan 20260713-wave0-ai-integration-p0 chunk C4
    # (AC-8 / RK-2-immediate; marker RATIFICATION-2026-07-13.md — active-plan.json is orchestrator-
    # session state a background agent cannot regenerate). US-GAAP-only scoping per ADR 0003.
    lines.append(
        "\n— This is a triage/routing layer, not authoritative GAAP. Confirm any "
        "conclusion against current FASB ASC text or approved firm guidance. "
        "First establish the reporting basis (GAAP vs tax/cash/modified-cash/"
        "FRF for SMEs); ASC applies only on a GAAP basis — specifically US "
        "GAAP. Non-US financial-reporting frameworks — IFRS, ASPE, and "
        "FRS 102 — are OUT OF SCOPE for this triage layer (ADR 0003: "
        "Vesper Bot's standards knowledge is US-GAAP-only). If the tenant's "
        "reporting basis is a non-US-GAAP framework (IFRS, ASPE, FRS 102, or "
        "another national framework), REFUSE to triage under ASC and say why: "
        "applying US-GAAP guidance to a different framework is a mis-triage, "
        "and this corpus carries no IFRS/ASPE/FRS 102 knowledge. Do not "
        "conflate book and tax treatment."
    )
    if needs_review:
        lines.append(
            "— One or more matched topics are high-risk/judgment-heavy and are "
            "flagged for CPA (and possibly valuation/legal/tax-specialist) review."
        )
    return "\n".join(lines)

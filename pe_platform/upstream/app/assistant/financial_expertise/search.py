"""
In-process retrieval over the financial-expertise knowledge base.

Mirrors ``app.assistant.asc_knowledge.search`` (the stage-2-ratified keyword-v1
decision): no DB, no embeddings — the corpus is a few dozen short static
records, so a scored token match is fast, deterministic, and gate-tested by the
golden-query set in tests/test_financial_expertise_kb.py. The agent calls this
through the ``search_financial_expertise`` tool (read-only).

Ranking signals (highest weight first):
    * a standard code in the query matching the record topic (e.g. "606", "855")
    * the domain name appearing in the query ("lbo", "cashflow", "close")
    * token overlap with the topic label
    * token overlap with the trigger phrase
    * token overlap with the guidance text

Rendering contract (tested): every hit surfaces its ``authority_tier``,
``source_citation``, and dates. A practitioner-methodology record is never
rendered as mandatory/regulatory; a proposed-not-binding record always carries
an unmistakable not-binding label.
"""
from __future__ import annotations

import re

from .data import DOMAINS, FINANCIAL_EXPERTISE_RECORDS

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "to", "is", "are", "do", "does", "what",
    "how", "should", "we", "i", "our", "my", "and", "or", "on", "in", "with",
    "about", "when", "this", "that", "it", "be", "can", "if", "under", "versus",
    "vs", "work", "works",
})

# Matches ASC-style codes ("606", "350-20", "855") so a code in the query is a
# strong, precise signal against the record topics that carry one.
_CODE_RE = re.compile(r"\b(\d{3}(?:-\d{2,3})?)\b")


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", (text or "").lower())
    return {tok for tok in raw if tok not in _STOPWORDS and len(tok) > 1}


def _codes_in(text: str) -> set[str]:
    return set(_CODE_RE.findall(text or ""))


def _score(record: dict, q_tokens: set[str], q_codes: set[str], q_lower: str) -> float:
    score = 0.0

    topic = record["topic"]
    if q_codes and (_codes_in(topic) & q_codes):
        score += 12.0

    domain = record["domain"]
    if domain in q_tokens or domain in q_lower.replace(" ", ""):
        score += 4.0

    if not q_tokens:
        return score

    score += 3.0 * len(q_tokens & _tokens(topic))
    score += 2.0 * len(q_tokens & _tokens(record["trigger"]))
    score += 1.0 * len(q_tokens & _tokens(record["guidance"]))
    return score


def search_financial_expertise(
    query: str, *, limit: int = 6, domain: str | None = None
) -> list[dict]:
    """Return the most relevant expertise records for a free-text query.

    Each hit is the record dict plus a ``score``; sorted by score (desc), then
    ascending ``id`` for stable ordering. ``domain``, when supplied and valid,
    restricts the search to that domain.
    """
    q = (query or "").strip()
    q_lower = q.lower()
    q_tokens = _tokens(q)
    q_codes = _codes_in(q)

    candidates = FINANCIAL_EXPERTISE_RECORDS
    if domain and domain in DOMAINS:
        candidates = [r for r in candidates if r["domain"] == domain]

    scored = []
    for record in candidates:
        s = _score(record, q_tokens, q_codes, q_lower)
        if s > 0:
            scored.append((s, record))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [{**record, "score": round(score, 2)} for score, record in scored[:limit]]


# Authority-tier labels. The three-way vocabulary is the plan's E5 schema
# decision: practitioner method content must never read as regulatory, and a
# proposal must never read as current law. The literal tier value is part of
# each label (asserted in tests/test_financial_expertise_kb.py).
_TIER_LABELS = {
    "binding": "BINDING (binding — effective GAAP / statute)",
    "proposed-not-binding": (
        "PROPOSED — NOT BINDING (proposed-not-binding; no final standard — "
        "do NOT treat as current/mandatory)"
    ),
    "practitioner-methodology": (
        "PRACTITIONER METHODOLOGY (practitioner-methodology — best practice, "
        "not a regulatory requirement)"
    ),
}


def _authority_line(r: dict) -> str:
    """Render authority tier + citation + dates into one readable line.

    All three AC-1.1 fields (tier, citation, dates) always surface so the
    agent can cite them. An ungrounded effective date says so plainly rather
    than inventing one.
    """
    tier = r.get("authority_tier")
    label = _TIER_LABELS.get(tier, f"UNKNOWN TIER ({tier})")  # SA-OK: in-memory dict lookup on a static label map, not a DB query
    parts = [f"Authority: {label}"]

    effective = r.get("effective_date")
    if effective:
        parts.append(f"Effective: {effective}")
    elif tier == "binding":
        parts.append("Effective date: not recorded — do not infer")

    parts.append(f"Source: {r.get('source_citation') or 'MISSING CITATION'}")
    parts.append(f"Accessed: {r.get('accessed_date') or 'n/a'}")
    parts.append(f"Last reviewed: {r.get('last_reviewed') or 'n/a'}")
    return "  |  ".join(parts)


def format_results(query: str, results: list[dict]) -> str:
    """Render search hits as an assistant-readable block (cf. asc_knowledge)."""
    if not results:
        domains = ", ".join(sorted(DOMAINS))
        return (
            f"No financial-expertise records matched '{query}'. Try a more "
            f"specific method term (e.g. 'cash sweep', '13-week forecast', "
            f"'flux thresholds') or a domain: {domains}."
        )

    lines = [
        f"=== FINANCIAL EXPERTISE — top {len(results)} match(es) for '{query}' ==="
    ]
    for r in results:
        lines.append(f"\n[{r['id']}] ({r['domain']}) {r['topic']}")
        lines.append(f"  {_authority_line(r)}")
        lines.append(f"  Triggered by: {r['trigger']}")
        lines.append(f"  Guidance: {r['guidance']}")

    lines.append(
        "\n— Reference layer distilled from cited research; cite the source, "
        "the authority tier, and the dates when you use a record. A "
        "practitioner-methodology item is best practice, not a regulatory "
        "requirement — never present it as required by GAAP or law. A value "
        "this layer does not carry (a rate, a threshold, a market grid) is a "
        "named gap: surface it, never guess it."
    )
    return "\n".join(lines)

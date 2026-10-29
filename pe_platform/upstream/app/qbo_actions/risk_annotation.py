# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C2 (AC-4 RK-3a) owns app/qbo_actions/risk_annotation.py — serve-time risk annotation, new pure module.
"""risk_annotation — serve-time ASC-topic risk annotation for PENDING JE proposals.

RK-3a (plan 20260713-wave0-ai-integration-p0, AC-4 / decision 3): JE drafting sits
adjacent to prompt-only accounting knowledge — the platform has REAL computational
engines for some ASC topics (leases, schedules, cut-off) and NOTHING but prompt
text for others (CECL, inventory, FX, income taxes, ...). A PENDING journal-entry
proposal whose accounts signal an engine-LESS high-risk topic carries an honest
serve-time annotation in the approval inbox: ``engine_backed: false`` + the topics
+ "no computational support — CPA review required". It annotates, never blocks.

This module is PURE: data tables + functions over plain payload lines and an
``account_id -> account_type`` dict the caller batch-fetched (ONE ``qbo_accounts``
IN-query per response — see ``app/routers/actions.py``). No DB, no I/O here.

Mapping honesty (mirrors the cutoff-check honest-degradation pattern,
``app/services/review_checks/cutoff_check.py``): a line that resolves NEITHER via
its account_type NOR via a name keyword lands in the returned
``unmapped_accounts`` list for logging — NEVER a guessed topic.

Topic clusters are grounded in the ASC corpus ``ROUTE_TAGS`` + records
(``app/assistant/asc_knowledge/data.py``): 326 (records 20–21, risk high/CECL),
330 (records 31–36, inventory_cogs), 830 (records 143–144, high/specialist),
740 (records 130–135, high/specialist), 815 (record 145, specialist),
718 (records 122–123, high).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TypedDict

# ProposedAction.action_type strings that mean "journal entry": POST_JE is the
# canonical proposer lane (qbo_actions/proposer.py:21), JOURNAL_ENTRY the legacy
# chat-handler lane (assistant/handlers.py:391). Both are executor-accepted
# (executor.py:365) — the serializer must annotate both.
JE_ACTION_TYPES = frozenset({"POST_JE", "JOURNAL_ENTRY"})

RISK_NOTICE = "no computational support — CPA review required"


class RiskAnnotation(TypedDict):
    """The serve-time annotation attached under ``risk_annotation``."""

    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 6 contract update: THIS pure
    # module still ALWAYS emits engine_backed: False. The actions.py serializer
    # (_attach_risk_annotation) MAY replace the annotation with an
    # engine_backed: true variant for a PENDING JE whose payload carries the
    # BACKEND-stamped ``computation_evidence`` key — key-presence IS provenance
    # (every JE draft lane allowlist-constructs its payload, so a model cannot
    # forge the stamp; only propose_journal_entry's computation_evidence kwarg
    # writes it). The CPA-review notice NEVER lifts on the upgraded variant, and
    # ASC 326 STAYS in ENGINELESS_HIGH_RISK_TOPICS (topics never move registers).
    engine_backed: bool  # False from annotate_je_lines; serializer may upgrade on backend provenance
    topics: list[str]
    notice: str


# ── (a) The engine-backed topic register ─────────────────────────────────────
# Topics Peregrine has REAL computational support for (plan decision 3 register:
# "842 lease engine, 250 schedule engine, 606/855 cutoff check,
# schedule/depreciation math"). A JE touching only these clusters gets NO
# annotation — an engine, not just a prompt, stands behind the treatment.
ENGINE_BACKED_TOPICS: dict[str, str] = {
    "ASC 842": "lease engine (lease schedules)",
    "ASC 250": "schedule engine (change/correction roll-forwards)",
    "ASC 606": "revenue cut-off check (services/review_checks/cutoff_check.py)",
    "ASC 855": "subsequent-events cut-off check (services/review_checks/cutoff_check.py)",
    # "schedule/depreciation math" concretized to the two codes the schedule
    # engine's depreciation/amortization schedules actually cover:
    "ASC 360": "schedule/depreciation math (PP&E depreciation schedules)",
    "ASC 350": "schedule/amortization math (intangible amortization schedules)",
}

# ── Engine-less HIGH-RISK clusters (the annotation trigger set) ──────────────
# 326/330/830/740 are the plan's required minimum; 815 + 718 are added because
# their corpus records are risk "high"/"specialist" + requires_cpa_review=True
# with strong, low-FP account-name signals ("swap"/"hedge"/"derivative",
# "stock comp"/"share-based").
ENGINELESS_HIGH_RISK_TOPICS: dict[str, str] = {
    "ASC 326": "credit losses (CECL) — corpus records 20–21",
    "ASC 330": "inventory — corpus records 31–36",
    "ASC 830": "foreign currency — corpus records 143–144",
    "ASC 740": "income taxes — corpus records 130–135",
    "ASC 815": "derivatives and hedging — corpus record 145",
    "ASC 718": "stock compensation — corpus records 122–123",
}

# ── (b) QBO AccountType → ASC topic cluster ──────────────────────────────────
# Only the AccountType values that carry an unambiguous topic signal map
# directly; everything else resolves via name keywords or is routine.
_ACCOUNT_TYPE_TO_TOPIC: dict[str, str] = {
    "Accounts Receivable": "ASC 326",  # a direct JE to A/R is the allowance/write-off move
    "Cost of Goods Sold": "ASC 330",   # inventory_cogs cluster (330, 705)
    "Income": "ASC 606",               # revenue cluster — engine-backed (cut-off check)
    "Fixed Asset": "ASC 360",          # depreciation math — engine-backed
}

# AccountTypes that are a RESOLVED identity but carry no single topic — the name
# keywords are the only discriminator inside them; with no keyword hit the line
# is routine (resolved, no annotation contribution), never a guessed topic.
_NO_TOPIC_ACCOUNT_TYPES = frozenset(
    {
        "Bank",
        "Credit Card",
        "Accounts Payable",
        "Equity",
        "Expense",
        "Other Expense",
        "Other Income",
        "Other Current Asset",
        "Other Asset",
        "Other Current Liability",
        "Long Term Liability",
    }
)

# ── Account-name keyword → ASC topic cluster (ordered; first match wins) ─────
# Keywords are grounded in the corpus ROUTE_TAGS trigger vocabulary. More
# specific phrases come first ("valuation allowance" → 740 before any 326
# allowance phrasing could be confused). Matches are word-bounded, not bare
# substrings ("lease" must not match "Release").
_NAME_KEYWORD_TO_TOPIC: tuple[tuple[str, str], ...] = (
    # income taxes (route tag: "tax provision, deferred tax, uncertain tax position")
    ("valuation allowance", "ASC 740"),
    ("income tax", "ASC 740"),
    ("deferred tax", "ASC 740"),
    ("tax provision", "ASC 740"),
    ("uncertain tax", "ASC 740"),
    # credit losses (route tag: "allowance, bad debt, doubtful accounts, uncollectible")
    ("allowance for", "ASC 326"),
    ("bad debt", "ASC 326"),
    ("doubtful", "ASC 326"),
    ("uncollectible", "ASC 326"),
    ("credit loss", "ASC 326"),
    # inventory (route tag: "inventory, shrinkage, obsolescence, consignment")
    ("inventory", "ASC 330"),
    ("obsolescence", "ASC 330"),
    ("shrinkage", "ASC 330"),
    ("consignment", "ASC 330"),
    # foreign currency (route tag: "foreign currency"; corpus triggers: remeasure/translate)
    ("exchange gain", "ASC 830"),
    ("exchange loss", "ASC 830"),
    ("foreign exchange", "ASC 830"),
    ("foreign currency", "ASC 830"),
    ("currency", "ASC 830"),
    ("translation adjustment", "ASC 830"),
    ("remeasurement", "ASC 830"),
    ("fx gain", "ASC 830"),
    ("fx loss", "ASC 830"),
    # derivatives and hedging (route tag: "swap, hedge")
    ("derivative", "ASC 815"),
    ("swap", "ASC 815"),
    ("hedge", "ASC 815"),
    # stock compensation (route tag: "stock comp")
    ("stock compensation", "ASC 718"),
    ("stock comp", "ASC 718"),
    ("share-based", "ASC 718"),
    ("share based", "ASC 718"),
    ("stock option", "ASC 718"),
    ("equity compensation", "ASC 718"),
    # engine-backed clusters (suppress the annotation — an engine covers them)
    ("lease", "ASC 842"),
    ("right-of-use", "ASC 842"),
    ("right of use", "ASC 842"),
    ("rou asset", "ASC 842"),
    ("depreciation", "ASC 360"),
    ("amortization", "ASC 350"),
    ("deferred revenue", "ASC 606"),
    ("unearned revenue", "ASC 606"),
    ("contract liability", "ASC 606"),
    ("contract asset", "ASC 606"),
    ("revenue", "ASC 606"),
    ("sales", "ASC 606"),
    ("prior period", "ASC 250"),
    ("restatement", "ASC 250"),
)

_KEYWORD_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE), topic)
    for keyword, topic in _NAME_KEYWORD_TO_TOPIC
)


def _name_topic(account_name: str) -> str | None:
    """First word-bounded keyword hit in the account name, or None."""
    if not account_name:
        return None
    for pattern, topic in _KEYWORD_PATTERNS:
        if pattern.search(account_name):
            return topic
    return None


def _classify_line(account_type: str, account_name: str) -> tuple[bool, str | None]:
    """``(resolved, topic)`` for one JE line's account identity.

    ``resolved=True, topic=str`` — a topic cluster was grounded (type or name).
    ``resolved=True, topic=None`` — real identity, routine/no topic signal.
    ``resolved=False`` — identity resolvable NEITHER way → unmapped (honest branch).
    """
    if account_type:
        direct = _ACCOUNT_TYPE_TO_TOPIC.get(account_type)
        if direct is not None:
            return True, direct
        if account_type in _NO_TOPIC_ACCOUNT_TYPES:
            return True, _name_topic(account_name)
        # An unrecognized account_type string falls through to the name mapping.
    name_hit = _name_topic(account_name)
    if name_hit is not None:
        return True, name_hit
    return False, None


# ── (c) The pure annotation function ─────────────────────────────────────────

def annotate_je_lines(
    lines: Sequence[Mapping[str, object]],
    account_types_by_id: Mapping[str, str],
) -> tuple[RiskAnnotation | None, list[str]]:
    """Annotate a JE payload's lines against the engine registers.

    ``lines`` accepts BOTH draft lanes' payload shapes (decision 3 / E4): the
    chat tool requires ``account_name`` (``account_id`` optional); the router
    lane's ``JELine`` requires ``account_id`` (name optional). Account CLASS
    comes from ``account_types_by_id`` (the caller's ONE batched
    ``qbo_accounts.account_type`` lookup); the name→cluster mapping is the
    fallback for id-less/uncached lines.

    Returns ``(annotation, unmapped_accounts)``:
      - ``annotation`` is the TypedDict when ANY line grounds to an engine-less
        high-risk cluster, else None (engine-backed / routine / all-unmapped).
      - ``unmapped_accounts`` lists the identities of lines that resolved
        neither way — returned for logging, NEVER turned into a guessed topic.
    """
    topics: set[str] = set()
    unmapped: list[str] = []
    for line in lines:
        if not isinstance(line, Mapping):
            unmapped.append("<malformed line>")
            continue
        account_id = str(line.get("account_id") or "").strip()
        account_name = str(line.get("account_name") or "").strip()
        account_type = ""
        if account_id:
            account_type = str(account_types_by_id.get(account_id) or "")  # SA-OK: in-memory dict lookup over the prefetched batch, not a DB query
        resolved, topic = _classify_line(account_type, account_name)
        if not resolved:
            unmapped.append(account_name or account_id or "<no account identity>")
            continue
        if topic is not None and topic in ENGINELESS_HIGH_RISK_TOPICS:
            topics.add(topic)
    if not topics:
        return None, unmapped
    annotation: RiskAnnotation = {
        "engine_backed": False,
        "topics": sorted(topics),
        "notice": RISK_NOTICE,
    }
    return annotation, unmapped

"""
Request-intent classification for the agent runtime.

The classified intent drives three token-cost levers (see
docs/llm-optimization/current-state.md): tool-bundle selection, tiered model
routing (cheap "fast" model for simple chat, frontier model for analysis), and
the module context contract (which context sources get loaded).

Design: a deterministic keyword classifier is the always-on base — zero latency,
zero cost, fully predictable. When it cannot confidently place a request (falls
through to "general") and ``AGENT_LLM_INTENT_CLASSIFIER`` is enabled, a single
cheap-lane LLM call refines the label. Any error/timeout falls back to the
keyword result, so classification can never break or block a run.

All outputs are canonical ``AgentIntent`` values; ``canonicalize_intent`` in the
context builder is still applied downstream as a safety net.
"""
from __future__ import annotations

import logging
import re  # PLAN-EXEMPT: P0 intent-misroute fix (word-boundary keyword match) per Steven 2026-06-23
from typing import Optional

from ..config import settings

logger = logging.getLogger(__name__)


# Canonical intents the rest of the runtime understands (INTENT_BUNDLES keys).
VALID_INTENTS = frozenset(
    {
        "general",
        "reporting_analysis",
        "cashflow",
        "budgeting",
        "close_management",
        "documents",
        "memory",
        "billing",
        "admin",
        "asc_guidance",
        "receivables",
        # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent). Decision 11: the AP payment-run intent.
        "payables",
        # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent). Decision 1: the bank-reconciliation intent.
        "reconciliation",
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent). Decision 1: the amortization-schedules intent.
        "schedules",
    }
)

# Ordered keyword rules — FIRST match wins, so specific/operational intents are
# listed before the broad reporting_analysis catch-all. Each entry is a
# lowercase substring tested against the (lowercased) request.
_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "admin",
        (
            "disconnect qbo", "disconnect quickbooks", "reconnect qbo", "remove qbo",
            "remove quickbooks", "tenant setting", "organization setting", "org setting",
            "manage users", "add a user", "invite", "user role", "their role",
            "set role", "assign role", "permission",
        ),
    ),
    (
        "memory",
        (
            "remember that", "remember this", "teach vesper bot", "always classify",
            "always categorize", "standing instruction", "from now on", "make a note",
            "note that we", "accounting policy", "our policy is", "going forward",
        ),
    ),
    (
        # GAAP / accounting-standards questions. Placed before documents and
        # reporting_analysis so standards phrasing ("lease accounting", "revenue
        # recognition") wins over the bare "lease" / "revenue" keywords below.
        # Keywords are deliberately specific multi-word standards phrases (plus
        # "asc"/"gaap"/codes) so operational queries ("what does the lease say",
        # "what's our revenue") still fall through to the right intent.
        "asc_guidance",
        (
            "asc ", "u.s. gaap", "us gaap", "gaap", "accounting standard",
            "codification", "fasb", "revenue recognition", "recognize revenue",
            "rev rec", "deferred revenue", "contract liability", "contract asset",
            "performance obligation", "lease accounting", "lease standard",
            "capitalize", "capitalization", "impairment", "goodwill",
            "intangible", "net realizable value", "lower of cost", "obsolete inventory",
            "inventory write", "going concern", "deferred tax", "uncertain tax position",
            "valuation allowance", "business combination", "purchase price allocation",
            "earnout", "contingency", "contingencies", "loss contingency",
            "subsequent event", "related party", "related-party", "consolidat",
            "fair value", "hedge accounting", "interest rate swap", "derivative",
            "foreign currency", "stock compensation", "stock comp", "share-based",
            "asset retirement", "sale-leaseback", "sale leaseback", "internal-use software",
            "how should we account", "how do we account", "how to account for",
            "what's the accounting", "what is the accounting", "book treatment",
            "disclosure requirement",
        ),
    ),
    (
        # Placed AFTER asc_guidance so revenue-recognition phrasing that mentions
        # a "subscription" routes to asc_guidance, while plan/billing operations
        # still match here. asc_guidance has no bare billing keywords, so genuine
        # billing queries fall through to this rule unharmed.
        "billing",
        (
            # PLAN-EXEMPT: P0 hotfix (intent misroute) per Steven directive 2026-06-23. The bare
            # keyword "billing" hijacked operational requests that merely DESCRIBE an expense —
            # "accrue ... for Collective RCM's billing services" matched here (billing precedes
            # close_management) and routed to the EMPTY billing bundle (no JE tool). Billing is
            # the user's OWN Vesper Bot subscription; require a subscription-context phrase, not the
            # bare noun, so a vendor's "billing services" no longer hijacks the accrual.
            "subscription", "billing plan", "billing portal", "billing settings",
            "billing cycle", "my billing", "stripe", "pricing plan",
            "my plan", "seat", "seats", "upgrade plan", "downgrade", "payment method",
            "how much am i paying", "renew my subscription",
        ),
    ),
    (
        "documents",
        (
            "agreement", "contract", "lease", "renewal", "document", "this pdf",
            "the terms", "what does the covenant say", "loan agreement", "msa",
            "statement of work", "engagement letter",
        ),
    ),
    (
        # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent). Decision 1 (E3-corrected): the
        # bank-reconciliation intent. "reconcile"/"reconciliation" MIGRATE here OUT of
        # the close_management rule below (where they had lived since the C3-TOOLS
        # wave) so recon phrasing reaches the session/match/tie-out bundle. Placed
        # BEFORE close_management (first-match-wins). ACCEPTED RESIDUALS (plan
        # decision 1): "\bbank rec" prefix-matches "bank records"/"bank receipt"
        # (low-consequence in a finance app); "reconcile the balance sheet" routes
        # here (loses get_schedule_data — noted). "unmatched transaction" is the
        # word-start prefix form, so it matches the plural too.
        "reconciliation",
        (
            "reconcile", "reconciliation", "bank rec", "unmatched transaction",
            # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
            # RATIFICATION-2026-07-14.md). WF-12 (decision 10): the agent-plan
            # orchestration tools (propose_agent_plan / get_agent_plan_status) are bundled
            # in RECONCILIATION (the headroom set()-trim home — see the tool_registry
            # placement note), so plan-orchestration phrasing must ROUTE here to offer
            # them. The phrases are DELIBERATELY specific so they do not steal close's bare
            # "checklist" keyword (this rule is matched before close_management, but
            # "checklist of actions" only matches the full phrase, and "build a plan"/"do
            # these steps"/"multi-step" carry no close/billing collision).
            "build a plan", "multi-step", "multi step", "do these steps",
            "checklist of actions", "step-by-step plan", "agent plan",
        ),
    ),
    (
        "close_management",
        (
            "close task", "close item", "month-end close", "month end close",
            "closing the books", "checklist", "mark this close", "complete close",
            "close status",
            # PROVENANCE: "reconcile"/"reconciliation" lived here until plan
            # 20260713-wave2-3-bank-recon decision 1 migrated them to the dedicated
            # reconciliation rule above (migrate, don't double-route).
            # PLAN-EXEMPT: P0 hotfix H3 (2026-06-22) — route operational accrual requests
            # ("accrue X", "post an accrual") to close_management so the create_journal_entry
            # proposal tool is offered. Without this, "accrue rent expense" matched
            # reporting_analysis's "expense" (no JE tool) and bare "accrue X" fell to general,
            # so the agent refused to draft the JE. "accrual" also subsumes "accrual schedule".
            "accrue", "accrual",
            # PLAN-EXEMPT: P0 hotfix (intent misroute) per Steven directive 2026-06-23. Explicit
            # journal-entry phrasing must route to a JE-capable bundle, not fall through to general.
            "journal entry", "journal entries", "reclass", "reclassify",
            # PROVENANCE: "prepaid schedule"/"depreciation schedule" lived here until plan
            # 20260713-wave2-6-tool-hardening decision 1/8 migrated them to the dedicated
            # schedules rule below (migrate, don't double-route) — schedule-row phrasing
            # must reach the SCHEDULES bundle where the row writes are now offered.
        ),
    ),
    (
        # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent). Decision 11: AP payment-RUN
        # operations. Placed BEFORE cashflow (first-match-wins) so "payment run for
        # our payables" reaches the payment-batch tool instead of the liquidity
        # bundle. The keywords are deliberately operational (a run/batch/paying of
        # bills); broader liquidity phrasing — "pay vendors", "payable", "ap aging",
        # "vendor payments" — stays cashflow, preserving the existing
        # "pay vendors"→cashflow pin (tests/test_intent_classifier.py).
        "payables",
        (
            "pay bills", "payment run", "payment batch", "bill payment",
        ),
    ),
    (
        "cashflow",
        (
            "cash flow", "cashflow", "cash risk", "cash position", "cash balance", "13-week",
            "13 week", "thirteen week", "runway", "burn rate", "pay vendors",
            "vendor payments", "liquidity", "ar/ap", "ap aging", "ar aging",
            "receivable", "payable", "collections",
        ),
    ),
    (
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent). Decisions 1/8: the amortization-
        # schedules intent. Placed AFTER cashflow (first-match-wins) and keyed on
        # MULTI-WORD phrases ONLY — a bare "schedule" before cashflow would steal the
        # layer_cashflow_schedule fixture's phrasing ("Layer this schedule into the
        # cashflow" must stay cashflow) and prefix-match "scheduled". PROVENANCE:
        # "prepaid schedule"/"depreciation schedule" MIGRATED here from the
        # close_management rule above (their row-edit tools now live in the SCHEDULES
        # bundle); "amortization schedule"/"schedule row" are new. ACCEPTED RESIDUALS
        # (decision 8, enumerated + accepted — no fix): "lease amortization schedule"
        # routes documents (bare "lease" precedence in the documents rule above);
        # "Schedule a bill payment" routes payables ("bill payment" precedence).
        "schedules",
        (
            "amortization schedule", "depreciation schedule", "prepaid schedule",
            "schedule row",
        ),
    ),
    (
        # AR / sales (receivables) operations. Placed AFTER cashflow so AR-aging
        # ("ar aging", "receivable", "collections") stays cashflow (the liquidity
        # lens), and BEFORE reporting_analysis so invoice/billing/sales phrasing
        # ("have we invoiced Acme", "did we bill the client", "who owes us") routes
        # here instead of falling through to the broad reporting_analysis catch-all
        # or to general. "invoice for" was removed from the billing rule (which is
        # the user's OWN Vesper Bot subscription) so a customer invoice no longer
        # hijacks to the empty billing bundle. Word-boundary matched (re.search
        # \b<keyword>) like every rule here — see classify_intent_keyword.
        "receivables",
        (
            "invoice", "invoiced", "invoicing", "unpaid invoice", "open invoice",
            "bill the", "billed", "did we bill", "sales to", "sold to",
            "charge the customer", "charged", "outstanding from", "owed by",
            "receivable from", "who owes us",
            # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
            # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
            # dispatched session — standing precedent). Decision 7: CECL / bad-debt
            # phrasing routes to the AR home where propose_allowance_je is offered.
            # ACCEPTED RESIDUALS (documented, not fixed): (a) asc_guidance PRECEDES this
            # rule (first-match-wins), so standards phrasing — "asc 326", "how should we
            # account for bad debt", "what's the accounting for credit losses" — still
            # routes asc_guidance (the corpus lane; correct for a HOW-TO-ACCOUNT
            # question, and the ASC 326 records now name the engine tool); (b)
            # close_management's "accrue"/"accrual" also precede, so "accrue bad debt"
            # routes close_management (which offers create_journal_entry — a JE-capable
            # bundle, low-consequence); (c) cashflow's "receivable"/"collections"
            # precede, so pure liquidity phrasing stays cashflow by design.
            "bad debt", "doubtful", "uncollectible", "allowance for doubtful",
            "cecl", "credit loss estimate",
        ),
    ),
    (
        "budgeting",
        (
            "budget", "reforecast", "re-forecast", "forecast line", "driver",
            "scenario", "plan vs actual", "plan versus actual", "budget variance",
            "set budget", "update budget",
        ),
    ),
    (
        "reporting_analysis",
        (
            "revenue", "gross margin", "margin", "variance", "profit", "profitability",
            "expense", "spend", "spending", "why did", "why is", "explain", "compare",
            "trend", "board pack", "board deck", "financial report", "p&l", "pnl",
            "income statement", "balance sheet", "cash flow statement", "ebitda",
            "top line", "bottom line", "cogs", "operating income", "how did we do",
        ),
    ),
]


def classify_intent_keyword(question: str) -> tuple[str, bool]:
    """Deterministic keyword classification.

    Returns ``(intent, matched)`` where ``matched`` is False only when nothing
    matched and we fell through to ``"general"`` — that signal is what gates the
    optional LLM refinement.
    """
    q = (question or "").lower().strip()
    if not q:
        return "general", False
    for intent, keywords in _KEYWORD_RULES:
        # PLAN-EXEMPT: P0 hotfix (intent misroute) per Steven directive 2026-06-23. A naive
        # `keyword in q` substring test routed EVERY polite request to the documents intent:
        # "please" contains the documents keyword "lease", so "Can you please accrue ..." matched
        # documents (which has no journal-entry tool) instead of close_management — the agent then
        # truthfully reported it only had document tools and refused. Match at a word START
        # boundary instead: \b<keyword> still allows intended prefix/stem matches (\bconsolidat ->
        # "consolidation", \bbudget -> "budgeting") while "lease" no longer fires inside "please".
        if any(re.search(r"\b" + re.escape(keyword), q) for keyword in keywords):
            return intent, True
    return "general", False


# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
# RATIFICATION-2026-07-13.md on disk) — decision 11: the allowed values gain
# "payables". The receivables omission that decision flagged (a VALID_INTENTS
# member since plan 20260625-intent-routing, absent from this prompt) is now
# FIXED by plan 20260713-wave2-5-cecl-allowance chunk B decision 7 (below).
_LLM_SYSTEM_PROMPT = (
    "You label a finance assistant user request with exactly one intent so the "
    "system can select the right tools and model. Respond with JSON only: "
    '{"intent": "<one_of>"}. Allowed values: general, reporting_analysis, '
    "cashflow, budgeting, close_management, documents, memory, billing, admin, "
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 1: the allowed values + a
    # routing sentence gain "reconciliation" (AC-1's LLM-prompt value).
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 1/AC-1: the allowed values + a
    # routing sentence gain "schedules" (the amortization-schedules intent).
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 7: the allowed values + a
    # routing sentence gain "receivables" (fixing the flagged omission — the LLM
    # refiner must be able to emit the intent the new CECL keywords route to).
    "asc_guidance, payables, reconciliation, schedules, receivables. "
    "Use reporting_analysis for questions about financial results, variances, "
    "margins, revenue, or expenses. Use cashflow for liquidity, runway, AR/AP, "
    "or cash forecasting. Use receivables for accounts-receivable operations — "
    "creating or chasing customer invoices, recording customer payments, credit "
    "memos, or estimating bad debt / the allowance for doubtful accounts / "
    "expected credit losses (CECL). Use payables for operational bill-paying — a payment "
    "run, a payment batch, or paying specific vendor bills. Use reconciliation "
    "for bank-reconciliation work — opening or resuming a reconciliation "
    "session, matching bank statement lines to book transactions, unmatched "
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 10): the reconciliation routing
    # sentence additionally covers agent-plan orchestration — the plan tools are bundled
    # in RECONCILIATION (the headroom home), so the LLM refiner must emit "reconciliation"
    # for multi-step-plan phrasing.
    "transactions, or the reconciliation tie-out; AND for composing a novel "
    "MULTI-STEP plan (a checklist of actions / 'do these steps' / building an "
    "agent plan the assistant should carry out over several tools). "
    "Use schedules for working "
    "with amortization, depreciation, or prepaid schedules — adding, editing, "
    "or deleting schedule rows, disposing a scheduled asset, or the lease "
    "schedule lifecycle. Use asc_guidance "
    "for U.S. GAAP / accounting-standards "
    "(ASC) questions about how to recognize, measure, classify, or disclose a "
    "transaction under GAAP (e.g. revenue recognition, lease accounting, "
    "impairment, goodwill, deferred taxes, business combinations). Use general "
    "only for greetings, capability questions, or small talk that needs no "
    "financial data."
)


def classify_intent_llm(question: str) -> Optional[str]:
    """Refine an ambiguous request with one cheap-lane LLM call.

    Returns a canonical intent, or None on any failure / unrecognized label so
    the caller can fall back to the keyword result. Never raises.
    """
    try:
        from .llm import context_llm

        result = context_llm.predict_json(
            system_prompt=_LLM_SYSTEM_PROMPT,
            user_prompt=(question or "")[:1000],
            temperature=0.0,
        )
    except Exception as exc:  # pragma: no cover - defensive, provider may be down
        logger.debug("[Intent] LLM refinement failed: %s", exc)
        return None
    if not isinstance(result, dict):
        return None
    intent = str(result.get("intent") or "").strip().lower()
    return intent if intent in VALID_INTENTS else None


def classify_intent(question: str) -> str:
    """Canonical intent for a request: keyword-first, optional LLM refinement.

    The keyword classifier is authoritative when it matches. Only ambiguous
    ("general") requests are escalated to the LLM, and only when
    ``AGENT_LLM_INTENT_CLASSIFIER`` is enabled, so the hot path stays fast and
    cheap by default.
    """
    intent, matched = classify_intent_keyword(question)
    if matched:
        return intent
    if settings.agent_llm_intent_classifier:
        refined = classify_intent_llm(question)
        if refined and refined != "general":
            logger.info("[Intent] LLM refined ambiguous request -> %s", refined)
            return refined
    return intent

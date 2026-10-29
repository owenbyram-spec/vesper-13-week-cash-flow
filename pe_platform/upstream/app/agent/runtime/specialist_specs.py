# Plan 20260712-agentic-finance-os W5 (GAP-5) — specialist capability specs.
"""Advisory documentation for the existing intent bundles, not independent agents.

Mandate, output, completion and escalation prose describes intended use; no
natural-language completion evaluator or specialist dispatcher consumes it.
Structural validation checks names and vocabulary. ToolPolicyGate enforces the
explicit prohibited_tools set during both offering and execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Risk classes a specialist may declare.
RISK_READ_ONLY = "read_only"   # no state change — analysis / retrieval only
RISK_PROPOSAL = "proposal"     # mutations only via proposal-first (human approval)
RISK_MIXED = "mixed"           # both read and proposal-gated capabilities

_VALID_RISK = frozenset({RISK_READ_ONLY, RISK_PROPOSAL, RISK_MIXED})


@dataclass(frozen=True)
class SpecialistSpec:
    """One specialist capability, bound to an INTENT_BUNDLES key."""

    name: str
    bundle: str                 # the INTENT_BUNDLES key this specialist draws tools from
    mandate: str
    risk_class: str
    output_contract: str        # what the specialist returns (the typed-output description)
    completion_criteria: str
    escalation_criteria: str
    prohibited_tools: frozenset[str] = field(default_factory=frozenset)


SPECIALIST_SPECS: dict[str, SpecialistSpec] = {
    "reporting_analysis": SpecialistSpec(
        name="reporting_analysis",
        bundle="reporting_analysis",
        mandate="Answer management-reporting and variance questions from retrieved financial "
                "data; produce review-ready analysis with drill-down evidence.",
        risk_class=RISK_MIXED,
        output_contract="A narrative answer bound to verified claims + source references; "
                        "any material figure carries evidence or an explicit caveat.",
        completion_criteria="The objective is answered, material drivers are named, and every "
                            "material numeric claim is verified or caveated.",
        escalation_criteria="Escalate when required source data is missing/stale or a claim "
                            "cannot be verified — surface the gap, never fabricate.",
    ),
    "cashflow": SpecialistSpec(
        name="cashflow",
        bundle="cashflow",
        mandate="Produce and explain the cash outlook — the 13-week forecast, runway, and "
                "minimum-cash week — with freshness and reconciliation disclosed.",
        risk_class=RISK_MIXED,
        output_contract="A cash-position summary with week-ending balances, min-cash week, and "
                        "the as-of/freshness of the underlying data.",
        completion_criteria="The forecast horizon is covered and the min-cash week + runway are "
                            "reported with their data freshness.",
        escalation_criteria="Escalate on a stale/incomplete forecast base rather than presenting "
                            "a partial horizon as complete.",
    ),
    "budgeting": SpecialistSpec(
        name="budgeting",
        bundle="budgeting",
        mandate="Analyze budget-versus-actual and drive budget/reforecast updates through "
                "proposal-first workflows.",
        risk_class=RISK_MIXED,
        output_contract="A budget/variance analysis; any budget mutation is drafted as a "
                        "ProposedAction for human approval, never executed directly.",
        completion_criteria="The variance is explained and any recommended change is a drafted, "
                            "reviewable proposal.",
        escalation_criteria="Escalate when a reforecast depends on unconfirmed drivers.",
    ),
    "close_management": SpecialistSpec(
        name="close_management",
        bundle="close_management",
        mandate="Drive the month-end close — checklist, reconciliations, accruals, flux — with "
                "every material posting proposal-gated and the period-lock human-controlled.",
        risk_class=RISK_PROPOSAL,
        output_contract="Close status + drafted journal/accrual proposals; the agent NEVER "
                        "closes or locks a period itself.",
        completion_criteria="Close tasks are surfaced/advanced and any posting is a drafted "
                            "proposal awaiting approval.",
        escalation_criteria="Escalate any unreconciled account or cut-off ambiguity to a human.",
    ),
    "receivables": SpecialistSpec(
        name="receivables",
        bundle="receivables",
        mandate="Analyze AR/AP position, aging, and DSO/DPO; surface overdue and concentration risk.",
        risk_class=RISK_READ_ONLY,
        output_contract="An AR/AP summary with aging buckets, DSO/DPO, and net position.",
        completion_criteria="The aging + net position are reported with their as-of date.",
        escalation_criteria="Escalate when the AR/AP snapshot is still computing/unavailable.",
    ),
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Decision 11: the payables
    # specialist (the receivables precedent shape), bound to the new PAYABLES
    # bundle. RISK_MIXED: AP reads + the proposal-gated payment-batch draft.
    "payables": SpecialistSpec(
        name="payables",
        bundle="payables",
        mandate="Run the AP payment workflow — read open bills and their prioritization, "
                "and draft payment batches through proposal-first workflows.",
        risk_class=RISK_MIXED,
        output_contract="An AP payment analysis or a drafted payment-batch ProposedAction "
                        "for human approval — no bill is ever paid directly from chat.",
        completion_criteria="The open-bill position is reported, or a payment batch is a "
                            "drafted, reviewable proposal with its exclusions named.",
        escalation_criteria="Escalate when the bank account cannot be validated, a bill's "
                            "payment state is unresolved, or the batch exceeds the draft cap.",
    ),
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Decision 1: the reconciliation
    # specialist (the payables precedent shape), bound 1:1 to the new
    # RECONCILIATION bundle. RISK_MIXED: unmatched/tie-out reads + the two
    # proposal-gated session/match writes. Decision 2 is load-bearing in the
    # contract text: the agent NEVER completes a reconciliation — tie-out
    # presentation only; a human completes.
    "reconciliation": SpecialistSpec(
        name="reconciliation",
        bundle="reconciliation",
        mandate="Run a bank-reconciliation session end-to-end SHORT OF COMPLETION — "
                "open/resume proposal-gated sessions, surface unmatched items with why-not-"
                "matched reasons, draft bulk-match proposals, and present the tie-out.",
        risk_class=RISK_MIXED,
        output_contract="Unmatched-item pages and the session + subledger tie-out, with any "
                        "session open or match confirmation drafted as a ProposedAction for "
                        "human approval; the agent never completes or locks a reconciliation.",
        completion_criteria="The unmatched position and tie-out are reported (or honestly "
                            "degraded), and every mutation is a drafted, reviewable proposal.",
        escalation_criteria="Escalate an unexplained tie-out difference, a missing clean-start "
                            "anchor, or an unavailable subledger side — never plug a gap or "
                            "fabricate a zero.",
    ),
    "asc_guidance": SpecialistSpec(
        name="asc_guidance",
        bundle="asc_guidance",
        mandate="Retrieve and explain accounting-standard guidance grounded in cited sources.",
        risk_class=RISK_READ_ONLY,
        output_contract="An explanation bound to cited ASC/standard references — never an "
                        "un-sourced accounting conclusion.",
        completion_criteria="The guidance question is answered with source citations.",
        escalation_criteria="Escalate (refuse over fabricate) when no authoritative source supports the answer.",
    ),
    "documents": SpecialistSpec(
        name="documents",
        bundle="documents",
        mandate="Retrieve and search company documents/agreements as untrusted context for analysis.",
        risk_class=RISK_READ_ONLY,
        output_contract="Document search results / extracted context, treated as untrusted data "
                        "(never instruction).",
        completion_criteria="The relevant document context is retrieved.",
        escalation_criteria="Escalate when a needed document is absent or unreadable.",
    ),
    "memory": SpecialistSpec(
        name="memory",
        bundle="memory",
        mandate="Capture and retrieve scoped, versioned company memory — proposal-gated writes, "
                "non-authoritative until accepted.",
        risk_class=RISK_PROPOSAL,
        output_contract="Memory reads/writes; a taught memory is a drafted ProposedAction, and "
                        "an inferred memory is tentative until a user accepts it.",
        completion_criteria="The memory is retrieved, or a write is drafted for approval.",
        escalation_criteria="Escalate a contradiction rather than silently merging memories.",
    ),
}


def validate_specialist_specs(
    bundle_names: frozenset[str], registered_tool_names: frozenset[str]
) -> list[str]:
    """Structural violations in the specialist specs (empty == healthy).

    Enforced: every spec's ``bundle`` is a real INTENT_BUNDLES key; each bundle is
    claimed by at most one spec (1:1); ``risk_class`` is valid; mandate + criteria
    are non-empty; every ``prohibited_tools`` member is a really-registered tool
    (a phantom prohibition is dead metadata)."""
    violations: list[str] = []
    seen_bundles: dict[str, str] = {}
    for name, spec in SPECIALIST_SPECS.items():
        if spec.name != name:
            violations.append(f"specialist '{name}' key does not match spec.name '{spec.name}'")
        if spec.bundle not in bundle_names:
            violations.append(
                f"specialist '{name}' references bundle '{spec.bundle}' which is not an "
                "INTENT_BUNDLES key"
            )
        owner = seen_bundles.setdefault(spec.bundle, name)
        if owner != name:
            violations.append(
                f"bundle '{spec.bundle}' is claimed by two specialists ({owner} and {name})"
            )
        if spec.risk_class not in _VALID_RISK:
            violations.append(
                f"specialist '{name}' has invalid risk_class '{spec.risk_class}' — "
                f"expected {sorted(_VALID_RISK)}"
            )
        if not (spec.mandate.strip() and spec.completion_criteria.strip() and spec.escalation_criteria.strip()):
            violations.append(f"specialist '{name}' must have non-empty mandate + completion + escalation criteria")
        for tool in sorted(spec.prohibited_tools - registered_tool_names):
            violations.append(
                f"specialist '{name}' prohibits '{tool}' which is not a registered tool "
                "(phantom prohibition)"
            )
    return violations

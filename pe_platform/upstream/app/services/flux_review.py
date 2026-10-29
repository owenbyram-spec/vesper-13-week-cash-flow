# PLAN-EXEMPT: chunk B of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
"""flux_review — the standalone actual-vs-prior flux OBJECTIVE_RUNNER (WF-7).

Wave 2.7 chunk B (plan 20260714-wave2-7-lease-flux, AC-2). The DETECTION +
materiality gate already ship as ``ActualVsPriorFluxCheck`` (review_checks/
flux_check.py); this module is the RUNNER around it — WF-7's only gap was the
narrative + a home that supplies a LIVE QBO client.

WHY A STANDALONE RUNNER (decision 3 / E2): the close walk builds a
``CheckContext`` with ``qbo_client=None`` (close_orchestration.py:630), so the
flux check DEGRADES (could_not_verify) there. This runner instead builds a ctx
with a LIVE client (``get_qbo_client_from_db``) + the tenant's effective
materiality (``get_effective_materiality``) + a no-op ``propose``, and calls
``ActualVsPriorFluxCheck().scan(ctx)`` DIRECTLY (never ``run_check`` — it does
not need run_check's LLM-draft/propose gates; it consumes the deterministic scan
Finding and drafts its own review-only proposals). It is the FIRST runner to
invoke a registered check standalone.

PROPOSAL-FIRST (agentic-writeback Control 1): every material variance becomes a
PENDING ``ProposedAction`` a human approves. This runner NEVER posts to the GL,
NEVER calls ``execute_proposed_action``, and issues ONLY read (``get_report``)
calls to QBO. The narrative is ENGINE-DERIVED and refuses to invent a cause when
no GL line-level detail is available (decision 4 — the shared no-speculation
clause from budget's ``variance_narrative``).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..budget.variance_narrative import no_gl_detail_speculation_clause
from ..qbo_actions.proposal_fingerprint import compute_proposal_fingerprint

logger = logging.getLogger(__name__)

# The action_type carried by a flux-review proposal — reuses the flux check's
# registered task-type constant so the review lane, the check, and any downstream
# inbox filter name the SAME string (no drifting literal).
FLUX_REVIEW_ACTION_TYPE = "REVIEW_FLUX"

# Bound the number of proposals one noisy period can spawn (the scan already caps
# its payload at _MAX_PAYLOAD_LINES=25; this is the runner-side draft ceiling).
FLUX_REVIEW_MAX_DRAFTS_PER_RUN = 25

# The tripped-line ``pct`` is engine-shaped: a float, None, or the "n/m" sentinel
# (a zero prior-period base). Modeled precisely so the formatter stays type-checked.
PctValue = float | str | None


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def _fmt_pct(pct: PctValue) -> str:
    """Render a percent for the human narrative. A ``None`` / ``"n/m"`` pct (a
    zero prior base) is stated as new/zero-base — never a fabricated percent."""
    if pct is None or pct == "n/m":
        return "n/m (new or zero prior-period base)"
    try:
        return f"{float(pct):+.1f}%"
    except (TypeError, ValueError):
        return "n/m (new or zero prior-period base)"


def build_flux_finding_narrative(line: dict, *, has_gl_detail: bool = False) -> str:
    """A DETERMINISTIC, engine-derived narrative for ONE tripped flux line.

    The sibling of budget's ``build_variance_analysis_prompt`` (decision 4): it
    states ONLY the engine facts (statement, prior → current, variance, pct) and,
    because account-level flux carries NO transaction-level (GL) detail, appends
    the SHARED no-speculation clause and refuses to attribute the movement to any
    specific driver. It never fabricates a cause — the honest output is "the
    number moved this much; a driver-tied explanation is required."

    NOTE (honest deviation from the budget builder): favorability (is_revenue)
    is NOT asserted here. Actual-vs-prior flux has no budget/plan baseline to
    judge favorable-vs-unfavorable against, and the tripped-line payload carries
    no account_type — so a favorability verdict would be fabricated. The narrative
    states the direction of movement neutrally, matching the check's own message.
    """
    statement = str(line.get("statement", ""))
    account = str(line.get("account", "") or line.get("account_key", "") or "account")
    prior = float(line.get("prior", 0.0) or 0.0)
    current = float(line.get("current", 0.0) or 0.0)
    variance = float(line.get("variance", 0.0) or 0.0)
    pct = line.get("pct")

    direction = "increased" if variance > 0 else "decreased"
    facts = (
        f"{account} ({statement}) {direction} by {_fmt_money(abs(variance))} "
        f"({_fmt_pct(pct)}) versus the prior period "
        f"({_fmt_money(prior)} → {_fmt_money(current)})."
    )
    if has_gl_detail:
        return facts
    # No GL line detail at account granularity — reuse the ONE shared clause so
    # this path and the budget-variance path refuse fabricated causes identically.
    return (
        f"{facts} Line-level detail is unavailable, so the specific driver of this "
        f"movement cannot be determined from the account totals alone; a driver-tied "
        f"explanation is required and no cause is inferred here.{no_gl_detail_speculation_clause()}"
    )


def _noop_propose(*, ctx, finding, reasoning: str) -> None:
    """A ProposeCallable that does nothing — the runner calls ``scan`` DIRECTLY,
    which never invokes ``propose`` (only ``run_check`` does). Present only to
    satisfy the required ``CheckContext.propose`` field; it drafts nothing and,
    critically, never posts to the GL."""
    return None


def _flux_proposal_payload(*, period: str, prior_period: str, line: dict, narrative: str) -> dict:
    """The deterministic PENDING-proposal payload for one tripped flux line.

    Deterministic given the line + period so the ``inputs_fingerprint`` (stamped by
    the ProposedAction before_insert listener over action_type + payload) is stable
    — a re-run / redelivery for the same period + account + variance dedupes to the
    same fingerprint (the runner's batched existence check below skips it)."""
    pct = line.get("pct")
    return {
        "summary": f"Flux review: {line.get('account', '')} ({line.get('statement', '')})",
        "execution_mode": "review_only",
        "approval_effect": (
            "Records review of this actual-vs-prior variance; does NOT post a "
            "journal entry or execute any financial write automatically."
        ),
        "period": period,
        "prior_period": prior_period,
        "statement": line.get("statement"),
        "account": line.get("account"),
        "account_key": line.get("account_key"),
        "prior": line.get("prior"),
        "current": line.get("current"),
        "variance": line.get("variance"),
        "pct": pct if pct is not None else "n/m",
        "narrative": narrative,
    }


def flux_review(
    db: Session,
    realm: str,
    period: str,
    *,
    agent_run_id: int | None = None,
    company_name: str = "",
) -> dict:
    """Run the actual-vs-prior flux check for ``(realm, period)`` and draft one
    PENDING review proposal per MATERIAL variance. Proposes, never posts.

    Returns a summary dict (mirrors the OBJECTIVE_RUNNERS contract): ``status`` is
    one of ``could_not_verify`` (degraded source — honest, drafts nothing),
    ``clean`` (no material flux), or ``drafted`` (material flux → PENDING rows).
    """
    from ..qbo_client import get_qbo_client_from_db
    from .review_checks import CheckContext, FindingOutcome, get_effective_materiality
    from .review_checks.flux_check import ActualVsPriorFluxCheck

    check = ActualVsPriorFluxCheck()
    materiality = get_effective_materiality(db, check.check_id, realm_id=realm)
    if materiality is None:
        # A registered check always resolves a materiality; a None means a registry
        # inconsistency — fail closed (degrade honestly), never a fabricated clean pass.
        logger.warning("[flux_review] no registered materiality for %s; skipping", check.check_id)
        return {
            "company_id": realm,
            "period": period,
            "status": "could_not_verify",
            "reason": "no_registered_materiality",
            "drafted": [],
        }

    qbo_client = get_qbo_client_from_db(db, realm)
    ctx = CheckContext(
        db=db,
        realm_id=realm,
        period=period,
        propose=_noop_propose,
        materiality=materiality,
        qbo_client=qbo_client,
        company_name=company_name,
    )

    finding = check.scan(ctx)

    if not finding.verifiable:
        # could_not_verify: a degraded / unreadable source is surfaced, NEVER
        # treated as a clean pass and NEVER a fabricated proposal.
        return {
            "company_id": realm,
            "period": period,
            "status": "could_not_verify",
            "reason": finding.message,
            "drafted": [],
        }

    if finding.outcome is not FindingOutcome.HIT:
        return {
            "company_id": realm,
            "period": period,
            "status": "clean",
            "message": finding.message,
            "drafted": [],
        }

    prior_period = str(finding.payload.get("prior_period", ""))
    tripped_lines = list(finding.payload.get("tripped_lines", []))[:FLUX_REVIEW_MAX_DRAFTS_PER_RUN]

    return _draft_flux_proposals(
        db,
        realm=realm,
        period=period,
        prior_period=prior_period,
        tripped_lines=tripped_lines,
        finding_message=finding.message,
        agent_run_id=agent_run_id,
    )


def _draft_flux_proposals(
    db: Session,
    *,
    realm: str,
    period: str,
    prior_period: str,
    tripped_lines: list,
    finding_message: str,
    agent_run_id: int | None,
) -> dict:
    """Draft one PENDING ``ProposedAction`` per tripped line (idempotent).

    Pre-computes every would-be fingerprint, then ONE batched existence query (no
    per-draft query loop) so a re-run drafts ZERO duplicates — the collections_review
    dedup shape, adapted (flux proposals carry no close_task_id/plan_step_id, so the
    partial-unique constraints do not apply; the batched check IS the dedupe)."""
    from ..models import ProposedAction

    candidates = []
    for line in tripped_lines:
        narrative = build_flux_finding_narrative(line, has_gl_detail=False)
        payload = _flux_proposal_payload(
            period=period, prior_period=prior_period, line=line, narrative=narrative
        )
        fingerprint = compute_proposal_fingerprint(FLUX_REVIEW_ACTION_TYPE, payload)
        candidates.append((payload, narrative, fingerprint))

    existing_fingerprints: set = set()
    if candidates:
        rows = (
            db.query(ProposedAction.inputs_fingerprint)
            .filter(
                ProposedAction.company_id == realm,
                ProposedAction.action_type == FLUX_REVIEW_ACTION_TYPE,
                ProposedAction.status == "PENDING",
                ProposedAction.inputs_fingerprint.in_([fp for _, _, fp in candidates]),
            )
            .all()
        )
        existing_fingerprints = {row[0] for row in rows}

    drafted = []
    skipped_existing = 0
    for payload, narrative, fingerprint in candidates:
        if fingerprint in existing_fingerprints:
            skipped_existing += 1
            continue
        action = ProposedAction(
            company_id=realm,
            action_type=FLUX_REVIEW_ACTION_TYPE,
            payload=payload,
            reasoning=narrative,
            confidence_score=0.8,
            status="PENDING",
            source="system",
            category="flux_review",
            priority="medium",
            agent_run_id=agent_run_id,
            inputs_fingerprint=fingerprint,
        )
        db.add(action)
        db.flush()  # assign the id; the row stays PENDING (never executed here)
        drafted.append(
            {
                "action_id": action.id,
                "statement": payload["statement"],
                "account": payload["account"],
                "variance": payload["variance"],
                "pct": payload["pct"],
            }
        )

    return {
        "company_id": realm,
        "period": period,
        "prior_period": prior_period,
        "status": "drafted",
        "material_line_count": len(tripped_lines),
        "drafted": drafted,
        "skipped_existing": skipped_existing,
        "message": finding_message,
    }

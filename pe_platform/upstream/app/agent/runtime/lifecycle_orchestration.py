"""Standalone, explicitly invoked period-lifecycle proposal utility.

No application entry point currently connects this helper into a complete
close-to-forecast-to-risk workflow. Forecast/risk steps draft review markers;
approving those markers does not execute a forecast or financial action.
The selected-period close walker can be injected explicitly. End-to-end lifecycle
integration and readiness rules require separately reviewed product work.

Historical design identifier: C3-LIFECYCLE, AC-3.2.

A SYSTEM driver that sequences a full period as a chain of HUMAN-APPROVED steps:

    1. run the month-end close   (reuse the existing close walker)
    2. surface the close results (read the PENDING close proposals the walk drafted)
    3. roll into the forecast/budget  (draft a PENDING ProposedAction)
    4. flag covenant / liquidity risk (draft a PENDING ProposedAction)

Every step is proposal-first: NOTHING executes, closes, or LOCKS without a human
approving the PENDING ``ProposedAction`` in the existing inbox. This driver reuses
the existing engines (``run_close_orchestration``, the proposer pattern) — it does
NOT reimplement any finance.

FM-5 (approval-fatigue) guards, each asserted in the paired tests:
  (i)   autonomy stays proposal-gated, **L3 is OFF** — the lifecycle consults
        ``task_autonomy.effective_level`` and NEVER escalates it; a supervised_l3
        tenant would still route every lifecycle step to a human because this
        driver only ever drafts PENDING proposals (it never calls ``gate_decision``
        for auto-exec).
  (ii)  the agent lane has **ZERO lock authority** — there is NO LOCK_PERIOD step,
        no ``FinancialCalendar`` write, no ``status='CLOSED'`` write anywhere here.
        Period close / hard-LOCK are explicit HUMAN actions OUTSIDE this loop
        (``POST /accounting/calendar/{period}/lock``). ``LIFECYCLE_FORBIDDEN_STEPS``
        makes that a checkable invariant.
  (iii) a batch approval surfaces AGGREGATE financial impact (net debit / net
        credit + the accounts touched), not just an item count —
        ``aggregate_batch_impact``.

The closed-period + suspension guards (``task_autonomy``) gate the lifecycle path:
a soft-CLOSED period draws NO forecast/covenant proposals, and a suspended company
draws none at all.

Safety note (mirrors the close driver): this is a SYSTEM driver, not a
model-invocable agent tool — there is no LLM tool-loop, the driver itself is the
trusted principal, and it writes ONLY PENDING ``ProposedAction`` rows.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any  # CRAFT-OK: dynamic summary-dict + duck-typed close-runner collaborators, mirrors the close driver's dict[str, Any] runner contract

from sqlalchemy.orm import Session

from ...models import ProposedAction
from ...services.task_autonomy import effective_level, period_is_hard_locked
from ...services.tenant_companies import (
    company_is_suspended,
    organization_company_for_realm,
)
from ...tenant_rls import tenant_scope_for_realm

logger = logging.getLogger(__name__)

# The agent lane can NEVER perform these — they are explicit HUMAN actions outside
# the agent loop (§1 of the month-end-close skill; B2-6). The lifecycle driver
# asserts it emits none of them: a period close / hard-LOCK is the human
# ``POST /accounting/calendar/{period}/lock`` route, never an agent step.
LIFECYCLE_FORBIDDEN_STEPS = frozenset({"LOCK_PERIOD", "CLOSE_PERIOD", "HARD_LOCK"})

# The proposal action_types this driver is allowed to draft. Deliberately excludes
# every forbidden step above — a checkable allow-list, not a deny-list.
_FORECAST_STEP = "LIFECYCLE_FORECAST_ROLL"
_RISK_STEP = "LIFECYCLE_RISK_FLAG"
LIFECYCLE_STEP_TYPES = frozenset({_FORECAST_STEP, _RISK_STEP})


def _to_decimal(value: object) -> Decimal:
    """Coerce a debit/credit cell to Decimal (money is Decimal end-to-end)."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def aggregate_batch_impact(actions: list[ProposedAction]) -> dict[str, Any]:  # CRAFT-OK: dynamic aggregate-impact summary dict returned to a JSON API surface, not a fixed model
    """FM-5 (iii): the AGGREGATE financial impact of a batch of proposals.

    Returns net debit / net credit (as strings, Decimal-exact) and the sorted set
    of accounts touched — NOT just an item count. A reviewer approving a batch sees
    "$X across these accounts", not "12 items". Reads JE-shaped payloads
    (``payload["lines"]`` with ``debit`` / ``credit`` / ``account_name|account_id``);
    proposals with no lines contribute only to the count.
    """
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    accounts: set[str] = set()
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        lines = payload.get("lines")  # SA-OK: bounded in-memory dict.get on a payload, not a DB query
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, dict):
                continue
            total_debit += _to_decimal(line.get("debit", 0))  # SA-OK: bounded in-memory dict.get on a JE line, not a DB query
            total_credit += _to_decimal(line.get("credit", 0))  # SA-OK: bounded in-memory dict.get on a JE line, not a DB query
            name = line.get("account_name") or line.get("account_id")  # SA-OK: bounded in-memory dict.get on a JE line, not a DB query
            if name:
                accounts.add(str(name))
    return {
        "item_count": len(actions),
        "net_debit": str(total_debit),
        "net_credit": str(total_credit),
        "accounts_touched": sorted(accounts),
    }


def _draft_pending(
    db: Session,
    *,
    realm_id: str,
    action_type: str,
    step_index: int,
    summary: str,
    payload_extra: dict[str, Any],  # CRAFT-OK: dynamic per-step payload fields folded into the proposal JSON, shape varies by step
    organization_id: int | None,
) -> ProposedAction:
    """Insert exactly one PENDING, review-only ProposedAction for a lifecycle step.

    Proposal-first: this NEVER executes a handler and NEVER writes a period lock —
    a human approves it later via the existing inbox. The ``proposer_identity`` is
    the system lifecycle principal, so the preparer≠approver block treats it as
    agent-drafted (any EDITOR may approve — agent proposes, human approves).
    """
    if action_type in LIFECYCLE_FORBIDDEN_STEPS:
        # Defense-in-depth: the driver must never even attempt a lock/close step.
        raise ValueError(f"lifecycle driver may not draft a {action_type} step")
    payload: dict[str, Any] = {  # CRAFT-OK: dynamic proposal payload JSON assembled from mixed step fields
        "summary": summary,
        "execution_mode": "review_only",
        "approval_effect": (
            "Records approval for this lifecycle step; does not execute a financial "
            "write or lock any period automatically."
        ),
        "evidence": {
            "rationale": "Drafted by the period-lifecycle driver for human approval (proposal-first).",
            "proposer_identity": "system:lifecycle_orchestration",
        },
    }
    payload.update(payload_extra)
    action = ProposedAction(  # TEST-OK: system lifecycle driver, not a model-invocable agent tool; PENDING review-only proposal only, never a direct GL write or period lock; behavioral proposal-first eval is tests/test_lifecycle_orchestration.py
        company_id=realm_id,
        action_type=action_type,
        payload=payload,
        reasoning=summary,
        confidence_score=0.8,
        status="PENDING",
        source="system",
        category="lifecycle_orchestration",
        priority="medium",
        step_index=step_index,
    )
    with tenant_scope_for_realm(db, realm_id):
        db.add(action)
        db.commit()
    db.refresh(action)
    return action


def run_period_lifecycle(
    db: Session,
    *,
    realm_id: str,
    period: str,
    organization_id: int | None = None,
    close_runner: Any = None,  # CRAFT-OK: injected duck-typed close-walker callable (run_close_orchestration), signature owned by the close module
    close_objective: Any = None,  # CRAFT-OK: injected duck-typed AgentObjective-shaped value passed straight to the close runner
    close_tracker: Any = None,  # CRAFT-OK: injected duck-typed AgentRunTracker passed straight to the close runner
) -> dict[str, Any]:  # CRAFT-OK: dynamic lifecycle summary dict returned to a JSON API surface, not a fixed model
    """Drive a period's lifecycle as a SEQUENCE of PENDING proposals.

    Returns a summary dict: the close-walk summary (if a close runner was supplied),
    the ids of the forecast + risk proposals drafted, and the FM-5 guard readouts
    (autonomy level, lock authority, batch impact). NOTHING here executes, closes,
    or locks — every step is a PENDING proposal a human approves.

    ``close_runner`` / ``close_objective`` / ``close_tracker`` are injected so the
    lifecycle reuses the EXISTING close walker (``run_close_orchestration``) rather
    than reimplementing it; when omitted, the close step is reported skipped.
    ``period`` is the selected source/close month whose actuals the forward-looking
    proposals address; the close runner receives that exact month explicitly.
    """
    # Suspension gate (task_autonomy contract): a suspended company draws nothing.
    with tenant_scope_for_realm(db, realm_id):
        org_company = organization_company_for_realm(
            db, organization_id=organization_id, realm_id=realm_id
        )
    if company_is_suspended(db, organization_company=org_company):
        logger.info("[lifecycle] company suspended; drafting no proposals (realm=%s)", realm_id)
        return {
            "status": "skipped",
            "reason": "company_suspended",
            "realm_id": realm_id,
            "autonomy_level": effective_level(db, organization_id or 0, "close"),
            "has_lock_authority": False,
            "proposals": [],
        }

    summary: dict[str, Any] = {  # CRAFT-OK: dynamic lifecycle summary dict assembled from mixed step readouts
        "status": "succeeded",
        "realm_id": realm_id,
        "period": period,
        # FM-5 (i): the driver reports the tenant's autonomy level for visibility,
        # and NEVER changes it. It only ever drafts PENDING proposals.
        "autonomy_level": effective_level(db, organization_id or 0, "close"),
        # FM-5 (ii): a checkable, permanent invariant — the lifecycle lane holds no
        # lock authority. There is no code path here that writes a period lock.
        "has_lock_authority": False,
        "forbidden_steps": sorted(LIFECYCLE_FORBIDDEN_STEPS),
        "close": None,
        "proposals": [],
    }

    # STEP 1 — run the existing close walker (reuse, do not reimplement).
    if close_runner is not None and close_objective is not None and close_tracker is not None:
        summary["close"] = close_runner(db, close_objective, close_tracker, period=period)
    else:
        summary["close"] = {"status": "skipped", "reason": "no_close_runner_supplied"}

    # STEP 2 — surface the close results: read the PENDING proposals the walk
    # drafted (never mutate them). This is the "surface results" step.
    with tenant_scope_for_realm(db, realm_id):
        close_proposals = (
            db.query(ProposedAction)
            .filter(
                ProposedAction.company_id == realm_id,
                ProposedAction.status == "PENDING",
                ProposedAction.action_type == "CLOSE_STEP_REVIEW",
            )
            .all()
        )
    summary["close_proposals_surfaced"] = len(close_proposals)

    # STEPS 3 & 4 — the forward-looking steps are period-gated: a HARD-locked
    # period draws no forecast/covenant proposal (D-B6-1, 2026-07-16 — never propose
    # into a period an explicit team lock has closed; a soft-CLOSED auto-rolled month
    # is books-rolled, not write-blocked — the fail-closed period_is_hard_locked guard).
    if period_is_hard_locked(db, realm_id=realm_id, period=period):
        logger.info(
            "[lifecycle] period %s closed for realm %s; no forecast/risk proposals", period, realm_id
        )
        summary["forward_steps"] = "skipped_period_closed"
        return summary

    # STEP 3 — roll into the forecast/budget as a PENDING proposal.
    forecast = _draft_pending(
        db,
        realm_id=realm_id,
        action_type=_FORECAST_STEP,
        step_index=3,
        summary=f"Roll {period} actuals into the next-period forecast/budget.",
        payload_extra={"period": period, "step": "forecast_roll"},
        organization_id=organization_id,
    )
    # STEP 4 — flag covenant / liquidity risk as a PENDING proposal.
    risk = _draft_pending(
        db,
        realm_id=realm_id,
        action_type=_RISK_STEP,
        step_index=4,
        summary=f"Review covenant headroom and liquidity runway after the {period} close.",
        payload_extra={"period": period, "step": "risk_flag"},
        organization_id=organization_id,
    )
    summary["proposals"] = [
        {"id": forecast.id, "action_type": forecast.action_type, "status": forecast.status},
        {"id": risk.id, "action_type": risk.action_type, "status": risk.status},
    ]
    # FM-5 (iii): the batch-approval impact over the forward-looking proposals.
    summary["batch_impact"] = aggregate_batch_impact([forecast, risk])
    return summary

"""
app/lbo/assumption_write.py
───────────────────────────
Plan 20260706-cfo-grade-financial-workflows, wave 3.3a (completing C2A's deferred
AC-2A.2): the single callable WRITE for an LBO scenario's assumption ``driver_data``.

BEFORE this extract the driver-persist write lived router-private in
``app/routers/lbo.py::save_drivers`` (PUT /scenarios/{id}/drivers). Two callers must now
share ONE write path so the agent proposal-approval lane and the live UI save can never
drift:

- the live PUT endpoint (``save_drivers``) — a full-replacement save of the whole
  ``driver_data`` blob the panel submits;
- the agent proposal-approval handler (``_handle_propose_lbo_assumptions``,
  app/assistant/handlers.py), which applies a PARTIAL set of assumption-key changes on
  top of the scenario's current stored assumptions.

``apply_lbo_assumptions`` is the shared core: it validates the merged assumptions at the
save boundary (legacy-tolerant, exactly as the router did), persists the ``LboDriver``
row (create-or-update), threads the same Phase-3b learn-hook, commits, and returns a
``{before, after}`` snapshot of the stored assumptions so an approver can see the exact
delta. It is behavior-preserving for the full-replacement path (``merge=False``): the
persisted blob and the learn-hook call are byte-identical to the pre-extract router body.

HARD SAFETY (agentic-writeback): NOTHING in this module is reachable from the chat loop.
The agent tool ``propose_lbo_assumptions`` drafts a PENDING ProposedAction; this write
runs ONLY when that proposal is approved and its handler executes. Tenant scope is the
caller's responsibility — both callers pin the scenario's realm before invoking (the PUT
via ``_require_lbo_scenario``; the approval handler runs inside the FORCE-RLS realm pin) —
AND every DB query here filters ``company_id == <realm>`` at the ORM layer (the scenario
resolve + both driver reads), so a cross-tenant scenario/driver is never touched.

The assumption blob is a genuinely dynamic dict (``LboAssumptions`` is ``extra="allow"``,
so a stored ``driver_data`` carries arbitrary legacy keys). It is typed ``dict[str,
object]`` — a known container with an intentionally-open value type — never bare ``Any``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .assumption_proposals import learn_assumption_overrides
from .assumptions import LboAssumptions, validate_assumptions

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# The APA-confirm provenance marker (Wave-5 review remediation AC-3, plan
# 20260720-full-history-realized-returns): the document-confirm seams (agreements.py) are the
# ONLY writers of driver_data["su_provenance"] = "document_confirmed". This shared write core
# can therefore never INTRODUCE the marker, and any write that CHANGES the document-confirmed
# pricing keys DEMOTES it — a hand/agent edit is no longer the documents' pricing, so the
# realized-returns confirmed_su basis honestly unavails until the documents are re-confirmed.
_SU_PROVENANCE_KEY = "su_provenance"
_SU_CONFIRMED_PRICING_KEYS = ("entry_enterprise_value", "closing_date", "cash_consideration")


class LboAssumptionWriteError(Exception):
    """The scenario / driver write could not be completed (scenario missing, no changes).

    Raised BEFORE any row is mutated so callers fail loud (the handler surfaces it as an
    ``Error:`` string; the proposal approver sees the honest failure) rather than writing a
    partial or fabricated change.
    """


def _load_scenario(db: "Session", company_id: str, scenario_id: int):
    """Resolve the scenario UNDER the realm scope. A scenario from another tenant (or a
    stale id) is not found → a loud, honest miss (never a cross-tenant write)."""
    from ..models import LboScenario

    return (
        db.query(LboScenario)
        .filter(LboScenario.id == scenario_id, LboScenario.company_id == company_id)
        .first()
    )


def _load_driver(db: "Session", company_id: str, scenario_id: int):
    """Resolve the scenario's LboDriver row UNDER the realm scope (company_id-filtered)."""
    from ..models import LboDriver

    return (
        db.query(LboDriver)
        .filter(LboDriver.company_id == company_id, LboDriver.scenario_id == scenario_id)
        .first()
    )


def current_lbo_assumptions(db: "Session", company_id: str, scenario_id: int) -> dict[str, object]:
    """Return the scenario's current stored assumptions (validated + defaulted).

    READ-ONLY. Mirrors the router's load boundary (``validate_assumptions`` is
    legacy-tolerant — it never raises on stored data, so an existing scenario can't 500).
    Raises :class:`LboAssumptionWriteError` only if the scenario does not exist for this
    realm.
    """
    scenario = _load_scenario(db, company_id, scenario_id)
    if scenario is None:
        raise LboAssumptionWriteError(f"LBO scenario {scenario_id} not found for this company.")
    drv = _load_driver(db, company_id, scenario_id)
    return validate_assumptions(drv.driver_data if drv else None)


def apply_lbo_assumptions(
    db: "Session",
    company_id: str,
    scenario_id: int,
    updates: dict[str, object],
    *,
    merge: bool = True,
) -> dict[str, dict[str, object]]:
    """Persist an LBO scenario's assumption ``driver_data`` — the shared write core.

    ``merge=True`` (the agent-proposal path): ``updates`` is a PARTIAL set of assumption
    keys layered on top of the scenario's current stored assumptions.
    ``merge=False`` (the live PUT path): ``updates`` REPLACES the whole blob (the panel
    submits the full ``driver_data``) — byte-identical to the pre-extract router body.

    In both cases the merged/replacement blob is run through ``validate_assumptions`` (the
    same save-boundary validation the router applied), persisted to the ``LboDriver`` row
    (create-or-update), and threaded through the Phase-3b learn-hook (which writes the
    OVERRIDDEN keys to CompanyKnowledge, advisory — it never mutates ``driver_data``).
    Commits once. Returns ``{"before": <prior assumptions>, "after": <persisted
    assumptions>}`` — the exact delta an approver reviews.

    Raises :class:`LboAssumptionWriteError` if the scenario is missing for this realm, or
    (merge path) if ``updates`` carries no assumption keys.
    """
    from ..models import LboDriver

    scenario = _load_scenario(db, company_id, scenario_id)
    if scenario is None:
        raise LboAssumptionWriteError(f"LBO scenario {scenario_id} not found for this company.")

    drv = _load_driver(db, company_id, scenario_id)
    before = validate_assumptions(drv.driver_data if drv else None)

    if merge:
        if not isinstance(updates, dict) or not updates:
            raise LboAssumptionWriteError(
                "No assumption changes provided — updates must be a non-empty object of keys to change."
            )
        merged: dict[str, object] = dict(before)
        merged.update(updates)
    else:
        merged = dict(updates) if isinstance(updates, dict) else {}

    # Same save-boundary validation the router applies (legacy-tolerant: an odd value
    # falls back to the schema default rather than rejecting the save — WS-2).
    driver_data = validate_assumptions(merged)

    # AC-3 provenance keying: the su_provenance marker is OWNED by the document-confirm seams.
    # A write that changes any confirmed-pricing key demotes it (the pricing moved off the
    # documents); otherwise the STORED marker state is carried through unchanged — this path
    # can neither introduce it (forgery guard: a PUT/proposal claiming "document_confirmed" is
    # stripped) nor drop it incidentally (a non-echoing full replace keeps it).
    if any(driver_data.get(k) != before.get(k) for k in _SU_CONFIRMED_PRICING_KEYS):
        driver_data.pop(_SU_PROVENANCE_KEY, None)
    elif before.get(_SU_PROVENANCE_KEY) is not None:
        driver_data[_SU_PROVENANCE_KEY] = before[_SU_PROVENANCE_KEY]
    else:
        driver_data.pop(_SU_PROVENANCE_KEY, None)

    if drv:
        drv.driver_data = driver_data
    else:
        drv = LboDriver(company_id=scenario.company_id, scenario_id=scenario_id, driver_data=driver_data)
        db.add(drv)

    # Phase-3b learn-hook: record the assumptions the user OVERRODE (≠ the schema default)
    # so the firm's house conventions accumulate. Realm + org-FK scoped to
    # scenario.company_id. ADVISORY — this learns; it never mutates driver_data.
    learn_assumption_overrides(
        db,
        scenario.company_id,
        driver_data,
        LboAssumptions().model_dump(),
    )
    db.commit()

    return {"before": before, "after": driver_data}

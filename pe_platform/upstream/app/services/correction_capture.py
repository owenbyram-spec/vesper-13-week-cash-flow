"""Best-effort capture of human corrections to AI-produced proposals.

Every time a human accepts/edits/rejects/dismisses/confirms an AI suggestion
(categorization, reclassification, journal entry, budget value, schedule entry,
covenant/cashflow flag) we record one ``AICorrectionEvent`` — the structured
training signal behind confidence calibration and the tenant-knowledge flywheel
(Autonomy roadmap, Phase 1).

Design (mirrors ``record_llm_usage`` but with stronger isolation):

* **Never raises into the caller.** A capture failure must never break the
  human's action or the approval flow. Everything is wrapped in a catch-all.
* **Savepoint isolation.** When given the caller's session, the insert runs
  inside a SAVEPOINT (``begin_nested``) so a capture failure rolls back only the
  insert — never the human action already pending in the caller's transaction.
  ``record_llm_usage`` instead calls ``db.rollback()`` on failure, which would
  discard the caller's work; we deliberately avoid that here.
* **Realm isolation absolute.** Org/company are resolved from the realm when not
  supplied; a row whose ``organization_id`` can't be resolved is skipped — we
  never write a NULL-org (or cross-tenant) tenant correction.
* **Structured only.** Callers pass values/refs, never raw documents.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AICorrectionEvent
from .tenant_companies import organization_company_for_realm, organization_for_company

logger = logging.getLogger(__name__)

# Allowed task types (must match AICorrectionEvent docs + migration intent).
TASK_TYPES = frozenset(
    {
        "categorization",
        "reclassification",
        "journal_entry",
        "budget_value",
        "schedule_entry",
        "covenant_flag",
        "cashflow_flag",
        # Close-and-review findings (Phase D). A human accept/dismiss/edit of a
        # REVIEW_CHECK / CLOSE_STEP_REVIEW proposal is a real training signal — the
        # supervisor cockpit's headline content — so it must be a capturable type.
        # Without this the #16 loop-feed for close-review findings is silently false.
        "close_review",
    }
)

# Allowed human decisions.
HUMAN_DECISIONS = frozenset({"accepted", "edited", "rejected", "dismissed", "confirmed"})

# Task types that get shadow-mode evaluation (would-be-auto vs human) at capture.
_SHADOW_TASK_TYPES = frozenset({"categorization", "reclassification"})


# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel (marker
# RATIFICATION-2026-07-13.md) — C3 / AC-8: exemplar text carries real context.
# Context keys promoted into the composed exemplar text — the vendor/counterparty,
# memo, and line context the correction row ALREADY stores inside its
# ai_output/human_output JSON (incl. one level into the payload/updates/context
# sub-dicts the action lanes nest). Nothing is fetched beyond the row itself.
_VENDOR_KEYS = ("vendor_name", "vendor", "counterparty", "customer_name", "entity_name")
_MEMO_KEYS = ("memo", "description", "private_note", "note")
_ACCOUNT_KEYS = ("account_name", "predicted_account_name", "account")
_AMOUNT_KEYS = ("amount", "total_amount", "total_amt", "TotalAmt")
_NESTED_KEYS = ("payload", "updates", "context")


def _amount_class(value: object) -> str | None:
    """Bucket a raw amount into a stable magnitude class (embedding-friendly —
    exact floats fragment the vector space; classes cluster like corrections)."""
    try:
        magnitude = abs(float(value))  # type: ignore[arg-type]  # CRAFT-OK: duck-typed numeric probe, TypeError/ValueError handled
    except (TypeError, ValueError):
        return None
    if magnitude < 100:
        return "under-100"
    if magnitude < 1_000:
        return "hundreds"
    if magnitude < 10_000:
        return "thousands"
    if magnitude < 100_000:
        return "tens-of-thousands"
    return "hundred-thousands-plus"


def _context_sources(ai_output: dict | None, human_output: dict | None) -> list[dict]:
    """The flat dicts to scan for context: each output plus its known sub-dicts."""
    sources = []
    for out in (ai_output, human_output):
        if not isinstance(out, dict):
            continue
        sources.append(out)
        for nested_key in _NESTED_KEYS:
            nested = out.get(nested_key)  # SA-OK: bounded-loop 3 — plain-dict .get on in-memory JSON, no DB round-trip
            if isinstance(nested, dict):
                sources.append(nested)
    return sources


def _first_str(sources: list[dict], keys: tuple[str, ...]) -> str | None:
    for source in sources:
        for key in keys:
            val = source.get(key)  # SA-OK: bounded-loop 5 — plain-dict .get on in-memory JSON, no DB round-trip
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _compose_correction_text(task_type: str, ai_output: dict | None, human_output: dict | None) -> str:
    """A stable text representation of a correction, for embedding (U5).

    ``task_type`` + the AI's proposed value + the human's corrected value, plus
    the real-world context the row already stores (vendor/counterparty, memo,
    account names, a bucketed amount class) — null-safe: absent context simply
    contributes nothing, never a placeholder.
    """
    parts = [f"task: {task_type}"]
    for label, val in (("proposed", ai_output), ("corrected", human_output)):
        if isinstance(val, dict):
            parts.append(f"{label}: " + ", ".join(f"{k}={val[k]}" for k in sorted(val)))
        elif val is not None:
            parts.append(f"{label}: {val}")

    sources = _context_sources(ai_output, human_output)
    vendor = _first_str(sources, _VENDOR_KEYS)
    if vendor:
        parts.append(f"vendor: {vendor}")
    memo = _first_str(sources, _MEMO_KEYS)
    if memo:
        parts.append(f"memo: {memo}")
    accounts: list[str] = []
    for source in sources:
        for key in _ACCOUNT_KEYS:
            val = source.get(key)  # SA-OK: bounded-loop 3 — plain-dict .get on in-memory JSON, no DB round-trip
            if isinstance(val, str) and val.strip() and val.strip() not in accounts:
                accounts.append(val.strip())
    if accounts:
        parts.append("accounts: " + "; ".join(accounts))
    amount_cls = next(
        (cls for source in sources for key in _AMOUNT_KEYS if (cls := _amount_class(source.get(key)))),  # SA-OK: bounded-loop 4 — plain-dict .get on in-memory JSON, no DB round-trip
        None,
    )
    if amount_cls:
        parts.append(f"amount_class: {amount_cls}")
    return " | ".join(parts)


def record_correction(
    *,
    task_type: str,
    human_decision: str,
    company_id: Optional[str] = None,
    qbo_realm_id: Optional[str] = None,
    organization_id: Optional[int] = None,
    organization_company_id: Optional[int] = None,
    source_object_ref: Optional[Any] = None,
    ai_output: Optional[dict] = None,
    ai_confidence: Optional[Any] = None,
    human_output: Optional[dict] = None,
    actor_user_id: Optional[int] = None,
    would_autoexecute: Optional[bool] = None,
    matched_human: Optional[bool] = None,
    threshold: Optional[float] = None,
    db: Optional[Session] = None,
) -> Optional[int]:
    """Record one human correction of an AI proposal. Best-effort: never raises.

    Returns the new event id, or ``None`` if the write was skipped (unknown
    task_type/decision, no realm, unresolved org) or failed.

    ``company_id``/``qbo_realm_id`` are interchangeable realm identifiers; pass
    either. When ``organization_id`` / ``organization_company_id`` are omitted
    they are resolved from the realm. A provided ``db`` is used inside a SAVEPOINT
    and left for the caller to commit; otherwise a short-lived session is opened
    and committed immediately.
    """
    try:
        realm = qbo_realm_id or company_id
        realm = str(realm) if realm is not None else None
        if not realm:
            logger.debug("[correction_capture] skip: no realm for task_type=%s", task_type)
            return None
        if task_type not in TASK_TYPES or human_decision not in HUMAN_DECISIONS:
            logger.debug(
                "[correction_capture] skip: invalid task_type/decision %r/%r",
                task_type,
                human_decision,
            )
            return None

        owns_session = db is None
        session = db or SessionLocal()
        try:
            # Resolve tenant scope from the realm when not supplied.
            if organization_id is None:
                org = organization_for_company(session, realm)
                if org is not None:
                    organization_id = org.id
            if organization_company_id is None:
                company = organization_company_for_realm(
                    session,
                    organization_id=organization_id,
                    realm_id=realm,
                )
                if company is not None:
                    organization_company_id = company.id

            # Realm guard: never write a NULL-org (or cross-tenant) tenant row.
            if organization_id is None:
                logger.debug("[correction_capture] skip: unresolved org for realm=%s", realm)
                return None

            # Shadow-mode evaluation (Phase 2 PR1): for categorization-family
            # tasks with a raw confidence, compute would-be-auto vs human from the
            # tenant's calibration. Best-effort — a calibration failure leaves the
            # shadow fields NULL but never blocks the correction capture itself.
            if (
                would_autoexecute is None
                and matched_human is None
                and task_type in _SHADOW_TASK_TYPES
                and ai_confidence is not None
            ):
                try:
                    from .confidence_calibration import evaluate_shadow

                    # Calibration is per-tenant (org-level), never company-scoped
                    # — matches calibrated_confidence(organization_id, raw_score)
                    # and avoids fragmenting the reliability curve across companies.
                    shadow = evaluate_shadow(
                        session,
                        organization_id=organization_id,
                        task_type=task_type,
                        raw_confidence=float(ai_confidence),
                        human_decision=human_decision,
                        threshold=threshold,
                    )
                    would_autoexecute = shadow["would_autoexecute"]
                    matched_human = shadow["matched_human"]
                except Exception as shadow_exc:  # pragma: no cover - defensive
                    logger.debug("[correction_capture] shadow eval failed: %s", shadow_exc)

            # Embed the correction for U5 exemplar retrieval — ONLY for human
            # corrections (rejected/edited with an actor): the high-signal, low-volume
            # live path. Machine/backfill rows are embedded by the dedicated, batched
            # scripts/reembed_ai_corrections.py instead (bounds live-path OpenAI cost).
            # Best-effort + isolated: an embedding failure leaves it NULL but NEVER
            # drops the correction itself (never persist a None embedding).
            correction_embedding = None
            if human_decision in ("rejected", "edited") and actor_user_id is not None:
                try:
                    from .llm import analysis_llm

                    text = _compose_correction_text(task_type, ai_output, human_output)
                    if text:
                        emb = analysis_llm.get_embedding(text)
                        if emb:
                            correction_embedding = emb
                except Exception as embed_exc:  # pragma: no cover - defensive
                    logger.debug("[correction_capture] embed failed (left NULL): %s", embed_exc)

            event = AICorrectionEvent(
                task_type=task_type,
                organization_id=organization_id,
                organization_company_id=organization_company_id,
                qbo_realm_id=realm,
                source_object_ref=(str(source_object_ref) if source_object_ref is not None else None),
                ai_output=ai_output,
                ai_confidence=(float(ai_confidence) if ai_confidence is not None else None),
                correction_embedding=correction_embedding,
                human_decision=human_decision,
                human_output=human_output,
                actor_user_id=actor_user_id,
                would_autoexecute=would_autoexecute,
                matched_human=matched_human,
            )

            if owns_session:
                # PLAN-EXEMPT: RLS-hardening, same defect class as the llm_usage P0 /
                # proposer / entity_billing-webhook fixes. This self-opened-session branch
                # commits the org-keyed ai_correction_events (+ the circuit-breaker row)
                # under FORCE RLS with NO tenant GUC pinned, so a future background caller
                # passing db=None would have every correction silently RLS-denied (the outer
                # except swallows it). The owning org+realm are already resolved above, so
                # pin the REAL scope (not bypass) on this trusted server write; the begin
                # listener re-emits it onto the post-commit refresh / circuit-breaker
                # transactions from the pinned ContextVar.
                from ..tenant_rls import tenant_scope_live
                with tenant_scope_live(session, org_id=organization_id, realm_id=realm):
                    session.add(event)
                    # PLAN-EXEMPT: CI-red hotfix (Steven-directed) — take the PK from the
                    # flush INSIDE the pinned transaction. The post-commit refresh opened a
                    # NEW transaction whose GUC re-emit depends on the engine's begin
                    # listener; under FORCE RLS a missed re-emit reads zero rows and the
                    # swallowed raise lost the id (pg-lane AC pin).
                    session.flush()
                    event_id = event.id
                    session.commit()
                    _run_circuit_breaker(session, task_type, organization_id, commit=True)
                    return event_id

            # Shared caller session: isolate the insert in a SAVEPOINT so a
            # capture failure rolls back only this row, never the human action
            # already pending in the caller's transaction.
            with session.begin_nested():
                session.add(event)
                session.flush()
            # The row is now visible in this session; let the circuit breaker
            # evaluate degradation for L3 tasks (rides the caller's commit).
            _run_circuit_breaker(session, task_type, organization_id, commit=False)
            return event.id
        finally:
            if owns_session:
                session.close()
    except Exception as exc:  # pragma: no cover - defensive; never raise into the action path
        logger.debug("[correction_capture] failed to record correction: %s", exc)
        return None


def _run_circuit_breaker(session, task_type: str, organization_id: int, *, commit: bool) -> None:
    """Run the autonomy circuit breaker for L3 shadow-scored tasks. Best-effort —
    a breaker failure must never break the capture or the human's action."""
    if task_type not in _SHADOW_TASK_TYPES:
        return
    try:
        from .autonomy_guard import maybe_trip_breaker

        maybe_trip_breaker(session, organization_id=organization_id, task_type=task_type, commit=commit)
    except Exception as breaker_exc:  # pragma: no cover - defensive
        logger.debug("[correction_capture] circuit breaker failed: %s", breaker_exc)

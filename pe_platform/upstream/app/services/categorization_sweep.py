# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel chunk C1 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — Wave-0 precedent).
"""Daily categorization sweep — executable PENDING proposals (Wave-1 C1, WF-1).

Turns per-transaction categorization predictions (rules first, LLM fallback —
the same engines ``classify_pending_transactions`` uses) into PENDING
``ProposedAction`` rows with the line-level reclassification contract:

    type ``RECLASSIFY_TXN``
    payload ``{entity, txn_id, txn_date, to_account_id}``

No SyncToken or line snapshot is stored at draft time — the executor reads the
entity at execution time (``reclassifications.reclassify_transaction``) and
changes the category line, so a proposal approved hours or days later is still
executable.

Each proposed transaction also persists a ``ClassificationPrediction`` row (the
``sync_classify._persist_predictions`` precedent) so the existing disposition
capture (``routers/classification.py`` approve/reject) and the Needs-Review UI
keep working unchanged.

Idempotency (plan decision 3 / AC-3, re-keyed by plan
20260716-batch5-remediation AC-C1): ``inputs_fingerprint`` is still stamped at
draft (sha256 over action_type + canonical payload — the listener-lane
contract), but the sweep's dedup key is ``(company_id, payload txn_id,
status=PENDING)`` in ONE batched query per run — never per-transaction
existence checks. The top-level ``txn_date`` stamp changed the payload shape,
so a fingerprint key would miss every legacy PENDING proposal and re-draft
duplicates (fingerprint churn). Disposed predictions (latest prediction
``basis`` = ``REJECTED:*``, the reject lane's soft-delete marker) and
dismissed transactions (``classification_dismissed_at`` — first-class Skip,
AC-C3) are filtered out before classification and never re-proposed; the
closed-period SOFT floor (AC-C1) filters and COUNTS floored rows.

L3 auto-execution (plan decision 2 / AC-2) mirrors the existing
``actions.py:_maybe_auto_execute_reclassification`` recipe STEP-FOR-STEP by
importing its functions: ``gate_decision`` → cap-accounting
``record_correction`` BEFORE executing → ``action_is_expired`` guard → atomic
``claim_action_transition`` (reviewed_by="autonomy:supervised_l3") →
``execute_proposed_action``. The shadow default routes 100% of proposals to
the human inbox; ``gate_decision`` is never forked or widened.
"""
import logging
from .proposal_provenance import validate_autonomous_approval

from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models import ClassificationPrediction, ProposedAction, QBOTransaction
from ..qbo_actions.executor import execute_proposed_action
# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C1 (marker
# RATIFICATION-2026-07-13.md; active-plan.json absent in dispatched session) —
# converge on the ONE canonical fingerprint algorithm (C4's module, decision 3)
# instead of a local duplicate, so sweep-stamped and listener-stamped rows hash
# identically for the same action_type + payload.
from ..qbo_actions.proposal_fingerprint import compute_proposal_fingerprint
from ..qbo_actions.reclassifications import LINE_REQUIRED_ERROR, build_reclassification_payload
from ..qbo_actions.transitions import action_is_expired, claim_action_transition
from ..services.classifier import RuleBasedClassifier
from ..services.correction_capture import record_correction
from ..services.llm_classifier import LLMClassifier, llm_failure_reason
from ..services.redaction import redact_to_string
from ..services.task_autonomy import gate_decision, latest_locked_period
from ..services.tenant_companies import organization_for_company

logger = logging.getLogger(__name__)

# Entity types eligible for categorization (LOCK-B): Phase-2 bank-reconciliation
# rows also carry account_ref=NULL but are recon-only and must never be
# categorized; this predicate excludes them.
CATEGORIZATION_ENTITY_TYPES = ("Purchase", "Deposit")

# The sweep's provenance stamp on the proposals it drafts.
SWEEP_SOURCE = "categorization_sweep"

# The autonomy lane for the sweep (plan decision 2): task_type="categorization",
# reusing the existing supervised_l3 actor string — NOT a new lane.
SWEEP_TASK_TYPE = "categorization"
AUTONOMY_ACTOR = "autonomy:supervised_l3"

# Action types a still-PENDING proposal can carry for a reclassification
# (RECLASSIFICATION is the legacy alias the chat lane drafts, executor.py:36).
_RECLASSIFY_ACTION_TYPES = ("RECLASSIFY_TXN", "RECLASSIFICATION")


def closed_period_cutoff(db: Session, realm_id: str) -> date | None:
    """First day of the month AFTER the realm's HARD-lock floor, or None.

    Plan 20260716-batch5-remediation AC-C1 + D-B6-1 (2026-07-16): a classification
    approval is a GL-affecting QBO write through the same executor funnel as POST_JE,
    so the classification list/count/generation/sweep queries take the HARD-lock
    floor (``latest_locked_period`` ``hard_only=True`` — ``locked=TRUE`` OR status
    LOCKED, lock-as-of cascade), the SAME floor the executor's
    ``period_is_hard_locked`` guard enforces. List and push floors never split: a
    listed-but-409 row is a dead CTA; a pushed-but-hidden row is a silent
    closed-books write. Under D-B6-1 a soft-CLOSED (auto-rolled, ``locked=false``)
    month is books-rolled, not write-blocked — its rows stay actionable.

    ``None`` means no period is closed (no calendar rows / nothing locked or
    CLOSED) — callers apply NO date filter, so a no-calendar realm behaves
    exactly as before the floor existed. A non-None cutoff partitions rows:
    ``date >= cutoff`` is actionable; ``date < cutoff`` is floored — filtered
    from the actionable queries but ALWAYS surfaced as a count ("N transactions
    in closed periods — unlock or reclassify in an open month"), never silently
    dropped (SYNTHESIS 20260716 crit-eval finding 7).

    Read errors PROPAGATE (``latest_locked_period`` raises; it never fails
    open) — an indeterminate floor fails loud on these read surfaces rather
    than silently rendering closed-period rows actionable or silently blanking
    the queue. Hosted here (the classification-lane service module) because the
    ratified plan's surface_scope pins the exact files this chunk may touch.
    """
    if not realm_id:
        return None
    floor = latest_locked_period(db, realm_id=realm_id, hard_only=True)
    if not floor:
        return None
    year, month = int(floor[:4]), int(floor[5:7])
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def _pinned_payload(entity: str, txn_id: str, account_id: str, txn_date: str | None) -> dict:
    """The canonical line-level reclassification payload
    (``reclassifications.build_reclassification_payload``). No SyncToken and no
    line snapshot at draft: the executor reads both at execution time, so a
    deferred approval acts on the transaction as it is then.

    ``txn_date`` is the LOCAL row's date for the executor's closed-period guard
    (``CLOSED_PERIOD_DATE_KEYS["RECLASSIFY_TXN"]``); it is never sent to
    QuickBooks. No ``line_id``: the sweep cannot see lines, so a multi-line or
    item-based transaction is refused at execution with
    ``reclassification_line_required`` and is not re-drafted afterwards.
    """
    return build_reclassification_payload(entity, txn_id, account_id, txn_date=txn_date)


def _disposed_txn_ids(db: Session, realm_id: str, txn_ids: list) -> set:
    """Transactions whose LATEST prediction was rejected by a human (the reject
    lane stamps ``basis = 'REJECTED:<basis>'``) — never re-proposed (AC-3).
    One batched query; latest-per-txn resolved in Python (ascending id, last
    write wins)."""
    if not txn_ids:
        return set()
    rows = (
        db.query(ClassificationPrediction)
        .filter(
            ClassificationPrediction.company_id == realm_id,
            ClassificationPrediction.transaction_id.in_(txn_ids),
        )
        .order_by(ClassificationPrediction.id.asc())
        .all()
    )
    latest = {row.transaction_id: row for row in rows}
    return {
        txn_id
        for txn_id, row in latest.items()
        if (row.basis or "").startswith("REJECTED:")
    }


def _pending_reclassify_txn_ids(db: Session, realm_id: str) -> set:
    """THE one batched dedup query per run, keyed on
    ``(company_id, payload txn_id)`` over still-PENDING reclassification
    proposals. The ``txn_date`` stamp changed the payload shape, so a
    fingerprint key would miss every legacy PENDING proposal and re-draft a
    duplicate for the same transaction (fingerprint churn). A transaction with
    ANY still-PENDING reclassification proposal — old or new payload shape, or a
    different suggested account — is never re-drafted. Runs BEFORE
    classification so an already-proposed transaction is never re-billed
    against the LLM.

    The same query also returns FAILED proposals refused with
    ``reclassification_line_required``: the executor could not pick one
    category line on that transaction (multi-line or item-based), so
    re-drafting it daily would only repeat the refusal. A human chooses the
    line instead. Every other disposed proposal (approved, rejected, executed,
    failed for another reason) never blocks a fresh draft. The txn_id and the
    error code are JSON fields, so they are extracted in Python."""
    rows = (
        db.query(ProposedAction.payload, ProposedAction.status, ProposedAction.execution_result)
        .filter(
            ProposedAction.company_id == realm_id,
            ProposedAction.action_type.in_(_RECLASSIFY_ACTION_TYPES),
            ProposedAction.status.in_(("PENDING", "FAILED")),
        )
        .all()
    )
    blocked = set()
    for payload, status, result in rows:
        txn_id = (payload or {}).get("txn_id") if isinstance(payload, dict) else None
        if not txn_id:
            continue
        if status == "PENDING":
            blocked.add(str(txn_id))
            continue
        error = result.get("error") if isinstance(result, dict) else None
        if isinstance(error, dict) and error.get("error_code") == LINE_REQUIRED_ERROR:
            blocked.add(str(txn_id))
    return blocked


def _maybe_auto_execute(db: Session, *, action: ProposedAction, organization_id: int) -> bool:
    """Gated auto-execution for one freshly-drafted sweep proposal — a
    step-for-step mirror of ``app/routers/actions.py``
    ``_maybe_auto_execute_reclassification`` (plan decision 2), with
    ``task_type="categorization"``. Returns True iff the action auto-executed.
    """
    try:
        validate_autonomous_approval(action)
    except HTTPException:
        return False
    decision = gate_decision(
        db,
        organization_id=organization_id,
        task_type=SWEEP_TASK_TYPE,
        realm_id=action.company_id,
        raw_confidence=(float(action.confidence_score) if action.confidence_score is not None else None),
    )
    if not decision.get("auto"):
        return False  # proposal-first, unchanged — the shadow default lands here

    # Record the auto-execution as a training signal BEFORE executing, so the
    # per-day cap (autoexec_count_today counts exactly these rows) and the
    # circuit breaker see it. actor_user_id=None marks it as engine-driven;
    # matched_human=True because the AI output IS what is applied.
    record_correction(
        task_type=SWEEP_TASK_TYPE,
        human_decision="accepted",
        company_id=action.company_id,
        source_object_ref=str(action.id),
        ai_output={"action_type": action.action_type, "payload": action.payload},
        ai_confidence=action.confidence_score,
        human_output={"payload": action.payload, "auto_executed": True},
        actor_user_id=None,
        would_autoexecute=True,
        matched_human=True,
        db=db,
    )

    # Expired-proposal guard (defense-in-depth; the row was created moments ago
    # in this same sweep, so this fires only on a clock-skewed/edge row).
    if action_is_expired(action):
        return False

    # Atomic PENDING→APPROVED claim: if a concurrent human decision already
    # moved this action, the auto-execute must NOT fire a QBO write.
    if not claim_action_transition(
        db, action_id=action.id, from_status="PENDING", to_status="APPROVED",
        reviewed_by=AUTONOMY_ACTOR, expected_revision=action.revision, autonomous=True,
    ):
        db.rollback()
        return False
    db.commit()

    try:
        executed = execute_proposed_action(db, action.id)
        return executed.status == "EXECUTED"
    except HTTPException as exc:
        # AC-E1 (D-B6-2, 2026-07-16): a 409 period_locked is a clean refusal (the
        # row is dated in a HARD-locked period), NOT a failure — decline the
        # auto-exec and log it honestly. The sweep's candidate floor already floors
        # hard-locked rows, so this is a rare race; never break the sweep.
        logger.info(
            "[categorization_sweep] auto-execution declined for action %s (status %s)",
            action.id,
            exc.status_code,
        )
        return False
    except Exception as exc:  # auto-exec failed — surface, but don't break the sweep
        logger.warning(
            "[categorization_sweep] auto-execution failed for action %s: %s",
            action.id,
            redact_to_string(str(exc), max_length=500),
        )
        return False


def run_categorization_sweep(db: Session, realm_id: str, limit: int = 50) -> dict:
    """Run one realm's categorization sweep. The caller (the Celery task) owns
    the tenant scope (``tenant_scope_for_realm``) and the LLM-attribution
    binding (``bind_tenant_tag_for_realm``); every query here carries its own
    realm predicate as the app-layer primary gate."""
    # AC-C1 (plan 20260716-batch5-remediation) + D-B6-1 (2026-07-16): dismissed rows
    # (first-class Skip, AC-C3) and HARD-locked-period rows (the hard-lock floor) never
    # reach classification — a floored row's approval would 409 at the executor, so
    # drafting it is dead LLM spend. Floored rows are counted, never silently dropped
    # (finding 7).
    cutoff = closed_period_cutoff(db, realm_id)
    skipped_closed_period = 0
    pending_query = db.query(QBOTransaction).filter(
        QBOTransaction.realm_id == realm_id,
        QBOTransaction.account_ref.is_(None),
        QBOTransaction.entity_type.in_(CATEGORIZATION_ENTITY_TYPES),
        # AC-3 (plan 20260716-classification-hardening): lanes never split —
        # bill_com rows are excluded from list/generation/count; the sweep too.
        QBOTransaction.source != "bill_com",
        QBOTransaction.classification_dismissed_at.is_(None),
    )
    if cutoff is not None:
        skipped_closed_period = (
            db.query(QBOTransaction)
            .filter(
                QBOTransaction.realm_id == realm_id,
                QBOTransaction.account_ref.is_(None),
                QBOTransaction.entity_type.in_(CATEGORIZATION_ENTITY_TYPES),
        # AC-3 (plan 20260716-classification-hardening): lanes never split —
        # bill_com rows are excluded from list/generation/count; the sweep too.
        QBOTransaction.source != "bill_com",
                QBOTransaction.classification_dismissed_at.is_(None),
                QBOTransaction.date < cutoff,
            )
            .count()
        )
        pending_query = pending_query.filter(QBOTransaction.date >= cutoff)
    pending_txns = pending_query.order_by(desc(QBOTransaction.date)).limit(limit).all()
    result = {
        "realm_id": realm_id,
        "scanned": len(pending_txns),
        "skipped_closed_period": skipped_closed_period,
        "skipped_disposed": 0,
        "proposed": 0,
        "deduped": 0,
        "auto_executed": 0,
        # AC-C2 (plan 20260716-batch5-remediation): generation failures are
        # COUNTED with a reason breakdown — the silent-None funnel surfaced.
        "classification_failed": 0,
        "failure_reasons": {},
    }
    if not pending_txns:
        return result

    # Disposed predictions are never re-proposed (AC-3) — and never re-billed
    # against the LLM: filter BEFORE classification, in one batched query.
    disposed = _disposed_txn_ids(db, realm_id, [t.qbo_id for t in pending_txns])
    candidates = [t for t in pending_txns if t.qbo_id not in disposed]
    result["skipped_disposed"] = len(pending_txns) - len(candidates)
    if not candidates:
        return result

    # THE one batched dedup query per run, keyed (company, txn, PENDING) —
    # before classification, so an already-proposed txn costs no LLM call.
    already_pending = _pending_reclassify_txn_ids(db, realm_id)
    fresh = [t for t in candidates if str(t.qbo_id) not in already_pending]
    result["deduped"] = len(candidates) - len(fresh)
    candidates = fresh
    if not candidates:
        return result

    # Rules first, LLM fallback — the same engines classify_pending_transactions
    # (routers/classification.py) runs; the loop is its generator shape with the
    # pinned-payload drafting the "theoretical" task never had.
    rule_engine = RuleBasedClassifier(db)
    llm_engine = LLMClassifier(db)
    drafts = []

    def _count_failure(txn_id: str, reason: str) -> None:
        # AC-C2: the sweep's silent-None skip (the old bare `continue`) becomes
        # a counted, per-txn-logged failure — count and continue, never raise
        # through the loop, never silently.
        result["classification_failed"] += 1
        reasons = result["failure_reasons"]
        reasons[reason] = (reasons[reason] if reason in reasons else 0) + 1
        logger.warning(
            "[categorization_sweep] realm %s txn %s produced no classification (reason=%s)",
            realm_id,
            txn_id,
            reason,
        )

    for txn in candidates:  # SA-OK: bounded-loop limit<=50 — engine classify is per-txn by design (rule cache prefetched; LLM fallback only on rule miss)
        try:
            suggestion = rule_engine.classify(txn)
            source = "Rule"
            if not suggestion:
                suggestion = llm_engine.classify(txn)
                source = "LLM"
        except Exception as exc:
            result["classification_failed"] += 1
            reasons = result["failure_reasons"]
            reasons["classifier_error"] = (
                reasons["classifier_error"] if "classifier_error" in reasons else 0
            ) + 1
            logger.warning(
                "[categorization_sweep] realm %s txn %s classification errored: %s",
                realm_id,
                txn.qbo_id,
                redact_to_string(str(exc), max_length=500),
            )
            continue
        if not suggestion:
            _count_failure(txn.qbo_id, llm_failure_reason(llm_engine))
            continue
        if "account_id" not in suggestion or not suggestion["account_id"]:
            _count_failure(txn.qbo_id, "invalid_suggestion")
            continue
        payload = _pinned_payload(
            txn.entity_type or "Purchase",
            txn.qbo_id,
            suggestion["account_id"],
            txn_date=(txn.date.isoformat() if txn.date else None),
        )
        drafts.append(
            {
                "txn_id": txn.qbo_id,
        "entity_type": txn.entity_type,
                "payload": payload,
                "fingerprint": compute_proposal_fingerprint("RECLASSIFY_TXN", payload),
                "account_id": str(suggestion["account_id"]),
                "account_name": suggestion.get("account_name"),
                "reason": suggestion.get("reason"),
                "confidence": suggestion.get("confidence"),
                "source": source,
            }
        )
    if result["classification_failed"]:
        # AC-C2: the run-level summary the worker log surfaces — the reason
        # breakdown makes "0 proposed" diagnosable without per-txn digging.
        logger.warning(
            "[categorization_sweep] realm %s: %d of %d candidates failed classification (%s)",
            realm_id,
            result["classification_failed"],
            len(candidates),
            result["failure_reasons"],
        )
    if not drafts:
        return result

    actions = []
    for draft in drafts:
        action = ProposedAction(
            company_id=realm_id,
            action_type="RECLASSIFY_TXN",
            payload=draft["payload"],
            reasoning=draft["reason"] or "Daily categorization sweep",
            confidence_score=draft["confidence"],
            status="PENDING",
            source=SWEEP_SOURCE,
            category="reclassification",
            inputs_fingerprint=draft["fingerprint"],
        )
        db.add(action)
        # Persist the prediction (sync_classify._persist_predictions precedent)
        # so disposition capture + the Needs-Review UI keep working — only for
        # newly-drafted proposals, so a re-sweep accumulates no duplicate rows.
        db.add(
            ClassificationPrediction(
                company_id=realm_id,
                transaction_id=draft["txn_id"],
                # AC-1 (plan 20260716-classification-hardening): LOCK-1 identity.
                entity_type=draft["entity_type"] if "entity_type" in draft else None,
                predicted_account_id=draft["account_id"],
                predicted_account_name=draft["account_name"],
                confidence_score=(
                    Decimal(str(draft["confidence"])) if draft["confidence"] is not None else None
                ),
                basis=draft["source"],
                reasoning=draft["reason"],
            )
        )
        actions.append(action)
    db.commit()
    result["proposed"] = len(actions)

    # L3 auto-execution — only when the tenant deliberately promoted the
    # categorization task to supervised_l3 AND the gate passes per action; the
    # shadow default leaves every proposal PENDING for the human inbox.
    organization = organization_for_company(db, realm_id)
    if organization is None:
        return result  # can't resolve tenant scope → never auto-execute
    for action in actions:  # SA-OK: bounded-loop limit<=50 — per-proposal executor funnel is irreducible (each action claims its own gate decision + idempotency ledger row)
        if _maybe_auto_execute(db, action=action, organization_id=organization.id):
            result["auto_executed"] += 1
    return result

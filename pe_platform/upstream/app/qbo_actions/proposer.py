from typing import Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from ..models import ProposedAction
from ..services.proposal_provenance import human_provenance

# Journal-entry action types that a reversal can be built from. Both are
# canonical/accepted by the executor for the JE path (executor.py:264,289);
# no alias maps onto them, so an uppercase membership check is exact.
REVERSIBLE_JE_ACTION_TYPES = frozenset({"POST_JE", "JOURNAL_ENTRY"})


def propose_journal_entry(
    db: Session, 
    realm_id: str, 
    payload: Dict[str, Any], 
    reasoning: Optional[str] = None, 
    confidence_score: float = 0.95,
    *,
    dry_run: bool = False,
    human_preparer_user_id: int | None = None,
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json not synced into this
    # dispatched session — standing precedent). Decisions 5/6: the 2.1 run-linkage
    # kwargs + the backend evidence stamp + the memo linkage stamp, all defaulted
    # so every existing caller's behavior is byte-identical.
    agent_run_id: "int | None" = None,
    source: str = "ai_assistant",
    computation_evidence: "dict | None" = None,
    stamp_memo_linkage: bool = False,
) -> ProposedAction:
    """Proposes a Journal Entry action.

    ``dry_run=True``: build the SAME validated action and return it TRANSIENT —
    no add/commit, ``action.id is None``, zero rows written (the C3 handlers
    thread the model's ``_dry_run`` flag here).

    ``computation_evidence`` (decision 6, plan 20260713-wave2-5-cecl-allowance):
    a BACKEND-computed evidence block stamped into the payload PRE-insert under
    the ``computation_evidence`` key. Pre-insert is deliberate: it folds into the
    before_insert dedup fingerprint harmlessly (the engine is deterministic per
    inputs, so a retried turn still collapses onto its first copy). Key-presence
    IS provenance — every JE lane allowlist-constructs its payload (the chat
    handler's resolved_params, the router's JELine schema), so a model-supplied
    imitation of this key can never reach a stored payload; only this kwarg can.

    ``stamp_memo_linkage`` (decision 5 / E4): after the INSERT assigns the id,
    the payload's ``memo`` is stamped with the proposal linkage. On the
    simplified ``{date, memo, lines}`` shape the JE writer maps ``memo`` to QBO
    PrivateNote — the reconcile probe's match key — so a ``PrivateNote`` sibling
    key would be a silent NO-OP. Stamped AFTER insert, so the fingerprint hashes
    the SEMANTIC inputs only (the _propose_ar_action PrivateNote precedent).
    """
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-4: dry_run is a natural
    # no-write exit AFTER the payload is built; proposal-only routing stays proven by
    # the existing safety_expectation AgentEvalFixtures (app/agent/evals.py).
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B — decision 6:
    # the PRE-insert backend evidence stamp (see the docstring provenance note).
    if computation_evidence is not None:
        payload = {**(payload or {}), "computation_evidence": computation_evidence}
    action = ProposedAction(
        company_id=realm_id,
        **human_provenance(human_preparer_user_id),
        action_type="POST_JE",
        payload=payload,
        reasoning=reasoning,
        confidence_score=confidence_score,
        status="PENDING",
        agent_run_id=agent_run_id,
        source=source,
    )
    if dry_run:
        return action
    db.add(action)
    db.flush()
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B — decision 5/E4:
    # the POST-insert memo linkage stamp (the id exists only after the INSERT; stamping
    # after the before_insert fingerprint keeps retried drafts colliding).
    if stamp_memo_linkage:
        stamped = dict(action.payload or {})
        base_memo = str(stamped.get("memo") or "").strip()
        linkage = f"Peregrine proposal #{action.id}"
        stamped["memo"] = f"{base_memo} | {linkage}" if base_memo else linkage
        # Reassigning a new dict marks the JSON column dirty for SQLAlchemy.
        action.payload = stamped
        db.add(action)
    db.commit()
    db.refresh(action)
    return action

def propose_reclassification(
    db: Session, 
    realm_id: str, 
    payload: Dict[str, Any], 
    reasoning: Optional[str] = None, 
    confidence_score: float = 0.95,
    *,
    dry_run: bool = False,
    human_preparer_user_id: int | None = None,
) -> ProposedAction:
    """Proposes a transaction reclassification action.

    ``dry_run=True``: build the SAME validated action and return it TRANSIENT —
    no add/commit, ``action.id is None``, zero rows written.
    """
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-4: dry_run no-write exit;
    # proposal-only routing stays proven by the existing safety_expectation
    # AgentEvalFixture reclassify_transaction_proposal (app/agent/evals.py).
    action = ProposedAction(
        company_id=realm_id,
        **human_provenance(human_preparer_user_id),
        action_type="RECLASSIFY_TXN",
        payload=payload,
        reasoning=reasoning,
        confidence_score=confidence_score,
        status="PENDING"
    )
    if dry_run:
        return action
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


# ── Reverse an executed journal entry (routed as a NEW proposal) ──────────────
#
# A reversal is NOT a direct write and NOT a new write path: it flows through
# `propose_journal_entry` — the SAME proposer the manual propose-JE endpoint
# uses — so it becomes an ordinary POST_JE ProposedAction(PENDING) and, on
# approval, travels the normal executor funnel (closed-period guard +
# idempotency + approval requirement). The executor gate is therefore intact:
# nothing here touches executor.py.
#
# Reversal-chain guard (mirrors ScheduleEntry.reversal_of_entry_id, models.py:
# 2488): the new proposal is tagged with `reversal_of_action_id` in its payload
# and the original is linked back via `reversed_by_action_id`. A proposal that
# IS a reversal can never itself be reversed, and the original's back-link lets
# the UI show "already reversed". Stored on the existing payload JSON — no
# schema migration.

def _swap_je_line(line: Dict[str, Any]) -> Dict[str, Any]:
    """Debit/credit-swapped copy of a single JE line; all other fields kept."""
    swapped = dict(line)
    debit = line.get("debit") or 0.0
    credit = line.get("credit") or 0.0
    swapped["debit"] = credit
    swapped["credit"] = debit
    return swapped


def build_reversal_payload(original_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the reversing JE payload from an executed JE payload.

    Each line's debit/credit is swapped (which preserves debits==credits, so
    the executor/JE-writer balance check still passes). The reversal keeps the
    ORIGINAL date — i.e. the same accounting period — so the executor's
    closed-period guard naturally applies to that period: a reversal can never
    silently post into a locked period.
    """
    payload = original_payload if isinstance(original_payload, dict) else {}
    lines = [
        _swap_je_line(line)
        for line in (payload.get("lines") or [])
        if isinstance(line, dict)
    ]
    memo = payload.get("memo") or ""
    return {
        "date": payload.get("date"),
        "memo": f"Reversal of: {memo}" if memo else "Reversal",
        "lines": lines,
    }


def reversal_block_reason(action: ProposedAction) -> Optional[Tuple[int, str, str]]:
    """Return `(status_code, error_code, user_message)` if `action` cannot be
    reversed, else `None`.

    Only an EXECUTED journal-entry proposal that is neither itself a reversal
    nor already reversed is reversible.
    """
    action_type = str(action.action_type or "").upper()
    if action_type not in REVERSIBLE_JE_ACTION_TYPES:
        return (400, "not_a_journal_entry", "Only journal entries can be reversed.")
    if action.status != "EXECUTED":
        return (409, "not_executed", "Only an executed journal entry can be reversed.")
    payload = action.payload if isinstance(action.payload, dict) else {}
    if payload.get("reversal_of_action_id"):
        return (409, "already_a_reversal", "A reversal entry cannot itself be reversed.")
    if payload.get("reversed_by_action_id") or (action.execution_result or {}).get("reversed_by_action_id"):
        return (409, "already_reversed", "This entry has already been reversed.")
    return None


# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
# RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-4: dry_run kwarg threading.
def propose_je_reversal(db: Session, original: ProposedAction, *, dry_run: bool = False, human_preparer_user_id: int | None = None) -> ProposedAction:
    """Create a PENDING reversing-JE proposal from an EXECUTED JE proposal.

    Caller MUST have already cleared `reversal_block_reason(original)`. Returns
    the new PENDING ProposedAction (action_type POST_JE), linked to `original`
    in both directions via the payload JSON.

    ``dry_run=True``: build the SAME reversal payload and return the TRANSIENT
    action (no add/commit, ``id is None``); ``original`` is NOT touched (no
    back-link — nothing was drafted to link back to).
    """
    reversal_payload = build_reversal_payload(original.payload or {})
    reversal_payload["reversal_of_action_id"] = original.id

    executed_on = None
    if original.reviewed_at:
        executed_on = original.reviewed_at.strftime("%Y-%m-%d")
    elif original.created_at:
        executed_on = original.created_at.strftime("%Y-%m-%d")
    executed_on = executed_on or "an earlier date"
    original_reason = (original.reasoning or "Journal Entry").strip()
    reasoning = f"Reversal of JE #{original.id} executed {executed_on}: {original_reason}"

    # Route through the SAME proposer the propose-JE endpoint uses — no parallel
    # write path. This creates + commits the PENDING POST_JE proposal.
    reversal = propose_journal_entry(
        db=db,
        realm_id=original.company_id,
        payload=reversal_payload,
        reasoning=reasoning,
        confidence_score=1.0,
        dry_run=dry_run,
        human_preparer_user_id=human_preparer_user_id,
    )

    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-4: on a dry run the
    # transient reversal carries the forward link (original.id exists) but the
    # persisted ``original`` is NEVER dirtied — there is no drafted id to back-link.
    if dry_run:
        transient_payload = dict(reversal.payload or {})
        transient_payload["reversal_of_action_id"] = original.id
        reversal.payload = transient_payload
        return reversal

    # Historical executed payloads stay immutable: backlink is execution metadata.
    original_result = dict(original.execution_result or {})
    original_result["reversed_by_action_id"] = reversal.id
    original.execution_result = original_result

    db.add(reversal)
    db.add(original)
    db.commit()
    db.refresh(reversal)
    return reversal


def propose_reconciliation(
    db: Session, 
    realm_id: str, 
    payload: Dict[str, Any], 
    reasoning: Optional[str] = None, 
    confidence_score: float = 0.95,
    *,
    dry_run: bool = False,
) -> ProposedAction:
    """Proposes a reconciliation action.

    ``dry_run=True``: build the SAME validated action and return it TRANSIENT —
    no add/commit, ``action.id is None``, zero rows written.
    """
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-4: dry_run no-write exit;
    # proposal-only routing stays proven by the existing safety_expectation
    # AgentEvalFixtures (app/agent/evals.py).
    action = ProposedAction(
        company_id=realm_id,
        action_type="RECONCILE",
        payload=payload,
        reasoning=reasoning,
        confidence_score=confidence_score,
        status="PENDING"
    )
    if dry_run:
        return action
    db.add(action)
    db.commit()
    db.refresh(action)
    return action

def propose_vendor_update(
    db: Session, 
    realm_id: str, 
    payload: Dict[str, Any], 
    reasoning: Optional[str] = None, 
    confidence_score: float = 0.95,
    *,
    dry_run: bool = False,
) -> ProposedAction:
    """Proposes a vendor update action.

    ``dry_run=True``: build the SAME validated action and return it TRANSIENT —
    no add/commit, ``action.id is None``, zero rows written.
    """
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-4: dry_run no-write exit;
    # proposal-only routing stays proven by the existing safety_expectation
    # AgentEvalFixture update_vendor_proposal (app/agent/evals.py).
    action = ProposedAction(
        company_id=realm_id,
        action_type="UPDATE_VENDOR",
        payload=payload,
        reasoning=reasoning,
        confidence_score=confidence_score,
        status="PENDING"
    )
    if dry_run:
        return action
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


# ── Wave 2.1 AR collections proposers (AC-2, decisions 5/7/8/9/13) ────────────
# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C1 (marker
# RATIFICATION-2026-07-13.md on disk; dispatched session lacks active-plan state).
#
# One proposer per type; the chat handlers (C2) AND the collections runner (C4)
# both draft through these — no parallel write path. The OPTIONAL
# `agent_run_id`/`source` kwargs give the runner its linkage WITHOUT the
# close-orchestration direct-construction pattern (that pattern is close-lane-
# only; its dedup constraint excludes NULL close_task_id rows). The
# `inputs_fingerprint` is stamped by the central before_insert listener
# (proposal_fingerprint.py) — imported below so every proposer lane carries it.

from . import proposal_fingerprint  # noqa: F401,E402 — registers the before_insert fingerprint listener for every proposer lane


def _propose_ar_action(
    db: Session,
    realm_id: str,
    action_type: str,
    payload: dict,
    reasoning: "str | None",
    confidence_score: float,
    *,
    agent_run_id: "int | None" = None,
    source: str = "ai_assistant",
    stamp_private_note: bool = True,
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4 / AC-4: the shared dry_run exit.
    dry_run: bool = False,
) -> ProposedAction:
    """Shared AR proposal template (the :19 shape + run-linkage kwargs).

    ``dry_run=True``: build the SAME validated action and return it TRANSIENT —
    no add/commit, ``action.id is None``, zero rows written. The PrivateNote
    linkage stamp is skipped too: it embeds the proposal id, which does not
    exist on a dry run (and the real stamp happens AFTER the fingerprint is
    computed, so its absence cannot fork the fingerprint).

    ``stamp_private_note`` (decision 5): after the INSERT assigns the id, the
    three CREATE types get their payload's ``PrivateNote`` stamped with the
    proposal linkage — the deterministic reconcile-probe key the executor
    matches when a prior attempt ended uncertain. Stamped AFTER insert, so the
    ``inputs_fingerprint`` (computed before_insert) hashes the SEMANTIC inputs
    only and a retried draft still collides with its first copy.
    """
    action = ProposedAction(
        company_id=realm_id,
        action_type=action_type,
        payload=payload,
        reasoning=reasoning,
        confidence_score=confidence_score,
        status="PENDING",
        agent_run_id=agent_run_id,
        source=source,
    )
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 /
    # AC-4: the natural no-write exit (validated + built, never added/committed).
    if dry_run:
        return action
    db.add(action)
    # Publish the complete payload once; a reviewer must never see the draft
    # between its INSERT and the recovery-linkage stamp.
    db.flush()
    if stamp_private_note:
        stamped = dict(action.payload or {})
        base_note = str(stamped.get("PrivateNote") or "").strip()
        linkage = f"Peregrine proposal #{action.id}"
        stamped["PrivateNote"] = f"{base_note} | {linkage}" if base_note else linkage
        # Reassigning a new dict marks the JSON column dirty for SQLAlchemy.
        action.payload = stamped
        db.add(action)
    db.commit()
    db.refresh(action)
    return action


def propose_invoice_creation(
    db: Session,
    realm_id: str,
    payload: dict,
    reasoning: "str | None" = None,
    confidence_score: float = 0.95,
    *,
    agent_run_id: "int | None" = None,
    source: str = "ai_assistant",
    dry_run: bool = False,
) -> ProposedAction:
    """Proposes a CREATE_INVOICE action (decision 9 payload contract).

    ``dry_run=True`` returns the TRANSIENT action (no add/commit, zero rows)."""
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 / AC-4.
    return _propose_ar_action(
        db, realm_id, "CREATE_INVOICE", payload, reasoning, confidence_score,
        agent_run_id=agent_run_id, source=source, dry_run=dry_run,
    )


def propose_customer_payment(
    db: Session,
    realm_id: str,
    payload: dict,
    reasoning: "str | None" = None,
    confidence_score: float = 0.95,
    *,
    agent_run_id: "int | None" = None,
    source: str = "ai_assistant",
    dry_run: bool = False,
) -> ProposedAction:
    """Proposes a RECORD_CUSTOMER_PAYMENT action (decision 8 payload contract).

    ``dry_run=True`` returns the TRANSIENT action (no add/commit, zero rows)."""
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 / AC-4.
    return _propose_ar_action(
        db, realm_id, "RECORD_CUSTOMER_PAYMENT", payload, reasoning, confidence_score,
        agent_run_id=agent_run_id, source=source, dry_run=dry_run,
    )


def propose_credit_memo(
    db: Session,
    realm_id: str,
    payload: dict,
    reasoning: "str | None" = None,
    confidence_score: float = 0.95,
    *,
    agent_run_id: "int | None" = None,
    source: str = "ai_assistant",
    dry_run: bool = False,
) -> ProposedAction:
    """Proposes a CREATE_CREDIT_MEMO action (decision 9 shape, no DueDate).

    ``dry_run=True`` returns the TRANSIENT action (no add/commit, zero rows)."""
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 / AC-4.
    return _propose_ar_action(
        db, realm_id, "CREATE_CREDIT_MEMO", payload, reasoning, confidence_score,
        agent_run_id=agent_run_id, source=source, dry_run=dry_run,
    )


def propose_invoice_reminder(
    db: Session,
    realm_id: str,
    payload: dict,
    reasoning: "str | None" = None,
    confidence_score: float = 0.95,
    *,
    agent_run_id: "int | None" = None,
    source: str = "ai_assistant",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 / AC-4.
    dry_run: bool = False,
) -> ProposedAction:
    """Proposes a SEND_INVOICE_REMINDER action (decisions 2/7).

    ``dry_run=True``: the SAME payload validations run, then the TRANSIENT
    action is returned — no add/commit, zero rows written.

    The payload carries ``invoice_id`` + ``reminder_date`` (which differentiates
    the fingerprint of a deliberate repeat chase) plus CustomerRef/doc/display
    info for the reviewer. NO recipient field exists anywhere on this lane —
    QBO delivers to the invoice's own BillEmail (decision 2). No PrivateNote
    stamp: sending an email creates no QBO entity to probe by memo.
    """
    payload = payload or {}
    if not str(payload.get("invoice_id") or "").strip():
        raise ValueError("Reminder proposal requires 'invoice_id' in the payload.")
    if not str(payload.get("reminder_date") or "").strip():
        raise ValueError("Reminder proposal requires 'reminder_date' in the payload.")
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 / AC-4.
    return _propose_ar_action(
        db, realm_id, "SEND_INVOICE_REMINDER", payload, reasoning, confidence_score,
        agent_run_id=agent_run_id, source=source, stamp_private_note=False, dry_run=dry_run,
    )


# ── Wave 2.2 AP payment-batch proposer (plan AC-4, decisions 9/10) ────────────
# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk C (marker
# RATIFICATION-2026-07-13.md on disk; dispatched session lacks active-plan state).
#
# The proposer is the DRAFT-TIME gate (UX layer): named-reason exclusions + the
# AP_AGENT_BATCH_MAX_TOTAL cap. The UNIQUE(company_id, bill_id) constraint on
# BillPaymentExecution (decision 6) remains the enforcement.

from datetime import date as _date  # noqa: E402

from ..config import _env_int  # noqa: E402
from ..qbo_client import QBOClient  # noqa: E402
from .bill_payment_ledger import execution_status, find_extant_execution  # noqa: E402

AP_AGENT_BATCH_MAX_TOTAL_DEFAULT = 25_000

_EXTANT_EXCLUSION_REASONS = {
    "succeeded": "already_paid",
    "uncertain": "payment_unresolved",
    "claimed": "payment_in_flight",
}


def _ap_bill_amount(bill: dict) -> float:
    """The amount the core will pay: ``balance`` falling back to ``amount``."""
    raw = (bill or {}).get("balance", (bill or {}).get("amount", 0))  # SA-OK: dict.get on a JSON bill dict, not an ORM query
    try:
        return round(float(raw or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def propose_bill_payment_batch(
    db: Session,
    qbo: QBOClient,
    realm_id: str,
    payload: dict,
    reasoning: "str | None" = None,
    confidence_score: float = 0.95,
    *,
    agent_run_id: "int | None" = None,
    source: str = "ai_assistant",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 / AC-4.
    dry_run: bool = False,
) -> ProposedAction:
    """Draft a BILL_PAY payment-batch proposal (decisions 9/10).

    ``dry_run=True``: EVERY draft-time gate below still runs (account validation,
    exclusions, the cap, zero-bill refusal), then the TRANSIENT action is
    returned — no add/commit, zero rows written.

    Draft-time gates:

    - ``payment_account_id`` REQUIRED and validated via ``qbo`` as a QBO Bank
      account (the proposer takes the client so a future runner lane inherits
      the validation — decision 9); ``TxnDate`` REQUIRED (ISO date);
      ``payment_method`` 'check' only — ach/wire refused.
    - Bills with ANY extant BillPaymentExecution row (claimed / uncertain /
      succeeded — decision 6's broadened set) are EXCLUDED with a named reason
      recorded in the proposal payload's ``excluded_bills``.
    - Batches whose included total exceeds ``AP_AGENT_BATCH_MAX_TOTAL``
      (default $25,000; env-tunable, read at CALL time) are refused; zero-bill
      batches (including all-excluded) are refused (decision 10).
    """
    payload = payload or {}
    bills = payload.get("bills") or []
    if not bills:
        raise ValueError("Payment-batch proposal requires at least one bill — zero-bill batches are refused.")

    payment_method = str(payload.get("payment_method") or "check").strip().lower()
    if payment_method != "check":
        raise ValueError(
            f"Payment-batch proposals support payment_method 'check' only; '{payment_method}' is "
            "refused (decision 9 — no ach/wire in v1)."
        )

    raw_txn_date = str(payload.get("TxnDate") or "").strip()
    if not raw_txn_date:
        raise ValueError("Payment-batch proposal requires 'TxnDate'.")
    try:
        _date.fromisoformat(raw_txn_date[:10])
    except ValueError as exc:
        raise ValueError(f"Payment-batch 'TxnDate' is not a valid ISO date: {raw_txn_date!r}.") from exc

    account_id = str(payload.get("payment_account_id") or "").strip()
    if not account_id:
        raise ValueError("Payment-batch proposal requires 'payment_account_id'.")
    account = (qbo.get_transaction("account", account_id) or {}).get("Account") or {}
    account_type = str(account.get("AccountType") or "").strip()
    if account_type != "Bank":
        raise ValueError(
            f"payment_account_id {account_id} is not a QBO Bank account "
            f"(AccountType: {account_type or 'unknown'}) — refusing to draft a payment batch from it."
        )

    included: list = []
    excluded: list = []
    for bill in bills:  # SA-OK: bounded-loop (human-reviewable draft batch) — per-bill find_extant_execution is chunk A's read-side companion of the UNIQUE(company_id, bill_id) money gate
        bill = bill or {}
        bill_id = str(bill.get("bill_id") or "").strip()
        if not bill_id:
            raise ValueError("Every bill in a payment-batch proposal requires 'bill_id'.")
        extant = find_extant_execution(db, company_id=realm_id, bill_id=bill_id)
        if extant is not None:
            status = execution_status(extant)
            excluded.append(
                {
                    "bill_id": bill_id,
                    "reason": _EXTANT_EXCLUSION_REASONS.get(status, "payment_in_flight"),
                    "detail": (
                        f"Bill {bill_id} already has a payment-execution record "
                        f"(status: {status}, batch {extant.batch_id}); it cannot enter a second batch."
                    ),
                }
            )
            continue
        included.append(bill)

    if not included:
        raise ValueError(
            "Every bill in this batch is already covered by an extant payment execution — "
            "nothing to draft."
        )

    total = round(sum(_ap_bill_amount(bill) for bill in included), 2)
    cap = float(_env_int("AP_AGENT_BATCH_MAX_TOTAL", AP_AGENT_BATCH_MAX_TOTAL_DEFAULT))
    if total > cap:
        raise ValueError(
            f"Payment-batch total {total:.2f} exceeds the agent draft cap {cap:.2f} "
            "(AP_AGENT_BATCH_MAX_TOTAL). Split the run into smaller batches or raise the cap "
            "deliberately — each batch still requires its own human approval."
        )

    draft_payload = {
        "batch_name": str(payload.get("batch_name") or f"AI payment batch {raw_txn_date[:10]}"),
        "payment_account_id": account_id,
        "TxnDate": raw_txn_date[:10],
        "payment_method": "check",
        "bills": included,
        "excluded_bills": excluded,
        "total_amount": total,
    }
    # The 2.1 run-linkage template. No PrivateNote stamp: the BILL_PAY probe keys
    # on Line[].LinkedTxn.TxnId (the bill id), not a memo.
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 — decision 4 / AC-4.
    return _propose_ar_action(
        db, realm_id, "BILL_PAY", draft_payload, reasoning, confidence_score,
        agent_run_id=agent_run_id, source=source, stamp_private_note=False, dry_run=dry_run,
    )

from datetime import datetime
from typing import Dict, Any, List
import json
import re

from ..qbo_client import QBOClient, parse_qbo_fault, qbo_fault_public_message
# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B1 — typed uncertain-outcome error
from ..qbo_errors import QBOUncertainOutcomeError
from .probe_paging import (
    create_time_filter,
    has_any_proposal_stamp,
    has_proposal_stamp,
    proposal_stamp,
    query_all,
)
from .qbo_query import qbo_escape_str
from .refusals import QBOActionRefused

_UNCERTAIN_POST_MESSAGE = (
    "Journal Entry POST outcome unknown: the request may have reached QuickBooks "
    "before the connection failed. Do not blindly retry — reconcile first."
)


# Internal payload key: the proposal whose stamp ``create_journal_entry`` writes into
# the PrivateNote. Never sent to QuickBooks and never stored on the proposal.
PROPOSAL_STAMP_KEY = "_stamp_proposal_id"


def stamp_journal_entry_payload(payload: dict, proposal_id: int | None) -> dict:
    """A copy of ``payload`` that ``create_journal_entry`` posts with the stamp.

    The executor posts every journal entry with ``Vesper Bot proposal #<id>`` in
    its PrivateNote (the A/R lane's stamp format), so an uncertain post can be
    found by identity, not by a same-day same-total coincidence. The approved
    memo is left as is (the writer appends the stamp when it builds the
    PrivateNote), and the stored payload — and so the idempotency key — is
    unchanged.
    """
    if proposal_id is None:
        return payload
    stamped = dict(payload or {})
    stamped[PROPOSAL_STAMP_KEY] = int(proposal_id)
    return stamped


def _stamped_note(note: str | None, proposal_id: int | None) -> str:
    """``note`` with this proposal's stamp appended (unless it already carries it)."""
    text = str(note or "").strip()
    if proposal_id is None or has_proposal_stamp(text, proposal_id):
        return text
    stamp = proposal_stamp(proposal_id)
    return f"{text} | {stamp}" if text else stamp


def _payload_total_debit(payload: dict) -> tuple[str | None, str, float]:
    if "Line" in payload:
        total_debit = 0.0
        for line in payload.get("Line") or []:
            detail = (line or {}).get("JournalEntryLineDetail") or {}
            if str(detail.get("PostingType") or "").strip().lower() == "debit":
                total_debit += float((line or {}).get("Amount") or 0)
        return payload.get("TxnDate"), str(payload.get("PrivateNote") or "").strip(), total_debit
    total_debit = sum(float(line.get("debit") or 0) for line in (payload.get("lines") or []))
    return payload.get("date"), str(payload.get("memo") or "").strip(), total_debit


def _entity_total_debit(entity: dict) -> float:
    total = 0.0
    for line in entity.get("Line") or []:
        detail = (line or {}).get("JournalEntryLineDetail") or {}
        if str(detail.get("PostingType") or "").strip().lower() == "debit":
            total += float((line or {}).get("Amount") or 0)
    return total


def _ambiguous(count: int, txn_date: str) -> QBOUncertainOutcomeError:
    return QBOUncertainOutcomeError(
        f"Reconciliation is ambiguous: {count} journal entries on {txn_date} match the "
        "uncertain post's amount/memo. A human must reconcile before this action is retried."
    )


# QBO-OK: balanced-upstream idempotent-upstream — this probe is READ-ONLY (client.query); it
# never posts. It exists to protect the executor's idempotency ledger from a double-post.
def find_posted_journal_entry(
    client: QBOClient,
    payload: dict,
    created_after: datetime | None = None,
    proposal_id: int | None = None,
) -> dict | None:  # CRAFT-OK: dynamic QBO provider payload boundary
    """Deterministic reconciliation probe for an UNCERTAIN prior JE post (INV-UNCERTAIN).

    Reads every JournalEntry on the payload's TxnDate (all pages), restricted to
    entries created since the uncertain attempt started (``created_after``, less a
    clock-skew margin). With ``proposal_id`` (the executor's call):

    * exactly one entry carrying this proposal's stamp and the same debit total →
      the prior post landed; return ``{"JournalEntry": <entity>}`` (no re-POST);
    * more than one such entry, or a stamped entry whose total differs → raise
      ``QBOUncertainOutcomeError`` (a human reconciles);
    * no stamped entry, but an UNSTAMPED entry with the same date, total and memo
      exists in the window → raise as well: an attempt made before execution-time
      stamping posted the bare memo, so the look-alike may be that post; it is never
      adopted and never posted over;
    * otherwise → None (confirmed absent; safe to post).

    Without ``proposal_id`` (direct callers) the historic rule applies: same date +
    total (+ memo when present); one match → adopt, several → raise, none → None.
    """
    txn_date, memo, total_debit = _payload_total_debit(payload or {})
    if not txn_date:
        return None

    # QBO-OK: idempotent-upstream balanced-upstream — this IS the read-only reconciliation
    # probe (client.query; never POSTs a JE), the idempotency guard itself, not a JE write.
    where = (
        f"TxnDate = '{qbo_escape_str(str(txn_date)[:10])}'"  # ISO-prefix clamped FIRST, then escaped
        + create_time_filter(created_after)
    )
    candidates = query_all(client, "JournalEntry", where)
    same_total = [e for e in candidates if abs(_entity_total_debit(e) - total_debit) <= 0.005]

    if proposal_id is None:
        matches = [
            entity
            for entity in same_total
            if not memo or str(entity.get("PrivateNote") or "").strip() == memo
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise _ambiguous(len(matches), str(txn_date))
        return {"JournalEntry": matches[0], "reconciled_from_uncertain": True}

    stamped = [e for e in candidates if has_proposal_stamp(e.get("PrivateNote"), proposal_id)]
    if stamped:
        if len(stamped) > 1:
            raise _ambiguous(len(stamped), str(txn_date))
        if abs(_entity_total_debit(stamped[0]) - total_debit) > 0.005:
            raise QBOUncertainOutcomeError(
                f"Journal entry {stamped[0].get('Id')} carries this proposal's stamp but its total "
                "differs from the approved entry. A human must reconcile before this action is retried."
            )
        return {"JournalEntry": stamped[0], "reconciled_from_uncertain": True}

    base_memo = re.sub(rf"\s*\|?\s*{re.escape(proposal_stamp(proposal_id))}(?!\d)\s*$", "", memo).strip()
    look_alikes = [
        entity
        for entity in same_total
        if not has_any_proposal_stamp(entity.get("PrivateNote"))
        and (not base_memo or str(entity.get("PrivateNote") or "").strip() == base_memo)
    ]
    if look_alikes:
        raise QBOUncertainOutcomeError(
            f"{len(look_alikes)} unstamped journal entr{'y' if len(look_alikes) == 1 else 'ies'} on "
            f"{txn_date} match the uncertain post's amount/memo and may be an earlier attempt of "
            "this proposal. A human must reconcile before this action is retried."
        )
    return None


def _build_qbo_je_payload(
    date: str,
    memo: str,
    lines: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Converts our simplified line format into a QBO JournalEntry payload.

    Input lines format:
        [{ "account_id": "123", "debit": 500.00, "credit": 0, "description": "...",
           "entity_type": "Vendor", "entity_id": "56" }, ...]

    QBO requires each line to have a PostingType of "Debit" or "Credit".
    A line carrying a resolved ``entity_type`` ("Customer" | "Vendor") and
    ``entity_id`` gets ``JournalEntryLineDetail.Entity`` — required by QBO on
    every A/R (Customer) and A/P (Vendor) line. Names are resolved to ids
    before this point (``je_lines.resolve_je_lines``); a bare name is never sent.
    NOTE: Do NOT include "Id" on lines — QBO auto-assigns it and rejects payloads that include it.
    """
    qbo_lines = []
    for line in lines:
        account_id = str(line["account_id"])
        debit = float(line.get("debit") or 0)
        credit = float(line.get("credit") or 0)
        desc = line.get("description", "")
        entity_type = line.get("entity_type")
        entity_id = str(line.get("entity_id") or "").strip()
        entity = (
            {"EntityRef": {"value": entity_id}, "Type": entity_type}
            if entity_id and entity_type in ("Customer", "Vendor")
            else None
        )

        if debit > 0:
            detail = {
                "PostingType": "Debit",
                "AccountRef": {"value": account_id},
            }
            if entity:
                detail["Entity"] = entity
            qbo_lines.append({
                "Description": desc,
                "Amount": round(debit, 2),
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": detail,
            })
        elif credit > 0:
            detail = {
                "PostingType": "Credit",
                "AccountRef": {"value": account_id},
            }
            if entity:
                detail["Entity"] = entity
            qbo_lines.append({
                "Description": desc,
                "Amount": round(credit, 2),
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": detail,
            })

    return {
        "TxnDate": date,
        "PrivateNote": memo,
        "Line": qbo_lines,
    }



def create_journal_entry(client: QBOClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes a Journal Entry creation action against QBO.

    Accepts either:
    - Raw QBO payload (if "Line" key is present), OR
    - Simplified format: { date, memo, lines: [{account_id, debit, credit, description}] }
    """
    import logging
    import requests as _req
    logger = logging.getLogger(__name__)

    # The executor's proposal stamp (stamp_journal_entry_payload): appended to the
    # PrivateNote so the reconcile probe can find this post by identity.
    stamp_id = payload.get(PROPOSAL_STAMP_KEY)

    # If caller already built the full QBO payload, pass through directly
    if "Line" in payload:
        if not payload["Line"]:
            raise ValueError("Journal Entry payload must contain at least one 'Line'.")
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-BALANCE (SYNTHESIS
        # GAP-A7): the raw-QBO-payload branch previously skipped the debit==credit assert the
        # simplified branch has always had, so an out-of-balance raw payload reached QBO and
        # came back as a generic Intuit fault. Assert the same 0.005 tolerance here, reading
        # the native QBO line shape (JournalEntryLineDetail.PostingType + Amount).
        # QBO-OK: idempotent-upstream — the POST below is unchanged and stays guarded by the
        # executor's SHA-256 QBOActionExecution ledger (execute_proposed_action wraps this fn).
        raw_debit = 0.0
        raw_credit = 0.0
        for line in payload["Line"]:
            detail = (line or {}).get("JournalEntryLineDetail") or {}  # SA-OK: dict-key access on the in-memory JE line payload, not a DB query
            posting = str(detail.get("PostingType") or "").strip().lower()  # SA-OK: dict-key access, not a DB query
            try:
                amount = float((line or {}).get("Amount") or 0)  # SA-OK: dict-key access, not a DB query
            except (TypeError, ValueError) as exc:
                raise ValueError("Journal Entry line Amount is not numeric.") from exc
            if posting == "debit":
                raw_debit += amount
            elif posting == "credit":
                raw_credit += amount
            else:
                raise ValueError(
                    "Journal Entry line is missing JournalEntryLineDetail.PostingType "
                    "(Debit/Credit) — refusing to post an unverifiable entry."
                )
        if abs(raw_debit - raw_credit) > 0.005:
            raise ValueError(
                f"Journal Entry is out of balance: debits={raw_debit:.2f}, credits={raw_credit:.2f}"
            )
        qbo_payload = {key: value for key, value in payload.items() if key != PROPOSAL_STAMP_KEY}
        if stamp_id is not None:
            qbo_payload["PrivateNote"] = _stamped_note(qbo_payload.get("PrivateNote"), stamp_id)
    else:
        # Build from simplified format
        date = payload.get("date")
        memo = payload.get("memo", "")
        if stamp_id is not None:
            memo = _stamped_note(memo, stamp_id)
        lines = payload.get("lines", [])

        if not date:
            raise ValueError("Journal Entry requires a 'date'.")
        if not lines:
            raise ValueError("Journal Entry requires at least one line.")
        if any(not str((line or {}).get("account_id") or "").strip() for line in lines):
            # Never send AccountRef "" to QuickBooks (the executor's chart check
            # refuses this first; this is the writer's own floor).
            raise QBOActionRefused(
                "je_line_missing_account",
                "A journal entry line has no account. Pick an account for every line.",
            )

        total_debit = sum(float(l.get("debit") or 0) for l in lines)  # noqa: E741
        total_credit = sum(float(l.get("credit") or 0) for l in lines)  # noqa: E741
        if abs(total_debit - total_credit) > 0.005:
            raise ValueError(
                f"Journal Entry is out of balance: debits={total_debit:.2f}, credits={total_credit:.2f}"
            )

        qbo_payload = _build_qbo_je_payload(date, memo, lines)

    # Log structured metadata only — never the payload itself (it carries
    # account refs, vendor refs, memos and amounts).
    line_count = len(qbo_payload.get("Line") or [])
    txn_date = qbo_payload.get("TxnDate")
    logger.info(
        "[JE] Sending journal entry to QBO: realm_id=%s line_count=%s txn_date=%s",
        getattr(client, "realm_id", None),
        line_count,
        txn_date,
    )

    # minorversion=73 is required for correct JE write behaviour in current QBO API.
    # CurrencyRef is intentionally omitted — QBO infers the home currency from
    # the company's settings; specifying it explicitly can cause 6200 errors when
    # the company currency differs from what we send.
    # QBO-OK: balanced-upstream idempotent-upstream — both branches above assert debit==credit
    # (0.005) before reaching this send, and the POST runs only under the executor's SHA-256
    # QBOActionExecution ledger (execute_proposed_action wraps this function).
    url = f"{client.base_url}/journalentry?minorversion=73"
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B1 — INV-UNCERTAIN: a network
    # failure AFTER the POST left the socket means QBO may have applied the entry; a provider
    # 5xx is equally ambiguous. Both raise the typed QBOUncertainOutcomeError so the executor
    # records an UNCERTAIN outcome (action stays APPROVED, ledger row 'uncertain') instead of
    # a confirmed failure — and the retry path reconciles before any re-POST. A 4xx fault
    # stays the confirmed-reject ValueError below.
    try:
        resp = _req.post(url, headers=client._headers(), json=qbo_payload)
    except _req.exceptions.RequestException as exc:
        raise QBOUncertainOutcomeError(_UNCERTAIN_POST_MESSAGE) from exc
    if resp.status_code >= 500:
        fault = parse_qbo_fault(resp)
        logger.error(
            "[JE] QBO returned 5xx on journal entry (outcome ambiguous): realm_id=%s status=%s fault=%s",
            getattr(client, "realm_id", None),
            resp.status_code,
            json.dumps(fault, sort_keys=True, default=str),
        )
        raise QBOUncertainOutcomeError(
            f"QuickBooks returned HTTP {resp.status_code} for the Journal Entry — the write may "
            "or may not have been applied. Do not blindly retry — reconcile first."
        )

    if not resp.ok:
        # Never log or surface the response body — it can echo tenant data
        # (vendor/account names, memos). Log parsed fault metadata only.
        fault = parse_qbo_fault(resp)
        logger.error(
            "[JE] QBO rejected journal entry: realm_id=%s status=%s fault=%s",
            getattr(client, "realm_id", None),
            resp.status_code,
            json.dumps(fault, sort_keys=True, default=str),
        )
        raise ValueError(f"QBO rejected the Journal Entry — {qbo_fault_public_message(fault)}")

    return resp.json()

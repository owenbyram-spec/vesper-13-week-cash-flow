"""Reclassification execution: change the CATEGORY on one transaction line.

QuickBooks keeps a Purchase's category on
``Line[].AccountBasedExpenseLineDetail.AccountRef``. The header ``AccountRef`` is
the bank or credit-card account the money left from. A Deposit has no header
``AccountRef``; its header ``DepositToAccountRef`` is the bank account and the
category is ``Line[].DepositLineDetail.AccountRef``. The read side
(``services/sync.py`` ``_purchase_account_ref`` / ``_deposit_account_ref``)
derives the local ``account_ref`` from those line fields, so this writer changes
the same field and nothing else.

The canonical payload every drafting lane stores::

    {entity, txn_id, txn_date, to_account_id[, line_id]}

``txn_date`` is the local row's date for the executor's closed-period guard; it
is never sent to QuickBooks. ``line_id`` names the line to recategorize. Without
it the transaction must have exactly one account-based line.

At execution the full entity is read (fresh ``SyncToken`` and the current
``Line`` array together), exactly one line's ``AccountRef`` is replaced, and the
COMPLETE ``Line`` array is sent: a sparse update replaces ``Line`` wholesale, so
omitting the other lines would delete them. Re-running the same payload sets the
same value again, so a retry is idempotent.

Refusals (``QBOActionRefused``, raised before any write): an entity other than
Purchase/Deposit, a multi-line transaction without ``line_id``, an item-based or
non-category line, and any ``updates`` field other than Id/SyncToken/sparse.
``updates.AccountRef`` in particular is refused: legacy drafts used it for the
category, but sent as-is it rewrote the payment account (finding F121).

This module is a pure executor arm: it is invoked ONLY by
``executor.execute_proposed_action``, which refuses any action whose status is
not in ``{APPROVED, EXECUTED}`` and claims the SHA-256 idempotency ledger row
before dispatching here. Nothing in the chat loop calls this directly.
"""
from __future__ import annotations

import copy

from ..qbo_client import QBOClient
from .refusals import QBOActionRefused

# Keys a target account id may arrive under, in precedence order (the chat and
# actions-router lanes use to_account_id; account_id is the classification-
# review vocabulary).
_TARGET_ACCOUNT_KEYS = ("to_account_id", "account_id", "target_account_id")

# The line detail that carries the category, per supported entity.
LINE_DETAIL_BY_ENTITY = {
    "Purchase": "AccountBasedExpenseLineDetail",
    "Deposit": "DepositLineDetail",
}
_ENTITY_CANONICAL = {name.lower(): name for name in LINE_DETAIL_BY_ENTITY}

# Fields a stored ``updates`` dict may still carry. The executor builds the
# body itself; anything else (a header account ref, a TxnDate, a memo) would be
# an unreviewed write and is refused.
_ALLOWED_UPDATE_KEYS = frozenset({"Id", "SyncToken", "sparse"})

LINE_REQUIRED_ERROR = "reclassification_line_required"


def _refuse(error_code: str, message: str) -> QBOActionRefused:
    return QBOActionRefused(error_code, message)


def build_reclassification_payload(
    entity: str,
    txn_id: str,
    account_id: str,
    *,
    txn_date: str | None = None,
    line_id: str | None = None,
) -> dict:
    """The canonical RECLASSIFY_TXN payload every drafting lane stores."""
    payload = {
        "entity": entity,
        "txn_id": str(txn_id),
        "txn_date": txn_date,
        "to_account_id": str(account_id),
    }
    if line_id:
        payload["line_id"] = str(line_id)
    return payload


def normalize_reclassification_payload(payload: dict) -> tuple[str, str, str, str | None]:
    """Return ``(entity, txn_id, target_account_id, line_id)`` or refuse.

    Raises ``ValueError`` (``QBOActionRefused``) for a payload that names no
    entity/transaction or target account, names an unsupported entity, or
    carries a header-level / unsupported ``updates`` field.
    """
    payload = payload or {}
    entity = payload.get("entity") or payload.get("entity_type")
    txn_id = payload.get("txn_id") or payload.get("transaction_id")
    if not entity or not txn_id:
        raise _refuse("reclassification_invalid", "Reclassification payload requires 'entity' and 'txn_id'.")
    canonical = _ENTITY_CANONICAL.get(str(entity).strip().lower())
    if canonical is None:
        raise _refuse(
            "reclassification_unsupported_entity",
            f"Reclassification supports Purchase and Deposit transactions, not {entity}.",
        )

    updates = payload.get("updates") or {}
    if not isinstance(updates, dict):
        raise _refuse("reclassification_invalid", "Reclassification 'updates' must be an object.")
    if "AccountRef" in updates or "DepositToAccountRef" in updates:
        raise _refuse(
            "reclassification_header_account_refused",
            "This reclassification draft targets the transaction's payment account, not its "
            "category. Re-draft it so the category line is changed.",
        )
    unsupported = sorted(set(updates) - _ALLOWED_UPDATE_KEYS)
    if unsupported:
        raise _refuse(
            "reclassification_invalid",
            "Reclassification can only change a line's category; unsupported update "
            f"field(s): {', '.join(unsupported)}.",
        )

    target = next((payload[key] for key in _TARGET_ACCOUNT_KEYS if payload.get(key)), None)
    if target is None:
        raise _refuse(
            "reclassification_invalid",
            "Reclassification payload requires a target account id ('to_account_id').",
        )
    line_id = payload.get("line_id")
    return canonical, str(txn_id), str(target), (str(line_id) if line_id not in (None, "") else None)


def entity_body(entity: str, response: dict) -> dict | None:
    """The entity dict out of a single-entity GET (QBO nests it under the name)."""
    for key in (entity, entity.capitalize()):
        body = (response or {}).get(key)
        if isinstance(body, dict):
            return body
    return None


def select_category_line(entity: str, txn_id: str, body: dict, line_id: str | None) -> int:
    """Index of the line whose category changes, or refuse.

    Without ``line_id`` the transaction must have exactly one line. The chosen
    line must carry the entity's category detail and no item detail.
    """
    lines = body.get("Line")
    if not isinstance(lines, list) or not lines:
        raise _refuse(LINE_REQUIRED_ERROR, f"{entity} {txn_id} has no lines to recategorize.")
    if line_id is None:
        if len(lines) != 1:
            raise _refuse(
                LINE_REQUIRED_ERROR,
                f"{entity} {txn_id} has {len(lines)} lines. Choose the line to recategorize "
                "(line_id); one category cannot be applied to the whole transaction.",
            )
        index = 0
    else:
        matches = [i for i, line in enumerate(lines) if str((line or {}).get("Id")) == line_id]
        if len(matches) != 1:
            raise _refuse(LINE_REQUIRED_ERROR, f"{entity} {txn_id} has no line {line_id}.")
        index = matches[0]

    detail_key = LINE_DETAIL_BY_ENTITY[entity]
    line = lines[index] or {}
    detail_type = line.get("DetailType")
    if (
        "ItemBasedExpenseLineDetail" in line
        or not isinstance(line.get(detail_key), dict)
        or (detail_type is not None and detail_type != detail_key)
        # A line linked to another transaction (a deposited customer payment) takes
        # its account from that transaction; re-pointing it would break the link.
        or line.get("LinkedTxn")
    ):
        raise _refuse(
            LINE_REQUIRED_ERROR,
            f"The selected line of {entity} {txn_id} is not an account-category line "
            "(item-based or linked lines are categorized by their item or source transaction).",
        )
    return index


def build_line_update(entity: str, txn_id: str, body: dict, target_account_id: str, line_id: str | None) -> dict:
    """The sparse update body: the full ``Line`` array with one category changed."""
    index = select_category_line(entity, txn_id, body, line_id)
    sync_token = body.get("SyncToken")
    if sync_token is None:
        raise ValueError(
            f"Could not resolve a current SyncToken for {entity} {txn_id}; "
            "QuickBooks returned no SyncToken for the transaction."
        )
    lines = copy.deepcopy(body["Line"])
    detail = lines[index][LINE_DETAIL_BY_ENTITY[entity]]
    detail["AccountRef"] = {"value": str(target_account_id)}
    return {"Id": str(txn_id), "SyncToken": str(sync_token), "sparse": True, "Line": lines}


# WRITEBACK-OK: executor-arm, not a chat-loop write — the ONLY caller is
# executor.execute_proposed_action, which refuses status not in
# {APPROVED, EXECUTED} and claims the SHA-256 idempotency ledger row
# (_claim_execution) before dispatching here (AWB-02/AWB-07 gates upstream);
# same contract as the sibling create_journal_entry/update_vendor arms.
def reclassify_transaction(client: QBOClient, payload: dict) -> dict:
    """Recategorize one line of a Purchase or Deposit via a sparse update."""
    entity, txn_id, target, line_id = normalize_reclassification_payload(payload)
    current = client.get_transaction(entity, txn_id)
    body = entity_body(entity, current)
    if body is None:
        raise ValueError(f"QuickBooks returned no {entity} body for transaction {txn_id}.")
    update = build_line_update(entity, txn_id, body, target, line_id)
    return client.update_transaction(entity, txn_id, update)  # WRITEBACK-OK: approval-gated executor arm — reached only via execute_proposed_action's APPROVED/EXECUTED refusal + idempotency claim

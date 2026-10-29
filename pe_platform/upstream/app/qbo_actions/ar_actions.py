# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections chunk C1 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
"""AR collections executor arms — CREATE_INVOICE / SEND_INVOICE_REMINDER /
RECORD_CUSTOMER_PAYMENT / CREATE_CREDIT_MEMO (Wave 2.1 C1, decisions 3/5/6/7/8/9/10).

Pure executor arms: every function here is invoked ONLY by
``executor.execute_proposed_action``, which refuses any action whose status is
not in ``{APPROVED, EXECUTED}`` and claims the SHA-256 idempotency ledger row
before dispatching (AWB-02/AWB-07 gates upstream). Nothing in the chat loop
calls this module directly.

The module also owns the Wave-2.1 policy constants:

- ``CLOSED_PERIOD_DATE_KEYS`` + ``CLOSED_PERIOD_EXEMPT_ACTION_TYPES`` (decision 3):
  the executor's closed-period guard resolves each GL-dated type's target period
  from its own payload date key. WITHOUT the map the three AR creates would be
  entirely UNGUARDED — ``period_is_closed`` is fail-OPEN for a dateless type
  (``task_autonomy.py:266-276``: no period resolvable → OPEN). SEND_INVOICE_REMINDER
  is EXEMPT by documented design: delivering an email changes no ledger balance.
- ``EXTERNAL_SEND_ACTION_TYPES`` (decision 10): the action-type-level record that
  SEND_INVOICE_REMINDER has an OUTWARD side effect at approval time. The chat
  tools keep ``external_side_effect=False`` (E1 — that flag is a live policy
  kill-switch and the handler's only invocation-time effect is drafting a
  PENDING row); this frozenset is the truthful executor-side policy lever for
  later waves.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import _env_int
from ..models import ProposedAction, QBOActionExecution
from ..qbo_client import QBOClient
from ..qbo_errors import QBOUncertainOutcomeError
from .probe_paging import has_proposal_stamp, probe_or_uncertain, query_all
from .qbo_query import qbo_escape_str

logger = logging.getLogger(__name__)

AR_EXECUTABLE_ACTION_TYPES = frozenset(
    {"CREATE_INVOICE", "SEND_INVOICE_REMINDER", "RECORD_CUSTOMER_PAYMENT", "CREATE_CREDIT_MEMO"}
)

# Decision 10 (E1): the truthful record that this action TYPE causes an external
# (customer-facing) side effect at approval time. The paired chat tool keeps
# external_side_effect=False because tool_policy.py treats that flag as an
# unconditional kill-switch and the tool's only invocation-time effect is a
# PENDING draft. Policy for later waves keys off THIS set, not the tool flag.
EXTERNAL_SEND_ACTION_TYPES = frozenset({"SEND_INVOICE_REMINDER"})

# Decision 3: per-type payload date key for the executor's closed-period guard.
# POST_JE/JOURNAL_ENTRY keep their historical 'date' key (behavior byte-equivalent);
# the three AR GL types carry the QBO-native 'TxnDate'.
CLOSED_PERIOD_DATE_KEYS = {
    "POST_JE": "date",
    "JOURNAL_ENTRY": "date",
    "CREATE_INVOICE": "TxnDate",
    "RECORD_CUSTOMER_PAYMENT": "TxnDate",
    "CREATE_CREDIT_MEMO": "TxnDate",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk C (marker
    # RATIFICATION-2026-07-13.md) — decision 12: the AP BILL_PAY entry lives in this
    # AR-named module (naming smell tolerated + noted; this map is the executor's
    # single closed-period date-key source, so splitting it would fork the guard).
    "BILL_PAY": "TxnDate",
    # Plan 20260716-batch5-remediation AC-C1: a reclassification moves the closed
    # month's reported P&L/BS exactly as a JE does — it rides the same executor
    # funnel and takes the same SOFT floor. The key is a TOP-LEVEL payload
    # `txn_date`, stamped at BOTH draft sites (classification-review approve core
    # + the categorization sweep) from the LOCAL row's date — deliberately NOT
    # inside `updates`, which is the sparse QBO body (a TxnDate there would
    # rewrite the transaction's date in QuickBooks). Legacy dateless payloads
    # keep the documented current-period/dateless fallback below.
    "RECLASSIFY_TXN": "txn_date",
}

# Decision 3: SEND_INVOICE_REMINDER is exempt from the closed-period guard by
# DOCUMENTED DESIGN — sending QBO's own reminder email for an existing invoice
# posts nothing to the General Ledger, so a locked period is irrelevant to it.
CLOSED_PERIOD_EXEMPT_ACTION_TYPES = frozenset({"SEND_INVOICE_REMINDER"})

AR_REMINDER_MIN_INTERVAL_DAYS_DEFAULT = 3

_PROBE_ENTITY_BY_TYPE = {
    "CREATE_INVOICE": "Invoice",
    "RECORD_CUSTOMER_PAYMENT": "Payment",
    "CREATE_CREDIT_MEMO": "CreditMemo",
}


def closed_period_context(action_type: str, payload: dict | None) -> tuple[bool, datetime | None]:
    """Resolve the closed-period guard inputs for ``action_type`` (decision 3).

    Returns ``(exempt, when)``: ``exempt`` is True only for the documented
    no-GL-effect types (the guard is skipped entirely); ``when`` is the parsed
    target-period datetime for a GL-dated type, or None (which makes
    ``period_is_closed`` check the current-period/dateless path — unchanged
    behavior for RECONCILE/UPDATE_VENDOR, for legacy RECLASSIFY_TXN payloads
    drafted before the top-level ``txn_date`` stamp existed, and for a
    malformed or absent date, which QBO rejects downstream anyway).
    """
    if action_type in CLOSED_PERIOD_EXEMPT_ACTION_TYPES:
        return True, None
    when = None
    date_key = CLOSED_PERIOD_DATE_KEYS.get(action_type)
    if date_key:
        raw_date = (payload or {}).get(date_key)
        if raw_date:
            try:
                when = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                when = None  # malformed date — QBO rejects it downstream; check current period
    return False, when


def _customer_ref_value(payload: dict) -> str:
    ref = (payload or {}).get("CustomerRef")
    if isinstance(ref, dict):
        return str(ref.get("value") or "").strip()
    return str(ref or "").strip()


def _sales_lines(lines: "list | None") -> list:
    """Build QBO SalesItemLineDetail lines from the decision-9 payload lines.

    Every line must already carry ``item_id`` — ``item_name`` resolution happens
    at DRAFT time (the C2 handler refuses unresolvable names with candidates); an
    unresolved line reaching execution is REFUSED honestly, never guessed.
    """
    if not lines:
        raise ValueError("Payload requires at least one line in 'lines'.")
    built = []
    for line in lines:  # SA-OK: bounded in-memory payload lines, not a DB query
        line = line or {}
        item_id = str(line.get("item_id") or "").strip()
        if not item_id:
            raise ValueError(
                "Line is missing 'item_id' — item_name resolution happens at draft time; "
                "an unresolved line is refused, never guessed."
            )
        try:
            amount = round(float(line.get("amount")), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError("Line 'amount' is not numeric.") from exc
        detail: dict = {"ItemRef": {"value": item_id}}
        if line.get("qty") is not None:
            detail["Qty"] = float(line["qty"])
        if line.get("unit_price") is not None:
            detail["UnitPrice"] = float(line["unit_price"])
        entry: dict = {"DetailType": "SalesItemLineDetail", "Amount": amount, "SalesItemLineDetail": detail}
        if line.get("description"):
            entry["Description"] = str(line["description"])
        built.append(entry)
    return built


def _sales_body(payload: dict, *, entity_label: str, include_due_date: bool) -> dict:
    """Shared Invoice/CreditMemo body per decision 9: CustomerRef + TxnDate +
    lines (+ optional DueDate on invoices, + PrivateNote). ``TxnTaxDetail`` is
    deliberately OMITTED — tax follows the QBO company defaults; v1 excludes tax
    overrides, discounts, custom fields, and terms beyond an explicit DueDate."""
    payload = payload or {}
    customer = _customer_ref_value(payload)
    if not customer:
        raise ValueError(f"{entity_label} payload requires 'CustomerRef'.")
    txn_date = str(payload.get("TxnDate") or "").strip()
    if not txn_date:
        raise ValueError(f"{entity_label} payload requires 'TxnDate'.")
    body: dict = {
        "CustomerRef": {"value": customer},
        "TxnDate": txn_date[:10],
        "Line": _sales_lines(payload.get("lines")),
    }
    if include_due_date and payload.get("DueDate"):
        body["DueDate"] = str(payload["DueDate"])[:10]
    if payload.get("PrivateNote"):
        body["PrivateNote"] = str(payload["PrivateNote"])
    return body


def _expected_total(action_type: str, payload: dict) -> float:
    if action_type == "RECORD_CUSTOMER_PAYMENT":
        return round(float((payload or {}).get("TotalAmt") or 0), 2)
    total = 0.0
    for line in (payload or {}).get("lines") or []:  # SA-OK: bounded in-memory payload lines, not a DB query
        total += float((line or {}).get("amount") or 0)
    return round(total, 2)


def create_invoice(client: QBOClient, payload: dict) -> dict:
    """CREATE_INVOICE arm (decision 9)."""
    body = _sales_body(payload, entity_label="Invoice", include_due_date=True)
    return client.create_invoice(body)  # WRITEBACK-OK: approval-gated executor arm — reached only via execute_proposed_action's APPROVED/EXECUTED refusal + SHA-256 idempotency claim


def create_credit_memo(client: QBOClient, payload: dict) -> dict:
    """CREATE_CREDIT_MEMO arm (decision 9 shape, no DueDate)."""
    body = _sales_body(payload, entity_label="CreditMemo", include_due_date=False)
    return client.create_credit_memo(body)  # WRITEBACK-OK: approval-gated executor arm — reached only via execute_proposed_action's APPROVED/EXECUTED refusal + SHA-256 idempotency claim


def record_customer_payment(client: QBOClient, payload: dict) -> dict:
    """RECORD_CUSTOMER_PAYMENT arm (decision 8): CustomerRef + TotalAmt +
    Line[].LinkedTxn against the invoice; DepositToAccountRef OMITTED (QBO
    defaults to Undeposited Funds) unless the payload explicitly carries one.

    Partial payments are supported; the open balance is read at EXECUTION time
    (a draft-time balance would be stale by approval time) and an OVERPAYMENT is
    REFUSED with an honest error — never clipped, never guessed.
    """
    payload = payload or {}
    invoice_id = str(payload.get("invoice_id") or "").strip()
    if not invoice_id:
        raise ValueError("Payment payload requires 'invoice_id'.")
    customer = _customer_ref_value(payload)
    if not customer:
        raise ValueError("Payment payload requires 'CustomerRef'.")
    txn_date = str(payload.get("TxnDate") or "").strip()
    if not txn_date:
        raise ValueError("Payment payload requires 'TxnDate'.")
    try:
        amount = round(float(payload.get("TotalAmt")), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("Payment 'TotalAmt' is not numeric.") from exc
    if amount <= 0:
        raise ValueError("Payment 'TotalAmt' must be positive.")

    current = client.get_transaction("invoice", invoice_id)
    entity = (current or {}).get("Invoice") or {}
    open_balance = round(float(entity.get("Balance") or 0), 2)
    if amount > open_balance + 0.005:
        raise ValueError(
            f"Payment of {amount:.2f} exceeds invoice {invoice_id}'s open balance of "
            f"{open_balance:.2f} — refusing to record an overpayment. Record a partial "
            "payment up to the open balance, or draft a credit memo instead."
        )

    body: dict = {
        "CustomerRef": {"value": customer},
        "TotalAmt": amount,
        "TxnDate": txn_date[:10],
        "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]}],
    }
    if payload.get("PrivateNote"):
        body["PrivateNote"] = str(payload["PrivateNote"])
    deposit_ref = payload.get("DepositToAccountRef")
    if isinstance(deposit_ref, dict) and deposit_ref.get("value"):
        body["DepositToAccountRef"] = {"value": str(deposit_ref["value"])}
    return client.create_payment(body)  # WRITEBACK-OK: approval-gated executor arm — reached only via execute_proposed_action's APPROVED/EXECUTED refusal + SHA-256 idempotency claim


def _reminder_min_interval_days() -> int:
    # Call-time read (decision 7): the env knob is honored per execution, not
    # frozen at import; malformed values degrade to the default via _env_int.
    return _env_int("AR_REMINDER_MIN_INTERVAL_DAYS", AR_REMINDER_MIN_INTERVAL_DAYS_DEFAULT)


def send_invoice_reminder(client: QBOClient, *, db: Session, company_id: str, payload: dict) -> dict:
    """SEND_INVOICE_REMINDER arm (decisions 1/2/7): QBO-native send, recipient
    locked (``sendTo`` is NEVER passed — QBO delivers to the invoice's own
    BillEmail), throttled per invoice.

    Throttle (decision 7): refuse when a ledger row exists for this company +
    action type with status in ('succeeded','uncertain') finishing inside the
    AR_REMINDER_MIN_INTERVAL_DAYS window whose request summary names the same
    invoice. ``uncertain`` COUNTS — that email may have gone out. The indexed
    columns are SQL-filtered; the invoice-id match runs in Python over the
    bounded recent window (no JSON-operator dialect divergence).
    """
    payload = payload or {}
    invoice_id = str(payload.get("invoice_id") or "").strip()
    if not invoice_id:
        raise ValueError("Reminder payload requires 'invoice_id'.")

    days = _reminder_min_interval_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = (
        db.query(QBOActionExecution)
        .filter(
            QBOActionExecution.company_id == company_id,
            QBOActionExecution.action_type == "SEND_INVOICE_REMINDER",
            QBOActionExecution.status.in_(("succeeded", "uncertain")),
            QBOActionExecution.finished_at >= cutoff,
        )
        .all()
    )
    for row in recent:  # SA-OK: bounded-loop — Python invoice-id match over the already-fetched recent window, not a DB query
        summary = row.request_summary_json if isinstance(row.request_summary_json, dict) else {}
        if str(summary.get("invoice_id") or "").strip() == invoice_id:
            raise ValueError(
                f"A reminder for invoice {invoice_id} was already sent (or possibly sent — "
                f"execution {row.id} is '{row.status}') within the last {days} days. "
                "Refusing to send another; adjust AR_REMINDER_MIN_INTERVAL_DAYS or wait "
                "out the window if a repeat chase is intended."
            )

    # Decision 2 recipient lock: send_to is NEVER passed here — no model-suppliable
    # recipient exists anywhere on this lane (prompt-injection exfiltration guard).
    return client.send_invoice(invoice_id)  # WRITEBACK-OK: approval-gated executor arm — reached only via execute_proposed_action's APPROVED/EXECUTED refusal + SHA-256 idempotency claim; recipient locked per decision 2


def _entity_amount(entity_name: str, entity: dict) -> float:
    """The amount a landed entity is compared on.

    A Payment's ``TotalAmt`` is the payment itself. An Invoice / CreditMemo's
    ``TotalAmt`` includes the sales tax QBO computes (``TxnTaxDetail`` is never
    sent), so it is compared on its SUBTOTAL: the sum of the sales lines, which is
    what the payload's lines add up to (tax and the SubTotal line excluded).
    """
    if entity_name == "Payment":
        return round(float(entity.get("TotalAmt") or 0), 2)
    total = 0.0
    for line in entity.get("Line") or []:
        if (line or {}).get("DetailType") == "SalesItemLineDetail":
            total += float((line or {}).get("Amount") or 0)
    return round(total, 2)


# QBO-OK: idempotent-upstream — this probe is READ-ONLY (client.query); it never
# posts. It protects the executor's idempotency ledger from a double-create.
def find_created_ar_entity(
    client: QBOClient, entity_name: str, payload: dict, proposal_id: int | None = None
) -> dict | None:
    """Deterministic reconciliation probe for an UNCERTAIN prior create (decision 5).

    Reads every ``entity_name`` row on the payload's TxnDate + CustomerRef (all
    pages). When the payload's PrivateNote carries this proposal's stamp
    ("Peregrine proposal #<id>", added at draft time), the stamp is the identity:
    exactly one stamped row with the same amount → adopt; a stamped row with a
    different amount, or several stamped rows → raise for a human; none → None.
    Otherwise the row must match the PrivateNote exactly. The amount is the
    pre-tax subtotal for invoices / credit memos (``_entity_amount``). Follows the
    JE probe's trichotomy: guessing either way risks a double-post or a false
    success.
    """
    payload = payload or {}
    txn_date = str(payload.get("TxnDate") or "").strip()[:10]
    customer = _customer_ref_value(payload)
    if not txn_date or not customer:
        return None
    memo = str(payload.get("PrivateNote") or "").strip()
    expected_total = _expected_total(
        "RECORD_CUSTOMER_PAYMENT" if entity_name == "Payment" else "CREATE_INVOICE", payload
    )

    candidates = query_all(
        client,
        entity_name,
        f"TxnDate = '{qbo_escape_str(txn_date)}' AND CustomerRef = '{qbo_escape_str(customer)}'",
    )
    if proposal_id is not None and has_proposal_stamp(memo, proposal_id):
        stamped = [e for e in candidates if has_proposal_stamp(e.get("PrivateNote"), proposal_id)]
        if not stamped:
            return None
        if len(stamped) > 1 or abs(_entity_amount(entity_name, stamped[0]) - expected_total) > 0.005:
            raise QBOUncertainOutcomeError(
                f"Reconciliation is ambiguous: {len(stamped)} {entity_name} row(s) on {txn_date} carry "
                "this proposal's stamp but do not match its amount exactly. A human must "
                "reconcile before this action is retried."
            )
        return {entity_name: stamped[0], "reconciled_from_uncertain": True}

    matches = [
        entity
        for entity in candidates
        if abs(_entity_amount(entity_name, entity) - expected_total) <= 0.005
        and (not memo or str(entity.get("PrivateNote") or "").strip() == memo)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise QBOUncertainOutcomeError(
            f"Reconciliation is ambiguous: {len(matches)} {entity_name} rows on {txn_date} match "
            "the uncertain create's customer/amount/memo. A human must reconcile before this "
            "action is retried."
        )
    return {entity_name: matches[0], "reconciled_from_uncertain": True}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def find_sent_invoice_reminder(client: QBOClient, payload: dict, attempted_at: datetime | None) -> dict:
    """Time-windowed send reconcile (decision 6): adopt or STAY UNCERTAIN — never re-send.

    Reads the invoice and compares ``DeliveryInfo.DeliveryTime`` against the
    uncertain attempt's window: ``DeliveryTime >= attempted_at`` → that attempt's
    send landed (adopt success). Older or absent → the outcome is UNKNOWN (a bare
    EmailStatus cannot distinguish this attempt from last month's chase), so this
    RAISES the typed uncertain error — the ledger row stays ``uncertain`` for a
    human and the invoice is NEVER re-sent automatically.
    """
    invoice_id = str((payload or {}).get("invoice_id") or "").strip()
    attempted_at = _as_utc(attempted_at)
    delivered_at = None
    if invoice_id:
        current = client.get_transaction("invoice", invoice_id)
        entity = (current or {}).get("Invoice") or {}
        raw_time = ((entity.get("DeliveryInfo") or {}).get("DeliveryTime") or "").strip()
        if raw_time:
            try:
                delivered_at = _as_utc(datetime.fromisoformat(raw_time.replace("Z", "+00:00")))
            except ValueError:
                delivered_at = None
        if attempted_at is not None and delivered_at is not None and delivered_at >= attempted_at:
            return {"Invoice": entity, "reconciled_from_uncertain": True}
    raise QBOUncertainOutcomeError(
        f"The reminder send for invoice {invoice_id or '(unknown)'} is still unresolved: "
        "QuickBooks shows no delivery inside this attempt's window. Refusing to re-send — "
        "a human must verify whether the customer received the email."
    )


def execute_ar_action(
    *,
    client: QBOClient,
    db: Session,
    action: ProposedAction,
    action_type: str,
    prior_attempt_uncertain: bool,
    prior_started_at: datetime | None,
) -> dict:
    """Dispatch one AR action type — probe-first on an uncertain prior attempt.

    Called ONLY from ``executor.execute_proposed_action`` after the status gate,
    closed-period guard, and idempotency claim. ``prior_started_at`` is when the
    first attempt under this idempotency key began (the ledger row's creation),
    so the send probe's time window (decision 6) covers every earlier attempt. A
    probe that cannot read QuickBooks leaves the outcome uncertain
    (``probe_or_uncertain``); it never counts as a failure of the write.
    """
    payload = action.payload or {}
    if action_type == "SEND_INVOICE_REMINDER":
        if prior_attempt_uncertain:
            # Adopts (returns) or raises QBOUncertainOutcomeError — control NEVER
            # falls through to a re-send after an uncertain prior attempt.
            return probe_or_uncertain(find_sent_invoice_reminder, client, payload, prior_started_at)
        return send_invoice_reminder(client, db=db, company_id=action.company_id, payload=payload)

    entity_name = _PROBE_ENTITY_BY_TYPE[action_type]
    if prior_attempt_uncertain:
        reconciled = probe_or_uncertain(find_created_ar_entity, client, entity_name, payload, action.id)
        if reconciled is not None:
            logger.info(
                "[executor] action %s reconciled %s from uncertain prior post — no re-POST",
                action.id,
                entity_name,
            )
            return reconciled
    if action_type == "CREATE_INVOICE":
        return create_invoice(client, payload)
    if action_type == "RECORD_CUSTOMER_PAYMENT":
        return record_customer_payment(client, payload)
    return create_credit_memo(client, payload)

"""
QBO Webhooks Router
===================
Receives real-time change notifications from Intuit QuickBooks Online.

Intuit signs each request with HMAC-SHA256 using the verifier token from the
developer portal. We validate before processing any payload.

Environment variables required:
  QBO_WEBHOOK_VERIFIER_TOKEN  — verifier token from QBO Developer Portal → Webhooks

Webhook URL to register in Intuit Dev Portal (Production settings):
  ${API_BASE_URL}/webhooks/qbo

Supported entity handlers:
  BankTransaction  Create/Update → bank import → BankFeedItem (pending, for review)
  Purchase         Create/Update → debit from bank → BankFeedItem (approved if GL-categorised)
  Deposit          Create/Update → credit to bank  → BankFeedItem (approved if GL-categorised)
  BankTransaction/Purchase/Deposit  Delete/Void → retire the BankFeedItem
  Purchase/Deposit/BillPayment/Transfer/JournalEntry/Payment
                   Update/Void → re-read the mirrored bank-recon book rows
                   Delete      → retire the bank-recon book rows
  Vendor/Customer  Create/Update/Delete → re-read the cache row (dropped if gone)
                   Merge → also drop the merged-away duplicate (deletedId)
  Invoice/Bill     Create/Update → upsert the search index record

Every QBO read goes through get_qbo_client_from_db (token refresh + 401 self-heal).
A failed read raises, so the durable event is marked failed and retried by the
webhook recovery sweep instead of being recorded as processed.

Payload formats:
  CloudEvents (current; Intuit's migration deadline was July 31 2026): a JSON
  array of events {"type": "qbo.<entity>.<event>.v1", "intuitaccountid",
  "intuitentityid", "time", "data"}; the legacy {"eventNotifications": [...]}
  object is still accepted. Both feed the same claim/dedupe/routing pipeline.

Reference: https://developer.intuit.com/app/developer/qbo/docs/develop/webhooks
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, List, Optional

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Header

from ..rate_limit import limiter
from sqlalchemy import and_ as sa_and_, or_ as sa_or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import (
    BankFeedItem,
    DebugIncident,
    DebugNotificationDelivery,
    DebugNotificationSubscription,
    DebugTriageRun,
    QBOWebhookEvent,
    SentryWebhookEvent,
    User,
)
from ..config import is_dev_environment, settings
from ..qbo_client import QBOClient, get_qbo_client_from_db
from ..qbo_errors import QBO_NOT_CONNECTED_CODE
from ..services.cache import clear_company_cache
from ..services.email_service import send_debug_incident_notification
from ..services.redaction import redact_sensitive, redact_to_string
from ..services.tenant_companies import organization_for_company
from ..tenant_rls import tenant_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# ── QBO webhook stale-processing recovery (mirrors app/services/stripe.py) ───
# An event stuck in status="processing" longer than this is considered stale
# and may be reclaimed by another worker or the periodic sweep task.
QBO_WEBHOOK_PROCESSING_STALE_AFTER = timedelta(minutes=15)
# A stale event is reclaimed at most this many times before being marked failed.
QBO_WEBHOOK_MAX_RETRIES = 3
# A failed attempt (a QBO auth/transport failure raised by a handler) is retried by
# the recovery sweep once this long has passed, until QBO_WEBHOOK_MAX_RETRIES.
QBO_WEBHOOK_FAILED_RETRY_AFTER = timedelta(minutes=5)
_QBO_WEBHOOK_RETRY_EXHAUSTED_ERROR = "Processing retries exhausted after repeated stale claims [redacted]"

# ── Entities we route to the bank feed handler ────────────────────────────────
BANK_FEED_ENTITIES = {"BankTransaction", "Purchase", "Deposit"}

# ── Entities we index into the QBOSearchRecord FTS index on Create/Update so the
#    assistant's search_finance_records can find them immediately (an Invoice/Bill
#    event matched no prior branch — it was logged and dropped, and the index was
#    only ever refreshed inside the full sync_all_data pass, which has no beat
#    entry). The periodic reconcile (app/tasks/search_reconcile.py) is the backstop.
SEARCH_INDEX_ENTITIES = {"Invoice", "Bill"}

# Account names QBO assigns to uncategorised bank imports
_UNCATEGORIZED = {"uncategorized expense", "uncategorized income", "uncategorized asset"}


# ─────────────────────────────────────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────────────────────────────────────

def _verify_signature(payload_bytes: bytes, signature_header: str, verifier_token: str) -> bool:
    """
    Validates the intuit-signature header.
    Intuit sends: base64(HMAC-SHA256(verifier_token, raw_body))
    """
    expected = base64.b64encode(
        hmac.new(
            verifier_token.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature_header)


# The shared config helper decides: only development/dev/test skip signatures.
# "local" is production here exactly as it is for secret validation (S5,
# 2026-07-10 audit: an env name must never be dev for signature-skip while
# being prod for secret validation).
def _is_dev_environment() -> bool:
    return is_dev_environment(settings.environment)


def _requires_qbo_webhook_signature() -> bool:
    return not _is_dev_environment()


def _requires_sentry_webhook_signature() -> bool:
    return not _is_dev_environment()


def _verify_sentry_signature(payload_bytes: bytes, signature_header: str, webhook_secret: str) -> bool:
    expected = hmac.new(webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ─────────────────────────────────────────────────────────────────────────────
# Payload normalisation (legacy + CloudEvents)
# ─────────────────────────────────────────────────────────────────────────────

# CloudEvents ``type`` is ``qbo.<entity>.<event>.v<n>`` with the event in the past
# tense; the router keys on the legacy operation names.
_CLOUDEVENT_OPERATIONS = {
    "created": "Create",
    "updated": "Update",
    "deleted": "Delete",
    "merged": "Merge",
    "voided": "Void",
    "emailed": "Emailed",
}
# CloudEvents carry the entity name lowercase; the handlers and QBO's query
# endpoint use the canonical names.
_QBO_ENTITY_NAMES = {
    name.lower(): name
    for name in (
        "Account", "BankTransaction", "Bill", "BillPayment", "Budget", "Class", "CreditMemo",
        "Currency", "Customer", "Department", "Deposit", "Employee", "Estimate", "Invoice", "Item",
        "JournalCode", "JournalEntry", "Payment", "PaymentMethod", "Preferences", "Purchase",
        "PurchaseOrder", "RefundReceipt", "SalesReceipt", "TaxAgency", "Term", "TimeActivity",
        "Transfer", "Vendor", "VendorCredit",
    )
}


def _cloudevent_entity(event: Any) -> tuple[str, dict[str, Any]] | None:
    """One CloudEvent as ``(realm_id, entity)`` where ``entity`` has the legacy shape
    ``{name, id, operation, lastUpdated[, deletedId, eventId]}``; None when the event
    does not name a QBO entity change."""
    if not isinstance(event, dict):
        return None
    realm_id = str(event.get("intuitaccountid") or "").strip()
    entity_id = str(event.get("intuitentityid") or "").strip()
    parts = str(event.get("type") or "").split(".")
    if len(parts) < 4 or parts[0] != "qbo" or not realm_id or not entity_id:
        return None
    entity_key, event_name = parts[-3].lower(), parts[-2].lower()
    entity: dict[str, Any] = {
        "name": _QBO_ENTITY_NAMES.get(entity_key, entity_key[:1].upper() + entity_key[1:]),
        "id": entity_id,
        "operation": _CLOUDEVENT_OPERATIONS.get(event_name, event_name.capitalize()),
        "lastUpdated": str(event.get("time") or ""),
    }
    if event.get("id"):
        entity["eventId"] = str(event["id"])
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    deleted_id = data.get("deletedid") or data.get("deletedId")
    if deleted_id:
        entity["deletedId"] = str(deleted_id)
    return realm_id, entity


def _group_by_realm(pairs: list[tuple[str, dict[str, Any]]]) -> List[dict]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for realm_id, entity in pairs:
        grouped.setdefault(realm_id, []).append(entity)
    return [{"realm_id": realm_id, "entities": entities} for realm_id, entities in grouped.items()]


def _extract_notifications(payload: Any) -> List[dict]:
    """
    Returns a list of {realm_id, entities} dicts from any of Intuit's shapes:
      - CloudEvents: a JSON ARRAY of events (a single event object is accepted too),
        each {"specversion", "id", "type": "qbo.<entity>.<event>.v1", "time",
        "intuitentityid", "intuitaccountid", "data"}. Each becomes a legacy-shaped
        entity so the claim, dedupe and routing below are shared.
      - Legacy: {"eventNotifications": [{"realmId": ..., "dataChangeEvent": {"entities": [...]}}]},
        also accepted wrapped as {"specversion": ..., "data": {"eventNotifications": [...]}}.
    Anything else yields no notifications.
    """
    if isinstance(payload, list):
        pairs = [pair for pair in map(_cloudevent_entity, payload) if pair]
        if len(pairs) != len(payload):
            logger.warning(
                "QBO webhook skipped %s CloudEvent(s) without a qbo entity change", len(payload) - len(pairs)
            )
        return _group_by_realm(pairs)
    if not isinstance(payload, dict):
        return []
    if "intuitaccountid" in payload:
        pair = _cloudevent_entity(payload)
        return _group_by_realm([pair] if pair else [])
    if "specversion" in payload and isinstance(payload.get("data"), dict):
        payload = payload["data"]

    result = []
    for n in payload.get("eventNotifications") or []:
        if not isinstance(n, dict):
            continue
        realm_id = n.get("realmId", "")
        entities = [
            entity for entity in ((n.get("dataChangeEvent") or {}).get("entities") or [])
            if isinstance(entity, dict)
        ]
        if realm_id and entities:
            result.append({"realm_id": realm_id, "entities": entities})
    return result


def _qbo_event_key(realm_id: str, entity: dict[str, Any]) -> str:
    stable_fields = {
        "realm_id": realm_id,
        "name": entity.get("name", ""),
        "id": entity.get("id", ""),
        "operation": entity.get("operation", ""),
        "last_updated": entity.get("lastUpdated") or entity.get("lastUpdatedDate") or "",
    }
    # A CloudEvent carries its own id: two changes to one entity in the same second
    # stay distinct events. Legacy keys are unchanged.
    if entity.get("eventId"):
        stable_fields["event_id"] = entity["eventId"]
    raw = json.dumps(stable_fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _claim_qbo_events(db: Session, notifications: list[dict], *, deadline: float | None = None) -> tuple[list[int], int]:
    event_ids: list[int] = []
    duplicate_count = 0
    def admit():
        if deadline is None:
            return
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise HTTPException(status_code=503, detail="Webhook admission deadline exhausted")
        if db.get_bind().dialect.name == "postgresql":
            from ..accounting_sources.connections import configure_source_database_waits
            configure_source_database_waits(db, remaining_seconds=seconds)
    for notification in notifications:
        admit()
        realm_id = notification["realm_id"]
        for entity in notification["entities"]:
            admit()
            entity_id = str(entity.get("id") or "")
            entity_name = str(entity.get("name") or "")
            operation = str(entity.get("operation") or "")
            if not entity_id or not entity_name:
                continue
            event_key = _qbo_event_key(realm_id, entity)
            existing = db.query(QBOWebhookEvent).filter(QBOWebhookEvent.event_key == event_key).first()
            if existing:
                if existing.status == "failed":
                    existing.status = "pending"
                    existing.error = None
                    existing.payload_json = redact_sensitive(entity)
                    # A fresh Intuit redelivery grants a fresh retry budget; the
                    # stale-reclaim cap only bounds automated recovery loops.
                    existing.retry_count = 0
                    existing.processing_started_at = None
                    db.commit()
                    event_ids.append(existing.id)
                else:
                    duplicate_count += 1
                continue

            row = QBOWebhookEvent(
                event_key=event_key,
                realm_id=realm_id,
                entity_name=entity_name,
                entity_id=entity_id,
                operation=operation,
                status="pending",
                payload_json=redact_sensitive(entity),
            )
            db.add(row)
            try:
                db.commit()
                db.refresh(row)
                event_ids.append(row.id)
            except IntegrityError:
                db.rollback()
                duplicate_count += 1
    return event_ids, duplicate_count


# ─────────────────────────────────────────────────────────────────────────────
# Main webhook endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/qbo", status_code=200)  # public-route: Intuit webhook, authed by intuit-signature HMAC over the raw body (no user session)
@limiter.limit("60/minute")
async def receive_qbo_webhook(  # OBS-OK: request: Request is the first param on the line below (multi-line signature)
    request: Request,
    background_tasks: BackgroundTasks,
    intuit_signature: Optional[str] = Header(None, alias="intuit-signature"),
    db: Session = Depends(get_db),
):
    """
    Receives QBO real-time webhook notifications.

    Admit bounded durable source deliveries before ACK within the three-second
    provider window. Financial API lookups run outside the acknowledgement path.
    """
    verifier_token = settings.qbo_webhook_verifier_token
    source_deadline = time.monotonic() + 2.5 if settings.accounting_source_feeds_enabled else None

    # Read raw body BEFORE JSON parsing (needed for HMAC)
    if source_deadline is None:
        raw_body = await request.body()
    else:
        chunks, byte_count = [], 0
        async for chunk in request.stream():
            byte_count += len(chunk)
            if byte_count > 1024 * 1024:
                raise HTTPException(status_code=413, detail="Source webhook body exceeds its bound")
            if time.monotonic() >= source_deadline:
                raise HTTPException(status_code=503, detail="Source webhook body deadline exhausted")
            chunks.append(chunk)
        raw_body = b"".join(chunks)

    # Signature verification is fail-closed outside local development/test.
    if not verifier_token and _requires_qbo_webhook_signature():
        logger.error("QBO webhook verifier token is not configured; rejecting webhook")
        raise HTTPException(status_code=503, detail="Webhook verifier is not configured")
    if verifier_token or _requires_qbo_webhook_signature():
        if not intuit_signature:
            logger.warning("QBO webhook received without intuit-signature header")
            raise HTTPException(status_code=401, detail="Missing signature header")
        if not verifier_token or not _verify_signature(raw_body, intuit_signature, verifier_token):
            logger.warning("QBO webhook signature mismatch; rejected")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload: Any = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        notifications = _extract_notifications(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unsupported bounded webhook contract") from None
    source_delivery = _persist_review_qbo_delivery(db, raw_body, intuit_signature, verifier_token, deadline=source_deadline)
    logger.info("QBO webhook accepted notification_groups=%s", len(notifications))

    # Persist and dedupe before enqueueing work. qbo_webhook_events is realm-keyed under
    # FORCE RLS, so each notification's claim INSERT/SELECT runs pinned to its own realm
    # scope — without the pin the claim fails closed (no row) and the event is silently lost.
    event_ids: list[int] = []
    duplicate_count = 0
    for notification in notifications:
        with tenant_scope(realm_id=notification["realm_id"]):
            ids, dupes = _claim_qbo_events(db, [notification], deadline=source_deadline)
        event_ids.extend(ids)
        duplicate_count += dupes
    if source_deadline is not None and time.monotonic() >= source_deadline:
        raise HTTPException(status_code=503, detail="Source webhook acknowledgement deadline exhausted")
    for event_id in event_ids:
        background_tasks.add_task(_process_qbo_webhook_event, event_id)

    return {"status": "ok", "queued": len(event_ids), "duplicates": duplicate_count,
            **({"source_delivery": source_delivery} if source_delivery is not None else {})}


def _extract_sentry_event_id(payload: dict[str, Any], raw_body: bytes, resource: str | None) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event = data.get("event") if isinstance(data.get("event"), dict) else payload.get("event")
    issue = data.get("issue") if isinstance(data.get("issue"), dict) else payload.get("issue")
    for candidate in (
        payload.get("id"),
        payload.get("event_id"),
        event.get("event_id") if isinstance(event, dict) else None,
        event.get("id") if isinstance(event, dict) else None,
        issue.get("id") if isinstance(issue, dict) else None,
    ):
        if candidate:
            return f"{resource or 'sentry'}:{candidate}"
    return f"{resource or 'sentry'}:{hashlib.sha256(raw_body).hexdigest()}"


def _claim_sentry_event(
    db: Session,
    *,
    event_id: str,
    resource: str | None,
    action: str | None,
    payload: dict[str, Any],
) -> tuple[SentryWebhookEvent, bool]:
    existing = db.query(SentryWebhookEvent).filter(SentryWebhookEvent.event_id == event_id).first()
    if existing:
        return existing, True

    row = SentryWebhookEvent(
        event_id=event_id,
        resource=resource,
        action=action,
        status="processing",
        payload_json=redact_sensitive(payload),
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row, False
    except IntegrityError:
        db.rollback()
        return db.query(SentryWebhookEvent).filter(SentryWebhookEvent.event_id == event_id).first(), True


def _upsert_debug_incident(db: Session, *, event_id: str, payload: dict[str, Any]) -> DebugIncident:
    sanitized = redact_sensitive(payload)
    data = sanitized.get("data") if isinstance(sanitized.get("data"), dict) else {}
    event = data.get("event") if isinstance(data.get("event"), dict) else sanitized.get("event")
    issue = data.get("issue") if isinstance(data.get("issue"), dict) else sanitized.get("issue")
    event = event if isinstance(event, dict) else {}
    issue = issue if isinstance(issue, dict) else {}

    title = (
        issue.get("title")
        or event.get("title")
        or event.get("message")
        or sanitized.get("title")
        or "Sentry incident"
    )
    incident = db.query(DebugIncident).filter(DebugIncident.sentry_event_id == event_id).first()
    if not incident:
        incident = DebugIncident(
            sentry_event_id=event_id,
            sentry_issue_id=str(issue.get("id")) if issue.get("id") else None,
            project_slug=event.get("project") or issue.get("project") or sanitized.get("project"),
            environment=event.get("environment") or sanitized.get("environment"),
            level=event.get("level") or sanitized.get("level"),
            title=str(title)[:500],
            culprit=event.get("culprit") or sanitized.get("culprit"),
            sanitized_payload_json=sanitized,
            event_count=1,
        )
        db.add(incident)
    else:
        incident.sanitized_payload_json = sanitized
        incident.title = str(title)[:500]
        incident.event_count = (incident.event_count or 0) + 1
        incident.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(incident)
    return incident


_SENTRY_LEVEL_WEIGHTS = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
    "fatal": 4,
    "critical": 4,
}


def _level_weight(level: Optional[str]) -> int:
    return _SENTRY_LEVEL_WEIGHTS.get((level or "error").lower(), _SENTRY_LEVEL_WEIGHTS["error"])


def _incident_is_critical(incident: DebugIncident) -> bool:
    return _level_weight(incident.level) >= _SENTRY_LEVEL_WEIGHTS["fatal"]


def _matches_filter(value: Optional[str], allowed: Any) -> bool:
    if not allowed:
        return True
    if not isinstance(allowed, list):
        return True
    normalized = {str(item).strip().lower() for item in allowed if str(item).strip()}
    return not normalized or (value or "").strip().lower() in normalized


def triage_debug_incident(db: Session, incident_id: int) -> DebugTriageRun:
    """Create a deterministic read-only triage run from sanitized incident data."""
    incident = db.query(DebugIncident).filter(DebugIncident.id == incident_id).first()
    if not incident:
        raise ValueError("Debug incident not found")

    sanitized_payload = redact_sensitive(incident.sanitized_payload_json or {})
    severity = (incident.level or "error").lower()
    recommendations = [
        {
            "action": "Inspect the sanitized Sentry stack trace and recent deploy diff.",
            "priority": "high" if _level_weight(severity) >= _SENTRY_LEVEL_WEIGHTS["error"] else "medium",
        },
        {
            "action": "Verify customer, realm, token, and financial payloads remain redacted before sharing.",
            "priority": "high",
        },
    ]
    if _incident_is_critical(incident):
        recommendations.insert(
            0,
            {
                "action": "Page the platform owner and check availability impact immediately.",
                "priority": "critical",
            },
        )

    run = DebugTriageRun(
        incident_id=incident.id,
        status="completed",
        severity=severity,
        summary=f"{severity.upper()} in {incident.project_slug or 'unknown project'}: {incident.title}",
        recommendations_json=recommendations,
        sanitized_input_json=sanitized_payload,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _recent_delivery_exists(db: Session, subscription: DebugNotificationSubscription, incident: DebugIncident) -> bool:
    if not subscription.cooldown_seconds:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=int(subscription.cooldown_seconds))
    return (
        db.query(DebugNotificationDelivery)
        .filter(
            DebugNotificationDelivery.subscription_id == subscription.id,
            DebugNotificationDelivery.status == "sent",
            DebugNotificationDelivery.created_at >= cutoff,
        )
        .first()
        is not None
    )


def dispatch_debug_notifications(
    db: Session,
    incident_id: int,
    triage_run_id: Optional[int] = None,
    *,
    sender: Optional[Callable[..., Any]] = None,
) -> list[DebugNotificationDelivery]:
    """Persist notification attempts and mark each one with its real transport outcome."""
    incident = db.query(DebugIncident).filter(DebugIncident.id == incident_id).first()
    if not incident:
        raise ValueError("Debug incident not found")
    triage_run = db.query(DebugTriageRun).filter(DebugTriageRun.id == triage_run_id).first() if triage_run_id else None
    deliveries: list[DebugNotificationDelivery] = []
    transport = sender or send_debug_incident_notification
    subscriptions = (
        db.query(DebugNotificationSubscription)
        .join(User, DebugNotificationSubscription.created_by_user_id == User.id)
        .filter(DebugNotificationSubscription.is_active.is_(True))
        .filter(User.is_priority_admin.is_(True))
        .order_by(DebugNotificationSubscription.id.asc())
        .all()
    )
    for subscription in subscriptions:
        status = "pending"
        reason = None
        if _level_weight(incident.level) < _level_weight(subscription.min_level):
            status = "skipped"
            reason = "below_min_level"
        elif not _matches_filter(incident.environment, subscription.environments_json):
            status = "skipped"
            reason = "environment_filtered"
        elif not _matches_filter(incident.project_slug, subscription.project_slugs_json):
            status = "skipped"
            reason = "project_filtered"
        elif not _incident_is_critical(incident) and _recent_delivery_exists(db, subscription, incident):
            status = "skipped"
            reason = "cooldown"

        payload = redact_sensitive(
            {
                "incident_id": incident.id,
                "sentry_event_id": incident.sentry_event_id,
                "project_slug": incident.project_slug,
                "environment": incident.environment,
                "level": incident.level,
                "title": incident.title,
                "triage_summary": triage_run.summary if triage_run else None,
            }
        )
        delivery = DebugNotificationDelivery(
            subscription_id=subscription.id,
            incident_id=incident.id,
            triage_run_id=triage_run.id if triage_run else None,
            channel=subscription.channel,
            recipient=subscription.recipient_email,
            status=status,
            reason=reason,
            payload_json=payload,
        )
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        if delivery.status == "pending":
            try:
                result = transport(recipient=delivery.recipient, channel=delivery.channel, payload=payload)
                delivered = bool(getattr(result, "delivered", False))
                delivery.status = "sent" if delivered else "failed"
                delivery.reason = getattr(result, "reason", None)
                delivery.sent_at = datetime.now(timezone.utc) if delivered else None
            except Exception as exc:
                delivery.status = "failed"
                delivery.reason = redact_to_string(str(exc), max_length=300) or "send_failed"
                delivery.sent_at = None
            db.commit()
            db.refresh(delivery)
        deliveries.append(delivery)
    return deliveries


@router.post("/sentry", status_code=202)  # public-route: Sentry webhook, authed by sentry-hook-signature HMAC (no user session)
@limiter.limit("60/minute")
async def receive_sentry_webhook(  # OBS-OK: request: Request is the first param on the line below (multi-line signature)
    request: Request,
    sentry_hook_signature: Optional[str] = Header(None, alias="sentry-hook-signature"),
    sentry_hook_resource: Optional[str] = Header(None, alias="sentry-hook-resource"),
    sentry_hook_action: Optional[str] = Header(None, alias="sentry-hook-action"),
    db: Session = Depends(get_db),
):
    raw_body = await request.body()
    webhook_secret = settings.sentry_webhook_secret

    if not webhook_secret and _requires_sentry_webhook_signature():
        logger.error("Sentry webhook secret is not configured; rejecting webhook")
        raise HTTPException(status_code=503, detail="Sentry webhook secret is not configured")
    if webhook_secret or _requires_sentry_webhook_signature():
        if not sentry_hook_signature:
            raise HTTPException(status_code=401, detail="Missing signature header")
        if not webhook_secret or not _verify_sentry_signature(raw_body, sentry_hook_signature, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_id = _extract_sentry_event_id(payload, raw_body, sentry_hook_resource)
    event_row, duplicate = _claim_sentry_event(
        db,
        event_id=event_id,
        resource=sentry_hook_resource,
        action=sentry_hook_action,
        payload=payload,
    )
    if duplicate:
        return {"status": "duplicate", "event_id": event_id}

    try:
        incident = _upsert_debug_incident(db, event_id=event_id, payload=payload)
        triage_run = triage_debug_incident(db, incident.id)
        dispatch_debug_notifications(db, incident.id, triage_run.id)
        event_row.status = "processed"
        event_row.error = None
        event_row.processed_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": "accepted", "event_id": event_id, "incident_id": incident.id}
    except Exception as exc:
        db.rollback()
        event_row = db.query(SentryWebhookEvent).filter(SentryWebhookEvent.id == event_row.id).first()
        if event_row:
            event_row.status = "failed"
            event_row.error = redact_to_string(str(exc), max_length=1000)
            db.commit()
        raise HTTPException(status_code=500, detail="Failed to process Sentry webhook")


# ─────────────────────────────────────────────────────────────────────────────
# Background handler
# ─────────────────────────────────────────────────────────────────────────────

# Operations each handler acts on (legacy names; CloudEvents are normalised to them).
_BANK_FEED_OPERATIONS = {"Create", "Update", "Delete", "Void"}
_BOOK_SIDE_OPERATIONS = {"Update", "Delete", "Void"}
_LIST_ENTITY_OPERATIONS = {"Create", "Update", "Delete", "Merge"}


def _lazy_qbo_client(db: Session, realm_id: str) -> Callable[[], QBOClient]:
    """A realm client built on first use through get_qbo_client_from_db, which
    refreshes an expiring token and self-heals a 401. Delete-only batches never
    touch QBO, so they never build one."""
    built: list[QBOClient] = []

    def client() -> QBOClient:
        if not built:
            built.append(get_qbo_client_from_db(db, realm_id))
        return built[0]

    return client


def _is_realm_not_connected(exc: HTTPException) -> bool:
    return (
        exc.status_code == 409
        and isinstance(exc.detail, dict)
        and exc.detail.get("error_code") == QBO_NOT_CONNECTED_CODE
    )


def _process_notifications(db: Session, notifications: List[dict]) -> None:
    from ..services.sync import BOOK_SIDE_ENTITY_TYPES

    for n in notifications:
        realm_id = n["realm_id"]
        entities = n["entities"]
        client = _lazy_qbo_client(db, realm_id)

        bank_feed_items: list = []
        book_entities: list = []
        general_entities: list = []
        search_entities: list = []

        for entity in entities:  # SA-OK: bounded-loop over the webhook notification's entity dicts; entity.get(...) below are dict accessors, not DB queries
            name = entity.get("name", "")
            operation = entity.get("operation", "")
            entity_id = entity.get("id", "")
            logger.info(
                "QBO webhook entity realm_id=%s operation=%s entity=%s entity_id=%s",
                realm_id,
                operation,
                name,
                entity_id,
            )

            # Purchase/Deposit feed BOTH the bank-feed items and the book rows.
            if name in BANK_FEED_ENTITIES and operation in _BANK_FEED_OPERATIONS:
                bank_feed_items.append(entity)
            if name in BOOK_SIDE_ENTITY_TYPES and operation in _BOOK_SIDE_OPERATIONS:
                book_entities.append(entity)
            if name in ("Vendor", "Customer") and operation in _LIST_ENTITY_OPERATIONS:
                general_entities.append(entity)
            elif name in SEARCH_INDEX_ENTITIES and operation in ("Create", "Update"):
                search_entities.append(entity)

        try:
            # ── Bank feed items ──────────────────────────────────────────
            if bank_feed_items:
                _sync_bank_feed_entities(db, client, realm_id, bank_feed_items)

            # ── Bank-reconciliation book rows ────────────────────────────
            if book_entities:
                _sync_book_side_entities(db, client, realm_id, book_entities)

            # ── Vendor / Customer cache refresh ─────────────────────────
            if general_entities:
                _sync_vendor_customer(db, client, realm_id, general_entities)

            # ── Invoice / Bill FTS index upsert ──────────────────────────
            if search_entities:
                _sync_search_index_entities(db, client, realm_id, search_entities)
        except HTTPException as exc:
            # A realm with no QuickBooks connection at all has nothing to read; its
            # initial sync on (re)connect re-reads QBO. Every other failure — a dead
            # grant, a 401 the refresh could not heal, a transport error — raises
            # so the event is marked failed and retried.
            if not _is_realm_not_connected(exc):
                raise
            db.rollback()
            logger.warning("QBO webhook: realm_id=%s has no QuickBooks connection; event skipped", realm_id)

        # ── Invalidate Dashboard Cache ──────────────────────────────────
        clear_company_cache(realm_id)


def _qbo_stale_processing_cutoff() -> datetime:
    return datetime.now(timezone.utc) - QBO_WEBHOOK_PROCESSING_STALE_AFTER


def _claim_qbo_event_for_processing(db: Session, event_id: int) -> bool:
    """
    Atomically claim a QBO webhook event for processing.

    Concurrency-safe: claiming is a single conditional UPDATE whose WHERE clause
    only matches events that are pending/failed, or whose processing started
    more than QBO_WEBHOOK_PROCESSING_STALE_AFTER ago (stale reclaim). Two
    concurrent workers cannot both claim the same row: exactly one UPDATE wins.

    Retries are capped at QBO_WEBHOOK_MAX_RETRIES via retry_count; once
    exhausted, a stale event is marked failed with a redacted error reason.
    Returns True when this caller owns the event and should process it.
    """
    now = datetime.now(timezone.utc)
    stale_cutoff = _qbo_stale_processing_cutoff()

    claimable = sa_or_(
        QBOWebhookEvent.status.in_(["pending", "failed"]),
        sa_and_(
            QBOWebhookEvent.status == "processing",
            QBOWebhookEvent.processing_started_at.isnot(None),
            QBOWebhookEvent.processing_started_at < stale_cutoff,
        ),
    )

    claimed = (
        db.query(QBOWebhookEvent)
        .filter(
            QBOWebhookEvent.id == event_id,
            claimable,
            QBOWebhookEvent.retry_count < QBO_WEBHOOK_MAX_RETRIES,
        )
        .update(
            {
                QBOWebhookEvent.status: "processing",
                QBOWebhookEvent.processing_started_at: now,
                QBOWebhookEvent.attempts: QBOWebhookEvent.attempts + 1,
                QBOWebhookEvent.retry_count: QBOWebhookEvent.retry_count + 1,
                QBOWebhookEvent.error: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed:
        return True

    # Retry cap: a claimable-but-exhausted event is terminally failed.
    # Same conditional-UPDATE shape so concurrent sweeps stay idempotent.
    failed = (
        db.query(QBOWebhookEvent)
        .filter(
            QBOWebhookEvent.id == event_id,
            claimable,
            QBOWebhookEvent.retry_count >= QBO_WEBHOOK_MAX_RETRIES,
        )
        .update(
            {
                QBOWebhookEvent.status: "failed",
                QBOWebhookEvent.error: redact_to_string(
                    _QBO_WEBHOOK_RETRY_EXHAUSTED_ERROR, max_length=300
                ),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if failed:
        logger.warning(
            "QBO webhook event retry cap reached; marked failed event_id=%s", event_id
        )
    return False


def _process_qbo_webhook_event(event_id: int) -> None:
    """Resolve the event's owning realm/org, then process under that pinned scope.

    The single shared processor for BOTH the live FastAPI BackgroundTasks path and the
    Celery recovery task. ``qbo_webhook_events`` is realm-keyed and the downstream sync
    writes org-keyed (``qbo_tokens``) + realm-keyed (``bank_feed_items``) tables, all under
    FORCE RLS, so the work must run with both GUCs pinned. The realm is discovered by a
    transient-bypass PK lookup, then the processing runs under ``tenant_scope(org, realm)``.
    """
    resolve_db = SessionLocal()
    try:
        with tenant_scope(bypass=True):
            event = (
                resolve_db.query(QBOWebhookEvent)
                .filter(QBOWebhookEvent.id == event_id)
                .first()  # CROSS-TENANT-OK: PK lookup to resolve the realm before pinning tenant scope
            )
            realm_id = event.realm_id if event else None
            org = organization_for_company(resolve_db, realm_id) if realm_id else None
            org_id = org.id if org else None
    finally:
        resolve_db.close()

    if realm_id is None:
        logger.warning(
            "QBO webhook event missing or has no realm; skipping event_id=%s", event_id
        )
        return

    # org_id may be None for an unmapped realm: the realm-keyed claim + qbo_webhook_events
    # writes still apply, and org-keyed reads return zero rows (a safe no-op, never an
    # unscoped cross-tenant write).
    with tenant_scope(org_id=org_id, realm_id=realm_id):
        _process_qbo_webhook_event_scoped(event_id)


def _process_qbo_webhook_event_scoped(event_id: int) -> None:
    db = SessionLocal()  # SA-OK: closed-elsewhere — the try/finally below calls db.close() in finally
    try:
        if not _claim_qbo_event_for_processing(db, event_id):
            return
        event = db.query(QBOWebhookEvent).filter(QBOWebhookEvent.id == event_id).first()
        if not event:
            return

        _process_notifications(db, [{"realm_id": event.realm_id, "entities": [event.payload_json]}])

        event = db.query(QBOWebhookEvent).filter(QBOWebhookEvent.id == event_id).first()
        if event:
            event.status = "processed"
            event.error = None
            event.processed_at = datetime.now(timezone.utc)
            event.processing_started_at = None
            db.commit()
    except Exception as exc:
        db.rollback()
        event = db.query(QBOWebhookEvent).filter(QBOWebhookEvent.id == event_id).first()
        if event:
            event.status = "failed"
            event.error = redact_to_string(str(exc), max_length=1000)
            db.commit()
        logger.exception("QBO webhook event processing failed event_id=%s", event_id)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Bank feed entity handler
# ─────────────────────────────────────────────────────────────────────────────

_BANK_FEED_TXN_PREFIX = {"Purchase": "purchase", "Deposit": "deposit", "BankTransaction": "bt"}


def _sync_bank_feed_entities(
    db: Session, client: Callable[[], QBOClient], realm_id: str, entities: list
) -> None:
    """
    Mirror each Purchase / Deposit / BankTransaction event into bank_feed_items:
      Create/Update → fetch the current record and insert or refresh the item;
      Delete/Void   → retire the item (a deleted or voided transaction needs no review).

    Purchase/Deposit: already in GL → approved if categorised, pending while still
                      Uncategorized; re-categorising in QBO moves the item to approved.
    BankTransaction: pre-GL item (For Review) → always pending.

    A fetch failure (an auth failure after the client's refresh, a transport error)
    raises, so the event is marked failed and retried instead of processed.
    """
    for entity in entities:
        name = entity.get("name", "")
        entity_id = str(entity.get("id") or "")
        operation = entity.get("operation", "")
        if not entity_id or name not in _BANK_FEED_TXN_PREFIX:
            continue
        if operation in ("Delete", "Void"):
            _retire_bank_feed_item(db, realm_id, f"{_BANK_FEED_TXN_PREFIX[name]}-{entity_id}")
        elif name == "Purchase":
            _upsert_purchase(db, client(), realm_id, entity_id)
        elif name == "Deposit":
            _upsert_deposit(db, client(), realm_id, entity_id)
        else:
            _upsert_bank_transaction(db, client(), realm_id, entity_id)

    db.commit()

    # NOTE: the former `apply_description_rules` call here was removed — it
    # imported `app/services/bank_feed.py`, which no longer exists (the bank-feed/
    # reconciliation SERVICE was removed in e05dd51 and superseded by this
    # webhook-driven BankFeedItem caching). The import raised ModuleNotFoundError
    # on every webhook (swallowed + logged), so description-rules never ran.


def refresh_bank_feed_item(db: Session, client: QBOClient, realm_id: str, qbo_txn_id: str) -> str | None:
    """Re-read the QBO transaction behind a QBO-feed item and apply its current
    state, exactly as an Update event would. Returns the item's status, or None
    when QBO deleted or voided the transaction (the item is retired). Flushes but
    does not commit: the caller commits the change together with its audit row."""
    prefix, _, entity_id = (qbo_txn_id or "").partition("-")
    upsert = {"purchase": _upsert_purchase, "deposit": _upsert_deposit, "bt": _upsert_bank_transaction}.get(prefix)
    if upsert is None or not entity_id:
        raise ValueError(f"not a QBO-feed transaction id: {qbo_txn_id!r}")
    upsert(db, client, realm_id, entity_id)
    db.flush()
    item = (
        db.query(BankFeedItem)
        .filter(
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.source == "qbo_feed",
            BankFeedItem.qbo_txn_id == qbo_txn_id,
        )
        .first()
    )
    return item.status if item is not None else None


def _sync_book_side_entities(
    db: Session, client: Callable[[], QBOClient], realm_id: str, entities: list
) -> None:
    """Keep the bank-reconciliation book rows (qbo_transactions) in step with QBO
    between windowed syncs: Delete retires the transaction's rows; Update/Void
    re-reads a transaction the sync mirrored, retiring keys it no longer produces
    (moved to another bank account, voided out)."""
    from ..services import sync as sync_service

    for entity in entities:
        name = entity.get("name", "")
        entity_id = str(entity.get("id") or "")
        if not entity_id:
            continue
        if entity.get("operation") == "Delete":
            sync_service.retire_book_transaction(db, realm_id, name, entity_id)
        elif sync_service.book_transaction_is_mirrored(db, realm_id, name, entity_id):
            sync_service.refresh_book_transaction(db, client(), name, entity_id)


def _sync_search_index_entities(
    db: Session, client: Callable[[], QBOClient], realm_id: str, entities: list
) -> None:
    """For each Invoice / Bill Create|Update event: fetch the single entity from
    QBO and upsert it into the QBOSearchRecord FTS index via the CANONICAL indexer
    (services.sync.index_single_entity -> finance_search.index_qbo_entity).

    The upsert is idempotent (upsert by realm_id/entity_type/qbo_id), so a
    redelivered webhook refreshes the same row rather than duplicating it. A fetch
    failure raises so the event is retried.
    """
    from ..services import sync as sync_service

    for entity in entities:  # SA-OK: bounded-loop over the webhook batch's entity dicts; entity.get(...) are dict accessors, not DB queries
        name = entity.get("name", "")
        entity_id = entity.get("id", "")
        if not entity_id or name not in SEARCH_INDEX_ENTITIES:
            continue
        sync_service.index_single_entity(db, client(), name, entity_id)


def _retire_bank_feed_item(db: Session, realm_id: str, qbo_txn_id: str) -> int:
    """Remove the QBO-feed item mirroring a transaction QBO deleted or voided."""
    removed = (
        db.query(BankFeedItem)
        .filter(
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.source == "qbo_feed",
            BankFeedItem.qbo_txn_id == qbo_txn_id,
        )
        .delete(synchronize_session=False)
    )
    if removed:
        logger.info("QBO webhook: retired bank feed item %s", qbo_txn_id)
    return removed


def _is_voided(record: dict) -> bool:
    # QBO keeps a voided Purchase/Deposit with its amount zeroed.
    return _parse_decimal(record.get("TotalAmt", 0)) == 0


def _apply_bank_feed_item(
    db: Session, realm_id: str, qbo_txn_id: str, fields: dict[str, Any], *, needs_review: bool
) -> str:
    """Insert the item, or refresh the existing one from QBO's current record.

    The item follows QBO's categorisation (pending ⇄ approved); a reviewer's
    dismissal (``ignored``) stands. Returns the item's resulting status.
    """
    status = "pending" if needs_review else "approved"
    existing = (
        db.query(BankFeedItem)
        .filter(
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.source == "qbo_feed",
            BankFeedItem.qbo_txn_id == qbo_txn_id,
        )
        .first()
    )
    if existing is None:
        db.add(BankFeedItem(
            realm_id=realm_id, qbo_txn_id=qbo_txn_id, status=status, cleared_in_qbo=not needs_review, **fields,
        ))
        return status
    for key, value in fields.items():
        setattr(existing, key, value)
    existing.cleared_in_qbo = not needs_review
    if existing.status != "ignored":
        existing.status = status
    return existing.status


def _upsert_purchase(db: Session, client: QBOClient, realm_id: str, purchase_id: str) -> None:
    """Fetch a single Purchase by ID and insert or refresh its BankFeedItem."""
    from ..services.sync import fetch_entity_by_id

    qbo_txn_id = f"purchase-{purchase_id}"
    p = fetch_entity_by_id(client, "Purchase", purchase_id)
    if p is None or _is_voided(p):
        _retire_bank_feed_item(db, realm_id, qbo_txn_id)
        return

    account_ref = p.get("AccountRef") or {}
    lines = p.get("Line") or []
    memo = p.get("PrivateNote") or ""
    desc = memo
    line_account_ref = {}
    entity_ref = p.get("EntityRef") or {}

    if lines:
        first = lines[0]
        detail = (
            first.get("AccountBasedExpenseLineDetail")
            or first.get("ItemBasedExpenseLineDetail")
            or {}
        )
        line_account_ref = detail.get("AccountRef") or {}
        line_desc = first.get("Description", "") or ""
        # Prefer line description (actual bank memo) over PrivateNote (internal)
        # Combine both when available for maximum context
        if line_desc and memo and line_desc.strip() != memo.strip():
            desc = f"{line_desc} | {memo}"
        elif line_desc:
            desc = line_desc
        elif memo:
            desc = memo

    line_account_name = (line_account_ref.get("name", "") or "").lower()
    is_uncategorized = line_account_name in _UNCATEGORIZED or not line_account_ref.get("value")

    status = _apply_bank_feed_item(db, realm_id, qbo_txn_id, {
        "txn_date": _parse_date(p.get("TxnDate", "")),
        "amount": _parse_decimal(p.get("TotalAmt", 0)),
        "description": desc or p.get("DocNumber", ""),
        "txn_type": "DEBIT",
        "bank_account_ref": str(account_ref.get("value", "")),
        "bank_account_name": account_ref.get("name", ""),
        "suggested_account_id": None if is_uncategorized else line_account_ref.get("value"),
        "suggested_account_name": None if is_uncategorized else line_account_ref.get("name"),
        "confidence_score": None if is_uncategorized else Decimal("1.0000"),
        "ai_reasoning": None if is_uncategorized else "Imported from QBO GL — already categorised.",
        "vendor_id": str(entity_ref["value"]) if entity_ref.get("value") else None,
        "vendor_name": entity_ref.get("name"),
        "entity_type": "vendor" if entity_ref.get("value") else None,
    }, needs_review=is_uncategorized)
    logger.info(f"QBO webhook: upserted Purchase {purchase_id} as {status}")


def _upsert_deposit(db: Session, client: QBOClient, realm_id: str, deposit_id: str) -> None:
    """Fetch a single Deposit by ID and insert or refresh its BankFeedItem."""
    from ..services.sync import fetch_entity_by_id

    qbo_txn_id = f"deposit-{deposit_id}"
    d = fetch_entity_by_id(client, "Deposit", deposit_id)
    if d is None or _is_voided(d):
        _retire_bank_feed_item(db, realm_id, qbo_txn_id)
        return

    account_ref = d.get("DepositToAccountRef") or {}
    lines = d.get("Line") or []
    desc = d.get("PrivateNote") or ""
    line_account_ref = {}
    entity_ref = {}

    if lines:
        first = lines[0]
        detail = first.get("DepositLineDetail") or {}
        line_account_ref = detail.get("AccountRef") or {}
        entity_ref = (detail.get("Entity") or {}).get("EntityRef") or {}
        line_desc = first.get("Description", "") or ""
        # Prefer line description (actual bank memo) over PrivateNote (internal)
        if line_desc and desc and line_desc.strip() != desc.strip():
            desc = f"{line_desc} | {desc}"
        elif line_desc:
            desc = line_desc

    line_account_name = (line_account_ref.get("name", "") or "").lower()
    is_uncategorized = line_account_name in _UNCATEGORIZED or not line_account_ref.get("value")

    status = _apply_bank_feed_item(db, realm_id, qbo_txn_id, {
        "txn_date": _parse_date(d.get("TxnDate", "")),
        "amount": abs(_parse_decimal(d.get("TotalAmt", 0))),
        "description": desc,
        "txn_type": "CREDIT",
        "bank_account_ref": str(account_ref.get("value", "")),
        "bank_account_name": account_ref.get("name", ""),
        "suggested_account_id": None if is_uncategorized else line_account_ref.get("value"),
        "suggested_account_name": None if is_uncategorized else line_account_ref.get("name"),
        "confidence_score": None if is_uncategorized else Decimal("1.0000"),
        "ai_reasoning": None if is_uncategorized else "Imported from QBO GL — already categorised.",
        "customer_id": str(entity_ref["value"]) if entity_ref.get("value") else None,
        "customer_name": entity_ref.get("name"),
        "entity_type": "customer" if entity_ref.get("value") else None,
    }, needs_review=is_uncategorized)
    logger.info(f"QBO webhook: upserted Deposit {deposit_id} as {status}")


def _upsert_bank_transaction(db: Session, client: QBOClient, realm_id: str, bt_id: str) -> None:
    """
    Attempt to fetch a BankTransaction by ID.
    QBO doesn't support batch query for BankTransaction but individual GET
    may succeed for recently created items.

    A 400/404 answer (the entity is not exposed) keeps a minimal stub so we know the
    import exists; auth and transport failures raise so the event is retried.
    These are the true 'For Review' items — always pending.
    """
    qbo_txn_id = f"bt-{bt_id}"

    try:
        resp = client.get_transaction("banktransaction", bt_id)
        bt = resp.get("BankTransaction", {}) or {}
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code not in (400, 404):
            raise
        # BankTransaction individual GET often returns 400/404 — expected, keep a stub
        logger.debug(f"QBO webhook: BankTransaction {bt_id} not fetchable (expected): {exc}")
        bt = {}

    existing = (
        db.query(BankFeedItem)
        .filter(
            BankFeedItem.realm_id == realm_id,
            BankFeedItem.source == "qbo_feed",
            BankFeedItem.qbo_txn_id == qbo_txn_id,
        )
        .first()
    )
    if existing is not None and not bt:
        return  # nothing newer than the item we already hold

    # Extract what we can from the webhook entity or the fetched record
    amount = abs(_parse_decimal(bt.get("Amount", 0)))
    fields = {
        "txn_date": _parse_date(bt.get("TxnDate", "")),
        "amount": amount if amount > 0 else Decimal("0"),
        "description": bt.get("Description") or bt.get("Memo") or f"Bank Import #{bt_id}",
        "txn_type": "DEBIT" if _parse_decimal(bt.get("Amount", 0)) < 0 else "CREDIT",
    }
    if existing is None:
        db.add(BankFeedItem(realm_id=realm_id, qbo_txn_id=qbo_txn_id, status="pending", cleared_in_qbo=False, **fields))
        logger.info(f"QBO webhook: created pending BankTransaction stub {bt_id}")
        return
    for key, value in fields.items():
        setattr(existing, key, value)
    logger.info(f"QBO webhook: refreshed BankTransaction {bt_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Vendor / Customer refresh
# ─────────────────────────────────────────────────────────────────────────────

def _sync_vendor_customer(
    db: Session, client: Callable[[], QBOClient], realm_id: str, entities: list
) -> None:
    """Refresh the vendor/customer cache row each event names: re-read the entity
    and upsert it, dropping the row when QBO no longer returns it. A Merge also
    drops the merged-away duplicate (``deletedId``). A fetch failure raises so the
    event is retried."""
    from ..services import sync as sync_service

    for entity in entities:
        name = entity.get("name", "")
        entity_id = str(entity.get("id") or "")
        if not entity_id:
            continue
        if entity.get("operation") == "Merge" and entity.get("deletedId"):
            sync_service.remove_list_entity(db, realm_id, name, str(entity["deletedId"]))
        sync_service.refresh_list_entity(db, client(), name, entity_id)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> date:
    if not raw:
        return date.today()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return date.today()


def _parse_decimal(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return Decimal("0")



def _persist_review_qbo_delivery(db, raw_body, signature, verifier_secret, *, deadline=None):
    """Existing canonical realm bindings locate preauthorized numeric routes.

    A provider account ID never grants user access. This lane only invalidates
    retained coverage; it performs no financial extraction in the callback.
    """
    if not settings.accounting_source_feeds_enabled:
        return None
    from sqlalchemy import text
    from ..models import OrganizationCompany, AccountingBook, AccountingSourceConnection
    from ..tenant_rls import tenant_scope_live
    from ..accounting_sources.events import EventRoute, parse_verified_delivery, persist_verified_delivery
    from ..accounting_sources.contracts import SourceContractError
    if deadline is None:
        raise HTTPException(status_code=503, detail="Source webhook requires an admitted deadline")
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise HTTPException(status_code=503, detail="Source webhook admission deadline exhausted")
        if db.get_bind().dialect.name == "postgresql":
            from ..accounting_sources.connections import configure_source_database_waits
            configure_source_database_waits(db, remaining_seconds=seconds)
        return seconds
    try:
        events = parse_verified_delivery(provider="qbo", raw_body=raw_body,
                                         signature=signature, verifier_secret=verifier_secret)
        routes = []
        for realm in sorted({event.account_id for event in events}):
            remaining()
            # Canonical companies are the established non-RLS ownership mapping.
            # No orphan-token bypass, principal or source body selects an owner.
            company = db.query(OrganizationCompany).filter(OrganizationCompany.realm_id == realm).one_or_none()
            if company is None or company.status != "active":
                continue
            with tenant_scope_live(db, org_id=company.organization_id, company_id=company.id,
                                   realm_id=None, bypass=False):
                remaining()
                connections = db.query(AccountingSourceConnection).filter(
                    AccountingSourceConnection.organization_id == company.organization_id,
                    AccountingSourceConnection.organization_company_id == company.id,
                    AccountingSourceConnection.provider == "qbo",
                    AccountingSourceConnection.external_entity_id == company.realm_id,
                    AccountingSourceConnection.status == "active",
                ).order_by(AccountingSourceConnection.id).limit(501).all()
                for connection in connections:
                    remaining()
                    books = db.query(AccountingBook).filter(
                        AccountingBook.connection_id == connection.id,
                        AccountingBook.organization_id == company.organization_id,
                        AccountingBook.organization_company_id == company.id,
                        AccountingBook.status == "active",
                    ).order_by(AccountingBook.id).limit(501).all()
                    for book in books:
                        if len(routes) >= 500:
                            raise SourceContractError("source delivery route bound exceeded")
                        routes.append(EventRoute(company.organization_id, company.id, connection.id,
                                                 book.id, connection.provider, connection.environment,
                                                 connection.external_account_id, connection.external_entity_id))
        remaining()
        result = persist_verified_delivery(db, provider="qbo", raw_body=raw_body,
            signature=signature, verifier_secret=verifier_secret, routes=tuple(routes), deadline=deadline)
        remaining()
        db.commit()
        if time.monotonic() >= deadline:
            raise HTTPException(status_code=503, detail="Source webhook acknowledgement deadline exhausted")
        return {"persisted": result.persisted, "duplicates": result.duplicates,
                "unrouted": result.unrouted, "financial_completeness": False}
    except SourceContractError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Source webhook signature, route or bounded contract refused") from None
    except Exception:
        db.rollback()
        raise

"""Bounded signed change signals and transactional invalidation outbox.

Signatures authenticate provider delivery, not a company grant or completeness.
The caller supplies only routes already resolved through the existing ownership
ladder. Each route is reloaded in its own non-bypass numeric company scope.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (AccountingBook, AccountingSourceConnection, AccountingSourceEvent,
                      AccountingSourceHead, AccountingSourceInvalidation, AccountingSourceOutbox)
from ..services.crypto import encrypt_secret
from ..tenant_rls import tenant_scope_live
from .contracts import SourceContractError, canonical_digest, canonical_json, required_text
from .transport import strict_json_loads
from .connections import configure_source_database_waits, source_query, update_source_row

MAX_DELIVERY_BYTES = 1024 * 1024
MAX_EVENTS = 500
MAX_ROUTES = 500
EVENT_PRODUCER = "source-event/v1"


@dataclass(frozen=True)
class SourceEvent:
    provider: str
    provider_event_id: str
    account_id: str
    entity_id: str
    entity_type: str
    operation: str
    event_time: str
    delivery_digest: str


@dataclass(frozen=True)
class EventRoute:
    organization_id: int
    company_id: int
    connection_id: int
    book_id: int
    provider: str
    environment: str
    external_account_id: str
    external_entity_id: str

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            self.organization_id, self.company_id, self.connection_id, self.book_id,
        )):
            raise SourceContractError("positive canonical event route identities required")


@dataclass(frozen=True)
class DeliveryResult:
    persisted: int
    duplicates: int
    unrouted: int
    outbox_ids: tuple[int, ...]


def verify_raw_signature(raw_body: bytes, signature: str, verifier_secret: str) -> None:
    if type(raw_body) is not bytes or len(raw_body) > MAX_DELIVERY_BYTES:
        raise SourceContractError("source event delivery exceeds its byte bound")
    if not isinstance(signature, str) or not signature or not verifier_secret:
        raise SourceContractError("source delivery requires its raw-body signature")
    expected = base64.b64encode(hmac.new(verifier_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise SourceContractError("invalid source delivery signature")


def _event_time(value: Any, *, require_zone: bool = False) -> str:
    required_text(value, "event_time", maximum=100)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceContractError("invalid source event timestamp") from exc
    if parsed.tzinfo is None:
        if require_zone:
            raise SourceContractError("QBO event timestamp requires an explicit timezone")
        # Xero documents UTC timestamps that omit a zone suffix.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return value if require_zone else parsed.astimezone(timezone.utc).isoformat()


def _admit_deadline(db: Session, deadline: float | None) -> None:
    """Spend the HTTP owner's original absolute budget, including PG waits."""
    if deadline is None:
        return  # Pure replay tests; the HTTP integration always supplies a deadline.
    remaining = deadline - time.monotonic()
    if remaining <= 0 or remaining > 3.0:
        raise SourceContractError("source delivery absolute admission deadline exhausted or invalid")
    configure_source_database_waits(db, remaining_seconds=remaining)
    if time.monotonic() >= deadline:
        raise SourceContractError("source delivery absolute admission deadline exhausted")


def normalize_qbo_events(payload: Any, *, delivery_digest: str) -> tuple[SourceEvent, ...]:
    """Normalize documented CloudEvents arrays and supported legacy deliveries.

    Official contract: https://static.developer.intuit.com/output_html/qbo/docs/develop/webhooks/configure-webhooks.html
    The event ID is retained in addition to account/entity/operation/time.
    """
    result: list[SourceEvent] = []
    if isinstance(payload, list):
        if len(payload) > MAX_EVENTS:
            raise SourceContractError("too many source events")
        for item in payload:
            if not isinstance(item, dict) or item.get("specversion") != "1.0":
                raise SourceContractError("unsupported QBO CloudEvents contract")
            event_type = required_text(item.get("type"), "event type")
            match = re.fullmatch(r"qbo\.([A-Za-z][A-Za-z0-9]*)\.([A-Za-z][A-Za-z0-9]*)\.v1", event_type)
            if match is None:
                raise SourceContractError("unsupported QBO event type")
            result.append(SourceEvent(
                "qbo", required_text(item.get("id"), "event id"),
                required_text(item.get("intuitaccountid"), "account id"),
                required_text(item.get("intuitentityid"), "entity id"),
                match.group(1), match.group(2), _event_time(item.get("time"), require_zone=True), delivery_digest,
            ))
    elif isinstance(payload, dict):
        if "specversion" in payload:
            payload = payload.get("data")
        groups = payload.get("eventNotifications") if isinstance(payload, dict) else None
        if not isinstance(groups, list) or len(groups) > MAX_EVENTS:
            raise SourceContractError("invalid QBO legacy delivery")
        for group in groups:
            if not isinstance(group, dict):
                raise SourceContractError("invalid QBO notification")
            account = required_text(group.get("realmId"), "account id")
            change = group.get("dataChangeEvent")
            entities = change.get("entities") if isinstance(change, dict) else None
            if not isinstance(entities, list):
                raise SourceContractError("invalid QBO changed entities")
            for item in entities:
                if not isinstance(item, dict):
                    raise SourceContractError("invalid QBO changed entity")
                entity_id = required_text(item.get("id"), "entity id")
                name = required_text(item.get("name"), "entity type")
                operation = required_text(item.get("operation"), "operation")
                timestamp = _event_time(item.get("lastUpdated") or item.get("lastUpdatedDate"), require_zone=True)
                legacy_key = canonical_digest({"account": account, "entity_id": entity_id,
                                               "entity_type": name, "operation": operation, "time": timestamp})
                result.append(SourceEvent("qbo", f"legacy:{legacy_key}", account, entity_id,
                                          name, operation, timestamp, delivery_digest))
    else:
        raise SourceContractError("invalid QBO delivery shape")
    if len(result) > MAX_EVENTS:
        raise SourceContractError("too many source events")
    return tuple(result)


def normalize_xero_events(payload: Any, *, delivery_digest: str) -> tuple[SourceEvent, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise SourceContractError("invalid Xero event delivery")
    result = []
    for item in payload["events"]:
        if not isinstance(item, dict) or item.get("tenantType") != "ORGANISATION":
            raise SourceContractError("unsupported Xero tenant event")
        category = required_text(item.get("eventCategory"), "event category")
        # These documented categories are change signals, never Journals coverage.
        if category not in {"INVOICE", "CONTACT", "CREDITNOTE", "SUBSCRIPTION", "PREPAYMENT"}:
            raise SourceContractError("unadmitted Xero event category")
        account = required_text(item.get("tenantId"), "tenant id")
        entity = required_text(item.get("resourceId"), "resource id")
        operation = required_text(item.get("eventType"), "event type")
        timestamp = _event_time(item.get("eventDateUtc"))
        key = canonical_digest({"account": account, "entity": entity, "category": category,
                                "operation": operation, "time": timestamp,
                                "first_sequence": payload.get("firstEventSequence"),
                                "last_sequence": payload.get("lastEventSequence")})
        result.append(SourceEvent("xero", key, account, entity, category, operation, timestamp, delivery_digest))
        if len(result) > MAX_EVENTS:
            raise SourceContractError("too many source events")
    return tuple(result)


def parse_verified_delivery(*, provider: str, raw_body: bytes, signature: str,
                            verifier_secret: str) -> tuple[SourceEvent, ...]:
    verify_raw_signature(raw_body, signature, verifier_secret)
    payload = strict_json_loads(raw_body, maximum_bytes=MAX_DELIVERY_BYTES, maximum_depth=32)
    raw_digest = hashlib.sha256(raw_body).hexdigest()
    if provider == "qbo":
        return normalize_qbo_events(payload, delivery_digest=raw_digest)
    if provider == "xero":
        return normalize_xero_events(payload, delivery_digest=raw_digest)
    raise SourceContractError("no admitted signed-delivery contract for this provider")


def _invalidate_book(db: Session, connection: AccountingSourceConnection,
                     book: AccountingBook, event: SourceEvent, event_id: int,
                     deadline: float | None) -> int:
    # The connection row is already locked, so publication and first-head creation
    # cannot miss this invalidation. Unknown posting period invalidates all heads.
    _admit_deadline(db, deadline)
    heads = source_query(db, AccountingSourceHead).filter(
        AccountingSourceHead.connection_id == connection.id,
        AccountingSourceHead.book_id == book.id,
    ).order_by(AccountingSourceHead.period, AccountingSourceHead.id).with_for_update().all()
    prior = source_query(db, AccountingSourceInvalidation).with_entities(func.max(AccountingSourceInvalidation.epoch)).filter(
        AccountingSourceInvalidation.connection_id == connection.id,
    ).scalar() or 1
    epoch = max([prior, *(head.epoch for head in heads)]) + 1
    payload = {"organization_id": connection.organization_id, "company_id": connection.organization_company_id,
               "connection_id": connection.id, "book_id": book.id, "period": "*", "dataset": "*",
               "epoch": epoch, "reason": "source_event_unknown_posting_period", "source_event_id": event_id}
    db.add(AccountingSourceInvalidation(
        organization_id=connection.organization_id, organization_company_id=connection.organization_company_id,
        connection_id=connection.id, book_id=book.id, epoch=epoch,
        period="*", dataset="*", source_event_id=event_id,
        reason=payload["reason"], content_digest=canonical_digest(payload),
        producer_version=EVENT_PRODUCER,
    ))
    for head in heads:
        update_source_row(db, head, {"epoch": epoch, "current_snapshot_id": None,
            "lease_run_id": None, "lease_expires_at": None,
            "lease_generation": (head.lease_generation or 0) + 1,
            "qualified_checkpoint_json": {}, "revision": (head.revision or 0) + 1})
    return epoch


def persist_verified_delivery(
    db: Session, *, provider: str, raw_body: bytes, signature: str,
    verifier_secret: str, routes: tuple[EventRoute, ...],
    deadline: float | None = None,
) -> DeliveryResult:
    """Append delivery, invalidation and sealed outbox before caller's ACK commit.

    This function never commits a partially durable success. The HTTP owner must
    commit successfully before acknowledging, and must return failure if commit
    fails. Unknown routes are returned explicitly and never become qualification.
    """
    if type(routes) is not tuple or len(routes) > MAX_ROUTES or any(type(route) is not EventRoute for route in routes):
        raise SourceContractError("bounded preauthorized source routes required")
    _admit_deadline(db, deadline)
    events = parse_verified_delivery(provider=provider, raw_body=raw_body,
                                    signature=signature, verifier_secret=verifier_secret)
    selected_route_pairs = sum(sum(event.account_id == route.external_entity_id and event.provider == route.provider
                                   for event in events) for route in routes)
    if selected_route_pairs > MAX_EVENTS:
        raise SourceContractError("source event-to-book expansion exceeds the delivery bound")
    route_identities: dict[int, tuple] = {}
    book_identities: dict[int, tuple] = {}
    for route in routes:
        connection_identity = (route.organization_id, route.company_id, route.provider, route.environment,
                               route.external_account_id, route.external_entity_id)
        book_identity = (route.organization_id, route.company_id, route.connection_id)
        if route.connection_id in route_identities and route_identities[route.connection_id] != connection_identity:
            raise SourceContractError("source connection has conflicting route ownership")
        if route.book_id in book_identities and book_identities[route.book_id] != book_identity:
            raise SourceContractError("source book has conflicting route ownership")
        route_identities[route.connection_id], book_identities[route.book_id] = connection_identity, book_identity
    persisted, duplicates, routed_keys = 0, 0, set()
    outbox_ids = []
    from .connections import mark_source_storage_initialized
    for organization_id, company_id in sorted({(route.organization_id, route.company_id) for route in routes}):
        _admit_deadline(db, deadline)
        with tenant_scope_live(db, org_id=organization_id, company_id=company_id, realm_id=None, bypass=False):
            _admit_deadline(db, deadline)
            mark_source_storage_initialized(db, organization_id=organization_id, company_id=company_id)
            _admit_deadline(db, deadline)
    # Lock every existing connection first, then every book; only afterwards may
    # any invalidation lock a period head. Cross-company deliveries are metadata
    # only and each read/flush retains its own exact non-bypass company pin.
    connections: dict[int, AccountingSourceConnection] = {}
    books: dict[int, AccountingBook] = {}
    for route in sorted(routes, key=lambda item: item.connection_id):
        _admit_deadline(db, deadline)
        if route.connection_id in connections:
            continue
        with tenant_scope_live(db, org_id=route.organization_id, company_id=route.company_id, realm_id=None, bypass=False):
            _admit_deadline(db, deadline)
            connection = source_query(db, AccountingSourceConnection).filter(
                AccountingSourceConnection.id == route.connection_id,
                AccountingSourceConnection.organization_id == route.organization_id,
                AccountingSourceConnection.organization_company_id == route.company_id,
            ).populate_existing().with_for_update().one_or_none()
            if connection is None or connection.status != "active" or (
                connection.provider, connection.environment, connection.external_account_id, connection.external_entity_id,
            ) != (route.provider, route.environment, route.external_account_id, route.external_entity_id):
                raise SourceContractError("source delivery route was revoked or rebound")
            connections[route.connection_id] = connection
    for route in sorted(routes, key=lambda item: item.book_id):
        _admit_deadline(db, deadline)
        if route.book_id in books:
            continue
        with tenant_scope_live(db, org_id=route.organization_id, company_id=route.company_id, realm_id=None, bypass=False):
            book = source_query(db, AccountingBook).filter(
                AccountingBook.id == route.book_id, AccountingBook.connection_id == route.connection_id,
                AccountingBook.organization_id == route.organization_id,
                AccountingBook.organization_company_id == route.company_id,
            ).populate_existing().with_for_update().one_or_none()
            if book is None or book.status != "active":
                raise SourceContractError("source delivery book is unavailable")
            books[route.book_id] = book
    for route in sorted(routes, key=lambda item: (item.connection_id, item.book_id)):
        _admit_deadline(db, deadline)
        selected = [event for event in events if event.provider == route.provider == provider
                    and event.account_id == route.external_entity_id]
        if not selected:
            continue
        with tenant_scope_live(db, org_id=route.organization_id, company_id=route.company_id,
                               realm_id=None, bypass=False):
            connection, book = connections[route.connection_id], books[route.book_id]
            for event in selected:
                _admit_deadline(db, deadline)
                payload = {key: getattr(event, key) for key in SourceEvent.__dataclass_fields__}
                existing = source_query(db, AccountingSourceEvent).filter(
                    AccountingSourceEvent.connection_id == connection.id,
                    AccountingSourceEvent.book_id == book.id,
                    AccountingSourceEvent.provider_event_id == event.provider_event_id,
                ).one_or_none()
                routed_keys.add((event.provider_event_id, event.account_id))
                if existing is not None:
                    # A provider event may be redelivered in a differently batched
                    # raw body; compare normalized semantic fields, not batch hash.
                    old = dict(existing.payload_json)
                    new = dict(payload)
                    old.pop("delivery_digest", None)
                    new.pop("delivery_digest", None)
                    if canonical_digest(old) != canonical_digest(new):
                        raise SourceContractError("source event identity changed payload")
                    duplicates += 1
                    continue
                row = AccountingSourceEvent(
                    organization_id=route.organization_id, organization_company_id=route.company_id,
                    connection_id=connection.id, book_id=book.id,
                    provider_event_id=event.provider_event_id,
                    entity_type=event.entity_type, entity_id=event.entity_id, operation=event.operation,
                    event_time=datetime.fromisoformat(event.event_time), payload_json=payload,
                    content_digest=canonical_digest(payload), producer_version=EVENT_PRODUCER,
                )
                db.add(row)
                _admit_deadline(db, deadline)
                db.flush()
                _admit_deadline(db, deadline)
                epoch = _invalidate_book(db, connection, book, event, row.id, deadline)
                now = datetime.now(timezone.utc)
                payload = {"event_id": row.id, "organization_id": route.organization_id,
                           "company_id": route.company_id, "connection_id": connection.id,
                           "book_id": book.id, "epoch": epoch, "operation": "invalidate"}
                outbox = AccountingSourceOutbox(
                    organization_id=route.organization_id, organization_company_id=route.company_id,
                    connection_id=connection.id, book_id=book.id, run_id=None, event_id=row.id,
                    operation="invalidate", status="pending", task_id=secrets.token_hex(24),
                    payload_encrypted=encrypt_secret(canonical_json(payload)), available_at=now, attempts=0,
                )
                db.add(outbox)
                _admit_deadline(db, deadline)
                db.flush()
                _admit_deadline(db, deadline)
                outbox_ids.append(outbox.id)
                persisted += 1
    unrouted = sum((event.provider_event_id, event.account_id) not in routed_keys for event in events)
    _admit_deadline(db, deadline)
    return DeliveryResult(persisted, duplicates, unrouted, tuple(outbox_ids))

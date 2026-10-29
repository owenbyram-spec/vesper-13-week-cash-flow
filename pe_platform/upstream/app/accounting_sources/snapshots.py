"""Retained, fenced source staging; a sealed snapshot is not qualification.

These functions are application-service internals, never request-body receipt
deserializers. The dispatch verifier reloads retained admission on every write.
The caller owns the transaction: staging, qualification and outbox decisions
must commit atomically, and failures must roll back that transaction.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (AccountingBook, AccountingSourceConnection,
                      AccountingSourceHead, AccountingSourceInvalidation,
                      AccountingSourcePage, AccountingSourceRecord,
                      AccountingSourceRun, AccountingSourceSnapshot)
from ..services.crypto import decrypt_secret, encrypt_secret
from .contracts import (DATASETS, ParsedPage, SourceScope, canonical_json,
                        canonical_data, canonical_digest)

SCHEMA_VERSION = "accounting-source/v1"
PRODUCER_VERSION = "source-snapshot/v1"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ROWS = 100_000
MAX_PAGES = 1000
MAX_PARTITIONS = 32


class SourceStateError(ValueError):
    """Unqualified or stale source state; never successful emptiness."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    # SQLite round-trips timezone-aware values as naive UTC. Production PG uses
    # timestamptz; normalization never accepts a request-chosen evaluation time.
    if not isinstance(value, datetime):
        raise SourceStateError("retained timestamp is missing")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def owner_filter(model: Any, scope: SourceScope) -> tuple[Any, ...]:
    return (model.organization_id == scope.organization_id,
            model.organization_company_id == scope.company_id,
            model.connection_id == scope.connection_id,
            model.book_id == scope.book_id)


def source_scope_from_json(value: dict[str, Any]) -> SourceScope:
    from datetime import date
    if type(value) is not dict:
        raise SourceStateError("retained source scope is missing")
    required = {"organization_id", "company_id", "connection_id", "book_id", "provider",
                "environment", "external_account_id", "external_entity_id", "external_book_id",
                "basis", "currency", "period_start", "period_end", "review_period"}
    if set(value) != required:
        raise SourceStateError("retained source scope schema mismatch")
    data = dict(value)
    for name in ("period_start", "period_end"):
        if type(data[name]) is not str:
            raise SourceStateError("retained window date is missing")
        data[name] = date.fromisoformat(data[name])
    return SourceScope(**data)


def lock_current_source(db: Session, scope: SourceScope, dataset: str, *,
                        create_head: bool = False, expected_epoch: int | None = None,
                        expires_at: datetime | None = None):
    """Lock connection → book → head before run; never lock immutable rows.

    Invalidation/reconnect use this same existing connection lock even when no
    period head exists. Its retained invalidations determine first-head epochs,
    so a disconnect cannot race a newly created period into a stale epoch.
    """
    if type(scope) is not SourceScope or not scope.review_period or dataset not in DATASETS:
        raise SourceStateError("explicit source review period and dataset required")
    from .connections import admit_source_waits
    deadline = expires_at if expires_at is not None else utc_now() + timedelta(seconds=30)
    admit_source_waits(db, deadline)
    connection = (db.query(AccountingSourceConnection).filter(
        AccountingSourceConnection.id == scope.connection_id,
        AccountingSourceConnection.organization_id == scope.organization_id,
        AccountingSourceConnection.organization_company_id == scope.company_id,
    ).populate_existing().with_for_update().one_or_none())
    if connection is None:
        raise SourceStateError("source connection unavailable")
    admit_source_waits(db, deadline)
    book = (db.query(AccountingBook).filter(
        AccountingBook.id == scope.book_id, AccountingBook.connection_id == connection.id,
        AccountingBook.organization_id == scope.organization_id,
        AccountingBook.organization_company_id == scope.company_id,
    ).populate_existing().with_for_update().one_or_none())
    if book is None:
        raise SourceStateError("source book unavailable")
    actual = (connection.provider, connection.environment, connection.external_account_id,
              connection.external_entity_id, book.external_book_id or "", book.accounting_basis, book.currency)
    expected = (scope.provider, scope.environment, scope.external_account_id,
                scope.external_entity_id, scope.external_book_id, scope.basis, scope.currency)
    if actual != expected or connection.status != "active" or book.status != "active":
        raise SourceStateError("source identity or availability changed")
    admit_source_waits(db, deadline)
    head = (db.query(AccountingSourceHead).filter(*owner_filter(AccountingSourceHead, scope),
        AccountingSourceHead.period == scope.review_period, AccountingSourceHead.dataset == dataset,
    ).populate_existing().with_for_update().one_or_none())
    if head is None and create_head:
        admit_source_waits(db, deadline)
        last_epoch = db.query(func.max(AccountingSourceInvalidation.epoch)).filter(
            AccountingSourceInvalidation.organization_id == scope.organization_id,
            AccountingSourceInvalidation.organization_company_id == scope.company_id,
            AccountingSourceInvalidation.connection_id == scope.connection_id,
        ).scalar()
        head = AccountingSourceHead(
            organization_id=scope.organization_id, organization_company_id=scope.company_id,
            connection_id=scope.connection_id, book_id=scope.book_id,
            period=scope.review_period, dataset=dataset, epoch=max(1, last_epoch or 1),
            lease_generation=0, revision=0, checkpoint_json={}, qualified_checkpoint_json={},
            schema_version=SCHEMA_VERSION, producer_version=PRODUCER_VERSION,
        )
        db.add(head)
        admit_source_waits(db, deadline)
        db.flush()
    if head is not None and expected_epoch is not None and head.epoch != expected_epoch:
        raise SourceStateError("source epoch changed")
    return connection, book, head


def _verified_run(db: Session, run_id: int, dispatch_permit: str):
    # Lazy import avoids a connections -> lock helper -> connections cycle.
    from .connections import verify_source_run
    run = verify_source_run(db, run_id=run_id, dispatch_permit=dispatch_permit, for_write=True)
    scope = source_scope_from_json(run.scope_json)
    if run.scope_digest != canonical_digest(scope):
        raise SourceStateError("retained run scope digest mismatch")
    return run, scope


def admit_retained_work(db: Session, expires_at: datetime) -> None:
    """Reuse one server deadline for DB waits and post-calculation admission."""
    from .connections import admit_source_waits
    admit_source_waits(db, expires_at)


def read_page_bytes(page: AccountingSourcePage) -> bytes:
    """Required evidence decrypts and hashes, or fails; metadata is insufficient."""
    try:
        decoded = decrypt_secret(page.payload_encrypted)
        if not isinstance(decoded, str):
            raise ValueError("missing encrypted payload")
        raw = decoded.encode("utf-8", errors="strict")
    except Exception as exc:
        raise SourceStateError("required retained source payload unavailable") from exc
    if len(raw) != page.byte_count or hashlib.sha256(raw).hexdigest() != page.raw_digest:
        raise SourceStateError("retained source payload digest mismatch")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SourceStateError("retained source payload exceeds bound")
    return raw


def stage_page(db: Session, *, run_id: int, dispatch_permit: str, dataset: str,
               partition_key: str, cursor: str | None, raw: bytes, page: ParsedPage,
               observed_start: datetime, observed_end: datetime) -> AccountingSourcePage:
    """Append a raw response and its exact records after closed parser replay.

    ``page`` is a typed adapter result, but it is rederived from retained bytes
    rather than trusting an asserted amount, gap, pagination or origin field.
    """
    run, scope = _verified_run(db, run_id, dispatch_permit)
    if dataset != run.dataset or type(raw) is not bytes or not partition_key or len(partition_key) > 128:
        raise SourceStateError("source page does not match admitted extraction")
    if len(raw) > MAX_RESPONSE_BYTES or type(page) is not ParsedPage:
        raise SourceStateError("invalid source response envelope")
    if len(page.contract_id) > 128 or any(len(line.source_version) > 128 for line in page.records):
        raise SourceStateError("source contract/version exceeds retained schema")
    start, end = aware(observed_start), aware(observed_end)
    now = utc_now()
    if start > end or end > now or start < aware(run.evaluation_at):
        raise SourceStateError("invalid source observation interval")
    from .receipts import replay_source_page
    replay = replay_source_page(scope, dataset, page.contract_id, raw, cursor=cursor)
    from .receipts import SYNTHETIC_CONTRACT, assert_synthetic_admission
    if page.contract_id == SYNTHETIC_CONTRACT:
        assert_synthetic_admission(run, scope)
    if canonical_json(page) != canonical_json(replay):
        raise SourceStateError("adapter page differs from retained raw replay")
    admit_retained_work(db, run.permit_expires_at)
    existing = db.query(AccountingSourcePage).filter(*owner_filter(AccountingSourcePage, scope),
        AccountingSourcePage.run_id == run.id).order_by(AccountingSourcePage.ordinal).all()
    repeated = [p for p in existing if p.dataset == dataset and p.partition_key == partition_key and p.cursor == cursor]
    if repeated:
        if (len(repeated) != 1 or read_page_bytes(repeated[0]) != raw
            or repeated[0].contract_id != page.contract_id or repeated[0].next_cursor != page.next_cursor
            or repeated[0].terminal != page.terminal):
            raise SourceStateError("source page replay conflict")
        # Retain original observation/issuance clocks after commit-before-ACK.
        retained_inventory(db, run, scope, require_complete=False, expires_at=run.permit_expires_at)
        return repeated[0]
    if run.status == "qualified":
        raise SourceStateError("qualified source inventory is sealed")
    if len(existing) >= MAX_PAGES or sum(p.byte_count for p in existing) + len(raw) > MAX_SOURCE_BYTES:
        raise SourceStateError("source extraction page/byte limit")
    partitions = {p.partition_key for p in existing} | {partition_key}
    if len(partitions) > MAX_PARTITIONS:
        raise SourceStateError("source extraction partition limit")
    prior = [p for p in existing if p.partition_key == partition_key and p.dataset == dataset]
    if not prior and cursor is not None:
        # Every admitted partition begins at its reviewed initial request. A
        # worker cannot start halfway through a feed and call the tail complete.
        raise SourceStateError("initial source page cursor must be absent")
    if prior and (prior[-1].terminal or prior[-1].next_cursor != cursor):
        raise SourceStateError("source pagination did not progress")
    if page.next_cursor is not None and (page.next_cursor == cursor or any(p.cursor == page.next_cursor for p in prior)):
        raise SourceStateError("repeated source cursor")
    if (page.terminal and page.next_cursor is not None) or (not page.terminal and page.next_cursor is None):
        raise SourceStateError("ambiguous terminal source page")
    row_count = db.query(func.count(AccountingSourceRecord.id)).filter(
        *owner_filter(AccountingSourceRecord, scope), AccountingSourceRecord.run_id == run.id).scalar() or 0
    if row_count + len(page.records) > MAX_ROWS:
        raise SourceStateError("source extraction row limit")
    payload = {"dataset": dataset, "partition_key": partition_key, "cursor": cursor,
               "next_cursor": page.next_cursor, "terminal": page.terminal,
               "contract_id": page.contract_id, "raw_sha256": page.raw_sha256,
               "gaps": canonical_data(page.gaps), "reported_count": page.reported_count,
               "observed_start": start.isoformat(), "observed_end": end.isoformat()}
    item = AccountingSourcePage(
        organization_id=scope.organization_id, organization_company_id=scope.company_id,
        connection_id=scope.connection_id, book_id=scope.book_id, run_id=run.id,
        ordinal=len(existing) + 1, dataset=dataset, partition_key=partition_key,
        cursor=cursor, next_cursor=page.next_cursor, terminal=page.terminal,
        raw_digest=hashlib.sha256(raw).hexdigest(), byte_count=len(raw),
        payload_encrypted=encrypt_secret(raw.decode("utf-8", errors="strict")),
        contract_id=page.contract_id, page_json=payload, content_digest=canonical_digest(payload),
        schema_version=SCHEMA_VERSION, producer_version=PRODUCER_VERSION, created_at=now,
    )
    db.add(item)
    admit_retained_work(db, run.permit_expires_at)
    db.flush()
    for line in page.records:
        content = canonical_data(line)
        db.add(AccountingSourceRecord(
            organization_id=scope.organization_id, organization_company_id=scope.company_id,
            connection_id=scope.connection_id, book_id=scope.book_id, run_id=run.id,
            page_id=item.id, dataset=dataset, occurrence=line.occurrence, source_id=line.source_id,
            source_version=line.source_version, content_digest=canonical_digest(line), record_json=content,
            schema_version=SCHEMA_VERSION, producer_version=PRODUCER_VERSION, created_at=now,
        ))
    admit_retained_work(db, run.permit_expires_at)
    db.flush()
    head = db.query(AccountingSourceHead).filter(*owner_filter(AccountingSourceHead, scope),
        AccountingSourceHead.period == scope.review_period, AccountingSourceHead.dataset == dataset).one()
    from .connections import update_source_row
    update_source_row(db, head, {"checkpoint_json": {
        "run_id": run.id, "page_id": item.id, "cursor": page.next_cursor,
        "terminal": page.terminal, "epoch": run.epoch}})
    return item


def retained_inventory(db: Session, run: AccountingSourceRun, scope: SourceScope, *,
                       require_complete: bool = True, expires_at: datetime | None = None):
    deadline = expires_at if expires_at is not None else utc_now() + timedelta(seconds=30)
    admit_retained_work(db, deadline)
    pages = db.query(AccountingSourcePage).filter(*owner_filter(AccountingSourcePage, scope),
        AccountingSourcePage.run_id == run.id).order_by(AccountingSourcePage.ordinal).all()
    admit_retained_work(db, deadline)
    records = db.query(AccountingSourceRecord).filter(*owner_filter(AccountingSourceRecord, scope),
        AccountingSourceRecord.run_id == run.id).order_by(AccountingSourceRecord.id).all()
    if not pages or len(pages) > MAX_PAGES or len(records) > MAX_ROWS:
        raise SourceStateError("missing or excessive retained source inventory")
    if [p.ordinal for p in pages] != list(range(1, len(pages) + 1)):
        raise SourceStateError("source page inventory is incomplete")
    by_partition = defaultdict(list)
    expected_records = []
    from .receipts import replay_source_page
    for page in pages:
        admit_retained_work(db, deadline)
        if (page.content_digest != canonical_digest(page.page_json) or page.schema_version != SCHEMA_VERSION
            or page.producer_version != PRODUCER_VERSION or page.dataset != run.dataset
            or page.page_json.get("raw_sha256") != page.raw_digest):
            raise SourceStateError("retained page metadata mismatch")
        if any(page.page_json.get(k) != getattr(page, k) for k in
               ("dataset", "partition_key", "cursor", "next_cursor", "terminal", "contract_id")):
            raise SourceStateError("retained page columns disagree")
        start = datetime.fromisoformat(page.page_json["observed_start"])
        end = datetime.fromisoformat(page.page_json["observed_end"])
        if (start.tzinfo is None or end.tzinfo is None or start > end
            or start < aware(run.evaluation_at) or end > aware(page.created_at)):
            raise SourceStateError("retained source observation interval invalid")
        replay = replay_source_page(scope, page.dataset, page.contract_id, read_page_bytes(page), cursor=page.cursor)
        if (replay.raw_sha256 != page.raw_digest or replay.terminal != page.terminal
            or replay.next_cursor != page.next_cursor or canonical_data(replay.gaps) != page.page_json.get("gaps")
            or replay.reported_count != page.page_json.get("reported_count")):
            raise SourceStateError("retained source replay mismatch")
        by_partition[(page.dataset, page.partition_key)].append(page)
        expected_records.extend((page.id, line.occurrence, canonical_digest(line)) for line in replay.records)
    actual = [(r.page_id, r.occurrence, r.content_digest) for r in records]
    if sorted(expected_records) != sorted(actual) or any(
        r.content_digest != canonical_digest(r.record_json) or r.schema_version != SCHEMA_VERSION
        or r.producer_version != PRODUCER_VERSION or r.dataset != run.dataset
        or r.source_id != r.record_json.get("source_id")
        or r.source_version != r.record_json.get("source_version")
        or r.occurrence != r.record_json.get("occurrence") for r in records):
        raise SourceStateError("retained record inventory mismatch")
    if len({(r.dataset, r.occurrence) for r in records}) != len(records):
        raise SourceStateError("duplicate source occurrence")
    if len(by_partition) > MAX_PARTITIONS or sum(p.byte_count for p in pages) > MAX_SOURCE_BYTES:
        raise SourceStateError("retained source bounds exceeded")
    for partition in by_partition.values():
        if partition[0].cursor is not None or len({p.cursor for p in partition}) != len(partition):
            raise SourceStateError("source initial page or cursor inventory is invalid")
        if (require_complete and not partition[-1].terminal) or any(p.terminal for p in partition[:-1]):
            raise SourceStateError("source pagination is incomplete")
        if any(a.next_cursor != b.cursor for a, b in zip(partition, partition[1:])):
            raise SourceStateError("source page chain is broken")
    admit_retained_work(db, deadline)
    return pages, records


def seal_snapshot(db: Session, *, run_id: int, dispatch_permit: str,
                  idempotency_key: str) -> AccountingSourceSnapshot:
    """Seal verified inventory without moving the qualified source head."""
    run, scope = _verified_run(db, run_id, dispatch_permit)
    pages, records = retained_inventory(db, run, scope, expires_at=run.permit_expires_at)
    if not idempotency_key or len(idempotency_key) > 128:
        raise SourceStateError("canonical snapshot idempotency key required")
    manifest = {
        "scope": canonical_data(scope), "run_id": run.id, "epoch": run.epoch,
        "lease_generation": run.lease_generation, "dataset": run.dataset,
        "evaluation_at": aware(run.evaluation_at).isoformat(),
        "pages": [{"id": p.id, "digest": p.content_digest, "raw_digest": p.raw_digest} for p in pages],
        "records": [{"id": r.id, "digest": r.content_digest} for r in records],
        "record_count": len(records), "observed_start": min(p.page_json["observed_start"] for p in pages),
        "observed_end": max(p.page_json["observed_end"] for p in pages),
        "schema_version": SCHEMA_VERSION, "producer_version": PRODUCER_VERSION,
    }
    digest = canonical_digest(manifest)
    existing = db.query(AccountingSourceSnapshot).filter(*owner_filter(AccountingSourceSnapshot, scope),
        AccountingSourceSnapshot.idempotency_key == idempotency_key).one_or_none()
    if existing is not None:
        if existing.content_digest != digest:
            raise SourceStateError("snapshot idempotency conflict")
        return existing
    max_age = run.policy_json.get("qualification_max_age_seconds", 86400)
    if type(max_age) is not int or not 1 <= max_age <= 86400:
        raise SourceStateError("invalid retained snapshot freshness policy")
    expires = aware(run.evaluation_at) + timedelta(seconds=max_age)
    if expires <= utc_now():
        raise SourceStateError("source snapshot already expired")
    snapshot = AccountingSourceSnapshot(
        organization_id=scope.organization_id, organization_company_id=scope.company_id,
        connection_id=scope.connection_id, book_id=scope.book_id, run_id=run.id,
        period=scope.review_period, epoch=run.epoch, dataset=run.dataset, manifest_json=manifest,
        content_digest=digest, issued_at=utc_now(), expires_at=expires,
        idempotency_key=idempotency_key, schema_version=SCHEMA_VERSION, producer_version=PRODUCER_VERSION,
    )
    db.add(snapshot)
    admit_retained_work(db, run.permit_expires_at)
    db.flush()
    return snapshot

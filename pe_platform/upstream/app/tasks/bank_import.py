"""Durable, tenant-pinned Celery orchestration for bank-parser v2.

Logical chunk identity and delivery ordinals live in Postgres; Redis carries only
their IDs.  The worker uses a 300-second random-token lease with 60-second
heartbeats, treats every broker receipt as at-least-once, and commits a result only
when C2-b's persistence seam revalidates the matching token.  The registered legacy
``parse_statement_import_task(import_id)`` name remains solely as a payload upgrade
bridge into these same v2 descriptors—there is no whole-document provider fallback.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
import threading
from typing import TypedDict
from uuid import UUID, uuid4

from celery.exceptions import SoftTimeLimitExceeded
from kombu.exceptions import EncodeError, OperationalError as BrokerOperationalError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError as DatabaseOperationalError
from sqlalchemy.orm import Session

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import (
    BankStatementImport,
    BankStatementParseCandidate,
    BankStatementParseChunk,
    BankStatementParseDeliveryAttempt,
    BankStatementParseProviderAttempt,
    LLMUsageEvent,
)
from ..services.bank_recon import ingest
from ..services.bank_recon.parser_types import (
    CandidateResolutionAuthority,
    CandidateResolutionEvidence,
    CHUNK_TRANSITIONS,
    DELIVERY_TRANSITIONS,
    IMPORT_TRANSITIONS,
    ChunkLifecycleStatus,
    DeliveryLifecycleStatus,
    ImportLifecycleStatus,
    ParseRoute,
)
from ..services.llm_task_context import ProviderInvocationTrace, bind_llm_task_context
from ..services.redaction import redact_to_string
from ..services.tenant_companies import (
    bind_tenant_tag_for_realm,
    organization_company_for_realm,
    organization_for_company,
)
from ..services.tenant_tag import bind_tenant_tag
from ..tenant_rls import tenant_scope, tenant_scope_for_realm, tenant_scope_live

logger = logging.getLogger(__name__)


class ImportParseSummary(TypedDict, total=False):
    """The task's JSON-serializable outcome (the Celery result backend's payload).

    ``total=False`` because the keys are OUTCOME-dependent, not optional-by-accident:
    ``status`` is always present; ``line_count`` only on a parse, ``error`` only on a
    failure, ``reason`` only on a skip.
    """

    status: str        # "queued" | "parsed" | "error" | "skipped"
    import_id: int
    line_count: int    # rows NEWLY persisted by this import (post count-aware dedupe)
    error: str         # the honest, user-facing failure message
    reason: str        # why a delivery was skipped (not_found / already_<status>)
    delivery_count: int


# Bank-parser v2 durable-delivery contract.  These values intentionally mirror the
# frozen plan/interface bytes rather than Celery defaults: a delivery may sit in
# Redis for a while, but the *logical* claim belongs to Postgres and expires here.
BANK_IMPORT_LEASE_SECONDS = 300
BANK_IMPORT_HEARTBEAT_SECONDS = 60
BANK_IMPORT_REAPER_CADENCE_SECONDS = 60
BANK_IMPORT_DISPATCH_STALE_SECONDS = 60
BANK_IMPORT_BROKER_MESSAGE_EXPIRES_SECONDS = BANK_IMPORT_LEASE_SECONDS * 2
BANK_IMPORT_TASK_SOFT_LIMIT_SECONDS = 270
BANK_IMPORT_TASK_HARD_LIMIT_SECONDS = 300

_V2_DELIVERY_TERMINAL = frozenset(
    {
        DeliveryLifecycleStatus.SUCCEEDED,
        DeliveryLifecycleStatus.FAILED,
        DeliveryLifecycleStatus.EXPIRED,
    }
)
_V2_CHUNK_TERMINAL = frozenset(
    {ChunkLifecycleStatus.SUCCEEDED, ChunkLifecycleStatus.REVIEW_REQUIRED}
)


@dataclass(frozen=True, slots=True)
class ImportDispatchReceipt:
    """Detached result of one durable outbox-dispatch pass.

    UUIDs are rendered as strings because this value crosses the FastAPI/Celery
    boundary.  ``durable_pending_count`` includes an uncertain publish: the row is
    still recoverable by the 60-second sweep and therefore is not a failed import.
    """

    import_id: int
    delivery_ids: tuple[str, ...]
    broker_task_ids: tuple[str, ...]
    created_count: int
    published_count: int
    durable_pending_count: int


@dataclass(frozen=True, slots=True)
class _ClaimedChunk:
    import_id: int
    realm_id: str
    chunk_id: UUID
    delivery_id: UUID
    lease_token: str


@dataclass(frozen=True, slots=True)
class _RetryEvidence:
    """Durable facts that decide whether one automatic retry is safe."""

    retry_permitted: bool
    provider_call_possible: bool
    provider_attempt_count: int
    reason_code: str


def _aware_utc(value: datetime | None) -> datetime | None:
    """Normalize timestamps loaded from SQLite while preserving PG-aware values."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _transition_delivery(
    delivery: BankStatementParseDeliveryAttempt,
    target: DeliveryLifecycleStatus,
) -> None:
    current = DeliveryLifecycleStatus(delivery.lifecycle_status)
    if target not in DELIVERY_TRANSITIONS[current]:
        raise ValueError(
            f"illegal bank parser delivery transition {current.value}->{target.value}"
        )
    delivery.lifecycle_status = target.value


def _transition_chunk(
    chunk: BankStatementParseChunk,
    target: ChunkLifecycleStatus,
) -> None:
    current = ChunkLifecycleStatus(chunk.lifecycle_status)
    if target not in CHUNK_TRANSITIONS[current]:
        raise ValueError(
            f"illegal bank parser chunk transition {current.value}->{target.value}"
        )
    chunk.lifecycle_status = target.value


def _transition_chunk_to_review_for_recovery(
    chunk: BankStatementParseChunk,
) -> None:
    """Reach review atomically through only edges in the frozen lifecycle graph."""
    current = ChunkLifecycleStatus(chunk.lifecycle_status)
    if current is ChunkLifecycleStatus.REVIEW_REQUIRED:
        return
    if current is ChunkLifecycleStatus.FAILED:
        _transition_chunk(chunk, ChunkLifecycleStatus.PENDING)
        current = ChunkLifecycleStatus.PENDING
    if current is ChunkLifecycleStatus.PENDING:
        _transition_chunk(chunk, ChunkLifecycleStatus.LEASED)
        current = ChunkLifecycleStatus.LEASED
    if current is not ChunkLifecycleStatus.LEASED:
        raise ValueError(
            f"cannot recover bank parser chunk from {current.value} to review"
        )
    _transition_chunk(chunk, ChunkLifecycleStatus.REVIEW_REQUIRED)


def _transition_import(
    statement_import: BankStatementImport,
    target: ImportLifecycleStatus,
) -> None:
    current = ImportLifecycleStatus(statement_import.lifecycle_status)
    if target not in IMPORT_TRANSITIONS[current]:
        raise ValueError(
            f"illegal bank parser import transition {current.value}->{target.value}"
        )
    statement_import.lifecycle_status = target.value


def _lock_delivery(
    db: Session,
    *,
    delivery_id: UUID,
    realm_id: str,
) -> BankStatementParseDeliveryAttempt | None:
    return db.execute(
        select(BankStatementParseDeliveryAttempt)
        .where(
            BankStatementParseDeliveryAttempt.id == delivery_id,
            BankStatementParseDeliveryAttempt.realm_id == realm_id,
        )
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def _lock_chunk(
    db: Session,
    *,
    chunk_id: UUID,
    import_id: int,
    realm_id: str,
) -> BankStatementParseChunk | None:
    return db.execute(
        select(BankStatementParseChunk)
        .where(
            BankStatementParseChunk.id == chunk_id,
            BankStatementParseChunk.bank_statement_import_id == import_id,
            BankStatementParseChunk.realm_id == realm_id,
        )
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def _create_delivery_locked(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
) -> BankStatementParseDeliveryAttempt:
    """Create the next append-only delivery while the logical chunk is locked."""
    if chunk.lifecycle_status != ChunkLifecycleStatus.PENDING.value:
        raise ValueError("only a pending logical chunk may receive a delivery")
    if chunk.organization_id is None or chunk.organization_company_id is None:
        raise ValueError("v2 chunk tenant identity must be complete before dispatch")

    next_ordinal = db.execute(
        select(func.coalesce(func.max(BankStatementParseDeliveryAttempt.delivery_ordinal), -1))
        .where(
            BankStatementParseDeliveryAttempt.chunk_id == chunk.id,
            # chunk_id is an indexed FK, not unique, so it is no ceiling on its own. The
            # realm conjunct must stay a sibling argument of this .where() (a TOP-LEVEL
            # AND-conjunct) - nested inside an or_/and_ it stops bounding the result set.
            # `chunk` is always loaded by a realm-filtered read (_lock_chunk / the
            # dispatch select), and the same value is written to the new row below.
            BankStatementParseDeliveryAttempt.realm_id == chunk.realm_id,
        )
    ).scalar_one() + 1
    delivery = BankStatementParseDeliveryAttempt(
        bank_statement_import_id=chunk.bank_statement_import_id,
        chunk_id=chunk.id,
        delivery_ordinal=next_ordinal,
        broker_task_id=uuid4().hex,
        lifecycle_status=DeliveryLifecycleStatus.CREATED.value,
        realm_id=chunk.realm_id,
        organization_id=chunk.organization_id,
        organization_company_id=chunk.organization_company_id,
        celery_task_id=None,
        redelivered=False,
        parser_mode="resumable_v2",
        # Compatibility telemetry columns predate the v2 token budget.  The real
        # 8k/2k limits are enforced by parser_ambiguous; this conservative text
        # envelope is evidence only and never a refusal gate.
        max_input_chars=32_000,
        timeout_seconds=BANK_IMPORT_TASK_SOFT_LIMIT_SECONDS,
        max_retries=0,
        allow_fallback=False,
        status="created",
        telemetry_status="pending",
        evidence_eligible=False,
        provider_calls_started=0,
        provider_envelope_failures=0,
    )
    db.add(delivery)
    db.flush()
    return delivery


def _ensure_delivery_locked(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
) -> tuple[BankStatementParseDeliveryAttempt, bool]:
    """Return an active durable delivery or append the next ordinal.

    A dispatched row is intentionally reused: it may represent an uncertain broker
    acknowledgement and the only safe replay is the same persisted broker task ID.
    """
    latest = db.execute(
        select(BankStatementParseDeliveryAttempt)
        .where(
            BankStatementParseDeliveryAttempt.chunk_id == chunk.id,
            BankStatementParseDeliveryAttempt.realm_id == chunk.realm_id,
        )
        .order_by(BankStatementParseDeliveryAttempt.delivery_ordinal.desc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if latest is not None and latest.lifecycle_status in {
        DeliveryLifecycleStatus.CREATED.value,
        DeliveryLifecycleStatus.DISPATCHED.value,
    }:
        return latest, False
    if (
        latest is not None
        and latest.lifecycle_status == DeliveryLifecycleStatus.STARTED.value
        and chunk.lifecycle_status == ChunkLifecycleStatus.LEASED.value
    ):
        return latest, False
    return _create_delivery_locked(db, chunk=chunk), True


def _default_delivery_publisher(
    *,
    chunk_id: UUID,
    delivery_id: UUID,
    broker_task_id: str,
) -> None:
    process_bank_import_chunk_task.apply_async(
        args=(str(chunk_id), str(delivery_id)),
        task_id=broker_task_id,
        expires=BANK_IMPORT_BROKER_MESSAGE_EXPIRES_SECONDS,
        retry=False,
    )


def _mark_publish_failed(
    db: Session,
    *,
    delivery_id: UUID,
    realm_id: str,
    error_type: str,
) -> None:
    delivery = _lock_delivery(db, delivery_id=delivery_id, realm_id=realm_id)
    if delivery is None:
        db.rollback()
        return
    current = DeliveryLifecycleStatus(delivery.lifecycle_status)
    if current in _V2_DELIVERY_TERMINAL:
        db.rollback()
        return
    if current is DeliveryLifecycleStatus.CREATED:
        _transition_delivery(delivery, DeliveryLifecycleStatus.FAILED)
    elif current is DeliveryLifecycleStatus.DISPATCHED:
        _transition_delivery(delivery, DeliveryLifecycleStatus.FAILED)
    else:
        db.rollback()
        return
    delivery.status = "publish_failed"
    delivery.completed_at = datetime.now(timezone.utc)
    logger.error(
        "[bank_import_v2] delivery_id=%s definite publish failure=%s",
        delivery_id,
        error_type,
    )
    db.commit()


def _mark_publish_acknowledged(
    db: Session,
    *,
    delivery_id: UUID,
    realm_id: str,
    acknowledged_at: datetime,
) -> None:
    """Persist broker acknowledgement unless a fast worker already advanced it."""
    delivery = _lock_delivery(db, delivery_id=delivery_id, realm_id=realm_id)
    if delivery is None:
        db.rollback()
        return
    if delivery.lifecycle_status != DeliveryLifecycleStatus.DISPATCHED.value:
        db.rollback()
        return
    delivery.status = "dispatch_acknowledged"
    # ``started_at`` is the existing broker-attempt clock.  Reset it when the
    # broker accepts the message so expiry recovery grants the message its full
    # Celery TTL even if the durable descriptor waited in CREATED for a long time.
    delivery.started_at = acknowledged_at
    db.commit()


def _publish_delivery(
    db: Session,
    *,
    delivery_id: UUID,
    realm_id: str,
    publisher: Callable[..., None] = _default_delivery_publisher,
    allow_dispatched_resend: bool = False,
    dispatch_started_at: datetime | None = None,
    acknowledged_at: datetime | None = None,
) -> str:
    """Publish one persisted descriptor; never manufacture a second broker ID.

    ``dispatched`` plus ``dispatch_initiated`` is committed *before* calling the
    broker, closing the fast-worker race. A successful publish then records
    ``dispatch_acknowledged``; only an initiated/legacy-ambiguous row is eligible
    for same-ID recovery. Known serialization failures are definite and terminal.
    """
    delivery = _lock_delivery(db, delivery_id=delivery_id, realm_id=realm_id)
    if delivery is None:
        db.rollback()
        return "not_found"
    current = DeliveryLifecycleStatus(delivery.lifecycle_status)
    if current in _V2_DELIVERY_TERMINAL or current is DeliveryLifecycleStatus.STARTED:
        db.rollback()
        return "terminal_or_started"
    # ``commit`` and ``rollback`` expire ORM state.  The broker boundary must use
    # the exact descriptor scalars captured while the tenant-pinned transaction is
    # still live; reloading ``delivery`` afterward would create an unnecessary RLS
    # read and can fail closed if the caller's transaction scope has ended.
    descriptor_chunk_id = delivery.chunk_id
    descriptor_delivery_id = delivery.id
    descriptor_broker_task_id = delivery.broker_task_id
    publish_started_at = dispatch_started_at or datetime.now(timezone.utc)
    if current is DeliveryLifecycleStatus.CREATED:
        _transition_delivery(delivery, DeliveryLifecycleStatus.DISPATCHED)
        delivery.status = "dispatch_initiated"
        delivery.celery_task_id = delivery.broker_task_id
        delivery.started_at = publish_started_at
        db.commit()
    elif not allow_dispatched_resend:
        db.rollback()
        return "already_dispatched"
    elif delivery.status == "dispatch_acknowledged":
        db.rollback()
        return "already_acknowledged"
    else:
        # Throttle same-ID uncertain recovery from the actual resend attempt,
        # rather than repeatedly selecting an old descriptor every beat.
        delivery.status = "dispatch_initiated"
        delivery.started_at = publish_started_at
        db.commit()

    try:
        publisher(
            chunk_id=descriptor_chunk_id,
            delivery_id=descriptor_delivery_id,
            broker_task_id=descriptor_broker_task_id,
        )
    except (EncodeError, TypeError, ValueError) as exc:
        _mark_publish_failed(
            db,
            delivery_id=descriptor_delivery_id,
            realm_id=realm_id,
            error_type=type(exc).__name__,
        )
        return "definite_failure"
    except (BrokerOperationalError, ConnectionError, TimeoutError):
        # The broker may have accepted the frame before the connection failed.
        # Preserve DISPATCHED and let the outbox resend this exact broker ID.
        logger.warning(
            "[bank_import_v2] delivery_id=%s publish outcome uncertain; "
            "durable same-ID recovery remains scheduled",
            descriptor_delivery_id,
        )
        return "uncertain"
    except Exception as exc:  # noqa: BLE001 - unknown broker exceptions are uncertain
        logger.warning(
            "[bank_import_v2] delivery_id=%s unknown publish outcome=%s; "
            "durable same-ID recovery remains scheduled",
            descriptor_delivery_id,
            type(exc).__name__,
        )
        return "uncertain"
    try:
        _mark_publish_acknowledged(
            db,
            delivery_id=descriptor_delivery_id,
            realm_id=realm_id,
            acknowledged_at=acknowledged_at or datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 - ACK persisted outcome is uncertain
        db.rollback()
        logger.warning(
            "[bank_import_v2] delivery_id=%s broker acknowledged but marker failed=%s; "
            "durable same-ID recovery remains scheduled",
            descriptor_delivery_id,
            type(exc).__name__,
        )
        return "uncertain"
    return "published"


def dispatch_pending_import(
    db: Session,
    *,
    import_id: int,
    realm_id: str,
) -> ImportDispatchReceipt:
    """Create and publish durable deliveries for an authorized import.

    The caller MUST perform the in-handler membership gate before this first
    tenant query.  C2-b has already persisted stable logical chunk descriptors.
    This function locks those rows, commits every descriptor before any publish,
    and returns only detached scalar values so request teardown cannot lazy-load.
    """
    statement_import = db.execute(
        select(BankStatementImport)
        .where(
            BankStatementImport.id == import_id,
            BankStatementImport.realm_id == realm_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if statement_import is None:
        raise LookupError("bank statement import was not found in the authorized tenant")
    if statement_import.lifecycle_status in {
        ImportLifecycleStatus.PARSED.value,
        ImportLifecycleStatus.ERROR.value,
    }:
        return ImportDispatchReceipt(import_id, (), (), 0, 0, 0)

    existing_chunk_id = db.execute(
        select(BankStatementParseChunk.id)
        .where(
            BankStatementParseChunk.bank_statement_import_id == import_id,
            BankStatementParseChunk.realm_id == realm_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if existing_chunk_id is None:
        # Defense in depth for accepted headers created by an older API process or
        # recovered from the prepare->dispatch cutover boundary.  Current uploads
        # arrive pre-prepared from C2-b, so this path never reparses an import that
        # already owns stable descriptors.
        organization = organization_for_company(db, realm_id)
        organization_company = (
            organization_company_for_realm(
                db,
                organization_id=organization.id,
                realm_id=realm_id,
            )
            if organization is not None
            else None
        )
        if organization is None or organization_company is None:
            if statement_import.lifecycle_status in {
                ImportLifecycleStatus.QUEUED.value,
                ImportLifecycleStatus.PROCESSING.value,
            }:
                _transition_import(
                    statement_import,
                    ImportLifecycleStatus.REVIEW_REQUIRED,
                )
            statement_import.status = "error"
            statement_import.review_reason_code = "tenant_mapping_missing"
            statement_import.review_action = "retry"
            db.commit()
            return ImportDispatchReceipt(import_id, (), (), 0, 0, 0)

        from ..services.bank_recon.parser_persist import (
            SourceIdentityError,
            prepare_resumable_import,
        )

        try:
            prepare_resumable_import(
                db,
                statement_import_id=import_id,
                realm_id=realm_id,
                organization_id=organization.id,
                organization_company_id=organization_company.id,
            )
        except (SourceIdentityError, ValueError) as exc:
            db.rollback()
            statement_import = db.execute(
                select(BankStatementImport)
                .where(
                    BankStatementImport.id == import_id,
                    BankStatementImport.realm_id == realm_id,
                )
                .with_for_update()
            ).scalar_one()
            if statement_import.lifecycle_status in {
                ImportLifecycleStatus.QUEUED.value,
                ImportLifecycleStatus.PROCESSING.value,
            }:
                _transition_import(
                    statement_import,
                    ImportLifecycleStatus.REVIEW_REQUIRED,
                )
            statement_import.status = "error"
            statement_import.review_reason_code = "source_identity_invalid"
            statement_import.review_action = "reupload"
            statement_import.error_detail = (
                "The retained statement could not be verified. Please upload the "
                "statement again so it can be reviewed safely."
            )
            logger.warning(
                "[bank_import_v2] import_id=%s descriptor preparation failed: %s",
                import_id,
                redact_to_string(exc),
            )
            db.commit()
            return ImportDispatchReceipt(import_id, (), (), 0, 0, 0)

    chunks = list(
        db.execute(
            select(BankStatementParseChunk)
            .where(
                BankStatementParseChunk.bank_statement_import_id == import_id,
                BankStatementParseChunk.realm_id == realm_id,
                BankStatementParseChunk.lifecycle_status
                == ChunkLifecycleStatus.PENDING.value,
            )
            .order_by(
                BankStatementParseChunk.page_start,
                BankStatementParseChunk.span_start,
                BankStatementParseChunk.id,
            )
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    deliveries: list[BankStatementParseDeliveryAttempt] = []
    created_count = 0
    for chunk in chunks:
        delivery, created = _ensure_delivery_locked(db, chunk=chunk)
        deliveries.append(delivery)
        created_count += int(created)

    delivery_ids = tuple(str(delivery.id) for delivery in deliveries)
    broker_task_ids = tuple(str(delivery.broker_task_id) for delivery in deliveries)
    publish_keys = tuple((delivery.id, realm_id) for delivery in deliveries)
    # This commit is the outbox boundary.  No call to Celery may move above it.
    # Capture every scalar above it: expire_on_commit must not trigger an
    # unplanned reload beyond the request transaction's tenant GUC boundary.
    db.commit()
    published_count = 0
    durable_pending_count = 0
    for delivery_id, delivery_realm_id in publish_keys:
        outcome = _publish_delivery(
            db,
            delivery_id=delivery_id,
            realm_id=delivery_realm_id,
        )
        if outcome == "published":
            published_count += 1
        elif outcome in {"uncertain", "already_dispatched", "definite_failure"}:
            durable_pending_count += 1

    return ImportDispatchReceipt(
        import_id=import_id,
        delivery_ids=delivery_ids,
        broker_task_ids=broker_task_ids,
        created_count=created_count,
        published_count=published_count,
        durable_pending_count=durable_pending_count,
    )


def _delivery_realm_bootstrap(db: Session, delivery_id: UUID) -> str | None:
    """Read only the delivery's own realm inside the narrow system bootstrap."""
    with tenant_scope_live(db, bypass=True):
        realm_id = db.execute(
            select(BankStatementParseDeliveryAttempt.realm_id).where(
                BankStatementParseDeliveryAttempt.id == delivery_id
            )
        ).scalar_one_or_none()
    # Close the bypass transaction before any tenant row is read or written.
    db.rollback()
    return str(realm_id) if realm_id is not None else None


def _claim_delivery(
    db: Session,
    *,
    chunk_id: UUID,
    delivery_id: UUID,
    broker_task_id: str | None,
    realm_id: str,
    now: datetime | None = None,
) -> _ClaimedChunk | None:
    """Atomically claim one dispatched logical chunk with SKIP LOCKED."""
    claimed_at = now or datetime.now(timezone.utc)
    delivery = _lock_delivery(db, delivery_id=delivery_id, realm_id=realm_id)
    if delivery is None or delivery.chunk_id != chunk_id:
        db.rollback()
        return None
    current_delivery = DeliveryLifecycleStatus(delivery.lifecycle_status)
    if current_delivery in _V2_DELIVERY_TERMINAL:
        db.rollback()
        return None
    if broker_task_id is None or broker_task_id != delivery.broker_task_id:
        # A caller cannot substitute the persisted broker identity.  This is a
        # rejected duplicate/forgery, not a new delivery attempt.
        db.rollback()
        return None

    chunk = _lock_chunk(
        db,
        chunk_id=chunk_id,
        import_id=delivery.bank_statement_import_id,
        realm_id=realm_id,
    )
    if chunk is None:
        db.rollback()
        return None
    current_chunk = ChunkLifecycleStatus(chunk.lifecycle_status)
    if current_chunk in _V2_CHUNK_TERMINAL:
        if current_delivery is DeliveryLifecycleStatus.DISPATCHED:
            _transition_delivery(delivery, DeliveryLifecycleStatus.SUCCEEDED)
            delivery.status = "duplicate_terminal_chunk"
            delivery.completed_at = claimed_at
            db.commit()
        else:
            db.rollback()
        return None
    if current_delivery is DeliveryLifecycleStatus.STARTED:
        # A redelivery of the same message never steals a live (or stale) lease.
        # The reaper is the one authority that expires it and appends a new ordinal.
        db.rollback()
        return None
    if (
        current_delivery is not DeliveryLifecycleStatus.DISPATCHED
        or current_chunk is not ChunkLifecycleStatus.PENDING
    ):
        db.rollback()
        return None

    token = secrets.token_urlsafe(32)
    _transition_delivery(delivery, DeliveryLifecycleStatus.STARTED)
    delivery.status = "started"
    delivery.celery_task_id = broker_task_id
    _transition_chunk(chunk, ChunkLifecycleStatus.LEASED)
    chunk.status = "started"
    chunk.lease_token = token
    chunk.heartbeat_at = claimed_at
    chunk.lease_expires_at = claimed_at + timedelta(seconds=BANK_IMPORT_LEASE_SECONDS)

    statement_import = db.execute(
        select(BankStatementImport)
        .where(
            BankStatementImport.id == delivery.bank_statement_import_id,
            BankStatementImport.realm_id == realm_id,
        )
        .with_for_update()
    ).scalar_one()
    if statement_import.lifecycle_status == ImportLifecycleStatus.QUEUED.value:
        _transition_import(statement_import, ImportLifecycleStatus.PROCESSING)
    import_id = delivery.bank_statement_import_id
    db.commit()
    return _ClaimedChunk(
        import_id=import_id,
        realm_id=realm_id,
        chunk_id=chunk_id,
        delivery_id=delivery_id,
        lease_token=token,
    )


def heartbeat_chunk_lease(
    *,
    chunk_id: UUID,
    delivery_id: UUID,
    lease_token: str,
    now: datetime | None = None,
) -> bool:
    """Extend a matching live claim from a fresh, tenant-pinned SessionLocal."""
    heartbeat_at = now or datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        realm_id = _delivery_realm_bootstrap(db, delivery_id)
        if realm_id is None:
            return False
        with tenant_scope_for_realm(db, realm_id):
            matching_delivery = db.execute(
                select(BankStatementParseDeliveryAttempt.id).where(
                    BankStatementParseDeliveryAttempt.id == delivery_id,
                    BankStatementParseDeliveryAttempt.chunk_id == chunk_id,
                    BankStatementParseDeliveryAttempt.realm_id == realm_id,
                    BankStatementParseDeliveryAttempt.lifecycle_status
                    == DeliveryLifecycleStatus.STARTED.value,
                )
            ).scalar_one_or_none()
            if matching_delivery is None:
                db.rollback()
                return False
            updated = (
                db.query(BankStatementParseChunk)
                .filter(
                    BankStatementParseChunk.id == chunk_id,
                    BankStatementParseChunk.realm_id == realm_id,
                    BankStatementParseChunk.lifecycle_status
                    == ChunkLifecycleStatus.LEASED.value,
                    BankStatementParseChunk.lease_token == lease_token,
                )
                .update(
                    {
                        BankStatementParseChunk.heartbeat_at: heartbeat_at,
                        BankStatementParseChunk.lease_expires_at: heartbeat_at
                        + timedelta(seconds=BANK_IMPORT_LEASE_SECONDS),
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                db.rollback()
                return False
            db.commit()
            return True
    except Exception:
        db.rollback()
        logger.exception(
            "[bank_import_v2] chunk_id=%s delivery_id=%s heartbeat failed",
            chunk_id,
            delivery_id,
        )
        return False
    finally:
        db.close()


@contextmanager
def _lease_heartbeat(claim: _ClaimedChunk) -> Iterator[None]:
    """Keep a long provider call's DB lease live at the frozen 60-second cadence."""
    stopped = threading.Event()

    def _beat() -> None:
        while not stopped.wait(BANK_IMPORT_HEARTBEAT_SECONDS):
            if not heartbeat_chunk_lease(
                chunk_id=claim.chunk_id,
                delivery_id=claim.delivery_id,
                lease_token=claim.lease_token,
            ):
                return

    thread = threading.Thread(
        target=_beat,
        name=f"bank-import-heartbeat-{claim.chunk_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1.0)


def _outcome_value(outcome: object, field_name: str) -> object:
    if isinstance(outcome, Mapping):
        return outcome.get(field_name)
    return getattr(outcome, field_name, None)


@contextmanager
def _bind_claimed_llm_context(
    db: Session,
    *,
    claim: _ClaimedChunk,
    provider_trace: ProviderInvocationTrace,
) -> Iterator[None]:
    """Bind exact v2 delivery/chunk and tenant identity around physical calls.

    Attribution failure never blocks parsing.  It shadows any ambient tenant tag
    and makes the delivery evidence-ineligible through ``provider_trace``/the
    persisted attempt checks below; it never assigns spend to the wrong tenant.
    """
    with ExitStack() as stack:
        stack.enter_context(
            bind_llm_task_context(
                "bank_statement_parse",
                step=f"bank_statement_import:{claim.import_id}:chunk:{claim.chunk_id}",
                realm_id=claim.realm_id,
                bank_statement_import_id=claim.import_id,
                bank_statement_delivery_attempt_id=claim.delivery_id,
                bank_statement_chunk_id=claim.chunk_id,
                provider_trace=provider_trace,
            )
        )
        try:
            stack.enter_context(bind_tenant_tag_for_realm(db, claim.realm_id))
        except Exception as exc:  # noqa: BLE001 - attribution is fail-open by contract
            db.rollback()
            stack.enter_context(bind_tenant_tag(organization_id=None))
            logger.warning(
                "[bank_import_v2] import_id=%s chunk_id=%s tenant attribution "
                "failed; parsing remains enabled and evidence is ineligible: %s",
                claim.import_id,
                claim.chunk_id,
                type(exc).__name__,
            )
        yield


def _process_claimed_chunk(db: Session, claim: _ClaimedChunk) -> object:
    """Invoke C2-b's exact no-commit persistence seam."""
    from ..services.bank_recon.parser_persist import process_leased_chunk

    return process_leased_chunk(
        db,
        statement_import_id=claim.import_id,
        realm_id=claim.realm_id,
        chunk_id=claim.chunk_id,
        delivery_id=claim.delivery_id,
        lease_token=claim.lease_token,
    )


def _provider_call_possible(
    db: Session,
    *,
    import_id: int,
    chunk_id: UUID,
    realm_id: str,
) -> bool:
    """Return false only when durable candidate evidence proves a code-only chunk."""
    payloads = tuple(
        db.execute(
            select(BankStatementParseCandidate.resolution_payload).where(
                BankStatementParseCandidate.statement_import_id == import_id,
                BankStatementParseCandidate.chunk_id == chunk_id,
                BankStatementParseCandidate.realm_id == realm_id,
            )
        ).scalars()
    )
    if not payloads:
        return True
    for payload in payloads:
        try:
            evidence = CandidateResolutionEvidence.from_payload(payload or {})
        except (KeyError, TypeError, ValueError):
            return True
        if (
            evidence.parse_route is not ParseRoute.DETERMINISTIC
            or evidence.authority
            is not CandidateResolutionAuthority.DETERMINISTIC_CODE
        ):
            return True
    return False


def _is_proven_provider_timeout(
    db: Session,
    *,
    attempt: BankStatementParseProviderAttempt,
    delivery: BankStatementParseDeliveryAttempt,
    chunk: BankStatementParseChunk,
    expected_attempt_ordinal: int | None = None,
) -> bool:
    """Require a terminal, identity-complete timeout and its sole ledger row."""
    expected_ordinal = (
        int(chunk.application_retry_count or 0)
        if expected_attempt_ordinal is None
        else expected_attempt_ordinal
    )
    if (
        attempt.bank_statement_import_id != chunk.bank_statement_import_id
        or attempt.bank_statement_import_id != delivery.bank_statement_import_id
        or attempt.delivery_attempt_id != delivery.id
        or attempt.chunk_id != chunk.id
        or delivery.chunk_id != chunk.id
        or attempt.realm_id != chunk.realm_id
        or attempt.realm_id != delivery.realm_id
        or attempt.organization_id != chunk.organization_id
        or attempt.organization_id != delivery.organization_id
        or attempt.organization_company_id != chunk.organization_company_id
        or attempt.organization_company_id != delivery.organization_company_id
        or attempt.attempt_ordinal != expected_ordinal
        or attempt.lifecycle_status != "timed_out"
        or attempt.status != "error"
        or attempt.error_type != "timeout"
        or attempt.completed_at is None
        or attempt.provider_call_id != attempt.id.hex
        or not attempt.tenant_tag_succeeded
        or not attempt.recorder_succeeded
        or attempt.usage_event_id is None
        or attempt.latency_ms is None
    ):
        return False
    event = db.execute(
        select(LLMUsageEvent).where(LLMUsageEvent.id == attempt.usage_event_id)
    ).scalar_one_or_none()
    return bool(
        event is not None
        and event.task_type == "bank_statement_parse"
        and event.provider == attempt.provider
        and event.model == attempt.model
        and event.route == attempt.lane
        and event.status == "error"
        and event.error_type == "timeout"
        and event.latency_ms == attempt.latency_ms
        and event.organization_id == attempt.organization_id
        and event.organization_company_id == attempt.organization_company_id
        and event.qbo_realm_id == attempt.realm_id
    )


def _adjudicate_retry_from_durable_evidence(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
    delivery: BankStatementParseDeliveryAttempt,
) -> _RetryEvidence:
    """Decide retry eligibility without trusting an exception type or memory trace.

    A deterministic-only chunk may consume the same one-application-retry budget
    without creating a paid call.  Any chunk that could call a provider is stricter:
    its exact current delivery must own one completed timeout envelope and the chunk's
    attempt/ledger history must be cardinality-complete.  Every other possible-call
    state is review-only because another delivery could spend again.
    """
    retry_count = int(chunk.application_retry_count or 0)
    provider_call_possible = _provider_call_possible(
        db,
        import_id=chunk.bank_statement_import_id,
        chunk_id=chunk.id,
        realm_id=chunk.realm_id,
    )
    all_attempts = tuple(
        db.execute(
            select(BankStatementParseProviderAttempt)
            .where(
                BankStatementParseProviderAttempt.bank_statement_import_id
                == chunk.bank_statement_import_id,
                BankStatementParseProviderAttempt.chunk_id == chunk.id,
                BankStatementParseProviderAttempt.realm_id == chunk.realm_id,
            )
            .order_by(BankStatementParseProviderAttempt.attempt_ordinal)
        ).scalars()
    )
    current_attempts = tuple(
        attempt
        for attempt in all_attempts
        if attempt.delivery_attempt_id == delivery.id
    )
    if not provider_call_possible:
        if all_attempts:
            return _RetryEvidence(
                retry_permitted=False,
                provider_call_possible=False,
                provider_attempt_count=len(current_attempts),
                reason_code="unexpected_provider_attempt",
            )
        return _RetryEvidence(
            retry_permitted=retry_count < 1,
            provider_call_possible=False,
            provider_attempt_count=0,
            reason_code=(
                "deterministic_retry" if retry_count < 1 else "retry_budget_exhausted"
            ),
        )

    if len(current_attempts) != 1:
        return _RetryEvidence(
            retry_permitted=False,
            provider_call_possible=True,
            provider_attempt_count=len(current_attempts),
            reason_code=(
                "provider_call_state_unknown"
                if not current_attempts
                else "provider_attempt_cardinality_invalid"
            ),
        )
    expected_ordinals = set(range(retry_count + 1))
    observed_ordinals = {attempt.attempt_ordinal for attempt in all_attempts}
    usage_event_ids = [attempt.usage_event_id for attempt in all_attempts]
    if (
        len(all_attempts) != retry_count + 1
        or observed_ordinals != expected_ordinals
        or None in usage_event_ids
        or len(usage_event_ids) != len(set(usage_event_ids))
    ):
        return _RetryEvidence(
            retry_permitted=False,
            provider_call_possible=True,
            provider_attempt_count=1,
            reason_code="provider_attempt_history_incomplete",
        )

    attempt = current_attempts[0]
    if _is_proven_provider_timeout(
        db,
        attempt=attempt,
        delivery=delivery,
        chunk=chunk,
    ):
        return _RetryEvidence(
            retry_permitted=retry_count < 1,
            provider_call_possible=True,
            provider_attempt_count=1,
            reason_code=(
                "provider_timeout" if retry_count < 1 else "provider_retry_exhausted"
            ),
        )

    lifecycle = str(attempt.lifecycle_status or "")
    reason_by_lifecycle = {
        "succeeded": "provider_succeeded_result_uncommitted",
        "truncated": "provider_output_truncated",
        "failed": "provider_failed_terminal",
        "started": "provider_outcome_unknown",
        "timed_out": "provider_timeout_evidence_incomplete",
    }
    return _RetryEvidence(
        retry_permitted=False,
        provider_call_possible=True,
        provider_attempt_count=1,
        reason_code=reason_by_lifecycle.get(
            lifecycle,
            "provider_attempt_evidence_invalid",
        ),
    )


def _stage_retry_delivery_locked(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
    delivery: BankStatementParseDeliveryAttempt,
    evidence: _RetryEvidence,
    now: datetime,
) -> BankStatementParseDeliveryAttempt | None:
    """Atomically consume retry budget and append the next durable descriptor."""
    if not evidence.retry_permitted or int(chunk.application_retry_count or 0) >= 1:
        return None
    delivery_state = DeliveryLifecycleStatus(delivery.lifecycle_status)
    if delivery_state is DeliveryLifecycleStatus.STARTED:
        _transition_delivery(delivery, DeliveryLifecycleStatus.FAILED)
    elif delivery_state is not DeliveryLifecycleStatus.FAILED:
        return None
    chunk_state = ChunkLifecycleStatus(chunk.lifecycle_status)
    if chunk_state is ChunkLifecycleStatus.LEASED:
        _transition_chunk(chunk, ChunkLifecycleStatus.FAILED)
    elif chunk_state is not ChunkLifecycleStatus.FAILED:
        return None

    delivery.status = evidence.reason_code
    delivery.completed_at = delivery.completed_at or now
    delivery.provider_calls_started = max(
        int(delivery.provider_calls_started or 0),
        evidence.provider_attempt_count,
    )
    _transition_chunk(chunk, ChunkLifecycleStatus.PENDING)
    chunk.application_retry_count = int(chunk.application_retry_count or 0) + 1
    chunk.status = "started"
    chunk.lease_token = None
    chunk.lease_expires_at = None
    chunk.heartbeat_at = None
    chunk.completed_at = None
    return _create_delivery_locked(db, chunk=chunk)


def _route_chunk_to_review_locked(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
    delivery: BankStatementParseDeliveryAttempt,
    reason_code: str,
    provider_attempt_count: int,
    now: datetime,
) -> None:
    """Terminalize an uncertain paid-call boundary without another delivery."""
    delivery_state = DeliveryLifecycleStatus(delivery.lifecycle_status)
    if delivery_state is DeliveryLifecycleStatus.STARTED:
        _transition_delivery(delivery, DeliveryLifecycleStatus.SUCCEEDED)
        delivery.status = ChunkLifecycleStatus.REVIEW_REQUIRED.value
        delivery.completed_at = now
    elif delivery_state is DeliveryLifecycleStatus.FAILED:
        delivery.status = reason_code
        delivery.completed_at = delivery.completed_at or now
    delivery.provider_calls_started = max(
        int(delivery.provider_calls_started or 0),
        provider_attempt_count,
    )
    delivery.telemetry_status = "telemetry_incomplete"
    delivery.evidence_eligible = False

    chunk_state = ChunkLifecycleStatus(chunk.lifecycle_status)
    if chunk_state is ChunkLifecycleStatus.LEASED:
        _transition_chunk(chunk, ChunkLifecycleStatus.REVIEW_REQUIRED)
    elif chunk_state is ChunkLifecycleStatus.FAILED:
        # FAILED is a retry-intermediate state.  This direct normalization exists
        # only for rows committed by the old two-transaction seam; it never reopens
        # a terminal success/review chunk and never creates another delivery.
        _transition_chunk_to_review_for_recovery(chunk)
    elif chunk_state is not ChunkLifecycleStatus.REVIEW_REQUIRED:
        raise ValueError("only leased/failed work can be routed to parser review")
    chunk.status = "error"
    chunk.lease_token = None
    chunk.lease_expires_at = None
    chunk.heartbeat_at = None
    chunk.completed_at = now
    chunk.telemetry_status = "telemetry_incomplete"
    chunk.evidence_eligible = False
    chunk.resolution_payload = {
        "reason_code": reason_code,
        "retryable": False,
    }

    statement_import = db.execute(
        select(BankStatementImport)
        .where(
            BankStatementImport.id == chunk.bank_statement_import_id,
            BankStatementImport.realm_id == chunk.realm_id,
        )
        .with_for_update()
    ).scalar_one()
    if statement_import.lifecycle_status in {
        ImportLifecycleStatus.QUEUED.value,
        ImportLifecycleStatus.PROCESSING.value,
    }:
        _transition_import(statement_import, ImportLifecycleStatus.REVIEW_REQUIRED)
    statement_import.status = "error"
    statement_import.review_reason_code = reason_code
    # This path is reached only when another automatic delivery is not proven
    # safe. Direct the user to a fresh upload instead of advertising a retry
    # action that could repeat an already-paid or outcome-unknown provider call.
    statement_import.review_action = "reupload"
    statement_import.parse_telemetry_status = "telemetry_incomplete"
    statement_import.parse_evidence_eligible = False


def _provider_attempt_has_complete_v2_evidence(
    db: Session,
    *,
    attempt: BankStatementParseProviderAttempt,
    delivery: BankStatementParseDeliveryAttempt,
    chunk: BankStatementParseChunk,
) -> bool:
    """Verify one existing-ledger link without the retired chunk->delivery edge."""
    lifecycle = str(attempt.lifecycle_status or "")
    status = str(attempt.status or "")
    status_matches_lifecycle = bool(
        (lifecycle == "succeeded" and status == "ok" and attempt.output_truncated is False)
        or (lifecycle == "truncated" and status == "ok" and attempt.output_truncated is True)
        or (lifecycle in {"failed", "timed_out"} and status == "error")
    )
    if (
        attempt.bank_statement_import_id != delivery.bank_statement_import_id
        or attempt.delivery_attempt_id != delivery.id
        or attempt.chunk_id != chunk.id
        or delivery.chunk_id != chunk.id
        or chunk.bank_statement_import_id != delivery.bank_statement_import_id
        or attempt.realm_id != delivery.realm_id
        or chunk.realm_id != delivery.realm_id
        or attempt.organization_id != delivery.organization_id
        or chunk.organization_id != delivery.organization_id
        or attempt.organization_company_id != delivery.organization_company_id
        or chunk.organization_company_id != delivery.organization_company_id
        or attempt.attempt_ordinal is None
        or not attempt.provider_call_id
        or not status_matches_lifecycle
        or attempt.completed_at is None
        or not attempt.provider
        or not attempt.model
        or not attempt.lane
        or not attempt.tenant_tag_succeeded
        or not attempt.recorder_succeeded
        or attempt.usage_event_id is None
        or attempt.latency_ms is None
        or (status == "ok" and not attempt.token_usage_reported)
        or (status == "ok" and attempt.total_tokens is None)
        or (status == "ok" and attempt.estimated_cost_usd is None)
    ):
        return False

    event = db.execute(
        select(LLMUsageEvent).where(LLMUsageEvent.id == attempt.usage_event_id)
    ).scalar_one_or_none()
    if event is None:
        return False
    return bool(
        event.task_type == "bank_statement_parse"
        and event.provider == attempt.provider
        and event.model == attempt.model
        and event.route == attempt.lane
        and event.status == attempt.status
        and event.organization_id == attempt.organization_id
        and event.organization_company_id == attempt.organization_company_id
        and event.qbo_realm_id == attempt.realm_id
        and event.input_tokens == attempt.input_tokens
        and event.output_tokens == attempt.output_tokens
        and event.cached_input_tokens == attempt.cached_input_tokens
        and event.cache_creation_input_tokens == attempt.cache_creation_input_tokens
        and event.cache_read_input_tokens == attempt.cache_read_input_tokens
        and event.total_tokens == attempt.total_tokens
        and event.estimated_cost_usd == attempt.estimated_cost_usd
        and event.latency_ms == attempt.latency_ms
        and event.error_type == attempt.error_type
    )


def _apply_delivery_telemetry(
    db: Session,
    *,
    claim: _ClaimedChunk,
    delivery: BankStatementParseDeliveryAttempt,
    chunk: BankStatementParseChunk,
    provider_trace: ProviderInvocationTrace,
) -> None:
    """Fail closed on incomplete attribution while preserving the feature result."""
    attempts = tuple(
        db.execute(
            select(BankStatementParseProviderAttempt)
            .where(
                BankStatementParseProviderAttempt.bank_statement_import_id
                == claim.import_id,
                BankStatementParseProviderAttempt.delivery_attempt_id
                == claim.delivery_id,
                BankStatementParseProviderAttempt.chunk_id == claim.chunk_id,
                BankStatementParseProviderAttempt.realm_id == claim.realm_id,
            )
            .order_by(BankStatementParseProviderAttempt.attempt_ordinal)
        ).scalars()
    )
    observed_ids = tuple(provider_trace.provider_attempt_ids)
    persisted_ids = tuple(attempt.id for attempt in attempts)
    calls_started = int(provider_trace.calls_started)
    envelope_failures = int(provider_trace.envelope_failures)
    complete = bool(
        delivery.parser_mode == "resumable_v2"
        and delivery.chunk_id == chunk.id == claim.chunk_id
        and delivery.bank_statement_import_id
        == chunk.bank_statement_import_id
        == claim.import_id
        and delivery.realm_id == chunk.realm_id == claim.realm_id
        and delivery.organization_id is not None
        and delivery.organization_id == chunk.organization_id
        and delivery.organization_company_id is not None
        and delivery.organization_company_id == chunk.organization_company_id
        and delivery.max_retries == 0
        and delivery.allow_fallback is False
        and envelope_failures == 0
        and calls_started == len(attempts)
        and len(observed_ids) == len(set(observed_ids))
        and set(observed_ids) == set(persisted_ids)
        and all(
            _provider_attempt_has_complete_v2_evidence(
                db,
                attempt=attempt,
                delivery=delivery,
                chunk=chunk,
            )
            for attempt in attempts
        )
    )
    telemetry_status = "complete" if complete else "telemetry_incomplete"
    delivery.provider_calls_started = calls_started
    delivery.provider_envelope_failures = envelope_failures
    delivery.telemetry_status = telemetry_status
    delivery.evidence_eligible = complete
    chunk.telemetry_status = telemetry_status
    chunk.evidence_eligible = complete


def _finalize_ready_import(db: Session, claim: _ClaimedChunk) -> object:
    """Run C2-b's idempotent, code-calculated control/finalization seam."""
    from ..services.bank_recon.parser_persist import finalize_import

    return finalize_import(
        db,
        statement_import_id=claim.import_id,
        realm_id=claim.realm_id,
    )


def _terminalize_delivery(
    db: Session,
    *,
    claim: _ClaimedChunk,
    outcome: object,
    provider_trace: ProviderInvocationTrace,
) -> tuple[str, str | None]:
    """Commit C2-b's staged mutations only for the still-matching lease."""
    raw_status = _outcome_value(outcome, "lifecycle_status")
    status = (
        raw_status.value
        if isinstance(raw_status, ChunkLifecycleStatus)
        else str(raw_status or "")
    )
    retryable = bool(_outcome_value(outcome, "retryable"))
    reason_value = _outcome_value(outcome, "reason_code")
    reason_code = str(reason_value) if reason_value is not None else None
    if status not in {
        ChunkLifecycleStatus.SUCCEEDED.value,
        ChunkLifecycleStatus.REVIEW_REQUIRED.value,
        ChunkLifecycleStatus.FAILED.value,
    }:
        db.rollback()
        raise ValueError("chunk processor returned an unsupported lifecycle status")

    # C2-b has flushed its candidate/chunk mutations but never commits.  Re-read
    # the locked rows in this same transaction and reject a stale-token result.
    chunk = _lock_chunk(
        db,
        chunk_id=claim.chunk_id,
        import_id=claim.import_id,
        realm_id=claim.realm_id,
    )
    delivery = _lock_delivery(
        db,
        delivery_id=claim.delivery_id,
        realm_id=claim.realm_id,
    )
    if chunk is None or delivery is None:
        db.rollback()
        return "stale", reason_code
    # process_leased_chunk clears lease columns only after it has revalidated the
    # token.  Its terminal status plus our still-started delivery is the commit
    # predicate; if it left the chunk leased, require the literal token here.
    token_matches = (
        chunk.lease_token == claim.lease_token
        if chunk.lifecycle_status == ChunkLifecycleStatus.LEASED.value
        else chunk.lease_token is None
    )
    if (
        delivery.lifecycle_status != DeliveryLifecycleStatus.STARTED.value
        or not token_matches
        or chunk.lifecycle_status != status
    ):
        db.rollback()
        return "stale", reason_code

    _apply_delivery_telemetry(
        db,
        claim=claim,
        delivery=delivery,
        chunk=chunk,
        provider_trace=provider_trace,
    )
    now = datetime.now(timezone.utc)
    if retryable and status == ChunkLifecycleStatus.FAILED.value:
        evidence = _adjudicate_retry_from_durable_evidence(
            db,
            chunk=chunk,
            delivery=delivery,
        )
        retry_delivery = _stage_retry_delivery_locked(
            db,
            chunk=chunk,
            delivery=delivery,
            evidence=evidence,
            now=now,
        )
        if retry_delivery is None:
            _route_chunk_to_review_locked(
                db,
                chunk=chunk,
                delivery=delivery,
                reason_code=evidence.reason_code,
                provider_attempt_count=evidence.provider_attempt_count,
                now=now,
            )
            db.commit()
            return ChunkLifecycleStatus.REVIEW_REQUIRED.value, evidence.reason_code
        # FAILED terminalization, retry-budget consumption, and the deterministic
        # next ordinal are one transaction.  Publishing stays after this commit;
        # a crash there leaves CREATED for the reaper rather than stranding FAILED.
        db.commit()
        return "retryable_failure", evidence.reason_code

    _transition_delivery(delivery, DeliveryLifecycleStatus.SUCCEEDED)
    delivery.status = status
    delivery.completed_at = now
    db.commit()
    return status, reason_code


def _schedule_application_retry(
    db: Session,
    *,
    claim: _ClaimedChunk,
) -> bool:
    """Publish an already-durable retry descriptor, with legacy FAILED recovery."""
    chunk = _lock_chunk(
        db,
        chunk_id=claim.chunk_id,
        import_id=claim.import_id,
        realm_id=claim.realm_id,
    )
    if chunk is None:
        db.rollback()
        return False
    if chunk.lifecycle_status == ChunkLifecycleStatus.FAILED.value:
        # Compatibility recovery for a row committed by the retired two-step
        # terminalize-then-create seam.  New writes never expose this state.
        failed_delivery = db.execute(
            select(BankStatementParseDeliveryAttempt)
            .where(
                BankStatementParseDeliveryAttempt.chunk_id == chunk.id,
                BankStatementParseDeliveryAttempt.realm_id == chunk.realm_id,
                BankStatementParseDeliveryAttempt.lifecycle_status
                == DeliveryLifecycleStatus.FAILED.value,
            )
            .order_by(BankStatementParseDeliveryAttempt.delivery_ordinal.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if failed_delivery is None:
            db.rollback()
            return False
        evidence = _adjudicate_retry_from_durable_evidence(
            db,
            chunk=chunk,
            delivery=failed_delivery,
        )
        delivery = _stage_retry_delivery_locked(
            db,
            chunk=chunk,
            delivery=failed_delivery,
            evidence=evidence,
            now=datetime.now(timezone.utc),
        )
        if delivery is None:
            _route_chunk_to_review_locked(
                db,
                chunk=chunk,
                delivery=failed_delivery,
                reason_code=evidence.reason_code,
                provider_attempt_count=evidence.provider_attempt_count,
                now=datetime.now(timezone.utc),
            )
            db.commit()
            return False
    elif (
        chunk.lifecycle_status == ChunkLifecycleStatus.PENDING.value
        and int(chunk.application_retry_count or 0) == 1
    ):
        # New retry writes atomically persist this successor before returning.
        # Never manufacture a replacement here from PENDING alone: without the
        # descriptor, the commit boundary is ambiguous and the reaper must first
        # prove the durable paid-call history (or route the chunk to review).
        delivery = db.execute(
            select(BankStatementParseDeliveryAttempt)
            .where(
                BankStatementParseDeliveryAttempt.chunk_id == chunk.id,
                BankStatementParseDeliveryAttempt.realm_id == chunk.realm_id,
                BankStatementParseDeliveryAttempt.lifecycle_status.in_(
                    (
                        DeliveryLifecycleStatus.CREATED.value,
                        DeliveryLifecycleStatus.DISPATCHED.value,
                    )
                ),
            )
            .order_by(BankStatementParseDeliveryAttempt.delivery_ordinal.desc())
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if delivery is None:
            db.rollback()
            return False
    else:
        db.rollback()
        return False

    delivery_id = delivery.id
    realm_id = delivery.realm_id
    db.commit()
    outcome = _publish_delivery(db, delivery_id=delivery_id, realm_id=realm_id)
    return outcome in {"published", "uncertain", "already_dispatched"}


def _mark_processing_exception(
    db: Session,
    *,
    claim: _ClaimedChunk,
    recovery_allowed: bool,
    reason_code: str,
    provider_trace: ProviderInvocationTrace,
) -> tuple[str, str]:
    """Recover from a task exception using only durable paid-call evidence."""
    db.rollback()
    chunk = _lock_chunk(
        db,
        chunk_id=claim.chunk_id,
        import_id=claim.import_id,
        realm_id=claim.realm_id,
    )
    delivery = _lock_delivery(
        db,
        delivery_id=claim.delivery_id,
        realm_id=claim.realm_id,
    )
    if chunk is None or delivery is None:
        db.rollback()
        return "stale", reason_code

    chunk_state = ChunkLifecycleStatus(chunk.lifecycle_status)
    delivery_state = DeliveryLifecycleStatus(delivery.lifecycle_status)
    if (
        chunk_state is ChunkLifecycleStatus.PENDING
        and int(chunk.application_retry_count or 0) == 1
        and delivery_state is DeliveryLifecycleStatus.FAILED
    ):
        successor_exists = db.execute(
            select(BankStatementParseDeliveryAttempt.id).where(
                BankStatementParseDeliveryAttempt.chunk_id == chunk.id,
                BankStatementParseDeliveryAttempt.realm_id == chunk.realm_id,
                BankStatementParseDeliveryAttempt.delivery_ordinal
                > delivery.delivery_ordinal,
                BankStatementParseDeliveryAttempt.lifecycle_status.in_(
                    (
                        DeliveryLifecycleStatus.CREATED.value,
                        DeliveryLifecycleStatus.DISPATCHED.value,
                    )
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
        if successor_exists is not None:
            # The atomic retry transaction committed even though the client
            # observed an OperationalError. Publishing can continue from the
            # already-durable descriptor; this branch never creates one.
            persisted_reason = str(delivery.status or reason_code)
            db.rollback()
            return "retryable_failure", persisted_reason
    if chunk_state is ChunkLifecycleStatus.REVIEW_REQUIRED:
        payload = chunk.resolution_payload
        persisted_reason = (
            payload.get("reason_code")
            if isinstance(payload, Mapping)
            and isinstance(payload.get("reason_code"), str)
            else reason_code
        )
        db.rollback()
        return "review_required", persisted_reason
    if (
        chunk_state is ChunkLifecycleStatus.SUCCEEDED
        and delivery_state is DeliveryLifecycleStatus.SUCCEEDED
    ):
        db.rollback()
        return "succeeded", reason_code
    if (
        chunk_state not in {ChunkLifecycleStatus.LEASED, ChunkLifecycleStatus.FAILED}
        or delivery_state
        not in {DeliveryLifecycleStatus.STARTED, DeliveryLifecycleStatus.FAILED}
        or (
            chunk_state is ChunkLifecycleStatus.LEASED
            and chunk.lease_token != claim.lease_token
        )
    ):
        db.rollback()
        return "stale", reason_code

    _apply_delivery_telemetry(
        db,
        claim=claim,
        delivery=delivery,
        chunk=chunk,
        provider_trace=provider_trace,
    )
    evidence = _adjudicate_retry_from_durable_evidence(
        db,
        chunk=chunk,
        delivery=delivery,
    )
    now = datetime.now(timezone.utc)
    retry_delivery = None
    if recovery_allowed:
        retry_delivery = _stage_retry_delivery_locked(
            db,
            chunk=chunk,
            delivery=delivery,
            evidence=evidence,
            now=now,
        )
    if retry_delivery is not None:
        db.commit()
        return "retryable_failure", evidence.reason_code

    review_reason = (
        evidence.reason_code if evidence.provider_call_possible else reason_code
    )
    _route_chunk_to_review_locked(
        db,
        chunk=chunk,
        delivery=delivery,
        reason_code=review_reason,
        provider_attempt_count=evidence.provider_attempt_count,
        now=now,
    )
    db.commit()
    return "review_required", review_reason


@celery_app.task(
    bind=True,
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=BANK_IMPORT_TASK_SOFT_LIMIT_SECONDS,
    time_limit=BANK_IMPORT_TASK_HARD_LIMIT_SECONDS,
    name="app.tasks.bank_import.process_bank_import_chunk_task",
)
def process_bank_import_chunk_task(
    self,
    chunk_id: str,
    delivery_id: str,
) -> dict[str, object]:
    """Process one durable logical chunk; broker redelivery is a DB-guarded no-op."""
    try:
        parsed_chunk_id = UUID(chunk_id)
        parsed_delivery_id = UUID(delivery_id)
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "invalid_delivery_identity"}

    db = SessionLocal()
    try:
        realm_id = _delivery_realm_bootstrap(db, parsed_delivery_id)
        if realm_id is None:
            return {"status": "skipped", "reason": "delivery_not_found"}
        request = getattr(self, "request", None)
        broker_task_id = str(getattr(request, "id", "") or "") or None
        with tenant_scope_for_realm(db, realm_id):
            claim = _claim_delivery(
                db,
                chunk_id=parsed_chunk_id,
                delivery_id=parsed_delivery_id,
                broker_task_id=broker_task_id,
                realm_id=realm_id,
            )
            if claim is None:
                return {"status": "skipped", "reason": "duplicate_or_unclaimable"}

            provider_trace = ProviderInvocationTrace()
            try:
                with (
                    _lease_heartbeat(claim),
                    _bind_claimed_llm_context(
                        db,
                        claim=claim,
                        provider_trace=provider_trace,
                    ),
                ):
                    outcome = _process_claimed_chunk(db, claim)
                if not bool(_outcome_value(outcome, "retryable")):
                    _finalize_ready_import(db, claim)
                status, reason_code = _terminalize_delivery(
                    db,
                    claim=claim,
                    outcome=outcome,
                    provider_trace=provider_trace,
                )
            except (SoftTimeLimitExceeded, TimeoutError, ConnectionError) as exc:
                status, reason_code = _mark_processing_exception(
                    db,
                    claim=claim,
                    recovery_allowed=True,
                    reason_code=type(exc).__name__,
                    provider_trace=provider_trace,
                )
            except DatabaseOperationalError as exc:
                status, reason_code = _mark_processing_exception(
                    db,
                    claim=claim,
                    recovery_allowed=True,
                    reason_code=type(exc).__name__,
                    provider_trace=provider_trace,
                )
            except Exception as exc:  # noqa: BLE001 - task boundary converts to reviewable state
                logger.error(
                    "[bank_import_v2] chunk_id=%s processing failed: %s",
                    parsed_chunk_id,
                    redact_to_string(exc),
                )
                status, reason_code = _mark_processing_exception(
                    db,
                    claim=claim,
                    recovery_allowed=False,
                    reason_code="unexpected_processing_error",
                    provider_trace=provider_trace,
                )

            retry_scheduled = False
            if status == "retryable_failure":
                retry_scheduled = _schedule_application_retry(db, claim=claim)
            return {
                "status": status,
                "reason_code": reason_code,
                "import_id": claim.import_id,
                "chunk_id": str(claim.chunk_id),
                "delivery_id": str(claim.delivery_id),
                "retry_scheduled": retry_scheduled,
            }
    finally:
        db.close()


# Every status that means "this import is DONE" — the model's vocab minus the two
# pre-terminal states (models.py:2538). Arriving on one of these means a prior
# attempt already finalized the row, so this delivery is a duplicate: no-op.
_TERMINAL_STATUSES = frozenset({"parsed", "matched", "complete", "error"})

# The worker's LLM budget (AC-2). Deliberately the INVERSE of the inline default: a
# worker has no request to time out, and the parse legitimately takes 40-183s, so
# failing fast here would kill EVERY real statement. ONE long attempt is correct.
#
# CALIBRATION (corrected 2026-07-16 — the ratified plan's own numbers were wrong, and
# an adversarial pass caught it). The prod telemetry in the plan's SYNTHESIS records
# gpt-4o parse timeouts at avg 135.8s / **max 183.4s**. The plan specified a 180s budget
# with max_retries=1 — both wrong, and together they rebuilt the dead-end this batch
# exists to remove:
#   - 180s sits BELOW the 183.4s observed max, so the slowest real statements time out.
#   - max_retries maps onto the OpenAI SDK (llm.py with_options), where it counts retries
#     AFTER the initial request: 1 meant TWO attempts = 360s — contradicting the "one long
#     attempt" intent above, and blowing past the UI's poll ceiling. The parse would then
#     SUCCEED (~363s) after the UI had already given up, sending the user to re-upload →
#     duplicate guard → "retry with force_reimport", which the UI exposes no way to do.
# So: one attempt, with headroom ABOVE the observed max. The UI ceiling
# (useStatementImport.ts POLL_CEILING_MS) must stay above this + queue wait.
_WORKER_PARSE_TIMEOUT_SECONDS = 240
_WORKER_PARSE_MAX_RETRIES = 0
_WORKER_PARSE_ALLOW_FALLBACK = False
_WORKER_PARSE_MAX_INPUT_CHARS = ingest._PDF_MAX_INPUT_CHARS

# Honest terminal messages. Both keep the CSV escape hatch the inline errors already
# offer — it is the one action that reliably works when a PDF will not parse.
_NO_FILE_ERROR = (
    "The uploaded statement file could not be retrieved for parsing, so nothing was "
    "imported. Please upload the statement again, or upload a CSV export instead."
)
_UNEXPECTED_ERROR = (
    "Something went wrong while parsing this statement, so nothing was imported. "
    "Please try again, or upload a CSV export instead."
)


@dataclass(frozen=True)
class _ParseTelemetryHandle:
    delivery_attempt_id: UUID
    chunk_id: UUID | None


def _start_parse_telemetry(
    *,
    import_id: int,
    realm_id: str,
    celery_task_id: str | None,
    redelivered: bool,
    create_chunk: bool,
) -> _ParseTelemetryHandle | None:
    """Commit a fresh delivery identity without sharing the parser transaction."""
    telemetry_db = SessionLocal()
    try:
        with tenant_scope_live(telemetry_db, bypass=True):
            organization = organization_for_company(telemetry_db, realm_id)
            organization_company = (
                organization_company_for_realm(
                    telemetry_db,
                    organization_id=organization.id,
                    realm_id=realm_id,
                )
                if organization is not None
                else None
            )
            delivery = BankStatementParseDeliveryAttempt(
                bank_statement_import_id=import_id,
                realm_id=realm_id,
                organization_id=organization.id if organization is not None else None,
                organization_company_id=(
                    organization_company.id if organization_company is not None else None
                ),
                celery_task_id=celery_task_id,
                redelivered=redelivered,
                parser_mode="one_shot",
                max_input_chars=_WORKER_PARSE_MAX_INPUT_CHARS,
                timeout_seconds=_WORKER_PARSE_TIMEOUT_SECONDS,
                max_retries=_WORKER_PARSE_MAX_RETRIES,
                allow_fallback=_WORKER_PARSE_ALLOW_FALLBACK,
                status="started",
                telemetry_status="pending",
                evidence_eligible=False,
            )
            telemetry_db.add(delivery)
            telemetry_db.flush()

            chunk = None
            if create_chunk:
                chunk = BankStatementParseChunk(
                    bank_statement_import_id=import_id,
                    delivery_attempt_id=delivery.id,
                    realm_id=realm_id,
                    organization_id=delivery.organization_id,
                    organization_company_id=delivery.organization_company_id,
                    chunk_index=0,
                    status="started",
                    telemetry_status="pending",
                    evidence_eligible=False,
                )
                telemetry_db.add(chunk)
                telemetry_db.flush()

            batch = (
                telemetry_db.query(BankStatementImport)
                .filter(
                    BankStatementImport.id == import_id,
                    BankStatementImport.realm_id == realm_id,
                )
                .one()
            )
            batch.parse_telemetry_status = "pending"
            batch.parse_evidence_eligible = False
            telemetry_db.commit()
            return _ParseTelemetryHandle(
                delivery_attempt_id=delivery.id,
                chunk_id=chunk.id if chunk is not None else None,
            )
    except Exception as exc:
        telemetry_db.rollback()
        logger.warning(
            "[bank_import] import_id=%s realm=%s telemetry delivery start failed; "
            "parse remains enabled: %s",
            import_id,
            realm_id,
            type(exc).__name__,
        )
        return None
    finally:
        telemetry_db.close()


@contextmanager
def _bind_parse_llm_context(
    db,
    *,
    import_id: int,
    realm_id: str,
    telemetry: _ParseTelemetryHandle | None,
    provider_trace: ProviderInvocationTrace,
):
    """Bind attribution/correlation without letting a bind failure block parsing."""
    with ExitStack() as stack:
        stack.enter_context(
            bind_llm_task_context(
                "bank_statement_parse",
                step=f"bank_statement_import:{import_id}",
                realm_id=realm_id,
                bank_statement_import_id=import_id,
                bank_statement_delivery_attempt_id=(
                    telemetry.delivery_attempt_id if telemetry is not None else None
                ),
                bank_statement_chunk_id=telemetry.chunk_id if telemetry is not None else None,
                provider_trace=provider_trace,
            )
        )
        try:
            stack.enter_context(bind_tenant_tag_for_realm(db, realm_id))
        except Exception as exc:
            # This is an attribution-only seam. Roll back its failed read and let
            # the unchanged parser proceed. Explicitly shadow any ambient tenant
            # context so the provider can be untagged, never tagged to the wrong
            # tenant; the missing exact tag invalidates evidence below.
            db.rollback()
            stack.enter_context(bind_tenant_tag(organization_id=None))
            logger.warning(
                "[bank_import] import_id=%s realm=%s tenant telemetry bind failed; "
                "parse remains enabled: %s",
                import_id,
                realm_id,
                type(exc).__name__,
            )
        yield


def _provider_attempt_has_complete_evidence(
    db,
    attempt: BankStatementParseProviderAttempt,
    *,
    delivery: BankStatementParseDeliveryAttempt,
    chunk: BankStatementParseChunk | None,
) -> bool:
    if (
        chunk is None
        or attempt.bank_statement_import_id != delivery.bank_statement_import_id
        or attempt.delivery_attempt_id != delivery.id
        or attempt.chunk_id != chunk.id
        or chunk.delivery_attempt_id != delivery.id
        or chunk.bank_statement_import_id != delivery.bank_statement_import_id
        or attempt.realm_id != delivery.realm_id
        or chunk.realm_id != delivery.realm_id
        or attempt.organization_id != delivery.organization_id
        or chunk.organization_id != delivery.organization_id
        or attempt.organization_company_id != delivery.organization_company_id
        or chunk.organization_company_id != delivery.organization_company_id
        or not attempt.provider
        or not attempt.model
        or not attempt.lane
        or attempt.status not in {"ok", "error"}
        or attempt.completed_at is None
        or attempt.organization_id is None
        or attempt.organization_company_id is None
        or not attempt.realm_id
        or not attempt.tenant_tag_succeeded
        or not attempt.recorder_succeeded
        or attempt.usage_event_id is None
        or not attempt.token_usage_reported
        or not attempt.cache_tokens_reported
        or attempt.input_tokens is None
        or attempt.output_tokens is None
        or attempt.total_tokens is None
        or attempt.estimated_cost_usd is None
        or attempt.latency_ms is None
        or (attempt.status == "ok" and attempt.output_truncated is None)
    ):
        return False
    event = (
        db.query(LLMUsageEvent)
        .filter(LLMUsageEvent.id == attempt.usage_event_id)
        .one_or_none()
    )
    if event is None:
        return False
    return bool(
        event.task_type == "bank_statement_parse"
        and event.provider == attempt.provider
        and event.model == attempt.model
        and event.route == attempt.lane
        and event.status == attempt.status
        and event.organization_id == attempt.organization_id
        and event.organization_company_id == attempt.organization_company_id
        and event.qbo_realm_id == attempt.realm_id
        and event.input_tokens == attempt.input_tokens
        and event.output_tokens == attempt.output_tokens
        and event.cached_input_tokens == attempt.cached_input_tokens
        and event.cache_creation_input_tokens == attempt.cache_creation_input_tokens
        and event.cache_read_input_tokens == attempt.cache_read_input_tokens
        and event.total_tokens == attempt.total_tokens
        and event.estimated_cost_usd == attempt.estimated_cost_usd
        and event.latency_ms == attempt.latency_ms
    )


def _mark_import_telemetry_incomplete(*, import_id: int, realm_id: str) -> None:
    telemetry_db = SessionLocal()
    try:
        with tenant_scope_live(telemetry_db, bypass=True):
            batch = (
                telemetry_db.query(BankStatementImport)
                .filter(
                    BankStatementImport.id == import_id,
                    BankStatementImport.realm_id == realm_id,
                )
                .one_or_none()
            )
            if batch is not None:
                batch.parse_telemetry_status = "telemetry_incomplete"
                batch.parse_evidence_eligible = False
                telemetry_db.commit()
    except Exception as exc:
        telemetry_db.rollback()
        logger.warning(
            "[bank_import] import_id=%s realm=%s could not persist incomplete telemetry: %s",
            import_id,
            realm_id,
            type(exc).__name__,
        )
    finally:
        telemetry_db.close()


def _record_source_fingerprint(
    *,
    import_id: int,
    realm_id: str,
    telemetry: _ParseTelemetryHandle | None,
    file_bytes: bytes,
) -> None:
    """Bind a redacted exact-input identity to the one-shot chunk."""
    if telemetry is None or telemetry.chunk_id is None:
        return
    telemetry_db = SessionLocal()
    try:
        with tenant_scope_live(telemetry_db, bypass=True):
            chunk = (
                telemetry_db.query(BankStatementParseChunk)
                .filter(
                    BankStatementParseChunk.id == telemetry.chunk_id,
                    BankStatementParseChunk.bank_statement_import_id == import_id,
                    BankStatementParseChunk.realm_id == realm_id,
                )
                .one()
            )
            chunk.source_sha256 = hashlib.sha256(file_bytes).hexdigest()
            chunk.source_size_bytes = len(file_bytes)
            telemetry_db.commit()
    except Exception as exc:
        telemetry_db.rollback()
        logger.warning(
            "[bank_import] import_id=%s realm=%s source fingerprint failed; "
            "parse remains enabled: %s",
            import_id,
            realm_id,
            type(exc).__name__,
        )
    finally:
        telemetry_db.close()


def _finish_parse_telemetry(
    *,
    import_id: int,
    realm_id: str,
    telemetry: _ParseTelemetryHandle | None,
    provider_trace: ProviderInvocationTrace,
    summary: ImportParseSummary,
) -> None:
    """Terminalize evidence after the feature result has already committed."""
    if telemetry is None:
        _mark_import_telemetry_incomplete(import_id=import_id, realm_id=realm_id)
        return

    telemetry_db = SessionLocal()
    try:
        with tenant_scope_live(telemetry_db, bypass=True):
            delivery = (
                telemetry_db.query(BankStatementParseDeliveryAttempt)
                .filter(
                    BankStatementParseDeliveryAttempt.id == telemetry.delivery_attempt_id,
                    BankStatementParseDeliveryAttempt.bank_statement_import_id == import_id,
                    BankStatementParseDeliveryAttempt.realm_id == realm_id,
                )
                .one()
            )
            attempts = (
                telemetry_db.query(BankStatementParseProviderAttempt)
                .filter(
                    BankStatementParseProviderAttempt.delivery_attempt_id
                    == telemetry.delivery_attempt_id,
                    BankStatementParseProviderAttempt.bank_statement_import_id == import_id,
                    BankStatementParseProviderAttempt.realm_id == realm_id,
                )
                .order_by(BankStatementParseProviderAttempt.started_at.asc())
                .all()
            )
            delivery_chunks = (
                telemetry_db.query(BankStatementParseChunk)
                .filter(
                    BankStatementParseChunk.delivery_attempt_id
                    == telemetry.delivery_attempt_id,
                    BankStatementParseChunk.bank_statement_import_id == import_id,
                    BankStatementParseChunk.realm_id == realm_id,
                )
                .all()
            )
            chunk = None
            if telemetry.chunk_id is not None:
                chunk = (
                    telemetry_db.query(BankStatementParseChunk)
                    .filter(
                        BankStatementParseChunk.id == telemetry.chunk_id,
                        BankStatementParseChunk.bank_statement_import_id == import_id,
                        BankStatementParseChunk.realm_id == realm_id,
                    )
                    .one()
                )
            persisted_ids = {attempt.id for attempt in attempts}
            observed_ids = set(provider_trace.provider_attempt_ids)
            parsed_requires_provider = summary["status"] == "parsed"
            chunk_cardinality_complete = bool(
                (telemetry.chunk_id is None and not delivery_chunks)
                or (
                    telemetry.chunk_id is not None
                    and len(delivery_chunks) == 1
                    and chunk is not None
                    and delivery_chunks[0].id == chunk.id
                )
            )
            frozen_controls_complete = bool(
                delivery.parser_mode == "one_shot"
                and delivery.max_input_chars == _WORKER_PARSE_MAX_INPUT_CHARS
                and delivery.timeout_seconds == _WORKER_PARSE_TIMEOUT_SECONDS
                and delivery.max_retries == _WORKER_PARSE_MAX_RETRIES
                and delivery.allow_fallback is _WORKER_PARSE_ALLOW_FALLBACK
            )
            tenant_identity_complete = bool(
                delivery.organization_id is not None
                and delivery.organization_company_id is not None
                and delivery.bank_statement_import_id == import_id
                and delivery.realm_id == realm_id
                and (
                    chunk is None
                    or (
                        chunk.delivery_attempt_id == delivery.id
                        and chunk.bank_statement_import_id == import_id
                        and chunk.organization_id == delivery.organization_id
                        and chunk.organization_company_id
                        == delivery.organization_company_id
                        and chunk.realm_id == realm_id
                        and chunk.chunk_index == 0
                        and chunk.source_sha256 is not None
                        and len(chunk.source_sha256) == 64
                        and all(
                            character in "0123456789abcdef"
                            for character in chunk.source_sha256
                        )
                        and chunk.source_size_bytes is not None
                        and chunk.source_size_bytes > 0
                    )
                )
            )
            complete = bool(
                chunk_cardinality_complete
                and frozen_controls_complete
                and tenant_identity_complete
                and provider_trace.envelope_failures == 0
                and provider_trace.calls_started == len(attempts)
                and provider_trace.calls_started <= 1
                and observed_ids == persisted_ids
                and (
                    (chunk is None and provider_trace.calls_started == 0)
                    or chunk is not None
                )
                and (not parsed_requires_provider or provider_trace.calls_started == 1)
                and all(
                    _provider_attempt_has_complete_evidence(
                        telemetry_db,
                        attempt,
                        delivery=delivery,
                        chunk=chunk,
                    )
                    for attempt in attempts
                )
            )

            delivery.status = (
                f"skipped_{summary.get('reason', 'unknown')}"
                if summary["status"] == "skipped"
                else summary["status"]
            )
            delivery.provider_calls_started = provider_trace.calls_started
            delivery.provider_envelope_failures = provider_trace.envelope_failures
            delivery.telemetry_status = "complete" if complete else "telemetry_incomplete"
            delivery.evidence_eligible = complete
            delivery.completed_at = datetime.now(timezone.utc)

            if chunk is not None:
                chunk.status = summary["status"]
                chunk.telemetry_status = delivery.telemetry_status
                chunk.evidence_eligible = complete
                chunk.completed_at = delivery.completed_at

            telemetry_db.flush()
            deliveries = (
                telemetry_db.query(BankStatementParseDeliveryAttempt)
                .filter(
                    BankStatementParseDeliveryAttempt.bank_statement_import_id == import_id,
                    BankStatementParseDeliveryAttempt.realm_id == realm_id,
                )
                .all()
            )
            chunk_count = (
                telemetry_db.query(BankStatementParseChunk)
                .filter(
                    BankStatementParseChunk.bank_statement_import_id == import_id,
                    BankStatementParseChunk.realm_id == realm_id,
                )
                .count()
            )
            import_complete = bool(
                chunk_count > 0
                and deliveries
                and all(
                    row.telemetry_status == "complete" and row.evidence_eligible
                    for row in deliveries
                )
            )
            batch = (
                telemetry_db.query(BankStatementImport)
                .filter(
                    BankStatementImport.id == import_id,
                    BankStatementImport.realm_id == realm_id,
                )
                .one()
            )
            batch.parse_telemetry_status = (
                "complete" if import_complete else "telemetry_incomplete"
            )
            batch.parse_evidence_eligible = import_complete
            telemetry_db.commit()
    except Exception as exc:
        telemetry_db.rollback()
        logger.warning(
            "[bank_import] import_id=%s realm=%s telemetry finish failed; "
            "feature result preserved: %s",
            import_id,
            realm_id,
            type(exc).__name__,
        )
        _mark_import_telemetry_incomplete(import_id=import_id, realm_id=realm_id)
    finally:
        telemetry_db.close()


def _finalize_error(db, batch: BankStatementImport, detail: str) -> ImportParseSummary:
    """Land the import on the terminal `error` state with an honest, user-facing reason.

    ``line_count`` is deliberately LEFT ALONE (NULL) rather than set to 0: the poll DTO
    documents 0 as "parsed, zero rows", and defaulting a genuinely-unknown value to a
    plausible-looking one is the masked-error anti-pattern (error-logging.md §5).

    If this commit ITSELF fails the exception propagates and the row stays ``parsing``.
    Be honest about what happens next (this docstring previously claimed the task "never
    acks" and is re-delivered — FALSE, see the module header): under ``acks_late`` a task
    that RAISES is still ACKED (``acks_on_failure_or_timeout`` defaults True), so it is NOT
    retried and the row is stranded until a re-upload. That is bounded — the UI's poll
    ceiling ends the wait honestly and the NULL-balance orphan cannot trip the duplicate
    guard — but it is a real gap whose close-path is a beat reaper (module header §1).
    """
    batch.status = "error"
    batch.error_detail = detail
    db.commit()
    logger.info(
        "[bank_import] import_id=%s realm=%s finalized=error", batch.id, batch.realm_id
    )
    return {"status": "error", "import_id": batch.id, "error": detail}


def _parse_and_finalize(
    db,
    batch: BankStatementImport,
    telemetry: _ParseTelemetryHandle | None,
) -> ImportParseSummary:
    """Fetch → parse → persist → finalize. Every exit is a terminal status.

    The caller has already pinned this import's realm scope and confirmed the row is
    still ``parsing``.
    """
    import_id = batch.id
    realm_id = batch.realm_id
    try:
        file_bytes = ingest.fetch_original(batch.storage_key)
        if not file_bytes:
            # No stored copy (storage unconfigured/failed at upload, or the object is
            # gone). There is nothing to parse — say so, never guess at rows.
            logger.warning(
                "[bank_import] import_id=%s realm=%s has no retrievable original (key=%s)",
                import_id, realm_id, batch.storage_key,
            )
            return _finalize_error(db, batch, _NO_FILE_ERROR)

        _record_source_fingerprint(
            import_id=import_id,
            realm_id=realm_id,
            telemetry=telemetry,
            file_bytes=file_bytes,
        )

        # extract_text routes on the extension; the key's trailing segment is the
        # sanitized upload filename (_safe_storage_name), so it carries the .pdf.
        filename = (batch.storage_key or "statement.pdf").rsplit("/", 1)[-1]

        parsed = ingest.parse_pdf(
            file_bytes,
            filename,
            timeout_override=_WORKER_PARSE_TIMEOUT_SECONDS,
            max_retries_override=_WORKER_PARSE_MAX_RETRIES,
            allow_fallback=_WORKER_PARSE_ALLOW_FALLBACK,
        )
        if parsed.status == "error":
            # The scanned-PDF / no-rows / LLM-None honest errors, verbatim from the
            # shared parser (they already carry the "upload a CSV" hint).
            return _finalize_error(db, batch, parsed.error or "Could not parse statement.")

        # ONE canonical persist path, shared with the sync CSV lane: the batch-level
        # duplicate guard (self-excluded — see find_duplicate_import) and the
        # count-aware LINE-level dedupe both run here, and the header + its rows
        # commit together.
        result = ingest.persist(
            db,
            realm_id=realm_id,
            account_id=batch.account_id,
            account_name=batch.account_name,
            result=parsed,
            storage_key=batch.storage_key,
            # The user's force-reimport intent, carried across the request→worker
            # hand-off on the header row (the router enqueues only the id). NULL on
            # legacy/unforced rows → False, so the duplicate guard still runs by default.
            force_reimport=bool(batch.force_reimport),
            batch=batch,
        )
        if result.status == "duplicate":
            # Nothing was persisted; surface the guard's teaching message as the
            # terminal state rather than a silent zero-row "success".
            return _finalize_error(db, batch, result.error or "Duplicate statement.")

        logger.info(
            "[bank_import] import_id=%s realm=%s finalized=parsed line_count=%s",
            import_id, realm_id, result.line_count,
        )
        return {"status": "parsed", "import_id": import_id, "line_count": result.line_count}
    except Exception as exc:  # noqa: BLE001 - the always-finalize wrapper: see below
        # Deliberately broad. This is the backstop that makes "never a stuck parsing"
        # true: whatever went wrong, the row must not be left mid-flight. The failure
        # is logged (redacted) and surfaced honestly to the user, never swallowed.
        db.rollback()  # discard any half-written rows; batch reverts to `parsing`
        logger.error(
            "[bank_import] import_id=%s realm=%s parse failed: %s",
            import_id, realm_id, redact_to_string(exc),
        )
        return _finalize_error(db, batch, _UNEXPECTED_ERROR)


@celery_app.task(bind=True, max_retries=0)
def parse_statement_import_task(self, import_id: int) -> ImportParseSummary:
    """Upgrade an already-queued import payload into the v2 durable outbox.

    Older workers/messages carry only ``import_id``.  Keeping this registered task
    name lets those messages land after cutover, but the body never calls the retired
    one-shot parser: it prepares the same v2 descriptors as the HTTP path, commits
    them, and dispatches the chunk task with persisted broker identities.
    """
    db = SessionLocal()
    try:
        with tenant_scope(bypass=True):
            realm_id = (
                db.query(BankStatementImport.realm_id)  # CROSS-TENANT-OK: one-column legacy-message bootstrap; all row work below re-pins and filters this exact realm
                .filter(BankStatementImport.id == import_id)
                .scalar()
            )
        db.rollback()
        if realm_id is None:
            logger.warning("[bank_import_v2] import_id=%s not found", import_id)
            return {"status": "skipped", "reason": "not_found", "import_id": import_id}

        with tenant_scope_for_realm(db, realm_id):
            batch = (
                db.query(BankStatementImport)
                .filter(
                    BankStatementImport.id == import_id,
                    BankStatementImport.realm_id == realm_id,
                )
                .with_for_update()
                .one_or_none()
            )
            if batch is None:
                return {"status": "skipped", "reason": "not_found", "import_id": import_id}
            if (
                batch.lifecycle_status == ImportLifecycleStatus.PARSED.value
                or batch.status in {"parsed", "matched", "complete"}
            ):
                return {
                    "status": "skipped",
                    "reason": "already_parsed",
                    "import_id": import_id,
                }

            organization = organization_for_company(db, realm_id)
            organization_company = (
                organization_company_for_realm(
                    db,
                    organization_id=organization.id,
                    realm_id=realm_id,
                )
                if organization is not None
                else None
            )
            if organization is None or organization_company is None:
                if batch.lifecycle_status == ImportLifecycleStatus.PROCESSING.value:
                    _transition_import(batch, ImportLifecycleStatus.REVIEW_REQUIRED)
                elif batch.lifecycle_status == ImportLifecycleStatus.QUEUED.value:
                    _transition_import(batch, ImportLifecycleStatus.REVIEW_REQUIRED)
                batch.review_reason_code = "tenant_mapping_missing"
                batch.review_action = "retry"
                batch.status = "error"
                db.commit()
                return {
                    "status": "error",
                    "error": "The statement is preserved for review because its company mapping is unavailable.",
                    "import_id": import_id,
                }

            # The dispatcher owns the one descriptor/outbox boundary for both
            # current HTTP uploads and legacy import-id messages.  Its no-chunk
            # fallback upgrades this historical header, persists source-identity
            # failures as actionable review state, and commits the stable graph
            # before the first broker call.  Keeping that work in one seam avoids
            # a compatibility-only prepare path with weaker failure semantics.
            receipt = dispatch_pending_import(
                db,
                import_id=import_id,
                realm_id=realm_id,
            )
            if not receipt.delivery_ids:
                reviewed = db.execute(
                    select(BankStatementImport).where(
                        BankStatementImport.id == import_id,
                        BankStatementImport.realm_id == realm_id,
                    )
                ).scalar_one()
                if reviewed.status == "error":
                    return {
                        "status": "error",
                        "error": reviewed.error_detail
                        or "The statement is preserved for review and requires an updated source.",
                        "import_id": import_id,
                    }
            return {
                "status": "queued",
                "import_id": import_id,
                "delivery_count": receipt.created_count,
            }
    except Exception as exc:  # noqa: BLE001 - compatibility task must preserve accepted uploads
        db.rollback()
        logger.error(
            "[bank_import_v2] import_id=%s preparation failed: %s",
            import_id,
            redact_to_string(exc),
        )
        # The 60-second reaper can recover already-created descriptors.  If
        # preparation itself failed, C2-b records the exact review state where
        # possible; never fall back to the retired paid whole-document parser.
        return {
            "status": "error",
            "error": _UNEXPECTED_ERROR,
            "import_id": import_id,
        }
    finally:
        db.close()

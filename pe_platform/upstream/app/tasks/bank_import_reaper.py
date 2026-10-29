"""Durable outbox and expired-lease recovery for bank-parser v2."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import random
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import OperationalError as DatabaseOperationalError
from sqlalchemy.orm import Session

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import (
    BankStatementImport,
    BankStatementParseChunk,
    BankStatementParseDeliveryAttempt,
    BankStatementParseProviderAttempt,
)
from ..services.bank_recon.parser_types import (
    ChunkLifecycleStatus,
    DeliveryLifecycleStatus,
    ImportLifecycleStatus,
)
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope_for_realm, tenant_scope_live
from .bank_import import (
    BANK_IMPORT_BROKER_MESSAGE_EXPIRES_SECONDS,
    BANK_IMPORT_DISPATCH_STALE_SECONDS,
    _adjudicate_retry_from_durable_evidence,
    _create_delivery_locked,
    _is_proven_provider_timeout,
    _lock_chunk,
    _publish_delivery,
    _provider_call_possible,
    _route_chunk_to_review_locked,
    _stage_retry_delivery_locked,
    _transition_chunk,
    _transition_chunk_to_review_for_recovery,
    _transition_delivery,
    _transition_import,
)

logger = logging.getLogger(__name__)

_SWEEP_LIMIT = 500


def _cross_tenant_candidates(
    *,
    now: datetime,
) -> tuple[
    tuple[tuple[UUID, int, str], ...],
    tuple[tuple[UUID, str], ...],
    tuple[tuple[UUID, str], ...],
    tuple[tuple[UUID, str], ...],
    tuple[tuple[UUID, int, str], ...],
    tuple[tuple[UUID, int, str], ...],
]:
    """Census IDs only; every mutation below re-pins the row's exact realm."""
    db = SessionLocal()
    try:
        dispatch_cutoff = now - timedelta(seconds=BANK_IMPORT_DISPATCH_STALE_SECONDS)
        broker_expiry_cutoff = now - timedelta(
            seconds=BANK_IMPORT_BROKER_MESSAGE_EXPIRES_SECONDS
        )
        with tenant_scope_live(db, bypass=True):
            stale_chunks = tuple(
                db.execute(
                    select(
                        BankStatementParseChunk.id,
                        BankStatementParseChunk.bank_statement_import_id,
                        BankStatementParseChunk.realm_id,
                    )
                    .where(
                        BankStatementParseChunk.lifecycle_status
                        == ChunkLifecycleStatus.LEASED.value,
                        BankStatementParseChunk.lease_expires_at < now,
                    )
                    .order_by(BankStatementParseChunk.lease_expires_at)
                    .limit(_SWEEP_LIMIT)
                ).all()
            )
            unpublished = tuple(
                db.execute(
                    select(
                        BankStatementParseDeliveryAttempt.id,
                        BankStatementParseDeliveryAttempt.realm_id,
                    )
                    .where(
                        BankStatementParseDeliveryAttempt.lifecycle_status
                        == DeliveryLifecycleStatus.CREATED.value,
                    )
                    .order_by(BankStatementParseDeliveryAttempt.started_at)
                    .limit(_SWEEP_LIMIT)
                ).all()
            )
            uncertain = tuple(
                db.execute(
                    select(
                        BankStatementParseDeliveryAttempt.id,
                        BankStatementParseDeliveryAttempt.realm_id,
                    )
                    .where(
                        BankStatementParseDeliveryAttempt.lifecycle_status
                        == DeliveryLifecycleStatus.DISPATCHED.value,
                        BankStatementParseDeliveryAttempt.status.in_(
                            ("dispatch_initiated", "dispatched")
                        ),
                        BankStatementParseDeliveryAttempt.started_at < dispatch_cutoff,
                    )
                    .order_by(BankStatementParseDeliveryAttempt.started_at)
                    .limit(_SWEEP_LIMIT)
                ).all()
            )
            acknowledged_expired = tuple(
                db.execute(
                    select(
                        BankStatementParseDeliveryAttempt.id,
                        BankStatementParseDeliveryAttempt.realm_id,
                    )
                    .where(
                        BankStatementParseDeliveryAttempt.lifecycle_status
                        == DeliveryLifecycleStatus.DISPATCHED.value,
                        BankStatementParseDeliveryAttempt.status
                        == "dispatch_acknowledged",
                        BankStatementParseDeliveryAttempt.started_at
                        < broker_expiry_cutoff,
                    )
                    .order_by(BankStatementParseDeliveryAttempt.started_at)
                    .limit(_SWEEP_LIMIT)
                ).all()
            )
            active_delivery = exists(
                select(BankStatementParseDeliveryAttempt.id).where(
                    BankStatementParseDeliveryAttempt.chunk_id
                    == BankStatementParseChunk.id,
                    BankStatementParseDeliveryAttempt.realm_id
                    == BankStatementParseChunk.realm_id,
                    BankStatementParseDeliveryAttempt.lifecycle_status.in_(
                        (
                            DeliveryLifecycleStatus.CREATED.value,
                            DeliveryLifecycleStatus.DISPATCHED.value,
                            DeliveryLifecycleStatus.STARTED.value,
                        )
                    ),
                )
            )
            orphaned_pending = tuple(
                db.execute(
                    select(
                        BankStatementParseChunk.id,
                        BankStatementParseChunk.bank_statement_import_id,
                        BankStatementParseChunk.realm_id,
                    )
                    .where(
                        BankStatementParseChunk.lifecycle_status
                        == ChunkLifecycleStatus.PENDING.value,
                        ~active_delivery,
                    )
                    .order_by(BankStatementParseChunk.started_at)
                    .limit(_SWEEP_LIMIT)
                ).all()
            )
            failed_chunks = tuple(
                db.execute(
                    select(
                        BankStatementParseChunk.id,
                        BankStatementParseChunk.bank_statement_import_id,
                        BankStatementParseChunk.realm_id,
                    )
                    .where(
                        BankStatementParseChunk.lifecycle_status
                        == ChunkLifecycleStatus.FAILED.value,
                    )
                    .order_by(BankStatementParseChunk.started_at)
                    .limit(_SWEEP_LIMIT)
                ).all()
            )
        db.rollback()
        return (
            stale_chunks,
            unpublished,
            uncertain,
            acknowledged_expired,
            orphaned_pending,
            failed_chunks,
        )
    finally:
        db.close()


def _route_expired_chunk_to_review(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
    delivery: BankStatementParseDeliveryAttempt | None,
    now: datetime,
    reason_code: str,
    provider_attempt_count: int,
) -> None:
    """Preserve an uncertain paid-call boundary without automatic re-billing."""
    if delivery is not None:
        _transition_delivery(delivery, DeliveryLifecycleStatus.EXPIRED)
        delivery.status = reason_code
        delivery.completed_at = now
        delivery.provider_calls_started = max(
            int(delivery.provider_calls_started or 0),
            provider_attempt_count,
        )
        delivery.telemetry_status = "telemetry_incomplete"
        delivery.evidence_eligible = False
    _transition_chunk(chunk, ChunkLifecycleStatus.REVIEW_REQUIRED)
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
    statement_import.review_action = "reupload"
    statement_import.parse_telemetry_status = "telemetry_incomplete"
    statement_import.parse_evidence_eligible = False


def _reap_one_chunk(
    *,
    chunk_id: UUID,
    import_id: int,
    realm_id: str,
    now: datetime,
) -> tuple[UUID | None, bool]:
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            chunk = _lock_chunk(
                db,
                chunk_id=chunk_id,
                import_id=import_id,
                realm_id=realm_id,
            )
            if (
                chunk is None
                or chunk.lifecycle_status != ChunkLifecycleStatus.LEASED.value
                or chunk.lease_expires_at is None
            ):
                db.rollback()
                return None, False
            expires_at = chunk.lease_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at >= now:
                db.rollback()
                return None, False

            delivery = db.execute(
                select(BankStatementParseDeliveryAttempt)
                .where(
                    BankStatementParseDeliveryAttempt.chunk_id == chunk_id,
                    BankStatementParseDeliveryAttempt.realm_id == realm_id,
                    BankStatementParseDeliveryAttempt.lifecycle_status
                    == DeliveryLifecycleStatus.STARTED.value,
                )
                .order_by(BankStatementParseDeliveryAttempt.delivery_ordinal.desc())
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if delivery is None:
                _route_expired_chunk_to_review(
                    db,
                    chunk=chunk,
                    delivery=None,
                    now=now,
                    reason_code="started_delivery_missing",
                    provider_attempt_count=0,
                )
                db.commit()
                return None, True

            evidence = _adjudicate_retry_from_durable_evidence(
                db,
                chunk=chunk,
                delivery=delivery,
            )
            if evidence.provider_call_possible:
                successor = _stage_retry_delivery_locked(
                    db,
                    chunk=chunk,
                    delivery=delivery,
                    evidence=evidence,
                    now=now,
                )
                if successor is not None:
                    successor_id = successor.id
                    db.commit()
                    return successor_id, False
                _route_expired_chunk_to_review(
                    db,
                    chunk=chunk,
                    delivery=delivery,
                    now=now,
                    reason_code=evidence.reason_code,
                    provider_attempt_count=evidence.provider_attempt_count,
                )
                db.commit()
                return None, True

            if not evidence.retry_permitted:
                _route_expired_chunk_to_review(
                    db,
                    chunk=chunk,
                    delivery=delivery,
                    now=now,
                    reason_code=evidence.reason_code,
                    provider_attempt_count=evidence.provider_attempt_count,
                )
                db.commit()
                return None, True

            _transition_delivery(delivery, DeliveryLifecycleStatus.EXPIRED)
            delivery.status = "lease_expired_before_provider"
            delivery.completed_at = now
            delivery.telemetry_status = "complete"
            delivery.evidence_eligible = True

            _transition_chunk(chunk, ChunkLifecycleStatus.PENDING)
            chunk.application_retry_count = int(chunk.application_retry_count or 0) + 1
            chunk.status = "started"
            chunk.lease_token = None
            chunk.lease_expires_at = None
            chunk.heartbeat_at = None
            successor_delivery = _create_delivery_locked(db, chunk=chunk)
            successor_id = successor_delivery.id
            db.commit()
            return successor_id, False
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _recover_expired_acknowledged_delivery(
    *,
    delivery_id: UUID,
    realm_id: str,
    now: datetime,
) -> UUID | None:
    """Replace one broker-ACKed message that expired before any worker claim.

    A Celery ACK proves only that the broker accepted the message.  The message can
    still reach its explicit expiry before a worker starts it.  Once that TTL has
    passed, expire the dead delivery and append a fresh broker delivery ordinal for
    the same still-pending logical chunk.  This is transport recovery, not a parser
    retry: no provider-attempt budget or application retry counter is consumed.
    """
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            delivery = db.execute(
                select(BankStatementParseDeliveryAttempt)
                .where(
                    BankStatementParseDeliveryAttempt.id == delivery_id,
                    BankStatementParseDeliveryAttempt.realm_id == realm_id,
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if (
                delivery is None
                or delivery.lifecycle_status
                != DeliveryLifecycleStatus.DISPATCHED.value
                or delivery.status != "dispatch_acknowledged"
            ):
                db.rollback()
                return None
            started_at = delivery.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if started_at + timedelta(
                seconds=BANK_IMPORT_BROKER_MESSAGE_EXPIRES_SECONDS
            ) >= now:
                db.rollback()
                return None

            chunk = _lock_chunk(
                db,
                chunk_id=delivery.chunk_id,
                import_id=delivery.bank_statement_import_id,
                realm_id=realm_id,
            )
            if (
                chunk is None
                or chunk.lifecycle_status != ChunkLifecycleStatus.PENDING.value
            ):
                db.rollback()
                return None

            _transition_delivery(delivery, DeliveryLifecycleStatus.EXPIRED)
            delivery.status = "broker_ack_expired_before_start"
            delivery.completed_at = now
            delivery.telemetry_status = "complete"
            delivery.evidence_eligible = True
            successor = _create_delivery_locked(db, chunk=chunk)
            successor_id = successor.id
            db.commit()
            return successor_id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _pending_recovery_is_safe(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
    latest_delivery: BankStatementParseDeliveryAttempt | None,
) -> tuple[bool, str]:
    """Prove that appending a missing descriptor cannot add a paid attempt."""
    attempts = tuple(
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
    retry_count = int(chunk.application_retry_count or 0)
    if retry_count == 0:
        if attempts:
            return False, "provider_call_state_unknown"
        if latest_delivery is None:
            return True, "initial_descriptor_missing"
        if (
            latest_delivery.lifecycle_status == DeliveryLifecycleStatus.FAILED.value
            and latest_delivery.status == "publish_failed"
        ) or (
            latest_delivery.lifecycle_status == DeliveryLifecycleStatus.EXPIRED.value
            and latest_delivery.status == "lease_expired_before_provider"
        ):
            return True, "delivery_failed_before_provider"
        return False, "pending_delivery_history_invalid"

    if retry_count != 1:
        return False, "retry_budget_exhausted"
    if not _provider_call_possible(
        db,
        import_id=chunk.bank_statement_import_id,
        chunk_id=chunk.id,
        realm_id=chunk.realm_id,
    ):
        if attempts:
            return False, "unexpected_provider_attempt"
        if latest_delivery is None or latest_delivery.status in {
            "deterministic_retry",
            "publish_failed",
            "lease_expired_before_provider",
        }:
            return True, "deterministic_retry_descriptor_missing"
        return False, "pending_delivery_history_invalid"

    if len(attempts) != 1 or attempts[0].attempt_ordinal != 0:
        return False, "provider_attempt_history_incomplete"
    prior_attempt = attempts[0]
    prior_delivery = db.execute(
        select(BankStatementParseDeliveryAttempt).where(
            BankStatementParseDeliveryAttempt.id == prior_attempt.delivery_attempt_id,
            BankStatementParseDeliveryAttempt.chunk_id == chunk.id,
            BankStatementParseDeliveryAttempt.realm_id == chunk.realm_id,
        )
    ).scalar_one_or_none()
    if prior_delivery is None or not _is_proven_provider_timeout(
        db,
        attempt=prior_attempt,
        delivery=prior_delivery,
        chunk=chunk,
        expected_attempt_ordinal=0,
    ):
        reason_by_lifecycle = {
            "succeeded": "provider_succeeded_result_uncommitted",
            "truncated": "provider_output_truncated",
            "failed": "provider_failed_terminal",
            "started": "provider_outcome_unknown",
            "timed_out": "provider_timeout_evidence_incomplete",
        }
        return False, reason_by_lifecycle.get(
            str(prior_attempt.lifecycle_status or ""),
            "provider_attempt_evidence_invalid",
        )
    if (
        prior_delivery.lifecycle_status != DeliveryLifecycleStatus.FAILED.value
        or prior_delivery.status != "provider_timeout"
    ):
        return False, "provider_delivery_history_invalid"
    if (latest_delivery is not None and latest_delivery.id == prior_delivery.id) or (
        latest_delivery is not None
        and latest_delivery.delivery_ordinal > prior_delivery.delivery_ordinal
        and (
            (
                latest_delivery.lifecycle_status
                == DeliveryLifecycleStatus.FAILED.value
                and latest_delivery.status == "publish_failed"
            )
            or (
                latest_delivery.lifecycle_status
                == DeliveryLifecycleStatus.EXPIRED.value
                and latest_delivery.status == "lease_expired_before_provider"
            )
        )
    ):
        return True, "provider_timeout_retry_descriptor_missing"
    return False, "provider_call_state_unknown"


def _route_orphaned_pending_to_review(
    db: Session,
    *,
    chunk: BankStatementParseChunk,
    reason_code: str,
    now: datetime,
) -> None:
    """Fail closed for a PENDING row whose paid-call boundary is not provable."""
    _transition_chunk_to_review_for_recovery(chunk)
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
    statement_import.review_action = "reupload"
    statement_import.parse_telemetry_status = "telemetry_incomplete"
    statement_import.parse_evidence_eligible = False


def _recover_orphaned_pending_chunk(
    *,
    chunk_id: UUID,
    import_id: int,
    realm_id: str,
) -> tuple[UUID | None, bool]:
    """Close both the prepare->outbox and definite-publish-failure gaps."""
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            chunk = _lock_chunk(
                db,
                chunk_id=chunk_id,
                import_id=import_id,
                realm_id=realm_id,
            )
            if chunk is None or chunk.lifecycle_status != ChunkLifecycleStatus.PENDING.value:
                db.rollback()
                return None, False
            latest_delivery = db.execute(
                select(BankStatementParseDeliveryAttempt)
                .where(
                    BankStatementParseDeliveryAttempt.chunk_id == chunk.id,
                    BankStatementParseDeliveryAttempt.realm_id == chunk.realm_id,
                )
                .order_by(BankStatementParseDeliveryAttempt.delivery_ordinal.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest_delivery is not None and latest_delivery.lifecycle_status in {
                DeliveryLifecycleStatus.CREATED.value,
                DeliveryLifecycleStatus.DISPATCHED.value,
                DeliveryLifecycleStatus.STARTED.value,
            }:
                db.rollback()
                return None, False
            safe, reason_code = _pending_recovery_is_safe(
                db,
                chunk=chunk,
                latest_delivery=latest_delivery,
            )
            if not safe:
                _route_orphaned_pending_to_review(
                    db,
                    chunk=chunk,
                    reason_code=reason_code,
                    now=datetime.now(timezone.utc),
                )
                db.commit()
                return None, True
            delivery = _create_delivery_locked(db, chunk=chunk)
            delivery_id = delivery.id
            db.commit()
            return delivery_id, False
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _recover_failed_chunk(
    *,
    chunk_id: UUID,
    import_id: int,
    realm_id: str,
    now: datetime,
) -> tuple[UUID | None, bool]:
    """Repair rows committed between the retired FAILED and descriptor commits."""
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            chunk = _lock_chunk(
                db,
                chunk_id=chunk_id,
                import_id=import_id,
                realm_id=realm_id,
            )
            if chunk is None or chunk.lifecycle_status != ChunkLifecycleStatus.FAILED.value:
                db.rollback()
                return None, False
            delivery = db.execute(
                select(BankStatementParseDeliveryAttempt)
                .where(
                    BankStatementParseDeliveryAttempt.chunk_id == chunk_id,
                    BankStatementParseDeliveryAttempt.realm_id == realm_id,
                    BankStatementParseDeliveryAttempt.lifecycle_status
                    == DeliveryLifecycleStatus.FAILED.value,
                )
                .order_by(BankStatementParseDeliveryAttempt.delivery_ordinal.desc())
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if delivery is None:
                # A FAILED chunk without its delivery identity cannot prove whether
                # a provider call happened.  Preserve it for review; never invent a
                # descriptor that could spend again.
                _transition_chunk_to_review_for_recovery(chunk)
                chunk.status = "error"
                chunk.completed_at = now
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
                statement_import.review_reason_code = "failed_delivery_missing"
                statement_import.review_action = "reupload"
                db.commit()
                return None, True

            evidence = _adjudicate_retry_from_durable_evidence(
                db,
                chunk=chunk,
                delivery=delivery,
            )
            successor = _stage_retry_delivery_locked(
                db,
                chunk=chunk,
                delivery=delivery,
                evidence=evidence,
                now=now,
            )
            if successor is not None:
                successor_id = successor.id
                db.commit()
                return successor_id, False
            _route_chunk_to_review_locked(
                db,
                chunk=chunk,
                delivery=delivery,
                reason_code=evidence.reason_code,
                provider_attempt_count=evidence.provider_attempt_count,
                now=now,
            )
            db.commit()
            return None, True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _publish_recovered_delivery(
    *,
    delivery_id: UUID,
    realm_id: str,
    allow_dispatched_resend: bool,
) -> str:
    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            return _publish_delivery(
                db,
                delivery_id=delivery_id,
                realm_id=realm_id,
                allow_dispatched_resend=allow_dispatched_resend,
            )
    finally:
        db.close()


def reap_stale_bank_import_work_once(
    *,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """Run one idempotent recovery pass without Celery retry machinery."""
    swept_at = now or datetime.now(timezone.utc)
    (
        stale_chunks,
        unpublished,
        uncertain,
        acknowledged_expired,
        orphaned_pending,
        failed_chunks,
    ) = _cross_tenant_candidates(now=swept_at)
    reaped = 0
    routed_to_review = 0
    orphaned_recovered = 0
    orphaned_routed_to_review = 0
    failed_recovered = 0
    failed_routed_to_review = 0
    created_published = 0
    same_id_resent = 0
    acknowledged_expired_recovered = 0

    recovered: list[tuple[UUID, str]] = []
    for chunk_id, import_id, realm_id in stale_chunks:
        successor_id, review_required = _reap_one_chunk(
            chunk_id=chunk_id,
            import_id=import_id,
            realm_id=realm_id,
            now=swept_at,
        )
        routed_to_review += int(review_required)
        if successor_id is not None:
            reaped += 1
            recovered.append((successor_id, realm_id))

    for chunk_id, import_id, realm_id in orphaned_pending:
        delivery_id, review_required = _recover_orphaned_pending_chunk(
            chunk_id=chunk_id,
            import_id=import_id,
            realm_id=realm_id,
        )
        orphaned_routed_to_review += int(review_required)
        if delivery_id is not None:
            orphaned_recovered += 1
            recovered.append((delivery_id, realm_id))

    for chunk_id, import_id, realm_id in failed_chunks:
        delivery_id, review_required = _recover_failed_chunk(
            chunk_id=chunk_id,
            import_id=import_id,
            realm_id=realm_id,
            now=swept_at,
        )
        failed_routed_to_review += int(review_required)
        if delivery_id is not None:
            failed_recovered += 1
            recovered.append((delivery_id, realm_id))

    for delivery_id, realm_id in acknowledged_expired:
        successor_id = _recover_expired_acknowledged_delivery(
            delivery_id=delivery_id,
            realm_id=realm_id,
            now=swept_at,
        )
        if successor_id is not None:
            acknowledged_expired_recovered += 1
            recovered.append((successor_id, realm_id))

    # Rows found CREATED and successor rows created above are both durable outbox
    # entries.  A duplicate ID is harmless; de-duplicate the local worklist anyway.
    created_work = {
        (delivery_id, realm_id)
        for delivery_id, realm_id in (*unpublished, *recovered)
    }
    for delivery_id, realm_id in created_work:
        if _publish_recovered_delivery(
            delivery_id=delivery_id,
            realm_id=realm_id,
            allow_dispatched_resend=False,
        ) in {"published", "uncertain"}:
            created_published += 1

    for delivery_id, realm_id in uncertain:
        if _publish_recovered_delivery(
            delivery_id=delivery_id,
            realm_id=realm_id,
            allow_dispatched_resend=True,
        ) in {"published", "uncertain"}:
            same_id_resent += 1

    return {
        "status": "done",
        "expired_leases_reaped": reaped,
        "expired_leases_routed_to_review": routed_to_review,
        "orphaned_pending_recovered": orphaned_recovered,
        "orphaned_pending_routed_to_review": orphaned_routed_to_review,
        "failed_chunks_recovered": failed_recovered,
        "failed_chunks_routed_to_review": failed_routed_to_review,
        "created_deliveries_published": created_published,
        "same_id_deliveries_resent": same_id_resent,
        "acknowledged_expired_recovered": acknowledged_expired_recovered,
    }


@celery_app.task(
    bind=True,
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
    name="app.tasks.bank_import_reaper.reap_stale_bank_import_work",
)
def reap_stale_bank_import_work(self) -> dict[str, int | str]:
    """Beat entry: expire stale claims and recover durable broker outbox rows."""
    try:
        return reap_stale_bank_import_work_once()
    except DatabaseOperationalError as exc:
        backoff = min(60, 5 * (2 ** self.request.retries))
        countdown = backoff + random.uniform(0, backoff * 0.25)
        raise self.retry(exc=exc, countdown=countdown) from exc
    except Exception as exc:  # noqa: BLE001 - one loud sweep failure, then bounded retry
        logger.error(
            "[bank_import_reaper] sweep failed: %s",
            redact_to_string(exc),
        )
        backoff = min(60, 5 * (2 ** self.request.retries))
        countdown = backoff + random.uniform(0, backoff * 0.25)
        raise self.retry(exc=exc, countdown=countdown) from exc


__all__ = ["reap_stale_bank_import_work", "reap_stale_bank_import_work_once"]

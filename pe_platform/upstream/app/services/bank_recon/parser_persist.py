"""Durable merge, review, control, and projection for bank parser v2.

This module is the persistence boundary between pure parser value objects and the
SQLAlchemy schema.  Provider output is staged evidence only; only deterministic
code or a candidate-bound human audit row can satisfy statement control.  Public
mutators flush but never commit so the task/router caller owns one atomic unit of
work and its rollback.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from typing import cast
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    BankFeedItem,
    BankStatementControlCertificate,
    BankStatementImport,
    BankStatementParseCandidate,
    BankStatementParseChunk,
    BankStatementParseDeliveryAttempt,
    BankStatementParseProviderAttempt,
    OrganizationCompany,
    SystemAuditLog,
)
from .parser_ambiguous import LLMAmbiguousSpanResolver, _parse_proposal
from .parser_control import (
    calculate_control_certificate,
    resolution_audit_binding_sha256,
)
from .parser_deterministic import DeterministicBankStatementParser
from .parser_extract import extract_pdf
from .parser_types import (
    AMBIGUOUS_MAX_APPLICATION_ATTEMPTS,
    AmbiguityDisposition,
    AmbiguityParseRequest,
    AmbiguityParseResult,
    AmbiguousSpanResolver,
    CandidateResolutionAuthority,
    CandidateResolutionEvidence,
    CandidateResolutionStatus,
    ChunkLifecycleStatus,
    ControlCertificate,
    ControlCertificateStatus,
    ExpectedSourceLine,
    ImportLifecycleStatus,
    LogicalChunkDescriptor,
    ParseRoute,
    ParsedCandidate,
    ParserIdentity,
    SourceManifest,
    SourceSpan,
    TransactionType,
)

_CANDIDATE_NAMESPACE = UUID("fe5be067-a020-5a95-ad4c-730d25158d4a")
_TERMINAL_CHUNK_STATES = {
    ChunkLifecycleStatus.SUCCEEDED.value,
    ChunkLifecycleStatus.REVIEW_REQUIRED.value,
}
_ALL_TERMINAL_CHUNK_STATES = _TERMINAL_CHUNK_STATES | {
    ChunkLifecycleStatus.FAILED.value,
}
_HUMAN_ACTIONS = {
    CandidateResolutionStatus.ACCEPTED: "BANK_PARSE_CANDIDATE_ACCEPT",
    CandidateResolutionStatus.CORRECTED: "BANK_PARSE_CANDIDATE_CORRECT",
    CandidateResolutionStatus.REJECTED: "BANK_PARSE_CANDIDATE_REJECT",
}


class ParserPersistenceError(RuntimeError):
    """Base domain error for the resumable persistence boundary."""


class ImportNotFoundError(ParserPersistenceError):
    """The import is absent from the already-authorized realm."""


class CandidateNotFoundError(ParserPersistenceError):
    """The candidate is absent from the already-authorized import."""


class InvalidParserStateError(ParserPersistenceError):
    """A requested lifecycle operation is not legal in the current state."""


class SourceIdentityError(ParserPersistenceError):
    """Retained bytes, parser identity, or manifest structure drifted."""


class LeaseMismatchError(ParserPersistenceError):
    """A delivery does not own the chunk's current lease."""


@dataclass(frozen=True, slots=True)
class ImportSummary:
    import_id: int
    account_id: str
    account_name: str | None
    source: str
    lifecycle_status: str
    status: str
    review_reason_code: str | None
    review_action: str | None
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    line_count: int | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChunkProgress:
    chunk_id: UUID
    chunk_revision: int
    lifecycle_status: str
    page_start: int
    page_end: int
    span_start: int
    span_end: int
    candidate_count: int


@dataclass(frozen=True, slots=True)
class DeliveryProgress:
    delivery_id: UUID
    chunk_id: UUID
    delivery_ordinal: int
    lifecycle_status: str
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CandidateView:
    candidate_id: UUID
    chunk_id: UUID
    source_ordinal: int
    source_page: int
    source_text: str
    txn_date: date | None
    signed_amount: Decimal | None
    description: str | None
    txn_type: str | None
    resolution_status: str
    parse_route: str | None
    authority: str | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ControlView:
    certificate_version: int
    status: str
    expected_count: int
    observed_count: int
    accepted_count: int
    rejected_count: int
    opening_balance: Decimal | None
    signed_activity: Decimal | None
    closing_balance: Decimal | None
    residual: Decimal | None
    calculated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ImportProgress:
    summary: ImportSummary
    chunks: tuple[ChunkProgress, ...]
    deliveries: tuple[DeliveryProgress, ...]
    candidates: tuple[CandidateView, ...]
    control: ControlView | None


@dataclass(frozen=True, slots=True)
class ImportListPage:
    items: tuple[ImportSummary, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class PreparedImport:
    import_id: int
    chunk_ids: tuple[UUID, ...]
    already_prepared: bool


@dataclass(frozen=True, slots=True)
class ChunkProcessOutcome:
    import_id: int
    chunk_id: UUID
    lifecycle_status: str
    reason_code: str | None
    retryable: bool
    candidate_count: int


@dataclass(frozen=True, slots=True)
class RetryPreparation:
    import_id: int
    chunk_ids: tuple[UUID, ...]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_import(
    db: Session,
    *,
    statement_import_id: int,
    realm_id: str,
    for_update: bool = False,
) -> BankStatementImport:
    statement = select(BankStatementImport).where(
        BankStatementImport.id == statement_import_id,
        BankStatementImport.realm_id == realm_id,
    )
    if for_update:
        statement = statement.with_for_update()
    batch = db.execute(statement).scalar_one_or_none()
    if batch is None:
        raise ImportNotFoundError("bank statement import was not found in this realm")
    return batch


def _load_source_bytes(batch: BankStatementImport, source_bytes: bytes | None) -> bytes:
    if source_bytes is not None:
        return source_bytes
    from .ingest import fetch_original

    retained = fetch_original(batch.storage_key)
    if retained is None:
        raise SourceIdentityError("retained statement source is unavailable")
    return retained


def _identity_from_import(batch: BankStatementImport) -> ParserIdentity:
    values = (
        batch.source_sha256,
        batch.parser_version,
        batch.extractor_version,
        batch.prompt_schema_version,
    )
    if any(value is None for value in values):
        raise SourceIdentityError("statement import does not carry a complete v2 identity")
    return ParserIdentity(
        source_sha256=cast(str, batch.source_sha256),
        parser_version=cast(str, batch.parser_version),
        extractor_version=cast(str, batch.extractor_version),
        prompt_schema_version=cast(str, batch.prompt_schema_version),
    )


def _ensure_import_identity(batch: BankStatementImport, source_bytes: bytes) -> ParserIdentity:
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if batch.lineage_status in {"legacy_unbound", "legacy_unverified"}:
        legacy_values = (
            batch.source_sha256,
            batch.parser_version,
            batch.extractor_version,
            batch.prompt_schema_version,
        )
        if any(value is not None for value in legacy_values):
            raise SourceIdentityError("legacy import carries a partial v2 identity")
        identity = ParserIdentity.current(source_sha256)
        batch.source_sha256 = identity.source_sha256
        batch.parser_version = identity.parser_version
        batch.extractor_version = identity.extractor_version
        batch.prompt_schema_version = identity.prompt_schema_version
        batch.lineage_status = "controlled"
        return identity
    identity = _identity_from_import(batch)
    if identity != ParserIdentity.current(source_sha256):
        raise SourceIdentityError("retained statement bytes do not match import identity")
    return identity


def _parse_source(batch: BankStatementImport, source_bytes: bytes):
    identity = _ensure_import_identity(batch, source_bytes)
    try:
        statement = extract_pdf(source_bytes)
        if statement.source_sha256 != identity.source_sha256:
            raise SourceIdentityError("extracted source hash does not match import identity")
        result = DeterministicBankStatementParser().parse(
            statement,
            identity=identity,
            statement_import_id=batch.id,
            realm_id=batch.realm_id,
        )
    except SourceIdentityError:
        raise
    except (TypeError, ValueError) as exc:
        raise SourceIdentityError(
            "statement source could not be deterministically reconstructed"
        ) from exc
    return statement, result


def _stable_ambiguous_candidate_id(
    identity: ParserIdentity,
    *,
    statement_import_id: int,
    realm_id: str,
    chunk_id: UUID,
    source_ordinal: int,
) -> UUID:
    return uuid5(
        _CANDIDATE_NAMESPACE,
        "\x1f".join(
            (
                identity.source_sha256,
                identity.parser_version,
                identity.extractor_version,
                identity.prompt_schema_version,
                str(statement_import_id),
                realm_id,
                str(chunk_id),
                str(source_ordinal),
            )
        ),
    )


def _candidate_values(candidate: ParsedCandidate) -> dict[str, object]:
    txn_type = (
        candidate.txn_type.value
        if isinstance(candidate.txn_type, TransactionType)
        else candidate.txn_type
    )
    return {
        "statement_import_id": candidate.statement_import_id,
        "realm_id": candidate.realm_id,
        "candidate_id": str(candidate.candidate_id),
        "chunk_id": str(candidate.chunk_id),
        "source_ordinal": candidate.source_ordinal,
        "source_page": candidate.source_page,
        "source_text_sha256": hashlib.sha256(
            candidate.source_text.encode("utf-8")
        ).hexdigest(),
        "resolution_status": CandidateResolutionStatus(
            candidate.resolution_status
        ).value,
        "txn_date": candidate.txn_date.isoformat() if candidate.txn_date else None,
        "signed_amount": (
            format(candidate.signed_amount, ".2f")
            if candidate.signed_amount is not None
            else None
        ),
        "description": candidate.description,
        "txn_type": txn_type,
    }


def _write_candidate_row(
    row: BankStatementParseCandidate,
    candidate: ParsedCandidate,
) -> None:
    if row.id != candidate.candidate_id or row.source_ordinal != candidate.source_ordinal:
        raise SourceIdentityError("candidate identity changed during persistence")
    row.chunk_id = candidate.chunk_id
    row.source_page = candidate.source_page
    row.source_sha256 = candidate.identity.source_sha256
    row.parser_version = candidate.identity.parser_version
    row.extractor_version = candidate.identity.extractor_version
    row.prompt_schema_version = candidate.identity.prompt_schema_version
    row.source_text = candidate.source_text
    row.txn_date = candidate.txn_date
    row.signed_amount = candidate.signed_amount
    row.description = candidate.description
    row.txn_type = (
        candidate.txn_type.value
        if isinstance(candidate.txn_type, TransactionType)
        else candidate.txn_type
    )
    row.resolution_status = CandidateResolutionStatus(candidate.resolution_status).value
    evidence = candidate.resolution_evidence
    row.resolution_payload = (
        evidence.to_payload()
        if isinstance(evidence, CandidateResolutionEvidence)
        else cast(dict[str, object], evidence)
    )


def prepare_resumable_import(
    db: Session,
    *,
    statement_import_id: int,
    realm_id: str,
    organization_id: int,
    organization_company_id: int,
    source_bytes: bytes | None = None,
) -> PreparedImport:
    """Persist deterministic descriptors and every expected candidate before publish."""
    if organization_id < 1 or organization_company_id < 1:
        raise ValueError("organization identity must be positive")
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        for_update=True,
    )
    source = _load_source_bytes(batch, source_bytes)
    statement, result = _parse_source(batch, source)
    existing = tuple(
        db.execute(
            select(BankStatementParseChunk)
            .where(
                BankStatementParseChunk.bank_statement_import_id == batch.id,
                BankStatementParseChunk.realm_id == realm_id,
            )
            .order_by(BankStatementParseChunk.page_start, BankStatementParseChunk.id)
            .with_for_update()
        ).scalars()
    )
    if existing:
        expected = {descriptor.chunk_id: descriptor for descriptor in result.manifest.chunks}
        roots = tuple(chunk for chunk in existing if chunk.chunk_revision == 0)
        if set(expected) != {chunk.id for chunk in roots}:
            raise SourceIdentityError("persisted chunk roots differ from deterministic manifest")
        for chunk in roots:
            descriptor = expected[chunk.id]
            if (
                chunk.page_start,
                chunk.page_end,
                chunk.span_start,
                chunk.span_end,
            ) != (
                descriptor.page_start,
                descriptor.page_end,
                descriptor.span_start,
                descriptor.span_end,
            ):
                raise SourceIdentityError("persisted chunk geometry drifted")
        return PreparedImport(
            import_id=batch.id,
            chunk_ids=tuple(chunk.id for chunk in roots),
            already_prepared=True,
        )

    deterministic_by_ordinal = {
        candidate.source_ordinal: candidate for candidate in result.candidates
    }
    ambiguous_by_ordinal = {
        span.source_ordinal: span for span in result.ambiguous_spans
    }
    descriptors = result.manifest.chunks
    for index, descriptor in enumerate(descriptors):
        db.add(
            BankStatementParseChunk(
                id=descriptor.chunk_id,
                bank_statement_import_id=batch.id,
                delivery_attempt_id=None,
                realm_id=realm_id,
                organization_id=organization_id,
                organization_company_id=organization_company_id,
                chunk_index=index,
                source_sha256=descriptor.identity.source_sha256,
                source_size_bytes=len(source),
                parser_version=descriptor.identity.parser_version,
                extractor_version=descriptor.identity.extractor_version,
                prompt_schema_version=descriptor.identity.prompt_schema_version,
                page_start=descriptor.page_start,
                page_end=descriptor.page_end,
                span_start=descriptor.span_start,
                span_end=descriptor.span_end,
                chunk_revision=descriptor.chunk_revision,
                supersedes_chunk_id=descriptor.supersedes_chunk_id,
                lifecycle_status=ChunkLifecycleStatus.PENDING.value,
                application_retry_count=0,
                status="started",
                telemetry_status="pending",
                evidence_eligible=False,
            )
        )
    db.flush()

    ambiguous_evidence = CandidateResolutionEvidence(
        parse_route=ParseRoute.AMBIGUOUS,
        authority=CandidateResolutionAuthority.UNTRUSTED_MODEL,
        audit_log_id=None,
        audit_binding_sha256=None,
    )
    for expected_line in result.manifest.lines:
        candidate = deterministic_by_ordinal.get(expected_line.source_ordinal)
        if candidate is None:
            span = ambiguous_by_ordinal.get(expected_line.source_ordinal)
            if span is None:
                raise SourceIdentityError("manifest line has no deterministic or ambiguous evidence")
            candidate = ParsedCandidate(
                candidate_id=_stable_ambiguous_candidate_id(
                    result.manifest.identity,
                    statement_import_id=batch.id,
                    realm_id=realm_id,
                    chunk_id=span.chunk_id,
                    source_ordinal=span.source_ordinal,
                ),
                identity=result.manifest.identity,
                statement_import_id=batch.id,
                realm_id=realm_id,
                chunk_id=span.chunk_id,
                source_ordinal=span.source_ordinal,
                source_page=span.page_start,
                source_text=span.source_text,
                txn_date=None,
                signed_amount=None,
                description=None,
                txn_type=None,
                resolution_status=CandidateResolutionStatus.STAGED,
                resolution_evidence=ambiguous_evidence,
            )
        row = BankStatementParseCandidate(
            id=candidate.candidate_id,
            statement_import_id=batch.id,
            chunk_id=candidate.chunk_id,
            realm_id=realm_id,
            source_ordinal=candidate.source_ordinal,
            source_page=candidate.source_page,
            source_sha256=candidate.identity.source_sha256,
            parser_version=candidate.identity.parser_version,
            extractor_version=candidate.identity.extractor_version,
            prompt_schema_version=candidate.identity.prompt_schema_version,
            source_text=candidate.source_text,
            resolution_status=candidate.resolution_status.value,
        )
        _write_candidate_row(row, candidate)
        db.add(row)

    batch.opening_balance = result.opening_balance
    batch.closing_balance = result.closing_balance
    # Persisting work is not the worker claim.  The import remains queued until a
    # delivery owns a live lease; otherwise an accepted-but-not-yet-published import
    # falsely looks active and cannot be distinguished from a crashed worker.
    batch.lifecycle_status = (
        ImportLifecycleStatus.QUEUED.value
        if descriptors
        else ImportLifecycleStatus.REVIEW_REQUIRED.value
    )
    batch.status = "parsing" if descriptors else "error"
    batch.review_reason_code = None if descriptors else "source_text_unavailable"
    batch.review_action = None if descriptors else "reupload"
    batch.error_detail = None if descriptors else "No controlled text spans were extracted."
    db.flush()
    return PreparedImport(
        import_id=batch.id,
        chunk_ids=tuple(descriptor.chunk_id for descriptor in descriptors),
        already_prepared=False,
    )


def _descriptor_matches_chunk(
    descriptor: LogicalChunkDescriptor,
    chunk: BankStatementParseChunk,
) -> bool:
    """Verify immutable source geometry for either a root or its successor.

    The deterministic parser always recreates revision-zero descriptors.  A
    reviewed/retried successor deliberately has a new UUID/revision, so matching
    those mutable graph fields would make every legitimate successor unprocessable.
    Graph ancestry is validated separately by ``_current_leaves``.
    """
    return (
        descriptor.identity.source_sha256 == chunk.source_sha256
        and descriptor.identity.parser_version == chunk.parser_version
        and descriptor.identity.extractor_version == chunk.extractor_version
        and descriptor.identity.prompt_schema_version == chunk.prompt_schema_version
        and descriptor.statement_import_id == chunk.bank_statement_import_id
        and descriptor.realm_id == chunk.realm_id
        and descriptor.page_start == chunk.page_start
        and descriptor.page_end == chunk.page_end
        and descriptor.span_start == chunk.span_start
        and descriptor.span_end == chunk.span_end
    )


def _set_chunk_terminal(
    chunk: BankStatementParseChunk,
    *,
    lifecycle_status: ChunkLifecycleStatus,
) -> None:
    chunk.lifecycle_status = lifecycle_status.value
    chunk.status = "error" if lifecycle_status is ChunkLifecycleStatus.FAILED else "parsed"
    chunk.completed_at = _utcnow()
    chunk.lease_token = None
    chunk.lease_expires_at = None
    chunk.heartbeat_at = None


@dataclass(frozen=True, slots=True)
class _PreparedChunkProcessing:
    """Immutable source work prepared without holding mutable parser-row locks."""

    import_id: int
    realm_id: str
    chunk_id: UUID
    delivery_id: UUID
    lease_token: str
    descriptor: LogicalChunkDescriptor
    identity: ParserIdentity
    deterministic: tuple[ParsedCandidate, ...]
    spans: tuple[SourceSpan, ...]
    ambiguity_request: AmbiguityParseRequest | None
    candidate_count: int


def _prepare_leased_chunk_processing(
    db: Session,
    *,
    statement_import_id: int,
    realm_id: str,
    chunk_id: UUID,
    delivery_id: UUID,
    lease_token: str,
    source_bytes: bytes | None,
) -> _PreparedChunkProcessing | ChunkProcessOutcome:
    """Reconstruct exact provider input without locking rows across the call.

    Every query in this phase is an ordinary read.  The provider ledger and the
    lease heartbeat use independent sessions, so holding ``FOR UPDATE`` here would
    self-deadlock their FK/update writes.  The second phase reacquires locks and
    performs a literal lease-token CAS before accepting any result.
    """
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
    )
    chunk = db.execute(
        select(BankStatementParseChunk).where(
            BankStatementParseChunk.id == chunk_id,
            BankStatementParseChunk.bank_statement_import_id == statement_import_id,
            BankStatementParseChunk.realm_id == realm_id,
        )
    ).scalar_one_or_none()
    delivery = db.execute(
        select(BankStatementParseDeliveryAttempt).where(
            BankStatementParseDeliveryAttempt.id == delivery_id,
            BankStatementParseDeliveryAttempt.chunk_id == chunk_id,
            BankStatementParseDeliveryAttempt.bank_statement_import_id
            == statement_import_id,
            BankStatementParseDeliveryAttempt.realm_id == realm_id,
        )
    ).scalar_one_or_none()
    if chunk is None or delivery is None:
        raise LeaseMismatchError("delivery does not identify this import chunk")
    if chunk.lifecycle_status in _ALL_TERMINAL_CHUNK_STATES:
        return ChunkProcessOutcome(
            import_id=batch.id,
            chunk_id=chunk.id,
            lifecycle_status=chunk.lifecycle_status,
            reason_code="already_terminal",
            retryable=False,
            candidate_count=0,
        )
    if (
        chunk.lifecycle_status != ChunkLifecycleStatus.LEASED.value
        or chunk.lease_token != lease_token
        or not lease_token
        or delivery.lifecycle_status != "started"
    ):
        raise LeaseMismatchError("delivery does not own the chunk's active lease")

    source = _load_source_bytes(batch, source_bytes)
    _statement, result = _parse_source(batch, source)
    descriptor = next(
        (
            item
            for item in result.manifest.chunks
            if _descriptor_matches_chunk(item, chunk)
        ),
        None,
    )
    if descriptor is None:
        raise SourceIdentityError("leased chunk differs from deterministic source manifest")
    deterministic = tuple(
        replace(candidate, chunk_id=chunk.id)
        for candidate in result.candidates
        if candidate.chunk_id == descriptor.chunk_id
    )
    spans = tuple(
        replace(span, chunk_id=chunk.id)
        for span in result.ambiguous_spans
        if span.chunk_id == descriptor.chunk_id
    )
    expected_ordinals = {
        candidate.source_ordinal for candidate in deterministic
    } | {span.source_ordinal for span in spans}
    persisted_ordinals = set(
        db.execute(
            select(BankStatementParseCandidate.source_ordinal).where(
                BankStatementParseCandidate.statement_import_id
                == statement_import_id,
                BankStatementParseCandidate.realm_id == realm_id,
                BankStatementParseCandidate.chunk_id == chunk_id,
            )
        ).scalars()
    )
    if persisted_ordinals != expected_ordinals:
        raise SourceIdentityError("leased chunk candidate set differs from source manifest")
    request = (
        AmbiguityParseRequest(
            identity=result.manifest.identity,
            statement_import_id=statement_import_id,
            realm_id=realm_id,
            chunk_id=chunk_id,
            chunk_revision=cast(int, chunk.chunk_revision),
            spans=spans,
            attempt_ordinal=cast(int, chunk.application_retry_count),
        )
        if spans
        else None
    )
    return _PreparedChunkProcessing(
        import_id=batch.id,
        realm_id=realm_id,
        chunk_id=chunk.id,
        delivery_id=delivery.id,
        lease_token=lease_token,
        descriptor=descriptor,
        identity=result.manifest.identity,
        deterministic=deterministic,
        spans=spans,
        ambiguity_request=request,
        candidate_count=len(expected_ordinals),
    )


def _persist_prepared_chunk_processing(
    db: Session,
    *,
    prepared: _PreparedChunkProcessing,
    ambiguity: AmbiguityParseResult | None,
) -> ChunkProcessOutcome:
    """CAS a prepared result into the exact still-live lease transaction."""
    batch = _load_import(
        db,
        statement_import_id=prepared.import_id,
        realm_id=prepared.realm_id,
        for_update=True,
    )
    chunk = db.execute(
        select(BankStatementParseChunk)
        .where(
            BankStatementParseChunk.id == prepared.chunk_id,
            BankStatementParseChunk.bank_statement_import_id == prepared.import_id,
            BankStatementParseChunk.realm_id == prepared.realm_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    delivery = db.execute(
        select(BankStatementParseDeliveryAttempt)
        .where(
            BankStatementParseDeliveryAttempt.id == prepared.delivery_id,
            BankStatementParseDeliveryAttempt.chunk_id == prepared.chunk_id,
            BankStatementParseDeliveryAttempt.bank_statement_import_id
            == prepared.import_id,
            BankStatementParseDeliveryAttempt.realm_id == prepared.realm_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if chunk is None or delivery is None:
        raise LeaseMismatchError("delivery does not identify this import chunk")
    if chunk.lifecycle_status in _ALL_TERMINAL_CHUNK_STATES:
        return ChunkProcessOutcome(
            import_id=batch.id,
            chunk_id=chunk.id,
            lifecycle_status=chunk.lifecycle_status,
            reason_code="already_terminal",
            retryable=False,
            candidate_count=0,
        )
    if (
        chunk.lifecycle_status != ChunkLifecycleStatus.LEASED.value
        or chunk.lease_token != prepared.lease_token
        or not prepared.lease_token
        or delivery.lifecycle_status != "started"
        or _identity_from_import(batch) != prepared.identity
        or not _descriptor_matches_chunk(prepared.descriptor, chunk)
    ):
        raise LeaseMismatchError("prepared result no longer owns the chunk's active lease")

    rows = {
        row.source_ordinal: row
        for row in db.execute(
            select(BankStatementParseCandidate)
            .where(
                BankStatementParseCandidate.statement_import_id == prepared.import_id,
                BankStatementParseCandidate.realm_id == prepared.realm_id,
                BankStatementParseCandidate.chunk_id == prepared.chunk_id,
            )
            .with_for_update()
        ).scalars()
    }
    expected_ordinals = {
        candidate.source_ordinal for candidate in prepared.deterministic
    } | {span.source_ordinal for span in prepared.spans}
    if set(rows) != expected_ordinals:
        raise SourceIdentityError("leased chunk candidate set changed before persistence")
    for candidate in prepared.deterministic:
        _write_candidate_row(rows[candidate.source_ordinal], candidate)

    lifecycle = ChunkLifecycleStatus.SUCCEEDED
    reason_code: str | None = None
    retryable = False
    if prepared.spans:
        if ambiguity is None:
            raise InvalidParserStateError("ambiguous chunk result is missing")
        reason_code = ambiguity.reason_code
        if ambiguity.disposition is AmbiguityDisposition.RETRYABLE_FAILURE:
            lifecycle = ChunkLifecycleStatus.FAILED
            retryable = True
        elif ambiguity.disposition is AmbiguityDisposition.REVIEW_REQUIRED:
            lifecycle = ChunkLifecycleStatus.REVIEW_REQUIRED
        else:
            proposal_evidence = CandidateResolutionEvidence(
                parse_route=ParseRoute.AMBIGUOUS,
                authority=CandidateResolutionAuthority.UNTRUSTED_MODEL,
                audit_log_id=None,
                audit_binding_sha256=None,
            )
            for proposal, span in zip(
                ambiguity.proposals,
                prepared.spans,
                strict=True,
            ):
                candidate = ParsedCandidate(
                    candidate_id=rows[span.source_ordinal].id,
                    identity=prepared.identity,
                    statement_import_id=prepared.import_id,
                    realm_id=prepared.realm_id,
                    chunk_id=prepared.chunk_id,
                    source_ordinal=proposal.source_ordinal,
                    source_page=proposal.source_page,
                    source_text=proposal.source_text,
                    txn_date=proposal.txn_date,
                    signed_amount=proposal.signed_amount,
                    description=proposal.description,
                    txn_type=proposal.txn_type,
                    resolution_status=CandidateResolutionStatus.STAGED,
                    resolution_evidence=proposal_evidence,
                )
                _write_candidate_row(rows[span.source_ordinal], candidate)
            lifecycle = ChunkLifecycleStatus.REVIEW_REQUIRED
            reason_code = "human_review_required"

    _set_chunk_terminal(chunk, lifecycle_status=lifecycle)
    if reason_code is not None:
        chunk.resolution_payload = {
            "reason_code": reason_code,
            "retryable": retryable,
        }
    if lifecycle is ChunkLifecycleStatus.REVIEW_REQUIRED:
        batch.review_reason_code = reason_code
        batch.review_action = "correct"
    elif lifecycle is ChunkLifecycleStatus.FAILED:
        batch.review_reason_code = reason_code
    db.flush()
    return ChunkProcessOutcome(
        import_id=batch.id,
        chunk_id=chunk.id,
        lifecycle_status=lifecycle.value,
        reason_code=reason_code,
        retryable=retryable,
        candidate_count=len(rows),
    )


def process_leased_chunk(
    db: Session,
    *,
    statement_import_id: int,
    realm_id: str,
    chunk_id: UUID,
    delivery_id: UUID,
    lease_token: str,
    source_bytes: bytes | None = None,
    resolver: AmbiguousSpanResolver | None = None,
) -> ChunkProcessOutcome:
    """Resolve outside row locks, then persist only through an exact lease CAS."""
    prepared = _prepare_leased_chunk_processing(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        chunk_id=chunk_id,
        delivery_id=delivery_id,
        lease_token=lease_token,
        source_bytes=source_bytes,
    )
    if isinstance(prepared, ChunkProcessOutcome):
        return prepared
    ambiguity = (
        (resolver or LLMAmbiguousSpanResolver()).resolve(prepared.ambiguity_request)
        if prepared.ambiguity_request is not None
        else None
    )
    return _persist_prepared_chunk_processing(
        db,
        prepared=prepared,
        ambiguity=ambiguity,
    )


def _chunk_descriptor(chunk: BankStatementParseChunk, identity: ParserIdentity) -> LogicalChunkDescriptor:
    required = (
        chunk.page_start,
        chunk.page_end,
        chunk.span_start,
        chunk.span_end,
        chunk.chunk_revision,
    )
    if any(value is None for value in required):
        raise SourceIdentityError("chunk is missing required geometry")
    return LogicalChunkDescriptor(
        chunk_id=chunk.id,
        identity=identity,
        statement_import_id=chunk.bank_statement_import_id,
        realm_id=chunk.realm_id,
        page_start=cast(int, chunk.page_start),
        page_end=cast(int, chunk.page_end),
        span_start=cast(int, chunk.span_start),
        span_end=cast(int, chunk.span_end),
        chunk_revision=cast(int, chunk.chunk_revision),
        supersedes_chunk_id=chunk.supersedes_chunk_id,
    )


def _current_leaves(
    chunks: tuple[BankStatementParseChunk, ...],
    *,
    identity: ParserIdentity,
    source_text_length: int,
) -> tuple[BankStatementParseChunk, ...]:
    by_id = {chunk.id: chunk for chunk in chunks}
    if len(by_id) != len(chunks):
        raise SourceIdentityError("chunk IDs are not unique")
    roots = tuple(
        chunk
        for chunk in chunks
        if chunk.supersedes_chunk_id is None and chunk.chunk_revision == 0
    )
    if not roots and source_text_length:
        raise SourceIdentityError("chunk graph has no revision-zero roots")
    children: dict[UUID, BankStatementParseChunk] = {}
    for chunk in chunks:
        descriptor = _chunk_descriptor(chunk, identity)
        if (
            chunk.source_sha256 != identity.source_sha256
            or chunk.parser_version != identity.parser_version
            or chunk.extractor_version != identity.extractor_version
            or chunk.prompt_schema_version != identity.prompt_schema_version
        ):
            raise SourceIdentityError("chunk parser identity drifted")
        parent_id = descriptor.supersedes_chunk_id
        if parent_id is None:
            if descriptor.chunk_revision != 0:
                raise SourceIdentityError("non-root chunk has no predecessor")
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            raise SourceIdentityError("chunk successor references a missing parent")
        if parent_id in children:
            raise SourceIdentityError("chunk graph forks from one predecessor")
        parent_descriptor = _chunk_descriptor(parent, identity)
        if (
            descriptor.statement_import_id != parent_descriptor.statement_import_id
            or descriptor.realm_id != parent_descriptor.realm_id
            or descriptor.page_start != parent_descriptor.page_start
            or descriptor.page_end != parent_descriptor.page_end
            or descriptor.span_start != parent_descriptor.span_start
            or descriptor.span_end != parent_descriptor.span_end
            or descriptor.chunk_revision != parent_descriptor.chunk_revision + 1
        ):
            raise SourceIdentityError("chunk successor identity or revision drifted")
        children[parent_id] = chunk

    ordered_roots = sorted(roots, key=lambda item: (item.span_start, item.span_end, item.id.hex))
    cursor = 0
    for root in ordered_roots:
        if root.span_start != cursor or root.span_end is None:
            raise SourceIdentityError("chunk roots contain a gap or overlap")
        cursor = root.span_end
    if cursor != source_text_length:
        raise SourceIdentityError("chunk roots do not cover the canonical source")

    leaves: list[BankStatementParseChunk] = []
    graph_members: set[UUID] = set()
    for root in ordered_roots:
        current = root
        visited: set[UUID] = set()
        graph_members.add(current.id)
        while current.id in children:
            if current.id in visited:
                raise SourceIdentityError("chunk successor graph contains a cycle")
            visited.add(current.id)
            current = children[current.id]
            graph_members.add(current.id)
        leaves.append(current)
    if graph_members != set(by_id):
        raise SourceIdentityError("chunk graph contains an orphan successor chain")
    if len({leaf.id for leaf in leaves}) != len(leaves):
        raise SourceIdentityError("chunk roots converge on one successor")
    return tuple(leaves)


def _candidate_from_row(
    db: Session,
    row: BankStatementParseCandidate,
    *,
    identity: ParserIdentity,
) -> ParsedCandidate:
    try:
        row_identity = ParserIdentity(
            source_sha256=row.source_sha256,
            parser_version=row.parser_version,
            extractor_version=row.extractor_version,
            prompt_schema_version=row.prompt_schema_version,
        )
    except (TypeError, ValueError) as exc:
        raise SourceIdentityError("candidate parser identity is invalid") from exc
    raw_evidence: object = row.resolution_payload
    try:
        evidence = CandidateResolutionEvidence.from_payload(
            cast(dict[str, object], row.resolution_payload)
        )
    except (KeyError, TypeError, ValueError):
        evidence = cast(CandidateResolutionEvidence, raw_evidence)
    candidate = ParsedCandidate(
        candidate_id=row.id,
        # Keep the stored identity visible to the pure control function.  Replacing
        # it with the import identity would hide row-level drift and could turn
        # contradictory evidence into a passing certificate.
        identity=row_identity,
        statement_import_id=row.statement_import_id,
        realm_id=row.realm_id,
        chunk_id=row.chunk_id,
        source_ordinal=row.source_ordinal,
        source_page=row.source_page,
        source_text=row.source_text,
        txn_date=row.txn_date,
        signed_amount=row.signed_amount,
        description=row.description,
        txn_type=row.txn_type,
        resolution_status=cast(CandidateResolutionStatus, row.resolution_status),
        resolution_evidence=evidence,
    )
    if (
        isinstance(evidence, CandidateResolutionEvidence)
        and evidence.authority is CandidateResolutionAuthority.AUDITED_HUMAN
        and not _audit_matches_candidate(db, candidate)
    ):
        return replace(
            candidate,
            resolution_evidence=cast(CandidateResolutionEvidence, raw_evidence),
        )
    # The expected identity is intentionally not substituted above; this explicit
    # comparison keeps the control failure deterministic while retaining a narrow
    # exception for structurally invalid database values.
    _ = identity
    return candidate


def _audit_matches_candidate(db: Session, candidate: ParsedCandidate) -> bool:
    evidence = candidate.resolution_evidence
    if not isinstance(evidence, CandidateResolutionEvidence) or evidence.audit_log_id is None:
        return False
    audit = db.execute(
        select(SystemAuditLog).where(
            SystemAuditLog.id == evidence.audit_log_id,
            SystemAuditLog.company_id == candidate.realm_id,
        )
    ).scalar_one_or_none()
    try:
        status = CandidateResolutionStatus(candidate.resolution_status)
    except (TypeError, ValueError):
        return False
    return bool(
        audit is not None
        and audit.user_id is not None
        and audit.action == _HUMAN_ACTIONS.get(status)
        and audit.entity_type == "BankStatementParseCandidate"
        and audit.entity_id == str(candidate.candidate_id)
        and audit.new_state == _candidate_values(candidate)
        and evidence.audit_binding_sha256 == resolution_audit_binding_sha256(candidate)
    )


def _reconstruct(
    db: Session,
    *,
    batch: BankStatementImport,
    source_bytes: bytes,
) -> tuple[
    SourceManifest,
    tuple[ParsedCandidate, ...],
    tuple[BankStatementParseChunk, ...],
]:
    statement, deterministic = _parse_source(batch, source_bytes)
    identity = deterministic.manifest.identity
    chunks = tuple(
        db.execute(
            select(BankStatementParseChunk)
            .where(
                BankStatementParseChunk.bank_statement_import_id == batch.id,
                BankStatementParseChunk.realm_id == batch.realm_id,
            )
            .with_for_update()
        ).scalars()
    )
    leaves = _current_leaves(
        chunks,
        identity=identity,
        source_text_length=len(statement.source_text),
    )
    leaf_by_root_geometry = {
        (
            cast(int, leaf.page_start),
            cast(int, leaf.page_end),
            cast(int, leaf.span_start),
            cast(int, leaf.span_end),
        ): leaf
        for leaf in leaves
    }
    root_to_leaf: dict[UUID, BankStatementParseChunk] = {}
    for root in deterministic.manifest.chunks:
        leaf = leaf_by_root_geometry.get(
            (root.page_start, root.page_end, root.span_start, root.span_end)
        )
        if leaf is None:
            raise SourceIdentityError("persisted chunks do not match source manifest roots")
        root_to_leaf[root.chunk_id] = leaf

    rows = tuple(
        db.execute(
            select(BankStatementParseCandidate)
            .where(
                BankStatementParseCandidate.statement_import_id == batch.id,
                BankStatementParseCandidate.realm_id == batch.realm_id,
            )
            .order_by(BankStatementParseCandidate.source_ordinal)
            .with_for_update()
        ).scalars()
    )
    row_by_ordinal = {row.source_ordinal: row for row in rows}
    exact_rows = len(row_by_ordinal) == len(rows) == len(deterministic.manifest.lines)
    expected_lines: list[ExpectedSourceLine] = []
    for source_line in deterministic.manifest.lines:
        leaf = root_to_leaf[source_line.chunk_id]
        row = row_by_ordinal.get(source_line.source_ordinal)
        row_hash = (
            hashlib.sha256(row.source_text.encode("utf-8")).hexdigest()
            if row is not None
            else source_line.source_text_sha256
        )
        if (
            row is None
            or row.source_page != source_line.source_page
            or row_hash != source_line.source_text_sha256
            or row.chunk_id != leaf.id
        ):
            exact_rows = False
        expected_lines.append(
            ExpectedSourceLine(
                source_ordinal=source_line.source_ordinal,
                source_page=source_line.source_page,
                chunk_id=leaf.id,
                source_text_sha256=source_line.source_text_sha256,
            )
        )
    manifest = SourceManifest(
        identity=identity,
        source_text_length=len(statement.source_text),
        coverage_complete=(
            deterministic.manifest.coverage_complete
            and exact_rows
            and all(leaf.lifecycle_status in _TERMINAL_CHUNK_STATES for leaf in leaves)
        ),
        chunks=tuple(_chunk_descriptor(leaf, identity) for leaf in leaves),
        lines=tuple(expected_lines),
    )
    candidates = tuple(_candidate_from_row(db, row, identity=identity) for row in rows)
    return manifest, candidates, leaves


def _control_view(row: BankStatementControlCertificate | None) -> ControlView | None:
    if row is None:
        return None
    return ControlView(
        certificate_version=row.certificate_version,
        status=row.status,
        expected_count=row.expected_count,
        observed_count=row.observed_count,
        accepted_count=row.accepted_count,
        rejected_count=row.rejected_count,
        opening_balance=row.opening_balance,
        signed_activity=row.signed_activity,
        closing_balance=row.closing_balance,
        residual=row.residual,
        calculated_at=row.calculated_at,
    )


def _certificate_matches(
    row: BankStatementControlCertificate,
    certificate: ControlCertificate,
) -> bool:
    return (
        row.status == certificate.status.value
        and row.source_sha256 == certificate.identity.source_sha256
        and row.parser_version == certificate.identity.parser_version
        and row.extractor_version == certificate.identity.extractor_version
        and row.prompt_schema_version == certificate.identity.prompt_schema_version
        and row.expected_count == certificate.expected_count
        and row.observed_count == certificate.observed_count
        and row.accepted_count == certificate.accepted_count
        and row.rejected_count == certificate.rejected_count
        and row.candidate_set_sha256 == certificate.candidate_set_sha256
        and row.opening_balance == certificate.opening_balance
        and row.signed_activity == certificate.signed_activity
        and row.closing_balance == certificate.closing_balance
        and row.residual == certificate.residual
    )


def _persist_control(
    db: Session,
    *,
    batch: BankStatementImport,
    manifest: SourceManifest,
    candidates: tuple[ParsedCandidate, ...],
) -> tuple[ControlCertificate, BankStatementControlCertificate]:
    latest = db.execute(
        select(BankStatementControlCertificate)
        .where(
            BankStatementControlCertificate.statement_import_id == batch.id,
            BankStatementControlCertificate.realm_id == batch.realm_id,
        )
        .order_by(BankStatementControlCertificate.certificate_version.desc())
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()
    version = 1 if latest is None else latest.certificate_version + 1
    certificate = calculate_control_certificate(
        identity=manifest.identity,
        statement_import_id=batch.id,
        realm_id=batch.realm_id,
        certificate_version=version,
        manifest=manifest,
        candidates=candidates,
        opening_balance=batch.opening_balance,
        closing_balance=batch.closing_balance,
    )
    if latest is not None and _certificate_matches(latest, certificate):
        return certificate, latest
    row = BankStatementControlCertificate(
        statement_import_id=batch.id,
        realm_id=batch.realm_id,
        certificate_version=certificate.certificate_version,
        status=certificate.status.value,
        source_sha256=certificate.identity.source_sha256,
        parser_version=certificate.identity.parser_version,
        extractor_version=certificate.identity.extractor_version,
        prompt_schema_version=certificate.identity.prompt_schema_version,
        expected_count=certificate.expected_count,
        observed_count=certificate.observed_count,
        accepted_count=certificate.accepted_count,
        rejected_count=certificate.rejected_count,
        candidate_set_sha256=certificate.candidate_set_sha256,
        opening_balance=certificate.opening_balance,
        signed_activity=certificate.signed_activity,
        closing_balance=certificate.closing_balance,
        residual=certificate.residual,
    )
    db.add(row)
    db.flush()
    return certificate, row


def _lock_canonical_projection_scope(
    db: Session,
    *,
    realm_id: str,
) -> None:
    """Serialize the duplicate guard and canonical multiset for one realm."""
    projection_scope = db.execute(
        select(OrganizationCompany.id)
        .where(OrganizationCompany.realm_id == realm_id)
        .with_for_update()
    ).scalar_one_or_none()
    if projection_scope is None:
        raise SourceIdentityError(
            "controlled projection has no authoritative realm mapping"
        )


def _project_controlled_rows(
    db: Session,
    *,
    batch: BankStatementImport,
    candidates: tuple[ParsedCandidate, ...],
) -> int:
    accepted_statuses = {
        CandidateResolutionStatus.ACCEPTED,
        CandidateResolutionStatus.CORRECTED,
    }
    accepted = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if CandidateResolutionStatus(candidate.resolution_status)
                in accepted_statuses
            ),
            key=lambda candidate: candidate.source_ordinal,
        )
    )
    # The candidate is the durable import-occurrence record.  Its projection link
    # can reuse a canonical feed row from another import, but one import cannot use
    # the same physical row for two genuine repeated occurrences.
    candidate_rows = tuple(
        db.execute(
            select(BankStatementParseCandidate)
            .where(
                BankStatementParseCandidate.statement_import_id == batch.id,
                BankStatementParseCandidate.realm_id == batch.realm_id,
            )
            .order_by(BankStatementParseCandidate.source_ordinal)
            .with_for_update()
        ).scalars()
    )
    row_by_id = {row.id: row for row in candidate_rows}
    if len(row_by_id) != len(candidate_rows) or set(row_by_id) != {
        candidate.candidate_id for candidate in candidates
    }:
        raise InvalidParserStateError(
            "candidate projection rows do not match controlled evidence"
        )
    for row in candidate_rows:
        status = CandidateResolutionStatus(row.resolution_status)
        if status not in accepted_statuses and row.bank_feed_item_id is not None:
            raise InvalidParserStateError(
                "unaccepted candidate is linked to a projected feed row"
            )
    if not accepted:
        return 0

    def projection_key(
        *,
        txn_date: date | None,
        amount: Decimal | None,
        description: str | None,
    ) -> tuple[date, Decimal, str]:
        if txn_date is None or amount is None or description is None:
            raise InvalidParserStateError(
                "accepted candidate is missing financial projection fields"
            )
        return (txn_date, amount, description.strip().casefold())

    accepted_by_key: dict[
        tuple[date, Decimal, str],
        list[tuple[ParsedCandidate, BankStatementParseCandidate]],
    ] = {}
    for candidate in accepted:
        row = row_by_id[candidate.candidate_id]
        expected = (
            candidate.chunk_id,
            candidate.source_ordinal,
            candidate.txn_date,
            candidate.signed_amount,
            candidate.description,
            TransactionType(candidate.txn_type).value,
        )
        actual = (
            row.chunk_id,
            row.source_ordinal,
            row.txn_date,
            row.signed_amount,
            row.description,
            row.txn_type,
        )
        if actual != expected:
            raise InvalidParserStateError(
                "candidate projection row conflicts with controlled evidence"
            )
        key = projection_key(
            txn_date=candidate.txn_date,
            amount=candidate.signed_amount,
            description=candidate.description,
        )
        accepted_by_key.setdefault(key, []).append((candidate, row))

    period_start = min(key[0] for key in accepted_by_key)
    period_end = max(key[0] for key in accepted_by_key)
    canonical_rows = tuple(
        db.execute(
            select(BankFeedItem)
            .where(
                BankFeedItem.realm_id == batch.realm_id,
                BankFeedItem.bank_account_ref == batch.account_id,
                BankFeedItem.source == "statement_upload",
                BankFeedItem.txn_date >= period_start,
                BankFeedItem.txn_date <= period_end,
            )
            .order_by(BankFeedItem.id)
            .with_for_update()
        ).scalars()
    )
    canonical_by_id = {row.id: row for row in canonical_rows}
    canonical_by_key: dict[tuple[date, Decimal, str], list[BankFeedItem]] = {}
    for row in canonical_rows:
        key = projection_key(
            txn_date=row.txn_date,
            amount=row.amount,
            description=row.description,
        )
        canonical_by_key.setdefault(key, []).append(row)

    # Validate every pre-existing link before mutating anything.  This preserves
    # all-or-nothing behavior if a prior write or migration produced contradictory
    # lineage.
    used_feed_ids: set[int] = set()
    for key, occurrences in accepted_by_key.items():
        for _candidate, row in occurrences:
            if row.bank_feed_item_id is None:
                continue
            feed = canonical_by_id.get(row.bank_feed_item_id)
            if (
                feed is None
                or projection_key(
                    txn_date=feed.txn_date,
                    amount=feed.amount,
                    description=feed.description,
                )
                != key
                or row.bank_feed_item_id in used_feed_ids
            ):
                raise InvalidParserStateError(
                    "existing candidate projection conflicts with controlled evidence"
                )
            used_feed_ids.add(row.bank_feed_item_id)

    created = 0
    pending_links: list[
        tuple[BankStatementParseCandidate, BankFeedItem]
    ] = []
    for key, occurrences in accepted_by_key.items():
        available = [
            feed
            for feed in canonical_by_key.get(key, [])
            if feed.id not in used_feed_ids
        ]
        available_index = 0
        for candidate, row in occurrences:
            if row.bank_feed_item_id is not None:
                continue
            if available_index < len(available):
                feed = available[available_index]
                available_index += 1
                row.bank_feed_item_id = feed.id
                used_feed_ids.add(feed.id)
                continue
            feed = BankFeedItem(
                realm_id=batch.realm_id,
                statement_import_id=batch.id,
                parse_chunk_id=candidate.chunk_id,
                source_ordinal=candidate.source_ordinal,
                lineage_status="controlled",
                source="statement_upload",
                qbo_txn_id=None,
                txn_date=candidate.txn_date,
                amount=candidate.signed_amount,
                description=candidate.description,
                txn_type=TransactionType(candidate.txn_type).value,
                bank_account_ref=batch.account_id,
                bank_account_name=batch.account_name,
                status="pending",
            )
            db.add(feed)
            pending_links.append((row, feed))
            created += 1

    if pending_links:
        db.flush()
        for candidate_row, feed in pending_links:
            candidate_row.bank_feed_item_id = feed.id
    db.flush()
    return created


def finalize_import(
    db: Session,
    *,
    statement_import_id: int,
    realm_id: str,
    source_bytes: bytes | None = None,
) -> ImportProgress:
    """Reconstruct exact evidence, calculate control, and project pass-only rows."""
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        for_update=True,
    )
    if batch.lifecycle_status == ImportLifecycleStatus.PARSED.value:
        return get_import_progress(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
        )
    try:
        source = _load_source_bytes(batch, source_bytes)
        manifest, candidates, leaves = _reconstruct(
            db,
            batch=batch,
            source_bytes=source,
        )
    except SourceIdentityError:
        # Retained-byte, graph, or row-identity drift is review evidence, not a
        # reason to fabricate a partial manifest or discard the user's import.
        batch.lifecycle_status = ImportLifecycleStatus.REVIEW_REQUIRED.value
        batch.status = "error"
        batch.review_reason_code = "source_identity_invalid"
        batch.review_action = "reupload"
        batch.error_detail = (
            "The retained statement evidence could not be verified; re-upload "
            "the original statement."
        )
        db.flush()
        return get_import_progress(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
        )

    leaf_states = {cast(str, leaf.lifecycle_status) for leaf in leaves}
    if any(state not in _ALL_TERMINAL_CHUNK_STATES for state in leaf_states):
        # A worker finalizes after every chunk.  Do not calculate a partial
        # certificate or move a multi-chunk import into review while siblings are
        # still pending/leased.
        batch.lifecycle_status = (
            ImportLifecycleStatus.PROCESSING.value
            if ChunkLifecycleStatus.LEASED.value in leaf_states
            or batch.lifecycle_status == ImportLifecycleStatus.PROCESSING.value
            else ImportLifecycleStatus.QUEUED.value
        )
        batch.status = "parsing"
        batch.review_reason_code = None
        batch.review_action = None
        batch.error_detail = None
        db.flush()
        return get_import_progress(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
        )
    if ChunkLifecycleStatus.FAILED.value in leaf_states:
        failed_reason = next(
            (
                leaf.resolution_payload.get("reason_code")
                for leaf in leaves
                if leaf.lifecycle_status == ChunkLifecycleStatus.FAILED.value
                and isinstance(leaf.resolution_payload, dict)
                and isinstance(leaf.resolution_payload.get("reason_code"), str)
            ),
            None,
        )
        batch.lifecycle_status = ImportLifecycleStatus.ERROR.value
        batch.status = "error"
        batch.review_reason_code = failed_reason or "chunk_failed"
        batch.review_action = "retry"
        batch.error_detail = "Statement processing failed and can be retried."
        db.flush()
        return get_import_progress(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
        )

    certificate, _row = _persist_control(
        db,
        batch=batch,
        manifest=manifest,
        candidates=candidates,
    )
    if certificate.status is ControlCertificateStatus.PASSED:
        financial_candidates = tuple(
            candidate
            for candidate in candidates
            if CandidateResolutionStatus(candidate.resolution_status)
            in {
                CandidateResolutionStatus.ACCEPTED,
                CandidateResolutionStatus.CORRECTED,
            }
        )
        financial_dates = tuple(
            candidate.txn_date
            for candidate in financial_candidates
            if candidate.txn_date is not None
        )
        batch.period_start = min(financial_dates, default=None)
        batch.period_end = max(financial_dates, default=None)

        # The duplicate predicate and canonical feed rows are shared across
        # imports. Serialize both under the realm's durable company row; this is
        # a short DB-only critical section entered after all provider work.
        _lock_canonical_projection_scope(db, realm_id=batch.realm_id)

        # Preserve the established whole-statement guard before projecting any
        # row.  The accepted upload remains a durable terminal header (the async
        # API already returned its ID), but an unforced duplicate never doubles
        # the canonical statement feed.
        from .ingest import find_duplicate_import

        duplicate = find_duplicate_import(
            db,
            realm_id=batch.realm_id,
            account_id=batch.account_id,
            period_start=batch.period_start,
            period_end=batch.period_end,
            closing_balance=batch.closing_balance,
            exclude_import_id=batch.id,
        )
        if duplicate is not None and not bool(batch.force_reimport):
            batch.line_count = 0
            batch.lifecycle_status = ImportLifecycleStatus.ERROR.value
            batch.status = "error"
            batch.review_reason_code = "duplicate_statement"
            batch.review_action = "retry"
            batch.error_detail = (
                "Duplicate statement: this account already has the same period "
                f"and closing balance in import #{duplicate.id}. Nothing was "
                "imported. Retry with force_reimport only for a deliberate "
                "corrected-statement or missing-line repair."
            )
            db.flush()
            return get_import_progress(
                db,
                realm_id=realm_id,
                statement_import_id=statement_import_id,
            )
        try:
            batch.line_count = _project_controlled_rows(
                db,
                batch=batch,
                candidates=candidates,
            )
        except InvalidParserStateError:
            batch.lifecycle_status = ImportLifecycleStatus.REVIEW_REQUIRED.value
            batch.status = "error"
            batch.review_reason_code = "projection_conflict"
            batch.review_action = "correct"
            batch.error_detail = (
                "Existing statement rows conflict with the controlled import evidence."
            )
        else:
            batch.lifecycle_status = ImportLifecycleStatus.PARSED.value
            batch.status = "parsed"
            batch.review_reason_code = None
            batch.review_action = None
            batch.error_detail = None
    else:
        review_reason = next(
            (
                leaf.resolution_payload.get("reason_code")
                for leaf in leaves
                if leaf.lifecycle_status == ChunkLifecycleStatus.REVIEW_REQUIRED.value
                and isinstance(leaf.resolution_payload, dict)
                and isinstance(leaf.resolution_payload.get("reason_code"), str)
            ),
            None,
        )
        reason = review_reason or (
            certificate.issues[0].code.value
            if certificate.issues
            else "control_incomplete"
        )
        batch.lifecycle_status = ImportLifecycleStatus.REVIEW_REQUIRED.value
        batch.status = "error"
        batch.review_reason_code = reason
        batch.review_action = "correct"
        batch.error_detail = "Statement evidence requires review before persistence."
    db.flush()
    return get_import_progress(
        db,
        realm_id=realm_id,
        statement_import_id=statement_import_id,
    )


def _summary(batch: BankStatementImport) -> ImportSummary:
    return ImportSummary(
        import_id=batch.id,
        account_id=batch.account_id,
        account_name=batch.account_name,
        source=batch.source,
        lifecycle_status=batch.lifecycle_status,
        status=batch.status,
        review_reason_code=batch.review_reason_code,
        review_action=batch.review_action,
        opening_balance=batch.opening_balance,
        closing_balance=batch.closing_balance,
        line_count=batch.line_count,
        created_at=batch.created_at,
    )


def list_imports(
    db: Session,
    *,
    realm_id: str,
    limit: int = 50,
    offset: int = 0,
) -> ImportListPage:
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be 1..200 and offset must be non-negative")
    total = db.execute(
        select(func.count(BankStatementImport.id)).where(
            BankStatementImport.realm_id == realm_id
        )
    ).scalar_one()
    rows = tuple(
        db.execute(
            select(BankStatementImport)
            .where(BankStatementImport.realm_id == realm_id)
            .order_by(BankStatementImport.created_at.desc(), BankStatementImport.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return ImportListPage(
        items=tuple(_summary(row) for row in rows),
        total=total,
        limit=limit,
        offset=offset,
    )


def _candidate_view(row: BankStatementParseCandidate) -> CandidateView:
    route: str | None = None
    authority: str | None = None
    try:
        evidence = CandidateResolutionEvidence.from_payload(
            cast(dict[str, object], row.resolution_payload)
        )
        route = evidence.parse_route.value
        authority = evidence.authority.value
    except (KeyError, TypeError, ValueError):
        pass
    return CandidateView(
        candidate_id=row.id,
        chunk_id=row.chunk_id,
        source_ordinal=row.source_ordinal,
        source_page=row.source_page,
        source_text=row.source_text,
        txn_date=row.txn_date,
        signed_amount=row.signed_amount,
        description=row.description,
        txn_type=row.txn_type,
        resolution_status=row.resolution_status,
        parse_route=route,
        authority=authority,
        updated_at=row.updated_at,
    )


def get_import_progress(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int,
) -> ImportProgress:
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
    )
    chunks = tuple(
        db.execute(
            select(BankStatementParseChunk)
            .where(
                BankStatementParseChunk.bank_statement_import_id == batch.id,
                BankStatementParseChunk.realm_id == realm_id,
            )
            .order_by(BankStatementParseChunk.page_start, BankStatementParseChunk.chunk_revision)
        ).scalars()
    )
    candidates = tuple(
        db.execute(
            select(BankStatementParseCandidate)
            .where(
                BankStatementParseCandidate.statement_import_id == batch.id,
                BankStatementParseCandidate.realm_id == realm_id,
            )
            .order_by(BankStatementParseCandidate.source_ordinal)
        ).scalars()
    )
    candidate_counts: dict[UUID, int] = {}
    for candidate in candidates:
        candidate_counts[candidate.chunk_id] = candidate_counts.get(candidate.chunk_id, 0) + 1
    deliveries = tuple(
        db.execute(
            select(BankStatementParseDeliveryAttempt)
            .where(
                BankStatementParseDeliveryAttempt.bank_statement_import_id == batch.id,
                BankStatementParseDeliveryAttempt.realm_id == realm_id,
                BankStatementParseDeliveryAttempt.lifecycle_status.is_not(None),
            )
            .order_by(
                BankStatementParseDeliveryAttempt.chunk_id,
                BankStatementParseDeliveryAttempt.delivery_ordinal,
            )
        ).scalars()
    )
    certificate = db.execute(
        select(BankStatementControlCertificate)
        .where(
            BankStatementControlCertificate.statement_import_id == batch.id,
            BankStatementControlCertificate.realm_id == realm_id,
        )
        .order_by(BankStatementControlCertificate.certificate_version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return ImportProgress(
        summary=_summary(batch),
        chunks=tuple(
            ChunkProgress(
                chunk_id=chunk.id,
                chunk_revision=cast(int, chunk.chunk_revision),
                lifecycle_status=cast(str, chunk.lifecycle_status),
                page_start=cast(int, chunk.page_start),
                page_end=cast(int, chunk.page_end),
                span_start=cast(int, chunk.span_start),
                span_end=cast(int, chunk.span_end),
                candidate_count=candidate_counts.get(chunk.id, 0),
            )
            for chunk in chunks
            if chunk.lifecycle_status is not None
        ),
        deliveries=tuple(
            DeliveryProgress(
                delivery_id=delivery.id,
                chunk_id=cast(UUID, delivery.chunk_id),
                delivery_ordinal=cast(int, delivery.delivery_ordinal),
                lifecycle_status=cast(str, delivery.lifecycle_status),
                started_at=delivery.started_at,
                completed_at=delivery.completed_at,
            )
            for delivery in deliveries
        ),
        candidates=tuple(_candidate_view(row) for row in candidates),
        control=_control_view(certificate),
    )


def _successor_chunk(
    db: Session,
    *,
    predecessor: BankStatementParseChunk,
    actor_user_id: int,
    action: str,
) -> BankStatementParseChunk:
    if predecessor.chunk_revision is None:
        raise SourceIdentityError("reviewed chunk has no controlled revision")
    next_revision = predecessor.chunk_revision + 1
    retry_count = cast(int, predecessor.application_retry_count or 0)
    if action == "retry":
        retry_count += 1
        if retry_count >= AMBIGUOUS_MAX_APPLICATION_ATTEMPTS:
            raise InvalidParserStateError("ambiguous parser retry budget is exhausted")
    existing = db.execute(
        select(BankStatementParseChunk).where(
            BankStatementParseChunk.supersedes_chunk_id == predecessor.id,
            BankStatementParseChunk.bank_statement_import_id
            == predecessor.bank_statement_import_id,
            BankStatementParseChunk.realm_id == predecessor.realm_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise InvalidParserStateError("reviewed chunk already has a successor")
    successor_id = uuid5(
        _CANDIDATE_NAMESPACE,
        f"successor\x1f{predecessor.id}\x1f{next_revision}",
    )
    successor = BankStatementParseChunk(
        id=successor_id,
        bank_statement_import_id=predecessor.bank_statement_import_id,
        delivery_attempt_id=None,
        realm_id=predecessor.realm_id,
        organization_id=predecessor.organization_id,
        organization_company_id=predecessor.organization_company_id,
        chunk_index=predecessor.chunk_index,
        source_sha256=predecessor.source_sha256,
        source_size_bytes=predecessor.source_size_bytes,
        parser_version=predecessor.parser_version,
        extractor_version=predecessor.extractor_version,
        prompt_schema_version=predecessor.prompt_schema_version,
        page_start=predecessor.page_start,
        page_end=predecessor.page_end,
        span_start=predecessor.span_start,
        span_end=predecessor.span_end,
        chunk_revision=next_revision,
        supersedes_chunk_id=predecessor.id,
        resolution_payload={
            "action": action,
            "actor_user_id": actor_user_id,
        },
        lifecycle_status=ChunkLifecycleStatus.PENDING.value,
        application_retry_count=retry_count,
        status="started",
        telemetry_status="pending",
        evidence_eligible=False,
    )
    db.add(successor)
    db.flush()
    return successor


def _retry_lineage_is_code_only(
    db: Session,
    *,
    leaf: BankStatementParseChunk,
    chunks_by_id: dict[UUID, BankStatementParseChunk],
) -> bool:
    """Authorize explicit retry only when durable evidence proves zero paid calls."""
    lineage_ids: set[UUID] = set()
    current = leaf
    while True:
        if current.id in lineage_ids:
            return False
        lineage_ids.add(current.id)
        if current.supersedes_chunk_id is None:
            break
        predecessor = chunks_by_id.get(current.supersedes_chunk_id)
        if predecessor is None:
            return False
        current = predecessor

    provider_attempt = db.execute(
        select(BankStatementParseProviderAttempt.id)
        .where(
            BankStatementParseProviderAttempt.bank_statement_import_id
            == leaf.bank_statement_import_id,
            BankStatementParseProviderAttempt.realm_id == leaf.realm_id,
            BankStatementParseProviderAttempt.chunk_id.in_(lineage_ids),
        )
        .limit(1)
    ).scalar_one_or_none()
    if provider_attempt is not None:
        return False

    payloads = tuple(
        db.execute(
            select(BankStatementParseCandidate.resolution_payload).where(
                BankStatementParseCandidate.statement_import_id
                == leaf.bank_statement_import_id,
                BankStatementParseCandidate.realm_id == leaf.realm_id,
                BankStatementParseCandidate.chunk_id == leaf.id,
            )
        ).scalars()
    )
    if not payloads:
        return False
    for payload in payloads:
        try:
            evidence = CandidateResolutionEvidence.from_payload(
                cast(dict[str, object], payload)
            )
        except (KeyError, TypeError, ValueError):
            return False
        if (
            evidence.parse_route is not ParseRoute.DETERMINISTIC
            or evidence.authority
            is not CandidateResolutionAuthority.DETERMINISTIC_CODE
        ):
            return False
    return True


def prepare_retry(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int,
    actor_user_id: int,
) -> RetryPreparation:
    if actor_user_id < 1:
        raise ValueError("actor_user_id must be positive")
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        for_update=True,
    )
    chunks = tuple(
        db.execute(
            select(BankStatementParseChunk)
            .where(
                BankStatementParseChunk.bank_statement_import_id == batch.id,
                BankStatementParseChunk.realm_id == realm_id,
            )
            .order_by(BankStatementParseChunk.chunk_index, BankStatementParseChunk.id)
            .with_for_update()
        ).scalars()
    )
    superseded_ids = {
        cast(UUID, chunk.supersedes_chunk_id)
        for chunk in chunks
        if chunk.supersedes_chunk_id is not None
    }
    by_id = {chunk.id: chunk for chunk in chunks}
    current_leaves = tuple(chunk for chunk in chunks if chunk.id not in superseded_ids)
    pending_retry_successors: list[BankStatementParseChunk] = []
    for chunk in current_leaves:
        payload = chunk.resolution_payload
        if (
            chunk.lifecycle_status != ChunkLifecycleStatus.PENDING.value
            or not isinstance(payload, dict)
            or payload.get("action") != "retry"
        ):
            continue
        predecessor = by_id.get(chunk.supersedes_chunk_id)
        if (
            predecessor is None
            or predecessor.bank_statement_import_id != batch.id
            or predecessor.realm_id != realm_id
            or predecessor.chunk_revision is None
            or chunk.chunk_revision != predecessor.chunk_revision + 1
            or (
                chunk.source_sha256,
                chunk.parser_version,
                chunk.extractor_version,
                chunk.prompt_schema_version,
                chunk.page_start,
                chunk.page_end,
                chunk.span_start,
                chunk.span_end,
            )
            != (
                predecessor.source_sha256,
                predecessor.parser_version,
                predecessor.extractor_version,
                predecessor.prompt_schema_version,
                predecessor.page_start,
                predecessor.page_end,
                predecessor.span_start,
                predecessor.span_end,
            )
        ):
            raise SourceIdentityError("pending retry successor lineage is invalid")
        pending_retry_successors.append(chunk)
    if pending_retry_successors:
        competing = tuple(
            chunk
            for chunk in current_leaves
            if chunk not in pending_retry_successors
            and chunk.lifecycle_status
            in {
                ChunkLifecycleStatus.FAILED.value,
                ChunkLifecycleStatus.REVIEW_REQUIRED.value,
            }
        )
        if competing:
            raise SourceIdentityError("retry successor set is only partially persisted")
        if any(
            not _retry_lineage_is_code_only(
                db,
                leaf=chunk,
                chunks_by_id=by_id,
            )
            for chunk in pending_retry_successors
        ):
            raise InvalidParserStateError(
                "this review state cannot be retried without risking another provider call"
            )
        ordered = tuple(
            sorted(
                pending_retry_successors,
                key=lambda chunk: (chunk.chunk_index, chunk.id.hex),
            )
        )
        return RetryPreparation(
            import_id=batch.id,
            chunk_ids=tuple(chunk.id for chunk in ordered),
        )
    retryable_leaves = tuple(
        chunk
        for chunk in current_leaves
        if chunk.lifecycle_status
        in {
            ChunkLifecycleStatus.FAILED.value,
            ChunkLifecycleStatus.REVIEW_REQUIRED.value,
        }
    )
    if not retryable_leaves:
        raise InvalidParserStateError(
            "this import has no current failed or review-required chunks to retry"
        )
    if any(
        int(chunk.application_retry_count or 0) >= 1
        or not _retry_lineage_is_code_only(
            db,
            leaf=chunk,
            chunks_by_id=by_id,
        )
        for chunk in retryable_leaves
    ):
        raise InvalidParserStateError(
            "this review state cannot be retried without risking another provider call"
        )
    successors = tuple(
        _successor_chunk(
            db,
            predecessor=chunk,
            actor_user_id=actor_user_id,
            action="retry",
        )
        for chunk in retryable_leaves
    )
    for predecessor, successor in zip(retryable_leaves, successors, strict=True):
        candidates = tuple(
            db.execute(
                select(BankStatementParseCandidate)
                .where(
                    BankStatementParseCandidate.statement_import_id == batch.id,
                    BankStatementParseCandidate.realm_id == realm_id,
                    BankStatementParseCandidate.chunk_id == predecessor.id,
                )
                .with_for_update()
            ).scalars()
        )
        for candidate in candidates:
            candidate.chunk_id = successor.id
    db.add(
        SystemAuditLog(
            company_id=realm_id,
            user_id=actor_user_id,
            action="BANK_PARSE_IMPORT_RETRY",
            entity_type="BankStatementImport",
            entity_id=str(batch.id),
            previous_state={"lifecycle_status": batch.lifecycle_status},
            new_state={"chunk_ids": [str(chunk.id) for chunk in successors]},
        )
    )
    batch.lifecycle_status = (
        ImportLifecycleStatus.QUEUED.value
        if batch.lifecycle_status == ImportLifecycleStatus.ERROR.value
        else ImportLifecycleStatus.PROCESSING.value
    )
    batch.status = "parsing"
    batch.review_reason_code = None
    batch.review_action = None
    batch.error_detail = None
    db.flush()
    return RetryPreparation(import_id=batch.id, chunk_ids=tuple(chunk.id for chunk in successors))


def _candidate_evidence(row: BankStatementParseCandidate) -> CandidateResolutionEvidence:
    try:
        return CandidateResolutionEvidence.from_payload(
            cast(dict[str, object], row.resolution_payload)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidParserStateError("candidate resolution evidence is invalid") from exc


def _ground_review_values(
    candidate: ParsedCandidate,
    *,
    status: CandidateResolutionStatus,
    txn_date: date | None,
    signed_amount: Decimal | None,
    description: str | None,
    txn_type: TransactionType | str | None,
) -> ParsedCandidate:
    supplied = (txn_date, signed_amount, description, txn_type)
    if status is CandidateResolutionStatus.CORRECTED:
        if any(value is None for value in supplied):
            raise InvalidParserStateError(
                "corrected candidates require every financial field"
            )
        effective = supplied
    elif status is CandidateResolutionStatus.ACCEPTED:
        if any(value is not None for value in supplied):
            raise InvalidParserStateError(
                "accepted candidates use the staged values without overrides"
            )
        effective = (
            candidate.txn_date,
            candidate.signed_amount,
            candidate.description,
            candidate.txn_type,
        )
        if any(value is None for value in effective):
            raise InvalidParserStateError(
                "accepted candidate fields must be complete"
            )
    else:
        if any(value is not None for value in supplied):
            raise InvalidParserStateError(
                "rejected candidates do not accept financial overrides"
            )
        return replace(candidate, resolution_status=status)

    effective_date, effective_amount, effective_description, effective_type = effective
    assert isinstance(effective_date, date)
    assert isinstance(effective_amount, Decimal)
    assert isinstance(effective_description, str)
    try:
        normalized_type = TransactionType(effective_type)
        proposal = _parse_proposal(
            {
                "source_ordinal": candidate.source_ordinal,
                "source_page": candidate.source_page,
                "source_text": candidate.source_text,
                "txn_date": effective_date.isoformat(),
                "signed_amount": format(effective_amount, "f"),
                "description": effective_description,
                "txn_type": normalized_type.value,
            },
            SourceSpan(
                source_ordinal=candidate.source_ordinal,
                chunk_id=candidate.chunk_id,
                page_start=candidate.source_page,
                page_end=candidate.source_page,
                span_start=0,
                span_end=len(candidate.source_text),
                source_text=candidate.source_text,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidParserStateError(
            "candidate correction is not grounded in its exact source span"
        ) from exc
    if (
        proposal.txn_date is None
        or proposal.signed_amount is None
        or proposal.description is None
        or proposal.txn_type is None
    ):
        raise InvalidParserStateError("candidate correction is incomplete")
    return replace(
        candidate,
        txn_date=proposal.txn_date,
        signed_amount=proposal.signed_amount,
        description=proposal.description,
        txn_type=proposal.txn_type,
        resolution_status=status,
    )


def _bind_human_audit(
    db: Session,
    *,
    candidate: ParsedCandidate,
    prior: ParsedCandidate,
    actor_user_id: int,
    parse_route: ParseRoute,
    carried_from_audit_log_id: int | None = None,
) -> ParsedCandidate:
    status = CandidateResolutionStatus(candidate.resolution_status)
    audit = SystemAuditLog(
        company_id=candidate.realm_id,
        user_id=actor_user_id,
        action=_HUMAN_ACTIONS[status],
        entity_type="BankStatementParseCandidate",
        entity_id=str(candidate.candidate_id),
        previous_state=(
            {
                "carried_from_audit_log_id": carried_from_audit_log_id,
                "candidate": _candidate_values(prior),
            }
            if carried_from_audit_log_id is not None
            else _candidate_values(prior)
        ),
        new_state=None,
    )
    db.add(audit)
    db.flush()
    temporary = replace(
        candidate,
        resolution_evidence=CandidateResolutionEvidence(
            parse_route=parse_route,
            authority=CandidateResolutionAuthority.AUDITED_HUMAN,
            audit_log_id=audit.id,
            audit_binding_sha256="0" * 64,
        ),
    )
    bound = replace(
        temporary,
        resolution_evidence=replace(
            temporary.resolution_evidence,
            audit_binding_sha256=resolution_audit_binding_sha256(temporary),
        ),
    )
    audit.new_state = _candidate_values(bound)
    return bound


def _carry_forward_human_audit(
    db: Session,
    *,
    candidate: ParsedCandidate,
    prior: ParsedCandidate,
    parse_route: ParseRoute,
) -> ParsedCandidate:
    """Rebind an earlier human decision to a successor without changing its actor.

    Human audit bindings include the immutable chunk revision.  A whole-chunk
    successor therefore needs a new binding row for an already-reviewed sibling,
    but it must remain attributable to the human who actually made that decision.
    The new row records its source audit explicitly; it is derived provenance, not
    a fabricated action by the user reviewing a different candidate now.
    """
    evidence = prior.resolution_evidence
    if (
        not isinstance(evidence, CandidateResolutionEvidence)
        or evidence.authority is not CandidateResolutionAuthority.AUDITED_HUMAN
        or evidence.audit_log_id is None
        or not _audit_matches_candidate(db, prior)
    ):
        raise InvalidParserStateError("previous candidate audit binding is invalid")
    source_audit = db.execute(
        select(SystemAuditLog).where(
            SystemAuditLog.id == evidence.audit_log_id,
            SystemAuditLog.company_id == prior.realm_id,
        )
    ).scalar_one_or_none()
    if source_audit is None or source_audit.user_id is None:
        raise InvalidParserStateError("previous candidate audit actor is unavailable")
    return _bind_human_audit(
        db,
        candidate=candidate,
        prior=prior,
        actor_user_id=source_audit.user_id,
        parse_route=parse_route,
        carried_from_audit_log_id=source_audit.id,
    )


def review_candidate(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int,
    candidate_id: UUID,
    actor_user_id: int,
    resolution_status: CandidateResolutionStatus,
    txn_date: date | None = None,
    signed_amount: Decimal | None = None,
    description: str | None = None,
    txn_type: TransactionType | str | None = None,
) -> ImportProgress:
    """Create and deterministically resolve one whole-chunk review successor."""
    if actor_user_id < 1:
        raise ValueError("actor_user_id must be positive")
    status = CandidateResolutionStatus(resolution_status)
    if status not in _HUMAN_ACTIONS:
        raise InvalidParserStateError("human review must accept, correct, or reject")
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        for_update=True,
    )
    target = db.execute(
        select(BankStatementParseCandidate)
        .where(
            BankStatementParseCandidate.id == candidate_id,
            BankStatementParseCandidate.statement_import_id == batch.id,
            BankStatementParseCandidate.realm_id == realm_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if target is None:
        raise CandidateNotFoundError("bank parser candidate was not found in this import")
    predecessor = db.execute(
        select(BankStatementParseChunk)
        .where(
            BankStatementParseChunk.id == target.chunk_id,
            BankStatementParseChunk.bank_statement_import_id == batch.id,
            BankStatementParseChunk.realm_id == realm_id,
        )
        .with_for_update()
    ).scalar_one()
    rows = tuple(
        db.execute(
            select(BankStatementParseCandidate)
            .where(
                BankStatementParseCandidate.statement_import_id == batch.id,
                BankStatementParseCandidate.realm_id == realm_id,
                BankStatementParseCandidate.chunk_id == predecessor.id,
            )
            .order_by(BankStatementParseCandidate.source_ordinal)
            .with_for_update()
        ).scalars()
    )
    if not rows or all(row.id != candidate_id for row in rows):
        raise SourceIdentityError("reviewed chunk candidate set is incomplete")
    identity = _identity_from_import(batch)
    prior_candidates = {
        row.id: _candidate_from_row(db, row, identity=identity) for row in rows
    }
    prior_target = prior_candidates[candidate_id]
    requested_target = _ground_review_values(
        prior_target,
        status=status,
        txn_date=txn_date,
        signed_amount=signed_amount,
        description=description,
        txn_type=txn_type,
    )
    target_evidence = prior_target.resolution_evidence
    if (
        isinstance(target_evidence, CandidateResolutionEvidence)
        and target_evidence.authority
        is CandidateResolutionAuthority.AUDITED_HUMAN
        and requested_target.resolution_status == prior_target.resolution_status
        and (
            requested_target.txn_date,
            requested_target.signed_amount,
            requested_target.description,
            requested_target.txn_type,
        )
        == (
            prior_target.txn_date,
            prior_target.signed_amount,
            prior_target.description,
            prior_target.txn_type,
        )
        and _audit_matches_candidate(db, prior_target)
    ):
        # A client may retry after the first PATCH committed but its response was
        # lost.  Return the durable result instead of manufacturing another
        # successor, certificate, or human action.
        return get_import_progress(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
        )
    if batch.lifecycle_status != ImportLifecycleStatus.REVIEW_REQUIRED.value:
        raise InvalidParserStateError(
            "statement candidates may be edited only while the import requires review"
        )
    if predecessor.lifecycle_status != ChunkLifecycleStatus.REVIEW_REQUIRED.value:
        raise InvalidParserStateError("candidate's current chunk is not review-required")
    successor = _successor_chunk(
        db,
        predecessor=predecessor,
        actor_user_id=actor_user_id,
        action=status.value,
    )

    for row in rows:
        prior = prior_candidates[row.id]
        evidence = _candidate_evidence(row)
        moved = replace(prior, chunk_id=successor.id)
        if row.id == candidate_id:
            moved = replace(requested_target, chunk_id=successor.id)
            moved = _bind_human_audit(
                db,
                candidate=moved,
                prior=prior,
                actor_user_id=actor_user_id,
                parse_route=evidence.parse_route,
            )
        elif evidence.authority is CandidateResolutionAuthority.AUDITED_HUMAN:
            # Audit bindings include chunk identity.  Moving the whole candidate set
            # to the successor therefore requires a fresh, candidate-bound audit for
            # every previously reviewed sibling; otherwise later reviews invalidate
            # earlier ones and the chunk can never converge.
            moved = _carry_forward_human_audit(
                db,
                candidate=moved,
                prior=prior,
                parse_route=evidence.parse_route,
            )
        _write_candidate_row(row, moved)

    successor.resolution_payload = {
        "action": status.value,
        "actor_user_id": actor_user_id,
        "candidate_id": str(candidate_id),
    }
    has_staged = any(
        row.resolution_status == CandidateResolutionStatus.STAGED.value for row in rows
    )
    _set_chunk_terminal(
        successor,
        lifecycle_status=(
            ChunkLifecycleStatus.REVIEW_REQUIRED
            if has_staged
            else ChunkLifecycleStatus.SUCCEEDED
        ),
    )
    batch.lifecycle_status = ImportLifecycleStatus.PROCESSING.value
    batch.status = "parsing"
    batch.review_reason_code = None
    batch.review_action = None
    batch.error_detail = None
    db.flush()
    return finalize_import(
        db,
        statement_import_id=batch.id,
        realm_id=realm_id,
    )


def _has_matching_balance_audit(
    db: Session,
    *,
    batch: BankStatementImport,
    opening_balance: Decimal | None,
    closing_balance: Decimal | None,
) -> bool:
    """Prove that unchanged terminal balances came from a committed human edit."""
    expected = {
        "opening_balance": (
            format(opening_balance, ".2f") if opening_balance is not None else None
        ),
        "closing_balance": (
            format(closing_balance, ".2f") if closing_balance is not None else None
        ),
    }
    audits = db.execute(
        select(SystemAuditLog)
        .where(
            SystemAuditLog.company_id == batch.realm_id,
            SystemAuditLog.action == "BANK_PARSE_BALANCES_SET",
            SystemAuditLog.entity_type == "BankStatementImport",
            SystemAuditLog.entity_id == str(batch.id),
        )
        .order_by(SystemAuditLog.id.desc())
    ).scalars()
    return any(
        audit.user_id is not None
        and isinstance(audit.new_state, dict)
        and audit.new_state.get("opening_balance") == expected["opening_balance"]
        and audit.new_state.get("closing_balance") == expected["closing_balance"]
        for audit in audits
    )


def set_statement_balances(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int,
    actor_user_id: int,
    opening_balance: Decimal | None,
    closing_balance: Decimal | None,
) -> ImportProgress:
    if actor_user_id < 1:
        raise ValueError("actor_user_id must be positive")
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        for_update=True,
    )
    balances_are_unchanged = (
        batch.opening_balance == opening_balance
        and batch.closing_balance == closing_balance
    )
    if balances_are_unchanged and (
        batch.lifecycle_status == ImportLifecycleStatus.REVIEW_REQUIRED.value
        or (
            batch.lifecycle_status == ImportLifecycleStatus.PARSED.value
            and _has_matching_balance_audit(
                db,
                batch=batch,
                opening_balance=opening_balance,
                closing_balance=closing_balance,
            )
        )
    ):
        # Network retries of an already-committed PATCH are reads, not new human
        # edits.  Do not manufacture an audit row whose identical control result
        # would correctly reuse the latest certificate.
        return get_import_progress(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
        )
    if batch.lifecycle_status != ImportLifecycleStatus.REVIEW_REQUIRED.value:
        raise InvalidParserStateError(
            "statement balances may be edited only while the import requires review"
        )
    previous = {
        "opening_balance": format(batch.opening_balance, ".2f") if batch.opening_balance is not None else None,
        "closing_balance": format(batch.closing_balance, ".2f") if batch.closing_balance is not None else None,
        "lifecycle_status": batch.lifecycle_status,
    }
    batch.opening_balance = opening_balance
    batch.closing_balance = closing_balance
    # The frozen state machine permits review_required -> processing; only the
    # code-calculated control below may then transition processing -> parsed.
    batch.lifecycle_status = ImportLifecycleStatus.PROCESSING.value
    batch.status = "parsing"
    batch.review_reason_code = None
    batch.review_action = None
    batch.error_detail = None
    db.add(
        SystemAuditLog(
            company_id=realm_id,
            user_id=actor_user_id,
            action="BANK_PARSE_BALANCES_SET",
            entity_type="BankStatementImport",
            entity_id=str(batch.id),
            previous_state=previous,
            new_state={
                "opening_balance": format(opening_balance, ".2f") if opening_balance is not None else None,
                "closing_balance": format(closing_balance, ".2f") if closing_balance is not None else None,
                "lifecycle_status": ImportLifecycleStatus.PROCESSING.value,
            },
        )
    )
    db.flush()
    return finalize_import(
        db,
        statement_import_id=batch.id,
        realm_id=realm_id,
    )


def resolve_import(
    db: Session,
    *,
    realm_id: str,
    statement_import_id: int,
    actor_user_id: int,
) -> ImportProgress:
    if actor_user_id < 1:
        raise ValueError("actor_user_id must be positive")
    batch = _load_import(
        db,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        for_update=True,
    )
    if batch.lifecycle_status == ImportLifecycleStatus.PARSED.value:
        return get_import_progress(db, realm_id=realm_id, statement_import_id=batch.id)
    db.add(
        SystemAuditLog(
            company_id=realm_id,
            user_id=actor_user_id,
            action="BANK_PARSE_IMPORT_RESOLVE_REQUEST",
            entity_type="BankStatementImport",
            entity_id=str(batch.id),
            previous_state={"lifecycle_status": batch.lifecycle_status},
            new_state={"authority": "code_calculated_control_only"},
        )
    )
    db.flush()
    return finalize_import(db, statement_import_id=batch.id, realm_id=realm_id)


__all__ = [
    "CandidateNotFoundError",
    "CandidateView",
    "ChunkProcessOutcome",
    "ChunkProgress",
    "ControlView",
    "DeliveryProgress",
    "ImportListPage",
    "ImportNotFoundError",
    "ImportProgress",
    "ImportSummary",
    "InvalidParserStateError",
    "LeaseMismatchError",
    "ParserPersistenceError",
    "PreparedImport",
    "RetryPreparation",
    "SourceIdentityError",
    "finalize_import",
    "get_import_progress",
    "list_imports",
    "prepare_resumable_import",
    "prepare_retry",
    "process_leased_chunk",
    "resolve_import",
    "review_candidate",
    "set_statement_balances",
]

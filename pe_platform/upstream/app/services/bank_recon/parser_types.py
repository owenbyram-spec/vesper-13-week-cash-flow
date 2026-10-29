"""Frozen value objects for the resumable bank-statement parser.

The parser pipeline crosses deterministic extraction, ambiguity-only model work,
durable persistence, and statement control.  This module is the one shared type
boundary for those stages.  It deliberately contains no ORM, queue, provider, or
request-layer imports.

Transaction ordinals and page numbers are one-based.  Character spans are
zero-based, half-open offsets over :attr:`ExtractedPdf.source_text`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from uuid import UUID

PARSER_VERSION = "bank-parser-v2"
EXTRACTOR_VERSION = "pdfplumber-0.11.9:bank-v1"
PROMPT_SCHEMA_VERSION = "bank-parser-ambiguous-v1"
RESOLUTION_EVIDENCE_SCHEMA = "bank-parser-candidate-resolution-v1"
AUDIT_BINDING_SCHEMA = "bank-parser-human-resolution-binding-v1"

CONTROL_TOLERANCE = Decimal("0.01")
AMBIGUOUS_MAX_INPUT_TOKENS = 8_000
AMBIGUOUS_MAX_OUTPUT_TOKENS = 2_000
AMBIGUOUS_MAX_APPLICATION_ATTEMPTS = 2
AMBIGUOUS_SDK_RETRIES = 0

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ImportLifecycleStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    REVIEW_REQUIRED = "review_required"
    PARSED = "parsed"
    ERROR = "error"


class ChunkLifecycleStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"


class DeliveryLifecycleStatus(StrEnum):
    CREATED = "created"
    DISPATCHED = "dispatched"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


class ProviderAttemptLifecycleStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TRUNCATED = "truncated"
    TIMED_OUT = "timed_out"


class CandidateResolutionStatus(StrEnum):
    STAGED = "staged"
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class ControlCertificateStatus(StrEnum):
    PENDING = "pending"
    FAILED = "failed"
    PASSED = "passed"


class ParseRoute(StrEnum):
    DETERMINISTIC = "deterministic"
    AMBIGUOUS = "ambiguous"


class CandidateResolutionAuthority(StrEnum):
    DETERMINISTIC_CODE = "deterministic_code"
    UNTRUSTED_MODEL = "untrusted_model"
    AUDITED_HUMAN = "audited_human"


class TransactionType(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class PdfTextStatus(StrEnum):
    TEXT = "text"
    MIXED = "mixed"
    IMAGE_ONLY = "image_only"
    EMPTY = "empty"


class AmbiguityDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    REVIEW_REQUIRED = "review_required"
    RETRYABLE_FAILURE = "retryable_failure"


class ControlFailureCode(StrEnum):
    MANIFEST_INCOMPLETE = "manifest_incomplete"
    SOURCE_ORDINAL_DUPLICATE = "source_ordinal_duplicate"
    SOURCE_COVERAGE_MISMATCH = "source_coverage_mismatch"
    CANDIDATE_ID_DUPLICATE = "candidate_id_duplicate"
    CANDIDATE_IDENTITY_MISMATCH = "candidate_identity_mismatch"
    CANDIDATE_SOURCE_MISMATCH = "candidate_source_mismatch"
    UNRESOLVED_CANDIDATE = "unresolved_candidate"
    CANDIDATE_FIELDS_MISSING = "candidate_fields_missing"
    CANDIDATE_AMOUNT_INVALID = "candidate_amount_invalid"
    CANDIDATE_TXN_TYPE_MISMATCH = "candidate_txn_type_mismatch"
    CANDIDATE_RESOLUTION_AUTHORITY_INVALID = (
        "candidate_resolution_authority_invalid"
    )
    MISSING_OPENING_BALANCE = "missing_opening_balance"
    MISSING_CLOSING_BALANCE = "missing_closing_balance"
    BALANCE_INVALID = "balance_invalid"
    BALANCE_MISMATCH = "balance_mismatch"


IMPORT_TRANSITIONS: Mapping[
    ImportLifecycleStatus, frozenset[ImportLifecycleStatus]
] = MappingProxyType(
    {
        ImportLifecycleStatus.QUEUED: frozenset(
            {
                ImportLifecycleStatus.PROCESSING,
                ImportLifecycleStatus.REVIEW_REQUIRED,
                ImportLifecycleStatus.ERROR,
            }
        ),
        ImportLifecycleStatus.PROCESSING: frozenset(
            {
                ImportLifecycleStatus.REVIEW_REQUIRED,
                ImportLifecycleStatus.PARSED,
                ImportLifecycleStatus.ERROR,
            }
        ),
        ImportLifecycleStatus.REVIEW_REQUIRED: frozenset(
            {ImportLifecycleStatus.PROCESSING}
        ),
        ImportLifecycleStatus.ERROR: frozenset({ImportLifecycleStatus.QUEUED}),
        ImportLifecycleStatus.PARSED: frozenset(),
    }
)

CHUNK_TRANSITIONS: Mapping[
    ChunkLifecycleStatus, frozenset[ChunkLifecycleStatus]
] = MappingProxyType(
    {
        ChunkLifecycleStatus.PENDING: frozenset({ChunkLifecycleStatus.LEASED}),
        ChunkLifecycleStatus.LEASED: frozenset(
            {
                ChunkLifecycleStatus.SUCCEEDED,
                ChunkLifecycleStatus.REVIEW_REQUIRED,
                ChunkLifecycleStatus.FAILED,
                ChunkLifecycleStatus.PENDING,
            }
        ),
        ChunkLifecycleStatus.FAILED: frozenset({ChunkLifecycleStatus.PENDING}),
        ChunkLifecycleStatus.SUCCEEDED: frozenset(),
        ChunkLifecycleStatus.REVIEW_REQUIRED: frozenset(),
    }
)

DELIVERY_TRANSITIONS: Mapping[
    DeliveryLifecycleStatus, frozenset[DeliveryLifecycleStatus]
] = MappingProxyType(
    {
        DeliveryLifecycleStatus.CREATED: frozenset(
            {DeliveryLifecycleStatus.DISPATCHED, DeliveryLifecycleStatus.FAILED}
        ),
        DeliveryLifecycleStatus.DISPATCHED: frozenset(
            {
                DeliveryLifecycleStatus.STARTED,
                DeliveryLifecycleStatus.FAILED,
                DeliveryLifecycleStatus.EXPIRED,
            }
        ),
        DeliveryLifecycleStatus.STARTED: frozenset(
            {
                DeliveryLifecycleStatus.SUCCEEDED,
                DeliveryLifecycleStatus.FAILED,
                DeliveryLifecycleStatus.EXPIRED,
            }
        ),
        DeliveryLifecycleStatus.SUCCEEDED: frozenset(),
        DeliveryLifecycleStatus.FAILED: frozenset(),
        DeliveryLifecycleStatus.EXPIRED: frozenset(),
    }
)


def _require_sha256(value: str, *, field_name: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase 64-character SHA-256")


def _require_finite_coordinate(value: float, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a real number")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")


@dataclass(frozen=True, slots=True)
class ParserIdentity:
    source_sha256: str
    parser_version: str
    extractor_version: str
    prompt_schema_version: str

    def __post_init__(self) -> None:
        _require_sha256(self.source_sha256, field_name="source_sha256")
        for field_name in (
            "parser_version",
            "extractor_version",
            "prompt_schema_version",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must be non-empty")

    @classmethod
    def current(cls, source_sha256: str) -> ParserIdentity:
        return cls(
            source_sha256=source_sha256,
            parser_version=PARSER_VERSION,
            extractor_version=EXTRACTOR_VERSION,
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
        )


@dataclass(frozen=True, slots=True)
class ExtractedWord:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("extracted word text must be non-empty")
        for field_name in ("x0", "top", "x1", "bottom"):
            _require_finite_coordinate(getattr(self, field_name), field_name=field_name)
        if self.x1 < self.x0 or self.bottom < self.top:
            raise ValueError("extracted word coordinates must form a non-negative box")


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    bbox: tuple[float, float, float, float]
    rows: tuple[tuple[str | None, ...], ...]

    def __post_init__(self) -> None:
        if len(self.bbox) != 4:
            raise ValueError("table bbox must contain x0, top, x1, bottom")
        for index, coordinate in enumerate(self.bbox):
            _require_finite_coordinate(coordinate, field_name=f"bbox[{index}]")
        x0, top, x1, bottom = self.bbox
        if x1 < x0 or bottom < top:
            raise ValueError("table bbox must form a non-negative box")


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int
    span_start: int
    span_end: int
    source_text: str
    width: float
    height: float
    rotation: int
    image_count: int
    words: tuple[ExtractedWord, ...]
    tables: tuple[ExtractedTable, ...]

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number must be one-based")
        if self.span_start < 0 or self.span_end < self.span_start:
            raise ValueError("page spans must be zero-based and half-open")
        if self.span_end - self.span_start != len(self.source_text):
            raise ValueError("page span length must equal its exact source text length")
        _require_finite_coordinate(self.width, field_name="width")
        _require_finite_coordinate(self.height, field_name="height")
        if self.width < 0 or self.height < 0:
            raise ValueError("page dimensions must be non-negative")
        if self.image_count < 0:
            raise ValueError("image_count must be non-negative")


@dataclass(frozen=True, slots=True)
class ExtractedPdf:
    source_sha256: str
    source_size_bytes: int
    extractor_version: str
    text_status: PdfTextStatus
    source_text: str
    pages: tuple[ExtractedPage, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.source_sha256, field_name="source_sha256")
        if self.source_size_bytes < 0:
            raise ValueError("source_size_bytes must be non-negative")
        if not self.extractor_version.strip():
            raise ValueError("extractor_version must be non-empty")

        cursor = 0
        for expected_page_number, page in enumerate(self.pages, start=1):
            if page.page_number != expected_page_number:
                raise ValueError("extracted pages must be contiguous and one-based")
            if page.span_start != cursor:
                raise ValueError("page spans must be contiguous over canonical source text")
            if self.source_text[page.span_start : page.span_end] != page.source_text:
                raise ValueError("page source text must equal its canonical document slice")
            cursor = page.span_end
        if cursor != len(self.source_text):
            raise ValueError("page spans must cover all canonical source text")

        has_text = [bool(page.source_text.strip()) for page in self.pages]
        has_images = any(page.image_count > 0 for page in self.pages)
        expected_status = (
            PdfTextStatus.TEXT
            if has_text and all(has_text)
            else PdfTextStatus.MIXED
            if any(has_text)
            else PdfTextStatus.IMAGE_ONLY
            if has_images
            else PdfTextStatus.EMPTY
        )
        if self.text_status is not expected_status:
            raise ValueError(
                f"text_status must be {expected_status.value!r} for extracted page evidence"
            )


@dataclass(frozen=True, slots=True)
class SourceSpan:
    source_ordinal: int
    chunk_id: UUID
    page_start: int
    page_end: int
    span_start: int
    span_end: int
    source_text: str

    def __post_init__(self) -> None:
        if self.source_ordinal < 1:
            raise ValueError("source_ordinal must be one-based")
        if self.page_start < 1 or self.page_end < self.page_start:
            raise ValueError("source page range must be one-based and inclusive")
        if self.span_start < 0 or self.span_end <= self.span_start:
            raise ValueError("source span must be non-empty, zero-based, and half-open")
        if self.span_end - self.span_start != len(self.source_text):
            raise ValueError("source span length must equal its exact source text length")


@dataclass(frozen=True, slots=True)
class ExpectedSourceLine:
    source_ordinal: int
    source_page: int
    chunk_id: UUID
    source_text_sha256: str

    def __post_init__(self) -> None:
        if self.source_ordinal < 1:
            raise ValueError("expected source ordinal must be one-based")
        if self.source_page < 1:
            raise ValueError("expected source page must be one-based")
        _require_sha256(self.source_text_sha256, field_name="source_text_sha256")


@dataclass(frozen=True, slots=True)
class LogicalChunkDescriptor:
    """Stable logical work identity persisted before any delivery is published."""

    chunk_id: UUID
    identity: ParserIdentity
    statement_import_id: int
    realm_id: str
    page_start: int
    page_end: int
    span_start: int
    span_end: int
    chunk_revision: int
    supersedes_chunk_id: UUID | None

    def __post_init__(self) -> None:
        if self.statement_import_id < 1:
            raise ValueError("statement_import_id must be positive")
        if not self.realm_id.strip():
            raise ValueError("realm_id must be non-empty")
        if self.page_start < 1 or self.page_end < self.page_start:
            raise ValueError("chunk page range must be one-based and inclusive")
        if self.span_start < 0 or self.span_end < self.span_start:
            raise ValueError("chunk span must be zero-based and half-open")
        if self.chunk_revision < 0:
            raise ValueError("chunk_revision must be non-negative")
        if self.supersedes_chunk_id == self.chunk_id:
            raise ValueError("a chunk cannot supersede itself")


@dataclass(frozen=True, slots=True)
class SourceManifest:
    identity: ParserIdentity
    source_text_length: int
    coverage_complete: bool
    chunks: tuple[LogicalChunkDescriptor, ...]
    lines: tuple[ExpectedSourceLine, ...]

    def __post_init__(self) -> None:
        if self.source_text_length < 0:
            raise ValueError("source_text_length must be non-negative")
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        chunk_id_set = set(chunk_ids)
        if len(chunk_id_set) != len(chunk_ids):
            raise ValueError("manifest chunk IDs must be unique")
        if any(chunk.identity != self.identity for chunk in self.chunks):
            raise ValueError("manifest chunks must share the manifest identity")
        if len({chunk.statement_import_id for chunk in self.chunks}) > 1:
            raise ValueError("manifest chunks must share one statement import")
        if len({chunk.realm_id for chunk in self.chunks}) > 1:
            raise ValueError("manifest chunks must share one realm")
        if any(line.chunk_id not in chunk_id_set for line in self.lines):
            raise ValueError("every manifest line must reference a manifest chunk")
        if self.coverage_complete and self.chunks:
            ordered = sorted(
                self.chunks,
                key=lambda chunk: (chunk.span_start, chunk.span_end, chunk.chunk_id.hex),
            )
            if ordered[0].span_start != 0:
                raise ValueError("complete manifest chunk coverage must start at zero")
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if current.span_start != previous.span_end:
                    raise ValueError(
                        "complete manifest chunks must be contiguous without overlap"
                    )
            if ordered[-1].span_end != self.source_text_length:
                raise ValueError(
                    "complete manifest chunk coverage must end at source_text_length"
                )
        elif self.coverage_complete and self.source_text_length != 0:
            raise ValueError("a nonempty complete manifest requires at least one chunk")


@dataclass(frozen=True, slots=True)
class CandidateResolutionEvidence:
    """Code-owned authority metadata stored in candidate ``resolution_payload``."""

    parse_route: ParseRoute
    authority: CandidateResolutionAuthority
    audit_log_id: int | None
    audit_binding_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.parse_route, ParseRoute):
            raise TypeError("parse_route must be a ParseRoute")
        if not isinstance(self.authority, CandidateResolutionAuthority):
            raise TypeError("authority must be a CandidateResolutionAuthority")
        if (
            self.authority is CandidateResolutionAuthority.AUDITED_HUMAN
            and (
                self.audit_log_id is None
                or self.audit_log_id < 1
                or self.audit_binding_sha256 is None
            )
        ):
            raise ValueError(
                "audited human authority requires an audit ID and binding SHA-256"
            )
        if self.audit_binding_sha256 is not None:
            _require_sha256(
                self.audit_binding_sha256,
                field_name="audit_binding_sha256",
            )
        if (
            self.authority is not CandidateResolutionAuthority.AUDITED_HUMAN
            and (
                self.audit_log_id is not None
                or self.audit_binding_sha256 is not None
            )
        ):
            raise ValueError("only audited human authority may carry audit evidence")

    def to_payload(self) -> dict[str, str | int | None]:
        return {
            "schema": RESOLUTION_EVIDENCE_SCHEMA,
            "parse_route": self.parse_route.value,
            "authority": self.authority.value,
            "audit_log_id": self.audit_log_id,
            "audit_binding_sha256": self.audit_binding_sha256,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CandidateResolutionEvidence:
        expected_keys = {
            "schema",
            "parse_route",
            "authority",
            "audit_log_id",
            "audit_binding_sha256",
        }
        if set(payload) != expected_keys:
            raise ValueError("resolution evidence payload must contain the exact schema keys")
        if payload["schema"] != RESOLUTION_EVIDENCE_SCHEMA:
            raise ValueError("resolution evidence schema is unsupported")
        audit_log_id = payload["audit_log_id"]
        if audit_log_id is not None and (
            isinstance(audit_log_id, bool) or not isinstance(audit_log_id, int)
        ):
            raise TypeError("audit_log_id must be an integer or None")
        return cls(
            parse_route=ParseRoute(payload["parse_route"]),
            authority=CandidateResolutionAuthority(payload["authority"]),
            audit_log_id=audit_log_id,
            audit_binding_sha256=payload["audit_binding_sha256"],
        )


@dataclass(frozen=True, slots=True)
class ParsedCandidate:
    candidate_id: UUID
    identity: ParserIdentity
    statement_import_id: int
    realm_id: str
    chunk_id: UUID
    source_ordinal: int
    source_page: int
    source_text: str
    txn_date: date | None
    signed_amount: Decimal | None
    description: str | None
    txn_type: TransactionType | str | None
    resolution_status: CandidateResolutionStatus
    resolution_evidence: CandidateResolutionEvidence


@dataclass(frozen=True, slots=True)
class DeterministicParseResult:
    manifest: SourceManifest
    candidates: tuple[ParsedCandidate, ...]
    ambiguous_spans: tuple[SourceSpan, ...]
    opening_balance: Decimal | None
    closing_balance: Decimal | None

    def __post_init__(self) -> None:
        chunk_ids = {chunk.chunk_id for chunk in self.manifest.chunks}
        if any(candidate.chunk_id not in chunk_ids for candidate in self.candidates):
            raise ValueError("every deterministic candidate must reference a result chunk")
        if any(span.chunk_id not in chunk_ids for span in self.ambiguous_spans):
            raise ValueError("every ambiguous span must reference a result chunk")


@dataclass(frozen=True, slots=True)
class AmbiguousCandidateProposal:
    """Provider-shaped evidence with no field capable of granting authority."""

    source_ordinal: int
    source_page: int
    source_text: str
    txn_date: date | None
    signed_amount: Decimal | None
    description: str | None
    txn_type: TransactionType | str | None

    def __post_init__(self) -> None:
        if self.source_ordinal < 1:
            raise ValueError("proposal source_ordinal must be one-based")
        if self.source_page < 1:
            raise ValueError("proposal source_page must be one-based")
        if not self.source_text:
            raise ValueError("proposal source_text must be non-empty")


@dataclass(frozen=True, slots=True)
class AmbiguityParseRequest:
    identity: ParserIdentity
    statement_import_id: int
    realm_id: str
    chunk_id: UUID
    chunk_revision: int
    spans: tuple[SourceSpan, ...]
    attempt_ordinal: int

    def __post_init__(self) -> None:
        if self.statement_import_id < 1:
            raise ValueError("statement_import_id must be positive")
        if not self.realm_id.strip():
            raise ValueError("realm_id must be non-empty")
        if self.chunk_revision < 0:
            raise ValueError("chunk_revision must be non-negative")
        if not 0 <= self.attempt_ordinal < AMBIGUOUS_MAX_APPLICATION_ATTEMPTS:
            raise ValueError("attempt_ordinal must be zero or one")
        if not self.spans:
            raise ValueError("ambiguous request must contain at least one source span")
        if any(span.chunk_id != self.chunk_id for span in self.spans):
            raise ValueError("every ambiguous span must belong to the request chunk")
        ordered_keys = [
            (span.span_start, span.span_end, span.source_ordinal)
            for span in self.spans
        ]
        if ordered_keys != sorted(ordered_keys) or len(set(ordered_keys)) != len(
            ordered_keys
        ):
            raise ValueError("ambiguous spans must be ordered and unique")


@dataclass(frozen=True, slots=True)
class AmbiguityParseResult:
    proposals: tuple[AmbiguousCandidateProposal, ...]
    disposition: AmbiguityDisposition
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class ControlIssue:
    code: ControlFailureCode
    source_ordinal: int | None = None
    candidate_id: UUID | None = None
    field_name: str | None = None


@dataclass(frozen=True, slots=True)
class ControlCertificate:
    identity: ParserIdentity
    statement_import_id: int
    realm_id: str
    certificate_version: int
    status: ControlCertificateStatus
    expected_count: int
    observed_count: int
    accepted_count: int
    rejected_count: int
    candidate_set_sha256: str
    opening_balance: Decimal | None
    signed_activity: Decimal | None
    closing_balance: Decimal | None
    residual: Decimal | None
    issues: tuple[ControlIssue, ...]


@runtime_checkable
class StatementExtractor(Protocol):
    def extract(self, source_bytes: bytes) -> ExtractedPdf: ...


@runtime_checkable
class DeterministicStatementParser(Protocol):
    def parse(
        self,
        statement: ExtractedPdf,
        *,
        identity: ParserIdentity,
        statement_import_id: int,
        realm_id: str,
    ) -> DeterministicParseResult: ...


@runtime_checkable
class AmbiguousSpanResolver(Protocol):
    def resolve(self, request: AmbiguityParseRequest) -> AmbiguityParseResult: ...


__all__ = [
    "AMBIGUOUS_MAX_APPLICATION_ATTEMPTS",
    "AMBIGUOUS_MAX_INPUT_TOKENS",
    "AMBIGUOUS_MAX_OUTPUT_TOKENS",
    "AMBIGUOUS_SDK_RETRIES",
    "AUDIT_BINDING_SCHEMA",
    "CHUNK_TRANSITIONS",
    "CONTROL_TOLERANCE",
    "DELIVERY_TRANSITIONS",
    "EXTRACTOR_VERSION",
    "IMPORT_TRANSITIONS",
    "PARSER_VERSION",
    "PROMPT_SCHEMA_VERSION",
    "RESOLUTION_EVIDENCE_SCHEMA",
    "AmbiguousCandidateProposal",
    "AmbiguityDisposition",
    "AmbiguityParseRequest",
    "AmbiguityParseResult",
    "AmbiguousSpanResolver",
    "CandidateResolutionStatus",
    "CandidateResolutionAuthority",
    "CandidateResolutionEvidence",
    "ChunkLifecycleStatus",
    "ControlCertificate",
    "ControlCertificateStatus",
    "ControlFailureCode",
    "ControlIssue",
    "DeliveryLifecycleStatus",
    "DeterministicParseResult",
    "DeterministicStatementParser",
    "ExpectedSourceLine",
    "ExtractedPage",
    "ExtractedPdf",
    "ExtractedTable",
    "ExtractedWord",
    "ImportLifecycleStatus",
    "LogicalChunkDescriptor",
    "ParseRoute",
    "ParserIdentity",
    "PdfTextStatus",
    "ProviderAttemptLifecycleStatus",
    "SourceManifest",
    "SourceSpan",
    "StatementExtractor",
    "ParsedCandidate",
    "TransactionType",
]

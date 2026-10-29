"""Deterministic, source-grounded bank-statement parsing.

The parser consumes the immutable page extraction contract and emits stable logical
chunks, accepted deterministic candidates, and exact spans for evidence that needs
the ambiguity lane.  It has no provider dependency: uncertain text stays uncertain.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import re
from uuid import UUID, uuid5

from .parser_types import (
    EXTRACTOR_VERSION,
    PARSER_VERSION,
    PROMPT_SCHEMA_VERSION,
    CandidateResolutionAuthority,
    CandidateResolutionEvidence,
    CandidateResolutionStatus,
    DeterministicParseResult,
    ExpectedSourceLine,
    ExtractedPdf,
    LogicalChunkDescriptor,
    ParsedCandidate,
    ParserIdentity,
    ParseRoute,
    PdfTextStatus,
    SourceManifest,
    SourceSpan,
    TransactionType,
)

_IDENTITY_NAMESPACE = UUID("c15f746a-5d8c-4ddf-88e9-16fd7d90fd92")
_CENT = Decimal("0.01")
_MAX_NUMERIC_18_2 = Decimal("9999999999999999.99")
_UNSIGNED_MONEY_PATTERN = r"(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}"
_CANONICAL_MONEY_PATTERN = (
    rf"(?:[+-]?{_UNSIGNED_MONEY_PATTERN}|\({_UNSIGNED_MONEY_PATTERN}\))"
)
_CANONICAL_MONEY_RE = re.compile(_CANONICAL_MONEY_PATTERN)
_ROW_START_RE = re.compile(
    r"^(?P<transaction_id>\S+)\s+(?P<txn_date>\d{4}-\d{2}-\d{2})\s+(?P<body>.+)$"
)
_ROW_RE = re.compile(
    r"^(?P<transaction_id>\S+)\s+"
    r"(?P<txn_date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount_form>[DCS])\s+"
    rf"(?P<amount>{_CANONICAL_MONEY_PATTERN})\s+"
    rf"B(?:\s+(?P<running_balance>{_CANONICAL_MONEY_PATTERN}))?$"
)
_DATE_LIKE_RE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b"
)
_PARTIAL_DATE_LIKE_RE = re.compile(
    r"\b(?:"
    r"(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?"
    r"|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)(?:\s+\d{4})?"
    r")\b",
    re.IGNORECASE,
)
_COLUMN_HEADER_RE = re.compile(
    r"^(?:ID\s+)?(?:TRANSACTION\s+)?DATE\s+DESCRIPTION\b",
    re.IGNORECASE,
)
_MONEY_LIKE_RE = re.compile(r"[$()]?[+-]?\d[\d,]*\.\d{2}[)]?")
_LABELED_SUMMARY_RE = re.compile(
    r"^(?:TOTAL\s+)?(?:DEPOSITS?|WITHDRAWALS?|FEES?|CREDITS?|DEBITS?|"
    r"PAYMENTS?|PURCHASES?|CHECKS?|INTEREST(?:\s+(?:PAID|EARNED))?|"
    r"AVERAGE\s+BALANCE|AVAILABLE\s+BALANCE|TOTAL\s+BALANCE)\b",
    re.IGNORECASE,
)
_CROSS_PAGE_MARKER = "[CONTINUED ON NEXT PAGE - SYNTHETIC AMBIGUITY]"
_CONTINUATION_PREFIX = "CONT:"
_ACTIVITY_CONTROL_RE = re.compile(
    rf"^ACTIVITY\s+{_CANONICAL_MONEY_PATTERN}\s*\|\s*"
    rf"(?:CLOSING|ENDING)\s+BALANCE\s*:?\s*(?:USD\s*)?\$?"
    rf"{_CANONICAL_MONEY_PATTERN}\s*\|\s*CONTROL\s+RESIDUAL\s+"
    rf"{_CANONICAL_MONEY_PATTERN}$",
    re.IGNORECASE,
)
_OPENING_BALANCE_RE = re.compile(
    rf"^(?:ACCOUNT\b.*?\s+)?(?:OPENING|BEGINNING)\s+BALANCE\s*:?\s*"
    rf"(?:USD\s*)?\$?({_CANONICAL_MONEY_PATTERN})(?:\s*\|.*)?$",
    re.IGNORECASE,
)
_CLOSING_BALANCE_RE = re.compile(
    rf"^(?:ACTIVITY\b[^|\n]*\|\s*)?(?:CLOSING|ENDING)\s+BALANCE\s*:?\s*"
    rf"(?:USD\s*)?\$?({_CANONICAL_MONEY_PATTERN})(?:\s*\|.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _SourceLine:
    page_number: int
    span_start: int
    span_end: int
    text: str


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    txn_date: date
    description: str
    signed_amount: Decimal


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    source_ordinal: int
    transaction_id: str
    txn_date_text: str
    page_start: int
    page_end: int
    span_start: int
    span_end: int
    first_line: _SourceLine
    continuation: _SourceLine | None
    parsed_row: _ParsedRow | None
    structurally_ambiguous: bool


@dataclass(frozen=True, slots=True)
class _RecordScan:
    records: tuple[_SourceRecord, ...]
    recognized_source_structure: bool


def _line_text_and_terminator_length(raw_line: str) -> tuple[str, int]:
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], 2
    if raw_line.endswith(("\n", "\r")):
        return raw_line[:-1], 1
    return raw_line, 0


def _source_lines(statement: ExtractedPdf) -> tuple[_SourceLine, ...]:
    lines: list[_SourceLine] = []
    for page in statement.pages:
        cursor = page.span_start
        for raw_line in page.source_text.splitlines(keepends=True):
            text, terminator_length = _line_text_and_terminator_length(raw_line)
            span_start = cursor
            span_end = span_start + len(text)
            lines.append(
                _SourceLine(
                    page_number=page.page_number,
                    span_start=span_start,
                    span_end=span_end,
                    text=text,
                )
            )
            cursor = span_end + terminator_length
        if cursor != page.span_end:
            raise ValueError("extracted page line offsets do not cover page source text")
    return tuple(lines)


def _money(value: str) -> Decimal | None:
    if _CANONICAL_MONEY_RE.fullmatch(value) is None:
        return None
    normalized = value.replace(",", "")
    negative_parentheses = normalized.startswith("(") and normalized.endswith(")")
    if negative_parentheses:
        normalized = normalized[1:-1]
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    if negative_parentheses:
        amount = -amount
    if not amount.is_finite() or abs(amount) > _MAX_NUMERIC_18_2:
        return None
    try:
        if amount.quantize(_CENT) != amount:
            return None
    except InvalidOperation:
        return None
    return amount


def _parse_row(source_text: str) -> _ParsedRow | None:
    match = _ROW_RE.fullmatch(source_text)
    if match is None:
        return None
    try:
        txn_date = date.fromisoformat(match.group("txn_date"))
    except ValueError:
        return None
    amount = _money(match.group("amount"))
    if amount is None:
        return None
    amount_form = match.group("amount_form")
    if amount_form == "D":
        if amount < 0:
            return None
        amount = -amount
    elif amount_form == "C":
        if amount < 0:
            return None
    description = match.group("description").strip()
    if not description:
        return None
    return _ParsedRow(
        txn_date=txn_date,
        description=description,
        signed_amount=amount,
    )


def _matching_cross_page_continuation(
    lines: tuple[_SourceLine, ...],
    *,
    start_index: int,
    transaction_id: str,
    txn_date_text: str,
) -> int | None:
    for index in range(start_index, len(lines)):
        match = _ROW_START_RE.fullmatch(lines[index].text)
        if match is None:
            continue
        if (
            match.group("transaction_id") == transaction_id
            and match.group("txn_date") == txn_date_text
        ):
            return index
        return None
    return None


def _known_non_transaction_line(source_text: str) -> bool:
    stripped = source_text.strip()
    return (
        not stripped
        or stripped == "VESPER BOT SYNTHETIC BANK STATEMENT"
        or stripped.startswith("Fixture:")
        or stripped.startswith("Account:")
        or stripped.lower().startswith("statement period:")
        or stripped.lower().startswith("page ")
        or _COLUMN_HEADER_RE.search(stripped) is not None
        or _ACTIVITY_CONTROL_RE.fullmatch(stripped) is not None
        or stripped.startswith("SYNTHETIC / REDACTED TEST ASSET")
        or _OPENING_BALANCE_RE.fullmatch(stripped) is not None
        or _CLOSING_BALANCE_RE.fullmatch(stripped) is not None
    )


def _has_transaction_date_signal(source_text: str) -> bool:
    if _DATE_LIKE_RE.search(source_text) is not None:
        return True
    return (
        _PARTIAL_DATE_LIKE_RE.search(source_text) is not None
        and _MONEY_LIKE_RE.search(source_text) is not None
    )


def _explicit_date_metadata(source_text: str) -> bool:
    stripped = source_text.strip().lower()
    return stripped.startswith(("account:", "fixture:", "page ", "statement period:"))


def _generic_page_prefix_metadata(source_text: str) -> bool:
    """Recognize bounded header prose only before a page's first row evidence.

    Ordinary bank names and addresses vary too much for a vendor allowlist.  A short
    non-monetary prefix line is safe metadata only in the page-header region; the
    same unknown text after a row remains unaccounted evidence and makes the
    manifest incomplete.
    """
    stripped = source_text.strip()
    return (
        bool(stripped)
        and len(stripped) <= 160
        and any(character.isalpha() for character in stripped)
        and not _has_transaction_date_signal(stripped)
        and (
            _MONEY_LIKE_RE.search(stripped) is None
            or _LABELED_SUMMARY_RE.search(stripped) is not None
        )
    )


def _records(statement: ExtractedPdf) -> _RecordScan:
    lines = _source_lines(statement)
    records: list[_SourceRecord] = []
    consumed: set[int] = set()
    recognized_lines: set[int] = set()

    for index, line in enumerate(lines):
        if index in consumed:
            continue
        start_match = _ROW_START_RE.fullmatch(line.text)
        if start_match is None:
            if _has_transaction_date_signal(
                line.text
            ) and not _explicit_date_metadata(line.text):
                records.append(
                    _SourceRecord(
                        source_ordinal=len(records) + 1,
                        transaction_id="",
                        txn_date_text="",
                        page_start=line.page_number,
                        page_end=line.page_number,
                        span_start=line.span_start,
                        span_end=line.span_end,
                        first_line=line,
                        continuation=None,
                        parsed_row=None,
                        structurally_ambiguous=True,
                    )
                )
                recognized_lines.add(index)
                continue
            if _known_non_transaction_line(line.text):
                recognized_lines.add(index)
            continue

        recognized_lines.add(index)
        transaction_id = start_match.group("transaction_id")
        txn_date_text = start_match.group("txn_date")
        continuation: _SourceLine | None = None
        structurally_ambiguous = False

        next_index = index + 1
        if next_index < len(lines) and lines[next_index].text == _CROSS_PAGE_MARKER:
            continuation_index = _matching_cross_page_continuation(
                lines,
                start_index=index + 2,
                transaction_id=transaction_id,
                txn_date_text=txn_date_text,
            )
            if continuation_index is not None:
                continuation = lines[continuation_index]
                consumed.add(continuation_index)
                recognized_lines.add(continuation_index)
            recognized_lines.add(next_index)
            structurally_ambiguous = True
        elif (
            next_index < len(lines)
            and lines[next_index].text.startswith(_CONTINUATION_PREFIX)
        ):
            continuation = lines[next_index]
            consumed.add(next_index)
            recognized_lines.add(next_index)
            structurally_ambiguous = True

        span_end = continuation.span_end if continuation is not None else line.span_end
        page_end = continuation.page_number if continuation is not None else line.page_number
        records.append(
            _SourceRecord(
                source_ordinal=len(records) + 1,
                transaction_id=transaction_id,
                txn_date_text=txn_date_text,
                page_start=line.page_number,
                page_end=page_end,
                span_start=line.span_start,
                span_end=span_end,
                first_line=line,
                continuation=continuation,
                parsed_row=_parse_row(line.text),
                structurally_ambiguous=structurally_ambiguous,
            )
        )
    first_evidence_by_page: dict[int, int] = {}
    for record in records:
        evidence_lines = (record.first_line, record.continuation)
        for evidence_line in evidence_lines:
            if evidence_line is None:
                continue
            current = first_evidence_by_page.get(evidence_line.page_number)
            if current is None or evidence_line.span_start < current:
                first_evidence_by_page[evidence_line.page_number] = (
                    evidence_line.span_start
                )

    recognized_source_structure = True
    for index, line in enumerate(lines):
        if index in recognized_lines or _known_non_transaction_line(line.text):
            continue
        page = statement.pages[line.page_number - 1]
        first_evidence = first_evidence_by_page.get(line.page_number, page.span_end)
        if (
            line.span_end <= first_evidence
            and _generic_page_prefix_metadata(line.text)
        ):
            continue
        recognized_source_structure = False
        break
    return _RecordScan(
        records=tuple(records),
        recognized_source_structure=recognized_source_structure,
    )


def _description_expansions(records: tuple[_SourceRecord, ...]) -> dict[str, str]:
    """Return only statement-local, uniquely evidenced truncation expansions.

    Expansion evidence comes from exact ``CONT:`` bytes inside this parse's immutable
    statement identity.  Conflicting suffixes disable the prefix entirely, and no
    module/global cache carries evidence across statements or parser invocations.
    """
    candidates: dict[str, set[str]] = {}
    for record in records:
        if (
            record.parsed_row is None
            or record.continuation is None
            or not record.continuation.text.startswith(_CONTINUATION_PREFIX)
        ):
            continue
        suffix = record.continuation.text.removeprefix(_CONTINUATION_PREFIX).lstrip()
        if not suffix:
            continue
        prefix = record.parsed_row.description
        candidates.setdefault(prefix, set()).add(prefix + suffix)
    return {
        prefix: next(iter(expansions))
        for prefix, expansions in candidates.items()
        if len(expansions) == 1
    }


def _stable_uuid(kind: str, *parts: object) -> UUID:
    encoded = "\x1f".join((kind, *(str(part) for part in parts)))
    return uuid5(_IDENTITY_NAMESPACE, encoded)


def _logical_chunks(
    statement: ExtractedPdf,
    *,
    identity: ParserIdentity,
    statement_import_id: int,
    realm_id: str,
    records: tuple[_SourceRecord, ...],
) -> tuple[LogicalChunkDescriptor, ...]:
    if not statement.source_text or not statement.pages:
        return ()

    linked_boundaries = {
        page_number
        for record in records
        if record.page_end > record.page_start
        for page_number in range(record.page_start, record.page_end)
    }
    groups: list[tuple[int, int]] = []
    group_start = 1
    for previous, current in zip(statement.pages, statement.pages[1:], strict=False):
        keep_together = (
            previous.page_number in linked_boundaries
            or previous.span_start == previous.span_end
            or current.span_start == current.span_end
        )
        if not keep_together:
            groups.append((group_start, previous.page_number))
            group_start = current.page_number
    groups.append((group_start, statement.pages[-1].page_number))

    chunks: list[LogicalChunkDescriptor] = []
    for page_start, page_end in groups:
        span_start = statement.pages[page_start - 1].span_start
        span_end = statement.pages[page_end - 1].span_end
        chunk_id = _stable_uuid(
            "logical-chunk-v1",
            identity.source_sha256,
            identity.parser_version,
            identity.extractor_version,
            identity.prompt_schema_version,
            statement_import_id,
            realm_id,
            page_start,
            page_end,
            span_start,
            span_end,
            0,
        )
        chunks.append(
            LogicalChunkDescriptor(
                chunk_id=chunk_id,
                identity=identity,
                statement_import_id=statement_import_id,
                realm_id=realm_id,
                page_start=page_start,
                page_end=page_end,
                span_start=span_start,
                span_end=span_end,
                chunk_revision=0,
                supersedes_chunk_id=None,
            )
        )
    return tuple(chunks)


def _chunk_for_record(
    record: _SourceRecord,
    chunks: tuple[LogicalChunkDescriptor, ...],
) -> LogicalChunkDescriptor:
    matches = tuple(
        chunk
        for chunk in chunks
        if chunk.page_start <= record.page_start
        and record.page_end <= chunk.page_end
        and chunk.span_start <= record.span_start
        and record.span_end <= chunk.span_end
    )
    if len(matches) != 1:
        raise ValueError("each parsed source record must belong to exactly one logical chunk")
    return matches[0]


def _unique_balance(
    pattern: re.Pattern[str], statement: ExtractedPdf
) -> Decimal | None:
    matches: list[str] = []
    for line in _source_lines(statement):
        stripped = line.text.strip()
        if _ROW_START_RE.fullmatch(stripped) is not None:
            continue
        if _has_transaction_date_signal(stripped) and not _explicit_date_metadata(
            stripped
        ):
            continue
        match = pattern.fullmatch(stripped)
        if match is not None:
            matches.append(match.group(1))
    if not matches:
        return None
    values = tuple(_money(value) for value in matches)
    if any(value is None for value in values):
        return None
    unique_values = set(values)
    if len(unique_values) != 1:
        return None
    value = next(iter(unique_values))
    assert isinstance(value, Decimal)
    return value


def _validate_inputs(
    statement: ExtractedPdf,
    *,
    identity: ParserIdentity,
    statement_import_id: int,
    realm_id: str,
) -> None:
    if statement.source_sha256 != identity.source_sha256:
        raise ValueError("parser identity source hash must match extracted statement")
    if statement.extractor_version != identity.extractor_version:
        raise ValueError("parser identity extractor version must match extracted statement")
    if identity.parser_version != PARSER_VERSION:
        raise ValueError("parser identity parser version is unsupported")
    if identity.extractor_version != EXTRACTOR_VERSION:
        raise ValueError("parser identity extractor version is unsupported")
    if identity.prompt_schema_version != PROMPT_SCHEMA_VERSION:
        raise ValueError("parser identity prompt schema version is unsupported")
    if isinstance(statement_import_id, bool) or not isinstance(statement_import_id, int):
        raise TypeError("statement_import_id must be an integer")
    if statement_import_id < 1:
        raise ValueError("statement_import_id must be positive")
    if not isinstance(realm_id, str):
        raise TypeError("realm_id must be a string")
    if not realm_id.strip():
        raise ValueError("realm_id must be non-empty")


class DeterministicBankStatementParser:
    """Parse controlled text rows and isolate every uncertain source span."""

    def parse(
        self,
        statement: ExtractedPdf,
        *,
        identity: ParserIdentity,
        statement_import_id: int,
        realm_id: str,
    ) -> DeterministicParseResult:
        _validate_inputs(
            statement,
            identity=identity,
            statement_import_id=statement_import_id,
            realm_id=realm_id,
        )
        if statement.text_status in {PdfTextStatus.IMAGE_ONLY, PdfTextStatus.EMPTY}:
            return DeterministicParseResult(
                manifest=SourceManifest(
                    identity=identity,
                    source_text_length=len(statement.source_text),
                    coverage_complete=False,
                    chunks=(),
                    lines=(),
                ),
                candidates=(),
                ambiguous_spans=(),
                opening_balance=None,
                closing_balance=None,
            )

        record_scan = _records(statement)
        records = record_scan.records
        expansions = _description_expansions(records)
        chunks = _logical_chunks(
            statement,
            identity=identity,
            statement_import_id=statement_import_id,
            realm_id=realm_id,
            records=records,
        )
        candidates: list[ParsedCandidate] = []
        ambiguous_spans: list[SourceSpan] = []
        manifest_lines: list[ExpectedSourceLine] = []
        evidence = CandidateResolutionEvidence(
            parse_route=ParseRoute.DETERMINISTIC,
            authority=CandidateResolutionAuthority.DETERMINISTIC_CODE,
            audit_log_id=None,
            audit_binding_sha256=None,
        )

        for record in records:
            chunk = _chunk_for_record(record, chunks)
            exact_span_text = statement.source_text[record.span_start : record.span_end]
            parsed_row = record.parsed_row
            description = parsed_row.description if parsed_row is not None else None
            if description is not None and len(description) == 31:
                description = expansions.get(description)
            is_ambiguous = (
                record.structurally_ambiguous
                or parsed_row is None
                or description is None
            )
            source_text = exact_span_text if is_ambiguous else record.first_line.text

            manifest_lines.append(
                ExpectedSourceLine(
                    source_ordinal=record.source_ordinal,
                    source_page=record.page_start,
                    chunk_id=chunk.chunk_id,
                    source_text_sha256=hashlib.sha256(
                        source_text.encode("utf-8")
                    ).hexdigest(),
                )
            )
            if is_ambiguous:
                ambiguous_spans.append(
                    SourceSpan(
                        source_ordinal=record.source_ordinal,
                        chunk_id=chunk.chunk_id,
                        page_start=record.page_start,
                        page_end=record.page_end,
                        span_start=record.span_start,
                        span_end=record.span_end,
                        source_text=exact_span_text,
                    )
                )
                continue

            assert parsed_row is not None
            assert description is not None
            candidate_id = _stable_uuid(
                "deterministic-candidate-v1",
                identity.source_sha256,
                identity.parser_version,
                identity.extractor_version,
                identity.prompt_schema_version,
                statement_import_id,
                realm_id,
                chunk.chunk_id,
                record.source_ordinal,
                record.span_start,
                record.span_end,
            )
            candidates.append(
                ParsedCandidate(
                    candidate_id=candidate_id,
                    identity=identity,
                    statement_import_id=statement_import_id,
                    realm_id=realm_id,
                    chunk_id=chunk.chunk_id,
                    source_ordinal=record.source_ordinal,
                    source_page=record.page_start,
                    source_text=record.first_line.text,
                    txn_date=parsed_row.txn_date,
                    signed_amount=parsed_row.signed_amount,
                    description=description,
                    txn_type=(
                        TransactionType.DEBIT
                        if parsed_row.signed_amount < 0
                        else TransactionType.CREDIT
                    ),
                    resolution_status=CandidateResolutionStatus.ACCEPTED,
                    resolution_evidence=evidence,
                )
            )

        manifest = SourceManifest(
            identity=identity,
            source_text_length=len(statement.source_text),
            coverage_complete=(
                record_scan.recognized_source_structure
                and statement.text_status is PdfTextStatus.TEXT
            ),
            chunks=chunks,
            lines=tuple(manifest_lines),
        )
        return DeterministicParseResult(
            manifest=manifest,
            candidates=tuple(candidates),
            ambiguous_spans=tuple(ambiguous_spans),
            opening_balance=_unique_balance(_OPENING_BALANCE_RE, statement),
            closing_balance=_unique_balance(_CLOSING_BALANCE_RE, statement),
        )


__all__ = ["DeterministicBankStatementParser"]

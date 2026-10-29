"""Pure statement-completeness and balance control for parser candidates.

Only this code calculates certificate status, counts, signed activity, and residual.
The caller cannot supply a desired status or tolerance.  Model and human-authored
values remain evidence to validate; neither is authority to mark an import parsed.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json

from .parser_types import (
    AUDIT_BINDING_SCHEMA,
    CONTROL_TOLERANCE,
    RESOLUTION_EVIDENCE_SCHEMA,
    CandidateResolutionAuthority,
    CandidateResolutionEvidence,
    CandidateResolutionStatus,
    ControlCertificate,
    ControlCertificateStatus,
    ControlFailureCode,
    ControlIssue,
    ParsedCandidate,
    ParserIdentity,
    ParseRoute,
    SourceManifest,
    TransactionType,
)

_CENT = Decimal("0.01")
_MAX_NUMERIC_18_2 = Decimal("9999999999999999.99")
_CANDIDATE_HASH_SCHEMA = "bank-parser-candidate-set-v1"


def _is_valid_money(value: object) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    try:
        quantized = value.quantize(_CENT)
    except InvalidOperation:
        return False
    return value == quantized and abs(value) <= _MAX_NUMERIC_18_2


def _canonical_money(value: object) -> str | None:
    if value is None:
        return None
    if _is_valid_money(value):
        assert isinstance(value, Decimal)
        quantized = value.quantize(_CENT)
        if quantized == Decimal("-0.00"):
            quantized = Decimal("0.00")
        return format(quantized, ".2f")
    return f"<invalid:{type(value).__name__}:{value!r}>"


def resolution_audit_binding_sha256(candidate: ParsedCandidate) -> str:
    """Bind one human audit row to the exact candidate action and new values."""
    evidence = candidate.resolution_evidence
    if not isinstance(evidence, CandidateResolutionEvidence):
        raise TypeError("candidate resolution evidence is required")
    resolution = (
        candidate.resolution_status.value
        if isinstance(candidate.resolution_status, CandidateResolutionStatus)
        else str(candidate.resolution_status)
    )
    action = {
        CandidateResolutionStatus.ACCEPTED.value: "BANK_PARSE_CANDIDATE_ACCEPT",
        CandidateResolutionStatus.CORRECTED.value: "BANK_PARSE_CANDIDATE_CORRECT",
        CandidateResolutionStatus.REJECTED.value: "BANK_PARSE_CANDIDATE_REJECT",
    }.get(resolution, "<invalid-resolution-action>")
    txn_date = (
        candidate.txn_date.isoformat()
        if isinstance(candidate.txn_date, date)
        else None
        if candidate.txn_date is None
        else f"<invalid:{type(candidate.txn_date).__name__}:{candidate.txn_date!r}>"
    )
    txn_type = (
        candidate.txn_type.value
        if isinstance(candidate.txn_type, TransactionType)
        else candidate.txn_type
        if isinstance(candidate.txn_type, str) or candidate.txn_type is None
        else f"<invalid:{type(candidate.txn_type).__name__}:{candidate.txn_type!r}>"
    )
    description = (
        candidate.description
        if isinstance(candidate.description, str) or candidate.description is None
        else f"<invalid:{type(candidate.description).__name__}:{candidate.description!r}>"
    )
    source_text = (
        candidate.source_text
        if isinstance(candidate.source_text, str)
        else f"<invalid:{type(candidate.source_text).__name__}:{candidate.source_text!r}>"
    )
    payload = {
        "action": action,
        "audit_log_id": evidence.audit_log_id,
        "authority": evidence.authority.value,
        "candidate_id": str(candidate.candidate_id),
        "chunk_id": str(candidate.chunk_id),
        "description": description,
        "extractor_version": candidate.identity.extractor_version,
        "parser_version": candidate.identity.parser_version,
        "prompt_schema_version": candidate.identity.prompt_schema_version,
        "realm_id": candidate.realm_id,
        "resolution_status": resolution,
        "route": evidence.parse_route.value,
        "schema": AUDIT_BINDING_SCHEMA,
        "signed_amount": _canonical_money(candidate.signed_amount),
        "source_ordinal": candidate.source_ordinal,
        "source_page": candidate.source_page,
        "source_sha256": candidate.identity.source_sha256,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "statement_import_id": candidate.statement_import_id,
        "txn_date": txn_date,
        "txn_type": txn_type,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_candidate(candidate: ParsedCandidate) -> dict[str, object]:
    txn_date = (
        candidate.txn_date.isoformat()
        if isinstance(candidate.txn_date, date)
        else None
        if candidate.txn_date is None
        else f"<invalid:{type(candidate.txn_date).__name__}:{candidate.txn_date!r}>"
    )
    txn_type = (
        candidate.txn_type.value
        if isinstance(candidate.txn_type, TransactionType)
        else candidate.txn_type
    )
    resolution_status = (
        candidate.resolution_status.value
        if isinstance(candidate.resolution_status, CandidateResolutionStatus)
        else str(candidate.resolution_status)
    )
    resolution_evidence = candidate.resolution_evidence
    canonical_evidence: dict[str, object]
    if isinstance(resolution_evidence, CandidateResolutionEvidence):
        canonical_evidence = resolution_evidence.to_payload()
    else:
        canonical_evidence = {
            "schema": RESOLUTION_EVIDENCE_SCHEMA,
            "parse_route": None,
            "authority": (
                f"<invalid:{type(resolution_evidence).__name__}:"
                f"{resolution_evidence!r}>"
            ),
            "audit_log_id": None,
            "audit_binding_sha256": None,
        }
    return {
        "candidate_id": str(candidate.candidate_id),
        "chunk_id": str(candidate.chunk_id),
        "description": (
            candidate.description
            if isinstance(candidate.description, str) or candidate.description is None
            else f"<invalid:{type(candidate.description).__name__}:{candidate.description!r}>"
        ),
        "extractor_version": candidate.identity.extractor_version,
        "parser_version": candidate.identity.parser_version,
        "prompt_schema_version": candidate.identity.prompt_schema_version,
        "realm_id": candidate.realm_id,
        "resolution_evidence": canonical_evidence,
        "resolution_status": resolution_status,
        "signed_amount": _canonical_money(candidate.signed_amount),
        "source_ordinal": candidate.source_ordinal,
        "source_page": candidate.source_page,
        "source_sha256": candidate.identity.source_sha256,
        "source_text": (
            candidate.source_text
            if isinstance(candidate.source_text, str)
            else f"<invalid:{type(candidate.source_text).__name__}:{candidate.source_text!r}>"
        ),
        "statement_import_id": candidate.statement_import_id,
        "txn_date": txn_date,
        "txn_type": txn_type,
    }


def candidate_set_sha256(candidates: Sequence[ParsedCandidate]) -> str:
    """Hash persisted candidate evidence independent of input sequence order."""
    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate.source_ordinal, candidate.candidate_id.hex),
    )
    payload = {
        "candidates": [_canonical_candidate(candidate) for candidate in ordered],
        "schema": _CANDIDATE_HASH_SCHEMA,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _issue(
    code: ControlFailureCode,
    *,
    candidate: ParsedCandidate | None = None,
    source_ordinal: int | None = None,
    field_name: str | None = None,
) -> ControlIssue:
    return ControlIssue(
        code=code,
        source_ordinal=(
            candidate.source_ordinal if candidate is not None else source_ordinal
        ),
        candidate_id=candidate.candidate_id if candidate is not None else None,
        field_name=field_name,
    )


def _identity_issues(
    candidate: ParsedCandidate,
    *,
    identity: ParserIdentity,
    statement_import_id: int,
    realm_id: str,
) -> list[ControlIssue]:
    issues: list[ControlIssue] = []
    for field_name, actual, expected in (
        ("identity", candidate.identity, identity),
        ("statement_import_id", candidate.statement_import_id, statement_import_id),
        ("realm_id", candidate.realm_id, realm_id),
    ):
        if actual != expected:
            issues.append(
                _issue(
                    ControlFailureCode.CANDIDATE_IDENTITY_MISMATCH,
                    candidate=candidate,
                    field_name=field_name,
                )
            )
    return issues


def _candidate_field_issues(candidate: ParsedCandidate) -> list[ControlIssue]:
    try:
        resolution = CandidateResolutionStatus(candidate.resolution_status)
    except (TypeError, ValueError):
        return [
            _issue(
                ControlFailureCode.UNRESOLVED_CANDIDATE,
                candidate=candidate,
                field_name="resolution_status",
            )
        ]

    issues = _candidate_resolution_authority_issues(candidate, resolution=resolution)
    if resolution is CandidateResolutionStatus.STAGED:
        issues.append(
            _issue(ControlFailureCode.UNRESOLVED_CANDIDATE, candidate=candidate)
        )
        return issues
    if resolution is CandidateResolutionStatus.REJECTED:
        return issues

    for field_name, valid in (
        ("txn_date", isinstance(candidate.txn_date, date)),
        ("signed_amount", candidate.signed_amount is not None),
        ("description", isinstance(candidate.description, str)),
        ("txn_type", candidate.txn_type is not None),
    ):
        if not valid:
            issues.append(
                _issue(
                    ControlFailureCode.CANDIDATE_FIELDS_MISSING,
                    candidate=candidate,
                    field_name=field_name,
                )
            )

    if candidate.signed_amount is not None and not _is_valid_money(
        candidate.signed_amount
    ):
        issues.append(
            _issue(
                ControlFailureCode.CANDIDATE_AMOUNT_INVALID,
                candidate=candidate,
                field_name="signed_amount",
            )
        )

    if _is_valid_money(candidate.signed_amount) and candidate.txn_type is not None:
        assert isinstance(candidate.signed_amount, Decimal)
        expected_type = (
            TransactionType.DEBIT
            if candidate.signed_amount < 0
            else TransactionType.CREDIT
        )
        try:
            actual_type = TransactionType(candidate.txn_type)
        except (TypeError, ValueError):
            actual_type = None
        if actual_type is not expected_type:
            issues.append(
                _issue(
                    ControlFailureCode.CANDIDATE_TXN_TYPE_MISMATCH,
                    candidate=candidate,
                    field_name="txn_type",
                )
            )
    return issues


def _candidate_resolution_authority_issues(
    candidate: ParsedCandidate,
    *,
    resolution: CandidateResolutionStatus,
) -> list[ControlIssue]:
    evidence = candidate.resolution_evidence
    if not isinstance(evidence, CandidateResolutionEvidence):
        return [
            _issue(
                ControlFailureCode.CANDIDATE_RESOLUTION_AUTHORITY_INVALID,
                candidate=candidate,
                field_name="resolution_evidence",
            )
        ]

    route = evidence.parse_route
    authority = evidence.authority
    audit_log_id = evidence.audit_log_id
    audit_binding_sha256 = evidence.audit_binding_sha256
    valid = False
    if (
        route is ParseRoute.AMBIGUOUS
        and authority is CandidateResolutionAuthority.UNTRUSTED_MODEL
        and audit_log_id is None
        and audit_binding_sha256 is None
    ):
        valid = resolution is CandidateResolutionStatus.STAGED
    elif (
        route is ParseRoute.DETERMINISTIC
        and authority is CandidateResolutionAuthority.DETERMINISTIC_CODE
        and audit_log_id is None
        and audit_binding_sha256 is None
    ):
        valid = resolution is CandidateResolutionStatus.ACCEPTED
    elif (
        route in {ParseRoute.DETERMINISTIC, ParseRoute.AMBIGUOUS}
        and authority is CandidateResolutionAuthority.AUDITED_HUMAN
        and isinstance(audit_log_id, int)
        and not isinstance(audit_log_id, bool)
        and audit_log_id > 0
        and audit_binding_sha256 == resolution_audit_binding_sha256(candidate)
    ):
        valid = resolution in {
            CandidateResolutionStatus.ACCEPTED,
            CandidateResolutionStatus.CORRECTED,
            CandidateResolutionStatus.REJECTED,
        }

    if valid:
        return []
    return [
        _issue(
            ControlFailureCode.CANDIDATE_RESOLUTION_AUTHORITY_INVALID,
            candidate=candidate,
            field_name="resolution_evidence",
        )
    ]


def calculate_control_certificate(
    *,
    identity: ParserIdentity,
    statement_import_id: int,
    realm_id: str,
    certificate_version: int,
    manifest: SourceManifest,
    candidates: Sequence[ParsedCandidate],
    opening_balance: Decimal | None,
    closing_balance: Decimal | None,
) -> ControlCertificate:
    """Calculate one immutable statement-control result from source evidence."""
    if statement_import_id < 1:
        raise ValueError("statement_import_id must be positive")
    if not realm_id.strip():
        raise ValueError("realm_id must be non-empty")
    if certificate_version < 1:
        raise ValueError("certificate_version must be positive")

    frozen_candidates = tuple(candidates)
    issues: list[ControlIssue] = []
    expected_ordinals = [line.source_ordinal for line in manifest.lines]
    candidate_ordinals = [candidate.source_ordinal for candidate in frozen_candidates]

    if manifest.identity != identity:
        issues.append(
            _issue(
                ControlFailureCode.CANDIDATE_IDENTITY_MISMATCH,
                field_name="manifest.identity",
            )
        )
    if any(
        chunk.statement_import_id != statement_import_id
        or chunk.realm_id != realm_id
        for chunk in manifest.chunks
    ):
        issues.append(
            _issue(
                ControlFailureCode.CANDIDATE_IDENTITY_MISMATCH,
                field_name="manifest.chunks",
            )
        )
    if not manifest.coverage_complete:
        issues.append(_issue(ControlFailureCode.MANIFEST_INCOMPLETE))

    expected_counts = Counter(expected_ordinals)
    if any(count > 1 for count in expected_counts.values()):
        issues.append(_issue(ControlFailureCode.SOURCE_ORDINAL_DUPLICATE))
    expected_sequence = list(range(1, len(manifest.lines) + 1))
    if sorted(expected_counts) != expected_sequence:
        issues.append(_issue(ControlFailureCode.SOURCE_COVERAGE_MISMATCH))

    candidate_ids = [candidate.candidate_id for candidate in frozen_candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        issues.append(_issue(ControlFailureCode.CANDIDATE_ID_DUPLICATE))
    candidate_counts = Counter(candidate_ordinals)
    if any(count > 1 for count in candidate_counts.values()):
        issues.append(_issue(ControlFailureCode.SOURCE_ORDINAL_DUPLICATE))
    if set(candidate_counts) != set(expected_counts):
        issues.append(_issue(ControlFailureCode.SOURCE_COVERAGE_MISMATCH))

    expected_by_ordinal = {line.source_ordinal: line for line in manifest.lines}
    resolutions: list[CandidateResolutionStatus | None] = []
    for candidate in frozen_candidates:
        issues.extend(
            _identity_issues(
                candidate,
                identity=identity,
                statement_import_id=statement_import_id,
                realm_id=realm_id,
            )
        )
        expected_line = expected_by_ordinal.get(candidate.source_ordinal)
        if expected_line is not None:
            source_text_sha256 = (
                hashlib.sha256(candidate.source_text.encode("utf-8")).hexdigest()
                if isinstance(candidate.source_text, str) and candidate.source_text
                else None
            )
            for field_name, actual, expected in (
                ("chunk_id", candidate.chunk_id, expected_line.chunk_id),
                ("source_page", candidate.source_page, expected_line.source_page),
                (
                    "source_text_sha256",
                    source_text_sha256,
                    expected_line.source_text_sha256,
                ),
            ):
                if actual != expected:
                    issues.append(
                        _issue(
                            ControlFailureCode.CANDIDATE_SOURCE_MISMATCH,
                            candidate=candidate,
                            field_name=field_name,
                        )
                    )
        issues.extend(_candidate_field_issues(candidate))
        try:
            resolutions.append(
                CandidateResolutionStatus(candidate.resolution_status)
            )
        except (TypeError, ValueError):
            resolutions.append(None)

    accepted_resolutions = {
        CandidateResolutionStatus.ACCEPTED,
        CandidateResolutionStatus.CORRECTED,
    }
    accepted_candidates = [
        candidate
        for candidate, resolution in zip(frozen_candidates, resolutions, strict=True)
        if resolution in accepted_resolutions
    ]
    accepted_count = len(accepted_candidates)
    rejected_count = sum(
        resolution is CandidateResolutionStatus.REJECTED
        for resolution in resolutions
    )

    if opening_balance is None:
        issues.append(_issue(ControlFailureCode.MISSING_OPENING_BALANCE))
    elif not _is_valid_money(opening_balance):
        issues.append(
            _issue(ControlFailureCode.BALANCE_INVALID, field_name="opening_balance")
        )
    if closing_balance is None:
        issues.append(_issue(ControlFailureCode.MISSING_CLOSING_BALANCE))
    elif not _is_valid_money(closing_balance):
        issues.append(
            _issue(ControlFailureCode.BALANCE_INVALID, field_name="closing_balance")
        )

    structural_codes = {
        ControlFailureCode.SOURCE_ORDINAL_DUPLICATE,
        ControlFailureCode.SOURCE_COVERAGE_MISMATCH,
        ControlFailureCode.CANDIDATE_ID_DUPLICATE,
        ControlFailureCode.CANDIDATE_IDENTITY_MISMATCH,
        ControlFailureCode.CANDIDATE_SOURCE_MISMATCH,
        ControlFailureCode.CANDIDATE_FIELDS_MISSING,
        ControlFailureCode.CANDIDATE_AMOUNT_INVALID,
        ControlFailureCode.CANDIDATE_TXN_TYPE_MISMATCH,
        ControlFailureCode.CANDIDATE_RESOLUTION_AUTHORITY_INVALID,
        ControlFailureCode.BALANCE_INVALID,
    }
    structural_failure = any(issue.code in structural_codes for issue in issues)
    unresolved = any(
        issue.code is ControlFailureCode.UNRESOLVED_CANDIDATE for issue in issues
    )

    signed_activity: Decimal | None = None
    residual: Decimal | None = None
    if not structural_failure and not unresolved:
        signed_activity = sum(
            (candidate.signed_amount for candidate in accepted_candidates),
            Decimal("0.00"),
        )
        if opening_balance is not None and closing_balance is not None:
            residual = opening_balance + signed_activity - closing_balance
            if abs(residual) > CONTROL_TOLERANCE:
                issues.append(_issue(ControlFailureCode.BALANCE_MISMATCH))

    pending_codes = {
        ControlFailureCode.MANIFEST_INCOMPLETE,
        ControlFailureCode.UNRESOLVED_CANDIDATE,
        ControlFailureCode.MISSING_OPENING_BALANCE,
        ControlFailureCode.MISSING_CLOSING_BALANCE,
    }
    if structural_failure:
        status = ControlCertificateStatus.FAILED
    elif any(issue.code in pending_codes for issue in issues):
        status = ControlCertificateStatus.PENDING
    elif any(issue.code is ControlFailureCode.BALANCE_MISMATCH for issue in issues):
        status = ControlCertificateStatus.FAILED
    else:
        status = ControlCertificateStatus.PASSED

    return ControlCertificate(
        identity=identity,
        statement_import_id=statement_import_id,
        realm_id=realm_id,
        certificate_version=certificate_version,
        status=status,
        expected_count=len(manifest.lines),
        observed_count=len(frozen_candidates),
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        candidate_set_sha256=candidate_set_sha256(frozen_candidates),
        opening_balance=opening_balance,
        signed_activity=signed_activity,
        closing_balance=closing_balance,
        residual=residual,
        issues=tuple(issues),
    )


__all__ = [
    "calculate_control_certificate",
    "candidate_set_sha256",
    "resolution_audit_binding_sha256",
]

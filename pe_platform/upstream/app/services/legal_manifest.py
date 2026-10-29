"""Verify-on-ingest manifest mirror (SPEC §6.9 + §7.3, plan 20260720 Wave 5, W5-a).

The single mirror table ``legal_document_versions`` is the ONE row every acceptance
flow reads (SPEC §6.9 step 3). Nothing downstream re-renders Markdown or re-fetches the
R2 snapshot for a version comparison — it trusts this table. So a version's bytes are
NEVER trusted on the manifest's say-so: this service re-hashes the served snapshot on
ingest and refuses to mirror a row whose bytes disagree with the manifest.

``verify_and_ingest`` is the whole contract:

  fetch(snapshot_uri) -> re-hash SHA-256 + byte length -> assert == manifest(sha256,
  byte_length) -> on MATCH upsert exactly ONE row (keyed on document_type+version);
  on MISMATCH do NOT upsert, RETAIN the prior version, emit a structured alert, and
  return a rejected result.

The fetch is an INJECTED callable (``fetch(uri) -> bytes``) so tests mock the HTTP
boundary — the re-hash/verify/upsert logic runs for real against fake served bytes. The
default fetch (``_default_fetch``) is the only place ``requests`` is touched, and it is
called lazily (never at import) so no network I/O fires on ``import``.

Idempotency: the upsert is keyed on the immutable ``(document_type, version)`` identity
(``uq_doc_version``), so re-ingesting the same verified entry re-verifies and updates the
same single row — it never inserts a duplicate. This is what makes the driving Celery task
(``app/tasks/legal_ingest.py``) safe under at-least-once redelivery.

NEVER logs the fetched document bytes (or any PII) — only the safe scalar evidence
(document_type, version, the two hex digests, the two byte lengths) reaches the alert.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from app.models import LegalDocumentVersion

logger = logging.getLogger(__name__)

# A fetch callable: takes the snapshot URI, returns the served bytes. Injected at the
# call site so the HTTP boundary is mockable; the default below is the only requests use.
FetchFn = Callable[[str], bytes]

# The upsert business key: a document version's canonical identity is (document_type,
# version) — the immutable ``uq_doc_version`` constraint. The manifest entry carries no
# row ``id``; version_ordinal and sha256 are alternate keys 1:1 with the version. So the
# mirror is keyed on (document_type, version): a re-ingest of the same version updates the
# one existing row (idempotent), a genuinely new version inserts a fresh row.


@dataclass(frozen=True)
class IngestResult:
    """Outcome of verifying + mirroring one manifest version entry.

    ``status`` is ``"ingested"`` (bytes verified, one row upserted) or ``"rejected"``
    (re-hash disagreed with the manifest — prior version retained, nothing written).
    """

    status: str  # "ingested" | "rejected"
    document_type: str
    version: str
    expected_sha256: str
    actual_sha256: str
    expected_byte_length: int
    actual_byte_length: int
    row_id: str | None = None  # the mirrored row's id on ingest; None on reject
    reason: str | None = None  # "sha256_mismatch" | "byte_length_mismatch" on reject

    @property
    def verified(self) -> bool:
        return self.status == "ingested"


def _default_fetch(uri: str) -> bytes:
    """Fetch the served snapshot bytes over HTTP (the production fetch boundary).

    Isolated here (the sole ``requests`` use) and called lazily so the import of this
    module does no network I/O — tests inject their own ``fetch`` and never reach this.
    The clickwrap fetches the SAME CDN path (SPEC §6.9 step 2), so re-hashing what this
    returns is re-hashing what a user is shown.
    """
    import requests  # local import: no network/SDK I/O at module import time

    resp = requests.get(uri, timeout=30)
    resp.raise_for_status()
    return resp.content


def _parse_dt(value: object) -> datetime:
    """Parse a manifest ISO-8601 timestamp (``...Z`` or offset) into a ``datetime``."""
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _parse_date(value: object) -> date:
    """Parse a manifest ISO date (``YYYY-MM-DD``) into a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _upsert_version(
    db,
    entry: dict,
    *,
    verified_at: datetime,
    verified_byte_length: int,
) -> LegalDocumentVersion:
    """Upsert exactly one ``legal_document_versions`` row keyed on (document_type, version).

    A matching row is updated in place (idempotent re-ingest); otherwise a new row is
    inserted with a fresh ``id``. ``content_media_type`` is left to its server default
    (``'text/html'``) when the manifest omits it. Flushes so the row ``id`` is populated
    before the caller commits.
    """
    document_type = entry["document_type"]
    version = entry["version"]

    existing = (
        # TENANT-OK: legal_document_versions is the GLOBAL manifest mirror (SPEC §7.3) — it carries no org column; one required-version set is shared across all tenants
        db.query(LegalDocumentVersion)
        .filter(
            LegalDocumentVersion.document_type == document_type,
            LegalDocumentVersion.version == version,
        )
        .first()
    )

    stable_url = entry["stable_url"] if "stable_url" in entry else entry.get("archive_url")
    fields: dict[str, object] = {
        "document_type": document_type,
        "version": version,
        "version_ordinal": int(entry["version_ordinal"]),
        "effective_date": _parse_date(entry["effective_date"]),
        "sha256": entry["sha256"],
        "snapshot_uri": entry["snapshot_uri"],
        "is_material": bool(entry["is_material"]),
        "stable_url": stable_url,
        "redline_url": entry.get("redline_url"),
        "changelog_summary": entry.get("changelog_summary"),
        # The verified length equals the manifest length here (a mismatch never reaches
        # this function) — store the length we actually re-measured on the served bytes.
        "byte_length": verified_byte_length,
        "verified_at": verified_at,  # the re-hash-OK time (server-stamped, not in manifest)
        "manifest_generated_at": _parse_dt(entry["manifest_generated_at"]),
    }
    if entry.get("published_at"):
        fields["published_at"] = _parse_dt(entry["published_at"])
    if entry.get("content_media_type"):
        fields["content_media_type"] = entry["content_media_type"]

    if existing is None:
        row = LegalDocumentVersion(id=uuid.uuid4(), **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(existing, key, value)
        row = existing

    db.flush()
    return row


def verify_and_ingest(
    db,
    manifest_entry: dict,
    *,
    fetch: FetchFn | None = None,
) -> IngestResult:
    """Verify one manifest version's served bytes and mirror it — the §6.9 verify-on-ingest.

    Fetches ``manifest_entry['snapshot_uri']`` through the injected ``fetch`` (defaults to
    the real HTTP fetch), recomputes SHA-256 + byte length, and requires BOTH to equal the
    manifest's ``sha256`` and ``byte_length``. On match, upserts exactly one
    ``legal_document_versions`` row (keyed on document_type+version) and commits. On
    mismatch, writes NOTHING (the prior mirrored version is retained), emits a structured
    ``log.error`` alert carrying only safe scalar evidence (NEVER the document bytes), and
    returns a rejected ``IngestResult``.
    """
    if fetch is None:
        fetch = _default_fetch

    document_type = manifest_entry["document_type"]
    version = manifest_entry["version"]
    expected_sha256 = manifest_entry["sha256"]
    expected_byte_length = int(manifest_entry["byte_length"])

    body = fetch(manifest_entry["snapshot_uri"])
    actual_sha256 = hashlib.sha256(body).hexdigest()
    actual_byte_length = len(body)

    if actual_sha256 != expected_sha256 or actual_byte_length != expected_byte_length:
        reason = (
            "sha256_mismatch"
            if actual_sha256 != expected_sha256
            else "byte_length_mismatch"
        )
        # Structured alert (SPEC §6.9 reject-and-alert). Only safe scalars — the fetched
        # document bytes are NEVER logged.
        logger.error(
            "legal manifest verify-on-ingest REJECTED (%s): document_type=%s version=%s "
            "expected_sha256=%s actual_sha256=%s expected_byte_length=%s "
            "actual_byte_length=%s — prior version retained; entry NOT ingested",
            reason,
            document_type,
            version,
            expected_sha256,
            actual_sha256,
            expected_byte_length,
            actual_byte_length,
        )
        return IngestResult(
            status="rejected",
            document_type=document_type,
            version=version,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
            expected_byte_length=expected_byte_length,
            actual_byte_length=actual_byte_length,
            row_id=None,
            reason=reason,
        )

    row = _upsert_version(
        db,
        manifest_entry,
        verified_at=datetime.now(timezone.utc),
        verified_byte_length=actual_byte_length,
    )
    row_id = str(row.id)
    db.commit()

    logger.info(
        "legal manifest verify-on-ingest OK: document_type=%s version=%s sha256=%s "
        "byte_length=%s row_id=%s",
        document_type,
        version,
        actual_sha256,
        actual_byte_length,
        row_id,
    )
    return IngestResult(
        status="ingested",
        document_type=document_type,
        version=version,
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
        expected_byte_length=expected_byte_length,
        actual_byte_length=actual_byte_length,
        row_id=row_id,
        reason=None,
    )


def iter_manifest_entries(manifest: dict) -> list[dict]:
    """Flatten a ``manifest.json`` (SPEC §6.8) into per-version ingest entries.

    Walks ``manifest['documents'][<type>]['versions'][*]``, threading the document type
    and the manifest-level ``generated_at`` onto each version dict so every yielded entry
    is a self-contained input to ``verify_and_ingest`` (carries document_type +
    manifest_generated_at + all version fields).
    """
    generated_at = manifest.get("generated_at")
    documents = manifest.get("documents") or {}
    entries: list[dict] = []
    for document_type, doc in documents.items():
        versions = doc["versions"] if "versions" in doc else []
        for version in versions:
            entry = dict(version)
            entry["document_type"] = document_type
            if "manifest_generated_at" not in entry:
                entry["manifest_generated_at"] = generated_at
            entries.append(entry)
    return entries

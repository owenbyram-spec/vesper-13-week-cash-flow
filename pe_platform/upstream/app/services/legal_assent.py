"""Append-only, hash-chained proof-of-assent write service (SPEC §7.4/§7.8/§8.4).

This is the W2-a assent spine. It owns three things:

1. ``current_required`` — the *sole* current-required-version resolver (DEC-F4):
   the ``effective_date <= today`` row with the greatest ``version_ordinal``.
   It NEVER reads an ``is_current_required`` flag (that column was dropped) — the
   effective-date query self-corrects if a Day-0 publish job lags.

2. ``record_assent`` — the idempotent, append-only, per-org hash-chained write.
   One ``assent_records`` row is inserted per acceptance, all sharing one
   ``assent_batch_id``; the ``legal_idempotency`` row is written in the SAME
   transaction (backed by ``UNIQUE(organization_id, idempotency_key)``). A replay
   (same key, same body) returns the winner's stored response; a same-key different
   body raises ``IdempotencyConflict``. For the three published document types
   (``tos``/``privacy``/``dpa``) each acceptance's echoed ``version``+``sha256`` is
   asserted against ``current_required`` (drift → ``VersionMismatch`` /
   ``Sha256Mismatch`` → 409). The manifest lookup + snapshot stamping is SKIPPED for
   ``entity_attestation`` and ``aup_ack`` (DEC-F1 — they have no published, hashed
   doc), leaving their document snapshot columns NULL.

The per-org tamper-evidence hash chain is computed in Python (DEC-enum/trigger:
house style has zero ``CREATE TRIGGER``/``FUNCTION``) inside the transaction, under a
Postgres ``pg_advisory_xact_lock`` that no-ops on SQLite (the chain still computes;
the advisory lock is only a Postgres concurrency guard). The hash payload field order
is FIXED and documented in ``_HASH_FIELD_ORDER`` / ``_row_hash`` below — it mirrors
SPEC §7.8's ``concat_ws(chr(31), ...)`` field list exactly.

Server-stamps ``member_*`` / ``ip_address`` / ``user_agent`` / ``session_id`` /
``accepted_at`` — never trusts the client for those (SPEC §5.6 trust boundary).
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AssentRecord, LegalDocumentVersion, LegalIdempotency

# Document types that have a published, hashed manifest snapshot. The other two
# accepted types — entity_attestation (a version-less firm warranty) and aup_ack
# (a per-member acknowledgment) — have NO manifest doc (DEC-F1), so the version/sha
# echo check and the document-snapshot stamping are skipped for them.
_MANIFEST_DOCUMENT_TYPES = frozenset({"tos", "privacy", "dpa"})

# ASCII Unit Separator (chr(31)) — the SPEC §7.8 concat_ws separator. Chosen because
# it cannot appear in any of the concatenated text fields, so the payload is
# unambiguous (no field value can forge a separator).
_UNIT_SEPARATOR = "\x1f"

# The FIXED per-org hash-chain payload field order, mirroring SPEC §7.8's
# concat_ws(chr(31), ...) list verbatim (23 fields; prev_row_hash appended last by
# ``_row_hash``). Documented here so the order is a stated contract, not an
# implementation accident.
_HASH_FIELD_ORDER = (
    "assent_id",
    "member_id",
    "member_email",
    "organization_id",
    "entity_legal_name",
    "connection_intent_id",
    "realm_id",
    "authority_attested",
    "document_type",
    "document_version",
    "document_effective_date",
    "document_sha256",
    "document_snapshot_uri",
    "acceptance_statement_text",
    "outcome",
    "assent_mechanism",
    "onboarding_context",
    "presented_at",
    "accepted_at",
    "ip_address",
    "user_agent",
    "session_id",
)


class VersionMismatch(Exception):
    """The client echoed a ``version`` that is not the current-required version.

    Maps to ``409 version_mismatch``; carries the current-required version so the
    client can self-heal (SPEC §8.4).
    """

    def __init__(self, document_type: str, current_version: str | None) -> None:
        self.document_type = document_type
        self.current_version = current_version
        super().__init__(
            f"version_mismatch for {document_type}: current_required={current_version}"
        )


class Sha256Mismatch(Exception):
    """The client echoed a ``sha256`` that is not the current-required sha256.

    Maps to ``409 sha256_mismatch``; carries the current-required sha256.
    """

    def __init__(self, document_type: str, current_sha256: str | None) -> None:
        self.document_type = document_type
        self.current_sha256 = current_sha256
        super().__init__(
            f"sha256_mismatch for {document_type}: current_required={current_sha256}"
        )


class IdempotencyConflict(Exception):
    """Same ``Idempotency-Key`` re-used with a DIFFERENT request body.

    Maps to ``409 idempotency_key_conflict`` (SPEC §8.4).
    """

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"idempotency_key_conflict for key={idempotency_key}")


def current_required(db: Session, document_type: str) -> LegalDocumentVersion | None:
    """Resolve the current-required version for ``document_type`` (DEC-F4, SPEC §7.3).

    The SOLE resolver: the row whose ``effective_date`` is on-or-before today with the
    greatest ``version_ordinal``. Never reads ``is_current_required`` (dropped) — a
    version published in advance does not become required before its effective date,
    and the effective-date query self-corrects a lagging Day-0 publish job.
    """
    today = datetime.now(timezone.utc).date()
    return (
        # TENANT-OK: legal_document_versions is the GLOBAL manifest mirror (SPEC §7.3) — it carries no org column; one required-version set is shared across all tenants
        db.query(LegalDocumentVersion)
        .filter(
            LegalDocumentVersion.document_type == document_type,
            LegalDocumentVersion.effective_date <= today,
        )
        .order_by(LegalDocumentVersion.version_ordinal.desc())
        .first()
    )


def _acquire_org_chain_lock(db: Session, organization_id: str) -> None:
    """Serialize concurrent inserts for one org so the hash chain cannot fork.

    Postgres-only: ``pg_advisory_xact_lock`` is a transaction-scoped advisory lock
    (auto-released at commit/rollback). On SQLite it is a clean no-op — SQLite
    serializes writers itself, and the chain still computes correctly (the lock is a
    concurrency guard, not a correctness input).
    """
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:org, 0))"),
        {"org": organization_id},
    )


def _serialize_hash_field(value: object) -> str:
    """Deterministically serialize one hash-payload field.

    Documented, fixed rules (the Python service is the SOLE chain authority — there is
    no DB trigger, DEC-enum/trigger): ``None`` -> ``""`` (matches the SPEC's
    ``coalesce(x,'')``); ``bool`` -> ``"true"``/``"false"`` (lowercase, matching
    Postgres ``::text``); ``datetime`` -> a **round-trip-stable** UTC-naive ISO 8601
    string; ``date`` -> ``.isoformat()``; everything else -> ``str(value)`` (UUIDs
    render lowercase-hyphenated). ``value`` is ``object`` (not ``Any``) so
    type-checking stays on: each branch narrows before use.

    The datetime rule is load-bearing for verifiability: the stored ``accepted_at``
    must serialize to the SAME string whether read as the tz-aware write-time value or
    the value read back from the DB (SQLite drops tzinfo; drivers may return a
    different tz). A tz-aware value is normalized to UTC then made naive; a naive value
    (always stored UTC) is used as-is. Both paths yield one canonical UTC-naive ISO
    string, so a later chain re-verification recomputes an identical hash.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc) if value.tzinfo is not None else value
        return normalized.replace(tzinfo=None).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _row_hash(row: AssentRecord, prev_row_hash: str | None) -> str:
    """SHA-256 the FIXED-order payload of ``row``'s fields + ``prev_row_hash`` last.

    Reads the 23 fields in ``_HASH_FIELD_ORDER`` off the fully-populated row, appends
    ``prev_row_hash`` (field 24), joins with the Unit Separator, and hexdigests. All
    hashed attributes must already be set on ``row``; ``seq`` is intentionally NOT a
    hashed field (it is an ordering key, not evidence — SPEC §7.8).
    """
    parts = [_serialize_hash_field(getattr(row, name)) for name in _HASH_FIELD_ORDER]
    parts.append(_serialize_hash_field(prev_row_hash))
    payload = _UNIT_SEPARATOR.join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_outcome(document_type: str) -> str:
    if document_type == "entity_attestation":
        return "warranted"
    if document_type == "aup_ack":
        return "acknowledged"
    return "accepted"


def _build_assent_row(
    db: Session,
    *,
    acceptance: dict,
    organization_id: str,
    member_id: str,
    member_email: str,
    idempotency_key: str,
    assent_batch_id: uuid.UUID,
    assent_mechanism: str,
    ip_address: str | None,
    user_agent: str | None,
    session_id: str,
    accepted_at: datetime,
) -> AssentRecord:
    """Construct one un-hashed, un-sequenced ``AssentRecord`` for an acceptance.

    Resolves + verifies the manifest snapshot for the three published document types
    (drift -> VersionMismatch/Sha256Mismatch); leaves the snapshot columns NULL for
    entity_attestation/aup_ack (DEC-F1 — no ``current_required`` lookup). ``seq`` /
    ``prev_row_hash`` / ``row_hash`` are assigned by the caller once the per-org chain
    position is known.
    """
    document_type = acceptance["document_type"]

    document_version_id = None
    document_version = None
    document_effective_date = None
    document_sha256 = None
    document_snapshot_uri = None

    if document_type in _MANIFEST_DOCUMENT_TYPES:
        current = current_required(db, document_type)
        echoed_version = acceptance.get("version")
        echoed_sha256 = acceptance.get("sha256")
        if current is None or echoed_version != current.version:
            raise VersionMismatch(
                document_type, current.version if current else None
            )
        if echoed_sha256 != current.sha256:
            raise Sha256Mismatch(document_type, current.sha256)
        document_version_id = current.id
        document_version = current.version
        document_effective_date = current.effective_date
        document_sha256 = current.sha256
        document_snapshot_uri = current.snapshot_uri

    outcome = acceptance.get("outcome") or _default_outcome(document_type)
    authority_attested = acceptance.get("authority_attested")
    if authority_attested is None:
        authority_attested = document_type != "aup_ack"

    connection_intent_id = acceptance.get("connection_intent_id")
    if isinstance(connection_intent_id, str):
        connection_intent_id = uuid.UUID(connection_intent_id)

    return AssentRecord(
        assent_id=uuid.uuid4(),
        assent_batch_id=assent_batch_id,
        idempotency_key=idempotency_key,
        member_id=member_id,
        member_email=member_email,
        organization_id=organization_id,
        entity_legal_name=acceptance.get("entity_legal_name") or "",
        entity_legal_name_source=acceptance.get("entity_legal_name_source"),
        connection_intent_id=connection_intent_id,
        realm_id=acceptance.get("realm_id"),
        authority_attested=authority_attested,
        business_status_evidence=acceptance.get("business_status_evidence") or {},
        document_version_id=document_version_id,
        document_type=document_type,
        document_version=document_version,
        document_effective_date=document_effective_date,
        document_sha256=document_sha256,
        document_snapshot_uri=document_snapshot_uri,
        acceptance_statement_text=acceptance.get("acceptance_statement_text") or "",
        outcome=outcome,
        assent_mechanism=assent_mechanism,
        onboarding_context=acceptance.get("onboarding_context") or "org_creation",
        presented_at=None,
        presented_at_client=None,
        accepted_at=accepted_at,
        ip_address=ip_address,
        ip_unavailable=ip_address is None,
        user_agent=user_agent,
        session_id=session_id,
        # created_at is stamped explicitly (not left to the Postgres now() server
        # default) so the same INSERT runs on the SQLite test lane, which has no now().
        created_at=accepted_at,
    )


def record_assent(
    db: Session,
    *,
    organization_id: str,
    member_id: str,
    member_email: str,
    acceptances: list[dict],
    idempotency_key: str,
    request_body_sha256: str,
    ip_address: str | None,
    user_agent: str | None,
    session_id: str,
    assent_mechanism: str,
) -> dict:
    """Append-only, idempotent, hash-chained multi-doc assent write (SPEC §8.4).

    Returns ``{"assent_batch_id", "rows": [...], "replayed": bool}``. On a replay
    (same key, same body) the winner's stored response is returned with
    ``replayed=True``; a same-key different-body raises ``IdempotencyConflict``. Each
    published-doc acceptance's echoed version/sha is verified against
    ``current_required`` (drift -> Version/Sha256Mismatch). One transaction: the assent
    rows AND the legal_idempotency row commit together, or neither does.
    """
    # Fast-path replay / conflict detection before doing any work.
    existing = (
        db.query(LegalIdempotency)
        .filter(
            LegalIdempotency.organization_id == organization_id,
            LegalIdempotency.idempotency_key == idempotency_key,
        )
        .first()
    )
    if existing is not None:
        if existing.request_body_sha256 == request_body_sha256:
            return {**existing.response_json, "replayed": True}
        raise IdempotencyConflict(idempotency_key)

    # Postgres advisory lock (no-op on SQLite) serializes same-org inserts so the
    # per-org hash chain cannot fork under concurrency.
    _acquire_org_chain_lock(db, organization_id)

    assent_batch_id = uuid.uuid4()
    accepted_at = datetime.now(timezone.utc)

    # Chain seed: the org's current tail (greatest seq) supplies prev_row_hash and the
    # next seq. Read once, then advance in-Python across the batch.
    tail = (
        db.query(AssentRecord)
        .filter(AssentRecord.organization_id == organization_id)
        .order_by(AssentRecord.seq.desc())
        .first()
    )
    prev_row_hash = tail.row_hash if tail is not None else None
    next_seq = (tail.seq if tail is not None else 0) + 1

    rows: list[AssentRecord] = []
    response_rows: list[dict[str, object]] = []
    for acceptance in acceptances:
        row = _build_assent_row(
            db,
            acceptance=acceptance,
            organization_id=organization_id,
            member_id=member_id,
            member_email=member_email,
            idempotency_key=idempotency_key,
            assent_batch_id=assent_batch_id,
            assent_mechanism=assent_mechanism,
            ip_address=ip_address,
            user_agent=user_agent,
            session_id=session_id,
            accepted_at=accepted_at,
        )
        row.seq = next_seq
        row.prev_row_hash = prev_row_hash
        row.row_hash = _row_hash(row, prev_row_hash)

        prev_row_hash = row.row_hash
        next_seq += 1
        rows.append(row)
        response_rows.append(
            {
                "assent_id": str(row.assent_id),
                "document_type": row.document_type,
                "outcome": row.outcome,
                "version": row.document_version,
                "seq": row.seq,
                "prev_row_hash": row.prev_row_hash,
                "row_hash": row.row_hash,
            }
        )

    response = {"assent_batch_id": str(assent_batch_id), "rows": response_rows}

    idem = LegalIdempotency(
        organization_id=organization_id,
        idempotency_key=idempotency_key,
        request_body_sha256=request_body_sha256,
        response_json=response,
        # Explicit created_at — the Postgres now() server default is not a SQLite
        # function, so the same INSERT must supply it on the test lane.
        created_at=accepted_at,
    )

    db.add_all(rows)
    db.add(idem)
    try:
        db.commit()
    except IntegrityError as exc:
        # A concurrent request claimed the same key between the fast-path read and this
        # commit. Roll back and return the winner's committed response (same body) or
        # raise a conflict (different body). The UNIQUE(org, idempotency_key) PK and the
        # assent uq_assent_idem constraint both serialize the race.
        db.rollback()
        winner = (
            db.query(LegalIdempotency)
            .filter(
                LegalIdempotency.organization_id == organization_id,
                LegalIdempotency.idempotency_key == idempotency_key,
            )
            .first()
        )
        if winner is not None and winner.request_body_sha256 == request_body_sha256:
            return {**winner.response_json, "replayed": True}
        raise IdempotencyConflict(idempotency_key) from exc

    return {**response, "replayed": False}

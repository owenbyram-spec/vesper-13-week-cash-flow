# Plan 20260712-agentic-finance-os W5 (GAP-5a) — scoped, versioned agent memory.
"""Enforce the directive's memory rules (§10) over the CompanyKnowledge table.

The load-bearing invariants this service guarantees:
  * Model output alone is NEVER authoritative — an inferred/model/document memory
    is tentative (accepted_by is None) until a user explicitly accepts it.
  * Facts retain provenance — source + evidence travel on the row.
  * Contradictions SUPERSEDE, never silently merge — a new version stamps the old
    row's superseded_by_id; the old row is preserved (history is traceable).
  * A user correction creates a NEW version, never rewrites the original.
  * Retrieval honors scope + expiry + supersession (and the caller's company).

Writes flush + commit within the caller's already-tenant-scoped transaction.
Tenant identity comes from the caller's authorized scope, never from model output.
"""
from __future__ import annotations

# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker RATIFICATION-2026-07-13.md
# on disk; active-plan.json sidecar absent in this worktree) — Memory-v2 live write path (AC-4).
from datetime import date, datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import CompanyKnowledge

# Sources whose memory is authoritative on write (a human taught/entered it).
_HUMAN_SOURCES = frozenset({"manual", "user"})
_VALID_SCOPES = frozenset({"company", "team", "personal", "workflow"})
_VALID_MEMORY_TYPES = frozenset({"factual", "preferential", "procedural"})


def is_authoritative(record: CompanyKnowledge) -> bool:
    """A memory may drive an answer as fact ONLY if a human authored it OR a user
    explicitly accepted it. A raw model/inferred memory is never authoritative."""
    if record.accepted_by:
        return True
    return (record.source or "").lower() in _HUMAN_SOURCES


def remember(
    db: Session,
    *,
    company_id: str,
    category: str,
    content: str,
    scope: str = "company",
    memory_type: str = "factual",
    source: str = "manual",
    owner_ref: str | None = None,
    confidence: str = "high",
    effective_from: date | None = None,
    expires_at: datetime | None = None,
    provenance: dict | None = None,  # CRAFT-OK: {source_type, evidence:[...], correction_of:id} — free-form provenance JSON
    sensitivity: str | None = None,
    organization_id: int | None = None,
    organization_company_id: int | None = None,
    supersedes: CompanyKnowledge | None = None,
    accepted_by: str | None = None,
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
    embedding: list[float] | None = None,
) -> CompanyKnowledge:
    """Persist a memory. If ``supersedes`` is given, this is a NEW version that
    stamps the old row superseded (contradiction handling); the old row is kept.
    A model/inferred/document memory stays tentative (accepted_by None) unless the
    caller passes an explicit ``accepted_by`` — a human source is authoritative.

    ``embedding`` is an optional PRECOMPUTED vector (pinned text-embedding-3-small,
    1536-d) so callers that already embedded the content (the teach / inferred lanes)
    write it in the same transaction. Null-guarded (AP-VEC-8): a ``None`` embedding
    leaves the column SQL NULL for the re-embed backfill — never a persisted None."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"invalid memory scope '{scope}' — expected {sorted(_VALID_SCOPES)}")
    if memory_type not in _VALID_MEMORY_TYPES:
        raise ValueError(f"invalid memory_type '{memory_type}' — expected {sorted(_VALID_MEMORY_TYPES)}")

    version = (supersedes.version + 1) if supersedes is not None else 1
    # A human-authored memory is accepted-by-construction; an inferred one is not.
    resolved_acceptance = accepted_by
    if resolved_acceptance is None and (source or "").lower() in _HUMAN_SOURCES:
        resolved_acceptance = owner_ref or "system:human-source"

    row = CompanyKnowledge(
        organization_id=organization_id,
        organization_company_id=organization_company_id,
        company_id=company_id,
        category=category,
        content=content,
        confidence=confidence,
        source=source,
        scope=scope,
        owner_ref=owner_ref,
        memory_type=memory_type,
        version=version,
        effective_from=effective_from,
        expires_at=expires_at,
        provenance=provenance,
        sensitivity=sensitivity,
        accepted_by=resolved_acceptance,
        accepted_at=datetime.now(timezone.utc) if resolved_acceptance else None,
    )
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
    # AP-VEC-8 gate: only a REAL vector is ever assigned; None leaves the column SQL NULL
    # (dialect-consistent via none_as_null) so the re-embed backfill finds it later.
    if embedding is not None:
        row.embedding_vector = embedding
    db.add(row)
    db.flush()  # assign row.id so the supersession pointer can reference it
    if supersedes is not None:
        supersedes.superseded_by_id = row.id
    db.commit()
    db.refresh(row)
    return row


def accept(db: Session, record: CompanyKnowledge, *, accepted_by: str) -> CompanyKnowledge:
    """A user explicitly accepts a tentative (inferred) memory → it becomes
    authoritative. Idempotent: keeps the first acceptance."""
    if not record.accepted_by:
        record.accepted_by = accepted_by
        record.accepted_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(record)
    return record


def record_correction(
    db: Session,
    *,
    old_record: CompanyKnowledge,
    corrected_content: str,
    corrected_by: str,
    provenance: dict | None = None,  # CRAFT-OK: free-form provenance JSON (see remember)
) -> CompanyKnowledge:
    """A user correction creates a NEW version that supersedes the old — the
    original is NEVER rewritten (its historical evidence is preserved). The new
    version is user-accepted (authoritative) and records correction_of."""
    merged_provenance = dict(provenance or {})
    merged_provenance["correction_of"] = old_record.id
    return remember(
        db,
        company_id=old_record.company_id,
        category=old_record.category,
        content=corrected_content,
        scope=old_record.scope or "company",
        memory_type=old_record.memory_type or "factual",
        source="user",
        owner_ref=old_record.owner_ref,
        confidence="high",
        provenance=merged_provenance,
        sensitivity=old_record.sensitivity,
        organization_id=old_record.organization_id,
        organization_company_id=old_record.organization_company_id,
        supersedes=old_record,
        accepted_by=corrected_by,
    )


# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
def find_reteach_target(
    db: Session,
    *,
    company_id: str,
    category: str,
    content: str,
    scope: str = "company",
) -> CompanyKnowledge | None:
    """The ACTIVE row a re-teach of ``content`` should supersede, or None.

    A re-teach (same company + category + scope, byte-identical content modulo
    case/whitespace) previously piled up duplicate rows. The teach lanes pass the
    returned row as ``supersedes`` to :func:`remember`, so re-teaching links a
    version chain (v1 → v2) instead of duplicating. Deterministic exact-content
    match only — near-duplicate merging is the consolidation task's job (LG-3).
    """
    normalized = (content or "").strip().lower()
    if not normalized:
        return None
    return (
        db.query(CompanyKnowledge)
        .filter(
            CompanyKnowledge.company_id == company_id,
            CompanyKnowledge.category == category,
            CompanyKnowledge.scope == scope,
            CompanyKnowledge.superseded_by_id.is_(None),
            func.lower(func.trim(CompanyKnowledge.content)) == normalized,
        )
        .order_by(CompanyKnowledge.id.desc())
        .first()
    )


def active_memories(
    db: Session,
    company_id: str,
    *,
    scope: str | None = None,
    owner_ref: str | None = None,
    memory_type: str | None = None,
    include_tentative: bool = True,
    now: datetime | None = None,
) -> list[CompanyKnowledge]:
    """Live memories for a company: NOT superseded, NOT expired, already effective,
    scoped. ``include_tentative=False`` returns only authoritative memories (human
    or accepted) — the set safe to treat as fact."""
    now = now or datetime.now(timezone.utc)
    query = (
        db.query(CompanyKnowledge)
        .filter(
            CompanyKnowledge.company_id == company_id,
            CompanyKnowledge.superseded_by_id.is_(None),
            or_(CompanyKnowledge.expires_at.is_(None), CompanyKnowledge.expires_at > now),
            or_(CompanyKnowledge.effective_from.is_(None), CompanyKnowledge.effective_from <= now.date()),
        )
    )
    if scope is not None:
        query = query.filter(CompanyKnowledge.scope == scope)
    if owner_ref is not None:
        query = query.filter(CompanyKnowledge.owner_ref == owner_ref)
    if memory_type is not None:
        query = query.filter(CompanyKnowledge.memory_type == memory_type)
    rows = query.order_by(CompanyKnowledge.updated_at.desc()).all()
    if not include_tentative:
        rows = [r for r in rows if is_authoritative(r)]
    return rows


def version_chain(db: Session, record: CompanyKnowledge) -> list[CompanyKnowledge]:
    """The full supersession lineage rooted at ``record`` (oldest→newest), so a
    superseded policy stays traceable (directive §10). Loads the candidate rows in
    ONE query, then walks the superseded_by pointers in-memory (no N+1)."""
    candidates = (
        db.query(CompanyKnowledge)
        .filter(
            CompanyKnowledge.company_id == record.company_id,
            CompanyKnowledge.category == record.category,
        )
        .all()
    )
    by_id = {r.id: r for r in candidates}
    by_id[record.id] = record  # ensure the root is indexed even if filters differ
    chain = [record]
    seen = {record.id}
    cursor = record
    while cursor.superseded_by_id is not None and cursor.superseded_by_id not in seen:
        nxt = by_id.get(cursor.superseded_by_id)  # SA-OK: dict-index on the batch loaded above, not a DB query
        if nxt is None:
            break
        chain.append(nxt)
        seen.add(nxt.id)
        cursor = nxt
    return chain

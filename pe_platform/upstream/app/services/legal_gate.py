"""Tier-1 assent-completeness gate (SPEC §8.8 ``assentGate``, AC-4/AC-7 spine).

``assent_gate(db, organization_id)`` is the single reusable guard mounted by Wave-3
(org activation) and Wave-4 (QBO OAuth start). It returns ``ok=True`` iff, for BOTH
``tos`` and ``privacy``, the org holds an ``assent_records`` row with
``outcome='accepted'`` whose accepted ``version_ordinal`` is at least the
current-required ``version_ordinal``.

The required version comes ONLY from the effective-date resolver
``legal_assent.current_required`` (DEC-F4) — never an ``is_current_required`` flag.
The org's accepted ordinal is read by joining ``assent_records`` (org-scoped) to the
global ``legal_document_versions`` mirror via the snapshot FK, so the comparison uses
real ordinals, not the stored label string.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import AssentRecord, LegalDocumentVersion
from app.services.legal_assent import current_required

# The Tier-1 documents whose current-required acceptance gates org activation +
# OAuth start (SPEC §4.7). DPA is Tier-1 too but gates entity-connect per tenant
# scope (DEC-F3), not the base activation gate — so it is not in this set.
_TIER1_DOCUMENT_TYPES = ("tos", "privacy")


def _accepted_max_ordinal(
    db: Session, organization_id: str, document_type: str
) -> int | None:
    """Greatest accepted ``version_ordinal`` the org holds for ``document_type``.

    Joins the org-scoped ``assent_records`` rows (``outcome='accepted'``) to the
    global manifest mirror via ``document_version_id`` and returns the max ordinal, or
    ``None`` if the org has never accepted this document. The ``organization_id``
    predicate on ``assent_records`` is the tenant scope on this read.
    """
    row = (
        db.query(LegalDocumentVersion.version_ordinal)
        .join(AssentRecord, AssentRecord.document_version_id == LegalDocumentVersion.id)
        .filter(
            AssentRecord.organization_id == organization_id,
            AssentRecord.document_type == document_type,
            AssentRecord.outcome == "accepted",
        )
        .order_by(LegalDocumentVersion.version_ordinal.desc())
        .first()
    )
    return row[0] if row is not None else None


def assent_gate(db: Session, organization_id: str) -> dict:
    """Return ``{"ok": bool, "missing": list[str], "reason": str}`` for the org.

    ``ok`` is True iff both ``tos`` and ``privacy`` have a current (accepted ordinal >=
    required ordinal) acceptance. ``missing`` lists the document types blocking the
    gate; ``reason`` is ``"assent_current"`` when ok, ``"no_assent"`` when a missing
    document has never been accepted, else ``"reacceptance_required"`` (an accepted but
    now-stale version).
    """
    missing: list[str] = []
    never_accepted = False

    for document_type in _TIER1_DOCUMENT_TYPES:
        required = current_required(db, document_type)
        accepted_ordinal = _accepted_max_ordinal(db, organization_id, document_type)

        if accepted_ordinal is None:
            # No accepted row at all — a hard miss regardless of whether a required
            # version is even published yet.
            missing.append(document_type)
            never_accepted = True
            continue

        # A required version must exist AND the org must be at-or-above its ordinal.
        if required is None or accepted_ordinal < required.version_ordinal:
            missing.append(document_type)

    if not missing:
        return {"ok": True, "missing": [], "reason": "assent_current"}

    reason = "no_assent" if never_accepted else "reacceptance_required"
    return {"ok": False, "missing": missing, "reason": reason}


def entity_attestation_exists(
    db: Session, organization_id: str, connection_intent_id: str | uuid.UUID
) -> bool:
    """True iff a warranted ``entity_attestation`` exists for (org, connection_intent).

    The Tier-2 evidence check for Gate #2 (QBO OAuth start — SPEC §4.6/§8.8): before
    the OAuth handshake begins, the connecting firm must have written an
    ``entity_attestation`` AssentRecord (``outcome='warranted'``) bound to THIS
    ``connection_intent_id`` via ``POST /qbo/connect-intent``.

    ``organization_id`` is the Stytch string org id — the legal.* tenant key (DEC-F2),
    the SAME value ``record_assent`` stamped; the ``organization_id`` predicate is the
    app-layer tenant scope on this read. A malformed ``connection_intent_id`` can never
    match a real row, so it resolves to ``False`` (the gate then 409s) rather than
    raising a 500 out of the handler.
    """
    if isinstance(connection_intent_id, str):
        try:
            connection_intent_id = uuid.UUID(connection_intent_id)
        except ValueError:
            return False

    row = (
        db.query(AssentRecord.assent_id)
        .filter(
            AssentRecord.organization_id == organization_id,
            AssentRecord.connection_intent_id == connection_intent_id,
            AssentRecord.document_type == "entity_attestation",
            AssentRecord.outcome == "warranted",
        )
        .first()
    )
    return row is not None

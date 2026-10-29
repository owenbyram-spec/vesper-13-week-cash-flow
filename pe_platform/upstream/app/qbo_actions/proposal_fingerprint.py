# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel (marker
# RATIFICATION-2026-07-13.md) — C4 / AC-10 (SH-2): central inputs_fingerprint stamping.
"""Central ``ProposedAction.inputs_fingerprint`` stamping (SH-2, plan decision 3).

``ProposedAction.inputs_fingerprint`` already exists (nullable + indexed,
``models.py:1264``) — this module makes EVERY draft lane populate it without
per-lane edits: an SQLAlchemy ``before_insert`` mapper listener stamps
``sha256(action_type + canonical(payload))`` onto any ProposedAction flushed
with a NULL fingerprint. Lanes that stamp their own fingerprint (the
close-orchestration dedupe, the C1 categorization sweep) are untouched — the
listener only fills NULLs.

Canonicalization (why a retried turn collides with its first draft):
- dict keys sorted; nested dicts/lists normalized recursively
- integral floats collapse to ints (``100.0`` == ``100``); bools preserved
- string whitespace normalized (stripped + internal runs collapsed)
- run/conversation *identity* keys are EXCLUDED (``source_data`` /
  ``source_metadata`` containers and scalar volatile keys) so "the same change
  drafted again by a retried chat turn / a redelivered job" hashes identically
  even though a fresh agent_run_id / conversation_id was minted. Any change to
  amount, account, period, target entity, or tool input changes the hash.

Registration: the ``@event.listens_for`` decorator below runs at import time.
``app/agent/runtime/tool_executor.py`` imports this module (the chat draft
lanes), and any process that loads the agent runtime (web app via the assistant
routers, the Celery worker via the agent/close-orchestration tasks) therefore
carries the listener for ALL ORM ProposedAction inserts in that process —
chat, router, and sweep lanes alike.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from sqlalchemy import event

from ..models import ProposedAction

# How far back the chat-time dedup seam (tool_executor._collapse_duplicate_proposal)
# looks for an older PENDING proposal with the same fingerprint. Bounded so an
# ancient PENDING row (pre-expiry-sweep legacy) can never swallow a fresh draft
# forever; chat proposals expire at +7d, so 24h comfortably covers a retried turn
# and a same-day re-ask while staying well inside the proposal lifetime.
PROPOSAL_DEDUP_WINDOW_HOURS = 24

# Keys that identify WHICH run/conversation/user-turn drafted a proposal rather
# than WHAT the proposal does. Excluded from the canonical form so semantically
# identical drafts collide regardless of the run that produced them.
VOLATILE_PAYLOAD_KEYS = frozenset(
    {
        "source_data",
        "source_metadata",
        "conversation_id",
        "agent_run_id",
        "run_id",
        "request_id",
        "timestamp",
        "ts",
        "nonce",
        "created_at",
    }
)


def _canonicalize(value: object) -> object:
    """Recursively normalize a JSON-ish value (sorted keys, volatile keys dropped,
    integral floats -> ints, whitespace-normalized strings, bools preserved)."""
    if isinstance(value, Mapping):
        return {
            str(k): _canonicalize(value[k])
            for k in sorted(value, key=str)
            if str(k) not in VOLATILE_PAYLOAD_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def canonical_payload(payload: object) -> str:
    """Deterministic JSON serialization of a normalized proposal payload."""
    return json.dumps(_canonicalize(payload if payload is not None else {}), sort_keys=True, separators=(",", ":"), default=str)


def compute_proposal_fingerprint(action_type: str | None, payload: object) -> str:
    """SHA-256 over the action type + canonicalized payload.

    The action type is folded into the hash, so two proposals with identical
    payloads but different action types can NEVER share a fingerprint — dedup
    is structurally incapable of collapsing across action types.
    """
    material = f"{action_type or ''}\n{canonical_payload(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@event.listens_for(ProposedAction, "before_insert")
def _stamp_inputs_fingerprint(mapper, connection, target) -> None:  # noqa: ARG001 — SQLAlchemy listener signature
    """Fill ``inputs_fingerprint`` where NULL on every ORM ProposedAction insert.

    Where-NULL only: a lane that computes its own fingerprint (close-orchestration's
    ``uq_proposed_action_closetask_fingerprint`` dedupe, the C1 sweep) keeps its value.
    """
    if target.inputs_fingerprint is None:
        target.inputs_fingerprint = compute_proposal_fingerprint(target.action_type, target.payload)

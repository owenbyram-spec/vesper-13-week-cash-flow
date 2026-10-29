"""Field-level diff for proposal review (C2 — richer proposal diffs).

Computes a generic, one-level before→after diff over a ProposedAction's review
contract so the review UI can show "this field: A → B" instead of two raw JSON
blobs. Presentation only — no proposal / approval / execution behavior depends on
it; the caller redacts the result before it leaves the serializer.

Before/after values are arbitrary proposal JSON, so they are typed ``object`` (the
diff only compares + carries them through; it never assumes a shape).
"""
from __future__ import annotations

from typing import TypedDict

# Keys that ``_review_contract`` surfaces as its OWN contract fields (summary,
# reason, risk, provenance, …). When a proposer stuffs them into the payload they
# would duplicate-and-noise the diff, so they are stripped before the field walk.
# The diff is about the proposal's DATA, not the serializer's metadata.
METADATA_KEYS: frozenset[str] = frozenset(
    {
        "summary",
        "originating_module",
        "originating_agent_run_id",
        "originating_agent_step_id",
        "requester",
        "affected_entity",
        "reason",
        "source_data",
        "risk_level",
        "required_role",
        "approval_effect",
        "execution_mode",
        "confirmation_fingerprint",
    }
)


class DiffRow(TypedDict):
    field: str
    label: str
    before: object
    after: object
    change: str


def humanize_label(field: str) -> str:
    """A snake_case field name → a Title Case label (``from_account_id`` → ``From Account Id``)."""
    return field.replace("_", " ").strip().title() or field


def _as_dict(value: object) -> dict[str, object]:
    """Coerce a before/after value to a dict for the one-level walk. A non-dict
    top-level value (None / scalar / list) yields ``{}`` → no field-level diff, so
    the caller falls back to the raw render. Never raises."""
    return value if isinstance(value, dict) else {}


def compute_diff(before: object, after: object) -> list[DiffRow]:
    """Generic one-level field diff.

    For each non-metadata key present in ``before`` and/or ``after``, emit a
    ``DiffRow`` with ``change`` in ``added | removed | changed | unchanged``
    (after-only → added, before-only → removed, both-and-equal → unchanged,
    both-and-different → changed). Dict/array values are carried raw (the renderer
    truncates them). Returns ``[]`` for a non-dict before/after pair.
    """
    before_d = _as_dict(before)
    after_d = _as_dict(after)

    fields = (set(before_d) | set(after_d)) - METADATA_KEYS
    rows: list[DiffRow] = []
    for field in sorted(fields):
        in_before = field in before_d
        in_after = field in after_d
        b = before_d.get(field)  # SA-OK: in-memory dict.get over before-state, not a DB query (FP)
        a = after_d.get(field)  # SA-OK: in-memory dict.get over after-state, not a DB query (FP)
        if in_before and not in_after:
            change = "removed"
        elif in_after and not in_before:
            change = "added"
        elif b == a:
            change = "unchanged"
        else:
            change = "changed"
        rows.append(
            {"field": field, "label": humanize_label(field), "before": b, "after": a, "change": change}
        )
    return rows

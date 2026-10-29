"""Server-owned proposal provenance and revision-bound financial approval.

Source labels, payload metadata and email text never establish human authorship.
Revision is independent from the deliberately lossy proposal dedup fingerprint.
"""
from __future__ import annotations

from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models import ProposedAction


def _refuse(code: str, message: str) -> None:
    raise HTTPException(status_code=409, detail={"error_code": code, "user_message": message, "retryable": False})


def human_provenance(user_id: int | None) -> dict:
    """Only authenticated route/service arguments may supply this identity."""
    if user_id is not None and (type(user_id) is not int or user_id <= 0):
        raise ValueError("human preparer must be a positive user id")
    return {"human_preparer_user_id": user_id, "provenance_version": 1}


def is_material_action(action: ProposedAction) -> bool:
    from .action_tiers import SideEffectTier, tier_for_action_type
    return tier_for_action_type(action.action_type) in {
        SideEffectTier.EXTERNAL_FINANCIAL, SideEffectTier.INTERNAL_MATERIAL,
    }


def validate_material_execution(action: ProposedAction) -> None:
    if is_material_action(action) and action.provenance_version != 1:
        _refuse("proposal_provenance_unknown", "This proposal has unknown preparation history. Draft a fresh proposal for review.")


def validate_approval(action: ProposedAction, *, approver_user_id: int | None, expected_revision: int | None) -> None:
    validate_material_execution(action)
    actors = {action.human_preparer_user_id, action.last_human_editor_user_id} - {None}
    if actors and approver_user_id is None:
        _refuse("approver_identity_required", "A known second human approver is required for this proposal.")
    if approver_user_id in actors:
        _refuse("self_approval_blocked", "The preparer or latest editor cannot approve their own action; a second approver is required.")
    if is_material_action(action) and (type(expected_revision) is not int or expected_revision <= 0):
        _refuse("proposal_revision_required", "Review the current proposal and submit its revision before approving.")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision != action.revision):
        _refuse("proposal_revision_conflict", "This proposal changed after your review. Refresh and review the new revision.")


def validate_autonomous_approval(action: ProposedAction) -> None:
    if action.provenance_version != 1:
        _refuse("proposal_provenance_unknown", "Unknown preparation history cannot be automatically approved.")
    if action.human_preparer_user_id is not None or action.last_human_editor_user_id is not None:
        _refuse("human_review_required", "Human-prepared or edited proposals require a separate human approver.")


def _review_payload(action: ProposedAction, payload: dict) -> dict:
    result = deepcopy(payload)
    if action.action_type in {"POST_JE", "JOURNAL_ENTRY"}:
        # Names added by the response serializer do not change the posted account.
        result.setdefault("memo", "")
        for line in result.get("lines", []):
            if isinstance(line, dict):
                line.pop("account_name", None)
                for key in ("vendor_name", "description"):
                    line[key] = line.get(key) or ""
                for key in ("debit", "credit"):
                    line[key] = line.get(key) or 0
    return result


def edit_proposal_payload(db: Session, action: ProposedAction, payload: dict, *, editor_user_id: int, expected_revision: int | None = None, reasoning: str | None = None) -> bool:
    """CAS a pending substantive edit; return False for a semantic no-op.

    Incrementing revision invalidates all older email/confirmation tokens without
    rewriting their historical rows. The caller owns commit and audit capture.
    """
    if action.status != "PENDING":
        _refuse("proposal_edit_bad_status", "Only PENDING proposals can be edited. Draft a correction or reversal for a posted entry.")
    validate_material_execution(action)
    if expected_revision is not None and expected_revision != action.revision:
        _refuse("proposal_revision_conflict", "This proposal changed. Refresh before editing.")
    if _review_payload(action, action.payload or {}) == _review_payload(action, payload):
        return False
    if type(editor_user_id) is not int or editor_user_id <= 0:
        raise ValueError("editor must be a positive user id")
    from ..qbo_actions.proposal_fingerprint import compute_proposal_fingerprint
    values = dict(payload=payload, revision=ProposedAction.revision + 1,
                  last_human_editor_user_id=editor_user_id,
                  inputs_fingerprint=compute_proposal_fingerprint(action.action_type, payload))
    if reasoning is not None:
        values["reasoning"] = reasoning
    with db.no_autoflush:
        result = db.execute(update(ProposedAction).where(
            ProposedAction.id == action.id,
            ProposedAction.company_id == action.company_id,
            ProposedAction.status == "PENDING",
            ProposedAction.revision == action.revision,
            ProposedAction.provenance_version == action.provenance_version,
        ).values(**values).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        _refuse("proposal_revision_conflict", "This proposal was edited or approved concurrently. Refresh before continuing.")
    db.expire(action)
    return True

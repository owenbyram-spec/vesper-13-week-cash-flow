from datetime import datetime
import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body, status  # noqa: F401
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..db import get_db
from ..models import ProposedAction, User
from ..dependencies import check_entity_tenant_access, check_tenant_access, require_admin
from ..qbo_actions.executor import execute_proposed_action, is_executable_action_type
# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — expiry predicate import (INV-EXPIRE)
from ..qbo_actions.transitions import action_is_expired, claim_action_transition
from .actions import _audit_action_review, _capture_action_correction, _validate_review, ApprovalRequest
from ..services.proposal_provenance import human_provenance, edit_proposal_payload
from ..services.errors import error_envelope, user_safe_http_exception
from ..services.schedule_engine import run_post_execution_schedule_hook

router = APIRouter(
    prefix="/proposals",
    tags=["Proposals"],
    dependencies=[Depends(require_admin)]
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Pydantic Models for API
# ------------------------------------------------------------------

class ProposalCreate(BaseModel):
    company_id: str
    action_type: str # JOURNAL_ENTRY, RECLASSIFICATION, VENDOR_UPDATE
    payload: Dict[str, Any]
    reasoning: Optional[str] = None
    confidence_score: Optional[float] = None

class ProposalResponse(BaseModel):
    id: int
    company_id: str
    action_type: str
    payload: Dict[str, Any]
    reasoning: Optional[str]
    confidence_score: Optional[float]
    status: str
    revision: int
    human_preparer_user_id: int | None = None
    last_human_editor_user_id: int | None = None
    created_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    execution_result: Optional[Dict[str, Any]]

    class Config:
        orm_mode = True

# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@router.get("/", response_model=List[ProposalResponse])
def list_proposals(
    status: str = "PENDING",  # noqa: F811
    company_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """
    List proposals, optionally filtered by status and company_id.
    """
    if company_id:
        check_tenant_access("VIEWER", company_id=company_id, db=db, user=user)
    elif not user.is_priority_admin:
        raise HTTPException(status_code=400, detail="company_id is required")
    else:
        # Priority-admin cross-tenant listing (no company_id). Mirrors
        # intelligence.py::list_connections (S3, 2026-07-10 audit): the
        # break-glass audit runs BEFORE the RLS bypass and is FAIL-CLOSED
        # (DIM-033) — _audit_tenant_scope_bypass commits the forensic
        # AuthAuditEvent row and RAISES (denying the bypass) if that write
        # fails. Imported (not re-typed) so the event shape cannot drift.
        # realm_id "*" records that this bypass enumerates ALL realms.
        from ..dependencies import _audit_tenant_scope_bypass
        from ..tenant_rls import pin_tenant_scope_live

        _audit_tenant_scope_bypass(
            db,
            user=user,
            company_id="*",
            minimum_role="INTERNAL_SUPER_ADMIN",
            reason="platform_admin_proposals_listing",
        )

        # RLS: proposed_actions is FORCE-RLS; without a pinned scope this
        # branch fail-closes to zero rows (the "silently broken" S3 state).
        # CROSS-TENANT-OK: platform-admin-only cross-tenant proposal listing
        pin_tenant_scope_live(db, bypass=True)

    query = db.query(ProposedAction)
    
    if status:
        query = query.filter(ProposedAction.status == status)
    
    if company_id:
        query = query.filter(ProposedAction.company_id == company_id)
        
    return query.order_by(ProposedAction.created_at.desc()).all()


class ProposalPayloadUpdate(BaseModel):
    """Partial update – the user can edit the JE lines / date / memo before approving."""
    payload: Dict[str, Any]
    expected_revision: int | None = None


@router.post("/", response_model=ProposalResponse)
def create_proposal(
    proposal: ProposalCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """
    Create a human-prepared proposal attributed to the authenticated caller.
    """
    check_tenant_access("EDITOR", company_id=proposal.company_id, db=db, user=user)
    db_proposal = ProposedAction(
        company_id=proposal.company_id,
        **human_provenance(user.id),
        action_type=proposal.action_type,
        payload=proposal.payload,
        reasoning=proposal.reasoning,
        confidence_score=proposal.confidence_score,
        status="PENDING"
    )
    db.add(db_proposal)
    db.commit()
    db.refresh(db_proposal)
    return db_proposal


@router.patch("/{proposal_id}", response_model=ProposalResponse)
def update_proposal_payload(
    proposal_id: int,
    body: ProposalPayloadUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """
    Update the payload of a PENDING proposal (e.g. edit JE lines before approval).
    """
    proposal = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=user,
        model=ProposedAction,
        entity_id=proposal_id,
        not_found_detail="Proposal not found",
    )

    if proposal.status != "PENDING":
        raise HTTPException(status_code=409, detail=f"Cannot edit a {proposal.status} proposal.")

    # Validate balance for JE payloads
    if proposal.action_type == "JOURNAL_ENTRY" and "lines" in body.payload:
        lines = body.payload["lines"]
        total_debit = round(sum(float(l.get("debit", 0)) for l in lines), 2)  # noqa: E741
        total_credit = round(sum(float(l.get("credit", 0)) for l in lines), 2)  # noqa: E741
        if abs(total_debit - total_credit) > 0.01:
            raise HTTPException(
                status_code=422,
                detail=f"Debits ({total_debit}) must equal credits ({total_credit})",
            )

    prior_payload = proposal.payload
    if not edit_proposal_payload(db, proposal, body.payload, editor_user_id=user.id, expected_revision=body.expected_revision):
        return proposal

    # Capture the human edit (before/after) as a structured Correction.
    # Best-effort + SAVEPOINT-isolated inside record_correction (db=db) — it
    # never raises into this handler and never gates the edit (proposal-first).
    _capture_action_correction(
        db,
        action=proposal,
        human_decision="edited",
        ai_output={
            "action_type": proposal.action_type,
            "payload": prior_payload,
            "reasoning": proposal.reasoning,
        },
        human_output={"payload": body.payload},
        actor_user_id=user.id,
    )
    db.commit()
    db.refresh(proposal)
    return proposal


@router.post("/{proposal_id}/approve", response_model=ProposalResponse)
def approve_proposal(proposal_id: int, body: ApprovalRequest | None = Body(None), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """
    Approve and Execute a proposal.

    The QBO write goes through the durable execution ledger
    (execute_proposed_action): QBOActionExecution row, idempotency key, and a
    REDACTED execution_result summary — never the raw provider response.
    Failures propagate honestly (502 + failed ledger row), and a failure of
    the schedule post-hook is surfaced loudly (audit row + warning in
    execution_result), not swallowed.
    """
    proposal = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=user,
        model=ProposedAction,
        entity_id=proposal_id,
        not_found_detail="Proposal not found",
    )

    if proposal.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Proposal is {proposal.status}, cannot approve.")

    expected_revision = body.expected_revision if body else None
    _validate_review(db, proposal, user.id, expected_revision, reviewer=user.email, lane="proposals")

    if not is_executable_action_type(proposal.action_type):
        raise HTTPException(status_code=400, detail=f"Unknown action type: {proposal.action_type}")

    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A4 — INV-EXPIRE on the admin lane;
    # mirrors the dashboard-lane refusal (audited, 409 action_expired).
    if action_is_expired(proposal):
        _audit_action_review(
            db,
            action=proposal,
            user_email=user.email,
            audit_action="APPROVE_ACTION_FAILED",
            details={"source": "proposals", "reason": "expired"},
        )
        db.commit()  # LAYER-OK: audit-the-refusal commit, mirrors the sibling APPROVE_ACTION_FAILED branch
        raise user_safe_http_exception(
            status_code=409,
            error_code="action_expired",
            user_message=(
                "This proposal expired and can no longer be approved. Re-draft it to "
                "review a fresh preview against current records."
            ),
        )

    # Atomic PENDING→APPROVED claim (F-07): the status precheck above is a
    # fast-path; this conditional UPDATE is the authoritative gate against a
    # concurrent approve+reject. On a lost race the proposal was decided
    # elsewhere between the precheck and here — return the same 400 rather than
    # execute a QBO write on an already-rejected proposal.
    if not claim_action_transition(
        db, action_id=proposal.id, from_status="PENDING", to_status="APPROVED",
        reviewed_by=user.email, expected_revision=expected_revision, approver_user_id=user.id,
    ):
        db.rollback()
        raise HTTPException(status_code=400, detail="Proposal is no longer PENDING, cannot approve.")
    db.commit()

    try:
        executed = execute_proposed_action(db, proposal.id)
    except HTTPException:
        # AC-E1 (D-B6-2, 2026-07-16): a 4xx the executor raises — e.g. a 409
        # period_locked, guarded BEFORE _claim_execution so no ledger row was
        # written and nothing reached QBO — must surface AS ITSELF, never be
        # re-wrapped as a 500. rollback is a safe no-op on the clean session; the
        # bare re-raise preserves the original 409 + traceback.
        db.rollback()
        raise
    except Exception as e:
        # Pre-write failures (e.g. a concurrent run already executing this
        # action). The executor handles write failures itself by returning a
        # FAILED proposal; anything raised here never reached QBO.
        db.rollback()
        raise user_safe_http_exception(
            status_code=500,
            error_code="proposal_execution_failed",
            user_message="The approved proposal could not be executed.",
            exc=e,
            retryable=True,
            log_context=f"proposal_execution_failed proposal_id={proposal_id}",
        )

    if executed.status == "FAILED":
        execution_result = executed.execution_result or {}
        raise HTTPException(
            status_code=502,
            detail=execution_result.get("error")
            or error_envelope(
                error_code="proposal_execution_failed",
                user_message="The approved proposal could not be posted to QuickBooks.",
                retryable=execution_result.get("execution_status") == "retryable_failed",
            ),
        )

    # Mark linked ScheduleEntry rows POSTED; failures are surfaced loudly
    # (SCHEDULE_POST_HOOK_FAILED audit row + warning in execution_result).
    run_post_execution_schedule_hook(db, executed, actor=user.email)

    db.refresh(executed)
    return executed


@router.post("/{proposal_id}/reject", response_model=ProposalResponse)
def reject_proposal(proposal_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """
    Reject a proposal and clean up linked schedule entries.
    """
    proposal = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=user,
        model=ProposedAction,
        entity_id=proposal_id,
        not_found_detail="Proposal not found",
    )

    # Atomic PENDING→REJECTED claim (F-07): the authoritative gate against a
    # concurrent approve+reject (the case where an approve would post while this
    # reject wins the status column). This also brings the proposals reject lane
    # in line with the dashboard reject lane, which already refuses non-PENDING
    # rows — rejecting an already-APPROVED/EXECUTED proposal is never valid.
    if not claim_action_transition(
        db, action_id=proposal.id, from_status="PENDING", to_status="REJECTED",
        reviewed_by=user.email,
    ):
        db.rollback()
        raise HTTPException(status_code=400, detail="Proposal is no longer PENDING, cannot reject.")

    # Capture the human rejection as a structured Correction. Best-effort +
    # SAVEPOINT-isolated inside record_correction (db=db) — never raises into
    # this handler and never gates the reject (proposal-first preserved).
    _capture_action_correction(
        db,
        action=proposal,
        human_decision="rejected",
        human_output=None,
        actor_user_id=user.id,
    )

    # Clean up linked ScheduleEntry records so schedules can be re-generated
    from ..models import ScheduleEntry
    deleted = db.query(ScheduleEntry).filter(  # noqa: F841
        ScheduleEntry.proposed_action_id == proposal_id,
    ).delete(synchronize_session="fetch")
    
    db.commit()
    db.refresh(proposal)
    return proposal

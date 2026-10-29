"""Deterministic two-person adjudication for the forward schedule receipt contract.

Every mutation locks ledger -> proposal -> case -> target. Access-audit helpers
must run before entry; the checks here are fresh read-only authorization checks.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models import (AuditEntry, EntityBilling, InternalActionExecution, InternalActionRecoveryCase,
                      Organization, OrganizationCompany, OrganizationMembership, ProposedAction,
                      User, UserCompanyAccess)
from ..roles import ROLE_WEIGHTS
from ..tenant_rls import tenant_scope_for_realm
from .internal_action_effects import (EFFECT_CONTRACT, digest, matching_receipt, primitive,
                                      scope_matches, source_matches)

_CASE_FIELDS = ("execution_id", "action_id", "company_id", "candidate_nonce", "preparer_user_id",
                "prepared_at", "effect_contract", "source_snapshot_digest", "approval_fingerprint",
                "proposal_revision", "finding", "receipt_id", "receipt_digest", "target_observation_digest",
                "owner_token_digest", "fenced_at", "reason", "evidence_locator")


def refuse(code="recovery_unresolved", status=409):
    raise HTTPException(status_code=status, detail={"error_code": code,
        "user_message": "Recovery requires current authorized evidence and a separate reviewer.", "retryable": False})


def authorize(db, company_id, actor_id, minimum="TENANT_ADMIN"):
    """Never trust the request's cached ORM actor or an actor supplied in JSON."""
    actor = db.query(User).filter(User.id == actor_id).populate_existing().one_or_none()
    company = db.query(OrganizationCompany).filter(OrganizationCompany.realm_id == company_id).populate_existing().one_or_none()
    organization = db.query(Organization).filter(Organization.id == company.organization_id).populate_existing().one_or_none() if company else None
    if (actor is None or company is None or organization is None or not organization.is_active
            or company.status != "active" or actor.force_password_reset):
        refuse("recovery_authority_unavailable", 403)
    if actor.is_priority_admin:
        return actor
    from .tenant_companies import COMPANY_SUSPENDED_BILLING_STATUSES
    billing = db.query(EntityBilling.subscription_status).filter(EntityBilling.realm_id == company_id,
        EntityBilling.organization_company_id == company.id).one_or_none()
    if billing and (billing[0] or "").lower() in COMPANY_SUSPENDED_BILLING_STATUSES:
        refuse("recovery_authority_unavailable", 403)
    membership = db.query(OrganizationMembership).filter(
        OrganizationMembership.organization_id == organization.id,
        OrganizationMembership.user_id == actor.id).populate_existing().one_or_none()
    if (membership is None or membership.status != "active"
            or ROLE_WEIGHTS.get((membership.role or "").upper(), 0) < ROLE_WEIGHTS[minimum]):
        refuse("recovery_authority_unavailable", 403)
    grants = {row[0] for row in db.query(UserCompanyAccess.organization_company_id).filter(
        UserCompanyAccess.organization_id == organization.id, UserCompanyAccess.user_id == actor.id).all()}
    if grants and company.id not in grants:
        refuse("recovery_authority_unavailable", 404)
    return actor


def _locked(db, company_id, action_id, execution_id):
    execution = db.query(InternalActionExecution).filter(InternalActionExecution.company_id == company_id,
        InternalActionExecution.action_id == action_id, InternalActionExecution.id == execution_id).populate_existing().with_for_update().one_or_none()
    action = db.query(ProposedAction).filter(ProposedAction.company_id == company_id,
        ProposedAction.id == action_id).populate_existing().with_for_update().one_or_none()
    if execution is None or action is None:
        refuse("recovery_not_found", 404)
    latest = db.query(InternalActionExecution.id).filter(InternalActionExecution.company_id == company_id,
        InternalActionExecution.action_id == action_id).order_by(InternalActionExecution.id.desc()).first()
    if latest is None or latest[0] != execution.id:
        refuse("recovery_stale_execution")
    return execution, action


def _require_known_preparation(source):
    # Null actor IDs mean AI preparation only under the known provenance contract.
    # Capturing an unknown historical version does not establish absence of humans.
    if (type(source) is not dict or type(source.get("provenance_version")) is not int
            or source["provenance_version"] != 1):
        refuse("recovery_provenance_unknown")


def _binding(db, execution, action):
    _require_known_preparation(execution.source_snapshot_json)
    if not scope_matches(db, execution) or not source_matches(action, execution):
        refuse("recovery_source_unresolved")


def _finding(db, execution, action, *, lock_target):
    _binding(db, execution, action)
    try:
        receipt = matching_receipt(db, execution, lock_target=lock_target)
    except (RuntimeError, ValueError, TypeError):
        refuse("recovery_receipt_unresolved")
    return ("applied", receipt) if receipt else ("not_applied", None)


def _case_digest(case):
    return digest({"version": 1, **{name: getattr(case, name) for name in _CASE_FIELDS}})


def _case_scope(case, execution):
    return all(getattr(case, name) == getattr(execution, name) for name in
               ("company_id", "organization_id", "organization_company_id", "action_id")) and case.execution_id == execution.id


def case_summary(case):
    return {"case_id": case.id, "execution_id": case.execution_id,
        "candidate_digest": case.candidate_digest, "finding": case.finding,
        "decision": case.decision, "prepared_at": primitive(case.prepared_at)["datetime"],
        "decided_at": primitive(case.decided_at)["datetime"] if case.decided_at else None}


def inspect_recovery(db, action):
    """No recovery write; expose neither snapshots, locator, actors nor owner token."""
    execution = db.query(InternalActionExecution).filter(InternalActionExecution.company_id == action.company_id,
        InternalActionExecution.action_id == action.id).order_by(InternalActionExecution.id.desc()).first()
    if execution is None:
        return {"eligible": False, "reason": "no_execution", "effect_contract": None}
    result = {"eligible": False, "reason": "unsupported_or_legacy", "execution_id": execution.id,
              "effect_contract": execution.effect_contract, "proposal_revision": execution.proposal_revision,
              "fenced": execution.effect_revoked_at is not None}
    if execution.effect_contract != EFFECT_CONTRACT:
        return result
    if execution.status not in {"running", "recovery_required"}:
        result["reason"] = "terminal_or_unowned"
    elif not execution.owner_claim_token or execution.active_intent_key != execution.intent_fingerprint or action.status != "APPROVED":
        result["reason"] = "source_or_ownership_unresolved"
    else:
        try:
            finding, _ = _finding(db, execution, action, lock_target=False)
            result.update(eligible=True, reason="eligible_for_preparation", finding=finding)
        except HTTPException:
            result["reason"] = "evidence_unresolved"
    case = db.query(InternalActionRecoveryCase).filter(InternalActionRecoveryCase.company_id == action.company_id,
        InternalActionRecoveryCase.execution_id == execution.id).order_by(InternalActionRecoveryCase.id.desc()).first()
    if case is not None and _case_scope(case, execution):
        result["case"] = case_summary(case)
    return result


def _validate_request(reason, evidence_locator):
    if (type(reason) is not str or not reason.strip() or len(reason) > 500
            or type(evidence_locator) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", evidence_locator)):
        refuse("recovery_request_invalid", 422)


def _prepared_reread(db, company_id, action_id, execution_id, actor_id, expected_revision, reason, locator):
    with Session(bind=db.get_bind()) as fresh:
        with tenant_scope_for_realm(fresh, company_id):
            authorize(fresh, company_id, actor_id)
            execution = fresh.query(InternalActionExecution).filter(InternalActionExecution.company_id == company_id,
                InternalActionExecution.action_id == action_id, InternalActionExecution.id == execution_id).one_or_none()
            case = fresh.query(InternalActionRecoveryCase).filter(InternalActionRecoveryCase.company_id == company_id,
                InternalActionRecoveryCase.execution_id == execution_id,
                InternalActionRecoveryCase.active_execution_id == execution_id).one_or_none()
            if (execution and case and _case_scope(case, execution) and case.preparer_user_id == actor_id
                    and case.proposal_revision == expected_revision and case.reason == reason
                    and case.evidence_locator == locator and case.candidate_digest == _case_digest(case)
                    and execution.effect_revoked_at is not None and execution.status == "recovery_required"
                    and execution.active_intent_key == execution.intent_fingerprint):
                events = fresh.query(AuditEntry).filter(AuditEntry.company_id == company_id,
                    AuditEntry.entity_id == str(action_id), AuditEntry.action == "INTERNAL_ACTION_RECOVERY_PREPARED").all()
                if sum(isinstance(event.details, dict) and event.details.get("case_id") == case.id
                       and event.details.get("execution_id") == execution_id
                       and event.details.get("candidate_digest") == case.candidate_digest for event in events) == 1:
                    return case_summary(case)
    return None


def prepare_recovery(db, *, company_id, action_id, execution_id, expected_revision, actor_id, reason, evidence_locator):
    _validate_request(reason, evidence_locator)
    try:
        execution, action = _locked(db, company_id, action_id, execution_id)
        actor = authorize(db, company_id, actor_id)
        _binding(db, execution, action)
        if (action.revision != expected_revision or action.status != "APPROVED"
                or execution.status not in {"running", "recovery_required"} or not execution.owner_claim_token
                or execution.active_intent_key != execution.intent_fingerprint):
            refuse("recovery_stale_execution")
        case = db.query(InternalActionRecoveryCase).filter(InternalActionRecoveryCase.company_id == company_id,
            InternalActionRecoveryCase.active_execution_id == execution.id).populate_existing().with_for_update().one_or_none()
        if case is not None:
            if (not _case_scope(case, execution) or case.preparer_user_id != actor_id or case.reason != reason
                    or case.evidence_locator != evidence_locator or case.proposal_revision != expected_revision
                    or case.candidate_digest != _case_digest(case)):
                refuse("recovery_candidate_conflict")
            _validate_candidate(db, execution, action, case)
            return case_summary(case)
        finding, receipt = _finding(db, execution, action, lock_target=True)
        now = datetime.now(timezone.utc)
        if execution.effect_revoked_at is None:
            execution.effect_revoked_at = now
            execution.effect_revoked_by_user_id = actor_id
        execution.status = "recovery_required"
        case = InternalActionRecoveryCase(execution_id=execution.id, action_id=action.id,
            company_id=company_id, organization_id=execution.organization_id,
            organization_company_id=execution.organization_company_id, active_execution_id=execution.id,
            candidate_nonce=str(uuid.uuid4()), preparer_user_id=actor_id, prepared_at=now,
            effect_contract=EFFECT_CONTRACT, source_snapshot_digest=execution.source_snapshot_digest,
            approval_fingerprint=execution.approval_fingerprint, proposal_revision=execution.proposal_revision,
            finding=finding, receipt_id=receipt.id if receipt else None,
            receipt_digest=receipt.effect_snapshot_digest if receipt else None,
            target_observation_digest=receipt.effect_snapshot_digest if receipt else None,
            owner_token_digest=digest(execution.owner_claim_token), fenced_at=execution.effect_revoked_at,
            reason=reason, evidence_locator=evidence_locator)
        case.candidate_digest = _case_digest(case)
        db.add(case)
        db.flush()
        db.add(AuditEntry(company_id=company_id, user_email=actor.email,
            action="INTERNAL_ACTION_RECOVERY_PREPARED", entity_type="proposed_action", entity_id=str(action_id),
            details={"execution_id": execution.id, "case_id": case.id, "candidate_digest": case.candidate_digest}))
        db.commit()
        return case_summary(case)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        try:
            saved = _prepared_reread(db, company_id, action_id, execution_id, actor_id, expected_revision, reason, evidence_locator)
            if saved:
                db.expire_all()
                return saved
        except Exception:
            pass
        refuse("recovery_durable_state_unavailable", 503)


def _validate_candidate(db, execution, action, case):
    if (not _case_scope(case, execution) or case.candidate_digest != _case_digest(case)
            or case.effect_contract != execution.effect_contract
            or case.source_snapshot_digest != execution.source_snapshot_digest
            or case.approval_fingerprint != execution.approval_fingerprint
            or case.proposal_revision != execution.proposal_revision
            or case.owner_token_digest != digest(execution.owner_claim_token)
            or execution.effect_revoked_at is None
            or primitive(case.fenced_at) != primitive(execution.effect_revoked_at)):
        refuse("recovery_candidate_stale")
    finding, receipt = _finding(db, execution, action, lock_target=True)
    if (case.finding != finding or case.receipt_id != (receipt.id if receipt else None)
            or case.receipt_digest != (receipt.effect_snapshot_digest if receipt else None)
            or case.target_observation_digest != (receipt.effect_snapshot_digest if receipt else None)):
        refuse("recovery_candidate_stale")


def _decision_reread(db, company_id, action_id, case_id, actor_id, candidate_digest, decision):
    with Session(bind=db.get_bind()) as fresh:
        with tenant_scope_for_realm(fresh, company_id):
            authorize(fresh, company_id, actor_id)
            case = fresh.query(InternalActionRecoveryCase).filter(InternalActionRecoveryCase.company_id == company_id,
                InternalActionRecoveryCase.action_id == action_id, InternalActionRecoveryCase.id == case_id).one_or_none()
            if (case is None or case.candidate_digest != candidate_digest or case.candidate_digest != _case_digest(case)
                    or case.active_execution_id is not None or case.decided_at is None
                    or case.decision != decision or case.decision_user_id != actor_id
                    or case.decision_digest != digest({"candidate": candidate_digest, "decision": decision, "actor_id": actor_id})):
                return None
            execution = fresh.query(InternalActionExecution).filter(InternalActionExecution.company_id == company_id,
                InternalActionExecution.id == case.execution_id, InternalActionExecution.action_id == action_id).one_or_none()
            action = fresh.query(ProposedAction).filter(ProposedAction.company_id == company_id,
                ProposedAction.id == action_id).one_or_none()
            if not execution or not action or not _case_scope(case, execution) or execution.effect_revoked_at is None:
                return None
            # Decision CAS and matching audit are one transaction; inspect exact case metadata.
            events = fresh.query(AuditEntry).filter(AuditEntry.company_id == company_id,
                AuditEntry.entity_id == str(action_id), AuditEntry.action == "INTERNAL_ACTION_RECOVERY_" + decision.upper()).all()
            if sum(isinstance(event.details, dict) and event.details.get("case_id") == case_id
                   and event.details.get("execution_id") == execution.id
                   and event.details.get("candidate_digest") == candidate_digest for event in events) != 1:
                return None
            if decision != "rejected":
                expected = "succeeded" if decision == "confirmed_applied" else "not_applied"
                if (execution.status != expected or execution.active_intent_key is not None
                        or not source_matches(action, execution)
                        or action.status != ("EXECUTED" if expected == "succeeded" else "FAILED")
                        or not isinstance(action.execution_result, dict)
                        or action.execution_result.get("execution_status") != expected
                        or action.execution_result.get("executed") is not (expected == "succeeded")):
                    return None
            return case_summary(case)


def decide_recovery(db, *, company_id, action_id, case_id, actor_id, candidate_digest, reject=False):
    decision = None
    try:
        pointer = db.query(InternalActionRecoveryCase.execution_id).filter(
            InternalActionRecoveryCase.company_id == company_id, InternalActionRecoveryCase.action_id == action_id,
            InternalActionRecoveryCase.id == case_id).first()
        if pointer is None:
            refuse("recovery_not_found", 404)
        execution, action = _locked(db, company_id, action_id, pointer[0])
        actor = authorize(db, company_id, actor_id)
        case = db.query(InternalActionRecoveryCase).filter(InternalActionRecoveryCase.company_id == company_id,
            InternalActionRecoveryCase.action_id == action_id, InternalActionRecoveryCase.id == case_id).populate_existing().with_for_update().one()
        # Even rejection obeys original and current preparer/editor separation.
        source = execution.source_snapshot_json
        if (type(source) is not dict or digest(source) != case.source_snapshot_digest
                or execution.source_snapshot_digest != case.source_snapshot_digest):
            refuse("recovery_source_unresolved")
        _require_known_preparation(source)
        excluded = {case.preparer_user_id, source.get("human_preparer_user_id"), source.get("last_human_editor_user_id"),
                    action.human_preparer_user_id, action.last_human_editor_user_id}
        if actor_id in excluded:
            refuse("recovery_separate_reviewer_required", 403)
        if (candidate_digest != case.candidate_digest or case.candidate_digest != _case_digest(case)
                or not _case_scope(case, execution)):
            refuse("recovery_candidate_conflict")
        decision = "rejected" if reject else "confirmed_" + case.finding
        decision_digest = digest({"candidate": candidate_digest, "decision": decision, "actor_id": actor_id})
        if case.decision is not None:
            if case.decision != decision or case.decision_digest != decision_digest:
                refuse("recovery_decision_conflict")
            return case_summary(case)
        if (execution.effect_revoked_at is None or execution.status != "recovery_required"
                or execution.active_intent_key != execution.intent_fingerprint
                or case.active_execution_id != execution.id):
            refuse("recovery_candidate_stale")
        if not reject:
            if action.status != "APPROVED":
                refuse("recovery_source_unresolved")
            _validate_candidate(db, execution, action, case)
        now = datetime.now(timezone.utc)
        result = db.execute(update(InternalActionRecoveryCase).where(
            InternalActionRecoveryCase.company_id == company_id, InternalActionRecoveryCase.id == case.id,
            InternalActionRecoveryCase.decision.is_(None), InternalActionRecoveryCase.candidate_digest == candidate_digest,
            InternalActionRecoveryCase.active_execution_id == execution.id).values(
                decision=decision, decision_user_id=actor_id, decided_at=now, decision_digest=decision_digest,
                active_execution_id=None).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            refuse("recovery_decision_conflict")
        if not reject:
            from .internal_action_execution import InternalExecutionOutcome
            applied = case.finding == "applied"
            outcome = InternalExecutionOutcome(applied, "Change applied." if applied else "This action was not applied.",
                                               "succeeded" if applied else "not_applied", applied)
            execution.status = outcome.execution_status
            execution.active_intent_key = None
            execution.finished_at = now
            execution.result_message = outcome.message
            execution.error = None
            action.status = "EXECUTED" if applied else "FAILED"
            action.execution_result = outcome.result()
        db.add(AuditEntry(company_id=company_id, user_email=actor.email,
            action="INTERNAL_ACTION_RECOVERY_" + decision.upper(), entity_type="proposed_action", entity_id=str(action_id),
            details={"execution_id": execution.id, "case_id": case.id, "candidate_digest": candidate_digest}))
        db.commit()
        db.refresh(case)
        return case_summary(case)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        try:
            if decision:
                saved = _decision_reread(db, company_id, action_id, case_id, actor_id, candidate_digest, decision)
                if saved:
                    db.expire_all()
                    return saved
        except Exception:
            pass
        refuse("recovery_durable_state_unavailable", 503)

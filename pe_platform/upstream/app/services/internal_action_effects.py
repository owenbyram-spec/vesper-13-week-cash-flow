"""Forward-only schedule-create/v1 effect evidence and exact Session write fence.

Only executor admission installs a context. Payload keys and direct handler calls
cannot supply it. These are application invariants, not privileged-DB protection.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import weakref

from ..models import AccountingSchedule, InternalActionEffectReceipt, InternalActionExecution, ProposedAction
from .internal_action_intent import action_intent_fingerprint, approval_fingerprint, canonical_json, executable_inputs
from .tenant_companies import organization_company_for_realm, organization_for_company

EFFECT_CONTRACT = "schedule-create/v1"
SCHEDULE_EFFECT_FIELDS = (
    "realm_id", "schedule_type", "name", "description", "vendor_name", "note",
    "qbo_txn_id", "qbo_account_id", "qbo_account_name", "expense_account_id",
    "expense_account_name", "original_amount", "remaining_amount", "accumulated_amount",
    "implementation_date", "start_date", "end_date", "useful_life_months", "asset_class",
    "depreciation_method", "salvage_value", "asset_cost_account_id", "asset_cost_account_name",
    "disposal_date", "disposal_proceeds", "disposal_gain_loss", "disposal_proposed_action_id",
    "automation_policy", "status", "detection_confidence", "ai_reasoning", "approved_by", "approved_at",
)


def primitive(value):
    """Version-one finite typed serializer; never stringify arbitrary objects."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite effect value")
        return {"decimal": format(value, "f")}
    if isinstance(value, datetime):
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return {"datetime": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise ValueError("Invalid effect keys")
        return {key: primitive(item) for key, item in value.items()}
    if type(value) is list:
        return [primitive(item) for item in value]
    canonical_json(value)  # exact JSON primitive validation, including finite floats
    return value


def digest(value):
    return hashlib.sha256(canonical_json(primitive(value)).encode("utf-8")).hexdigest()


def source_snapshot(action, execution):
    return primitive({"version": 1, "action_id": action.id, "company_id": action.company_id,
        "action_type": action.action_type, "tool_name": execution.tool_name,
        "inputs": executable_inputs(action.action_type, action.payload),
        "proposal_revision": action.revision, "intent_fingerprint": execution.intent_fingerprint,
        "approval_fingerprint": approval_fingerprint(action, execution.intent_fingerprint),
        "provenance_version": action.provenance_version,
        "human_preparer_user_id": action.human_preparer_user_id,
        "last_human_editor_user_id": action.last_human_editor_user_id,
        "reviewed_by": action.reviewed_by, "reviewed_at": action.reviewed_at})


def capture_source(action, execution):
    if action.action_type != "SCHEDULE_ROW_CREATE":
        return
    snapshot = source_snapshot(action, execution)
    snapshot["origin_organization_id"] = execution.organization_id
    snapshot["origin_organization_company_id"] = execution.organization_company_id
    execution.effect_contract = EFFECT_CONTRACT
    execution.source_snapshot_json = snapshot
    execution.source_snapshot_digest = digest(snapshot)


def scope_matches(db, execution):
    organization = organization_for_company(db, execution.company_id)
    company = organization_company_for_realm(db, organization_id=organization.id,
        realm_id=execution.company_id) if organization else None
    return bool(company and execution.organization_id == organization.id
                and execution.organization_company_id == company.id)


def source_matches(action, execution):
    try:
        stored = execution.source_snapshot_json
        if (execution.effect_contract != EFFECT_CONTRACT or execution.protocol_version != 2
                or type(stored) is not dict or digest(stored) != execution.source_snapshot_digest
                or execution.action_id != action.id or execution.company_id != action.company_id
                or execution.action_type != "SCHEDULE_ROW_CREATE" or execution.tool_name != "add_schedule_row"
                or execution.proposal_revision != action.revision
                or execution.intent_fingerprint != action_intent_fingerprint(action, execution.tool_name)
                or execution.approval_fingerprint != approval_fingerprint(action, execution.intent_fingerprint)):
            return False
        expected = source_snapshot(action, execution)
        observed = {key: value for key, value in stored.items()
                    if key not in {"origin_organization_id", "origin_organization_company_id"}}
        return observed == expected
    except (TypeError, ValueError, AttributeError):
        return False


def schedule_snapshot(schedule):
    return {"version": 1, "target_schedule_id": schedule.id, "fields": primitive({name: getattr(schedule, name) for name in SCHEDULE_EFFECT_FIELDS})}


@dataclass(frozen=True)
class _ExecutionContext:
    execution_id: int
    action_id: int
    company_id: str
    owner_token: str
    source_digest: str


_contexts = weakref.WeakKeyDictionary()


@contextmanager
def admitted_effect_context(db, execution, token):
    """Private server call site: exact durable ledger -> exact Session, never params."""
    if db in _contexts:
        raise RuntimeError("Nested effect context refused")
    if execution.effect_contract != EFFECT_CONTRACT or execution.owner_claim_token != token:
        raise RuntimeError("Unsupported effect context")
    context = _ExecutionContext(execution.id, execution.action_id, execution.company_id,
                                token, execution.source_snapshot_digest)
    _contexts[db] = context
    try:
        yield
    finally:
        _contexts.pop(db, None)


def lock_schedule_effect(db, realm_id, params):
    context = _contexts.get(db)
    if type(context) is not _ExecutionContext or context.company_id != realm_id:
        raise RuntimeError("Trusted execution context required")
    with db.no_autoflush:
        execution = db.query(InternalActionExecution).filter(
            InternalActionExecution.company_id == realm_id,
            InternalActionExecution.id == context.execution_id,
            InternalActionExecution.action_id == context.action_id,
        ).populate_existing().with_for_update().one_or_none()
        if (execution is None or execution.status != "running" or execution.effect_revoked_at is not None
                or execution.owner_claim_token != context.owner_token
                or execution.active_intent_key != execution.intent_fingerprint
                or execution.source_snapshot_digest != context.source_digest or not scope_matches(db, execution)):
            raise RuntimeError("Effect ownership unavailable")
        action = db.query(ProposedAction).filter(ProposedAction.company_id == realm_id,
            ProposedAction.id == context.action_id).populate_existing().with_for_update().one_or_none()
        if action is None or action.status != "APPROVED" or not source_matches(action, execution):
            raise RuntimeError("Effect source binding changed")
        if canonical_json(params) != canonical_json(executable_inputs(action.action_type, action.payload)):
            raise RuntimeError("Effect inputs changed")
        if db.query(InternalActionEffectReceipt).filter(
                InternalActionEffectReceipt.company_id == realm_id,
                InternalActionEffectReceipt.execution_id == execution.id).first() is not None:
            raise RuntimeError("Effect already recorded")
    return execution


def record_schedule_effect(db, execution, schedule):
    context = _contexts.get(db)
    if (type(context) is not _ExecutionContext or context.execution_id != execution.id
            or context.company_id != schedule.realm_id or context.owner_token != execution.owner_claim_token):
        raise RuntimeError("Trusted execution context required")
    # Flush and reload database Numeric/default values before freezing the snapshot.
    db.flush()
    db.refresh(schedule)
    if schedule.realm_id != execution.company_id or not scope_matches(db, execution):
        raise RuntimeError("Effect scope changed")
    snapshot = schedule_snapshot(schedule)
    receipt = InternalActionEffectReceipt(execution_id=execution.id, action_id=execution.action_id,
        company_id=execution.company_id, organization_id=execution.organization_id,
        organization_company_id=execution.organization_company_id, effect_contract=EFFECT_CONTRACT,
        source_snapshot_digest=execution.source_snapshot_digest, intent_fingerprint=execution.intent_fingerprint,
        approval_fingerprint=execution.approval_fingerprint, proposal_revision=execution.proposal_revision,
        target_schedule_id=schedule.id, effect_snapshot_json=snapshot, effect_snapshot_digest=digest(snapshot))
    db.add(receipt)
    db.flush()
    return receipt


def matching_receipt(db, execution, *, lock_target=False):
    """Return exact immutable receipt + unchanged current target, else refuse."""
    receipt = db.query(InternalActionEffectReceipt).filter(
        InternalActionEffectReceipt.company_id == execution.company_id,
        InternalActionEffectReceipt.execution_id == execution.id).one_or_none()
    if receipt is None:
        return None
    for name in ("action_id", "company_id", "organization_id", "organization_company_id", "effect_contract",
                 "source_snapshot_digest", "intent_fingerprint", "approval_fingerprint", "proposal_revision"):
        if getattr(receipt, name) != getattr(execution, name):
            raise RuntimeError("Receipt binding changed")
    if digest(receipt.effect_snapshot_json) != receipt.effect_snapshot_digest:
        raise RuntimeError("Receipt integrity changed")
    query = db.query(AccountingSchedule).filter(AccountingSchedule.realm_id == execution.company_id,
                                              AccountingSchedule.id == receipt.target_schedule_id).populate_existing()
    if lock_target:
        query = query.with_for_update()
    target = query.one_or_none()
    if target is None or schedule_snapshot(target) != receipt.effect_snapshot_json:
        raise RuntimeError("Receipt target unavailable or changed")
    return receipt

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Header
# PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import check_entity_tenant_access, check_tenant_access, global_tenant_guard, require_tenant_access  # noqa: F401
from ..models import (
    AgentObjective,
    AgentObjectiveOccurrence,
    AgentRun,
    AgentStep,
    AuditEntry,
    CloseTask,
    FinancialCalendar,  # PLAN-EXEMPT: close-cockpit coherence AC-3 — period-lock read for the progress panel.
    InsightArtifact,
    Organization,
    OrganizationCompany,
    ProposedAction,
    ReportSchedule,
    User,
)
from ..auth import get_current_user_from_token
from ..agent.evals import DEFAULT_AGENT_EVALS, eval_records_from_agent_runs
from ..services.errors import user_safe_http_exception
# PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md)
from ..services import agent_plans as agent_plans_service
from ..services.agent_plans import AgentPlanError
from ..services.agent_schedule import compute_next_run_at, normalize_objective_schedule
from ..services.close_run_start import (
    CLOSE_OBJECTIVE_TYPE,
    CloseRunBeforeStartError,
    CloseRunEnqueueError,
    CloseRunScopeError,
    CloseStartPeriodError,
    CloseStartSetting,
    close_period_from_run,
    close_run_request_id,
    close_start_setting_for_company,
    set_close_start_period,
    start_close_run,
)
from ..roles import ROLE_WEIGHTS
from ..services.tenant_companies import organization_company_for_realm, organization_for_company


router = APIRouter(prefix="/agent", tags=["Agent Runtime"])
SUPPORTED_OBJECTIVE_TYPES = {
    "morning_briefing",
    "cash_watch",
    "budget_variance_watch",
    "qbo_sync_health",
    "report_pack",
    "transaction_review",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections AC-6/decision 13 (marker
    # RATIFICATION-2026-07-13.md) — orchestrator reachability fix: the ratified deliverable
    # "objective creation stays on the existing surface" requires this allowlist to accept
    # the new type; unlike close_orchestration (Close-Cockpit-triggered), collections_review
    # has no other creation path, so omitting it ships a dead runner. File is outside the
    # plan's surface_scope — audited doorway, disclosed for the plan-close review.
    "collections_review",
    # PLAN-EXEMPT: chunk B of ratified plan 20260714-wave2-7-lease-flux (marker
    # RATIFICATION-2026-07-14.md) — WF-7 orchestrator reachability: the flux_review
    # runner has no other creation path (unlike close_orchestration, which the Close
    # Cockpit triggers), so omitting it from this allowlist ships a dead runner.
    "flux_review",
    # PLAN-EXEMPT: ratified plan 20260714-wave3-7-going-concern AC-2 (marker
    # RATIFICATION-2026-07-14.md) — RK-6 orchestrator reachability: the
    # going_concern_review runner has no other creation path, so omitting it from this
    # allowlist ships a dead runner. This router file is in the plan's surface_scope;
    # the PLAN-EXEMPT is the audited doorway kept consistent with the sibling entries.
    "going_concern_review",
    # Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder AC-1 / WF-8): orchestrator
    # reachability for the tax-package assembler runner — it has no other creation
    # path (unlike close_orchestration, which the Close Cockpit triggers), so
    # omitting it from this allowlist ships a dead runner. This router file is in the
    # plan's surface_scope.
    "tax_package",
    # Wave 3.4 (plan 20260714-wave3-4-tax-pbc-binder AC-2 / WF-9): orchestrator
    # reachability for the PBC evidence-binder assembler runner — it has no other
    # creation path, so omitting it from this allowlist ships a dead runner. This
    # router file is in the plan's surface_scope.
    "pbc_binder",
}



class ObjectivePolicyInput(BaseModel):
    @field_validator("approval_policy", check_fields=False)
    @classmethod
    def supported_policy(cls, value):
        if value not in (None, {}, {"external_writes": "never"}):
            raise ValueError("Only external_writes: never is supported for fixed runners")
        return value

    @field_validator("permission_scope", check_fields=False)
    @classmethod
    def references_only(cls, value):
        if value and set(value) - {"report_schedule_id"}:
            raise ValueError("Permission scope cannot establish execution authority")
        return value


class ObjectiveCreate(ObjectivePolicyInput):
    company_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    objective_type: str = Field("morning_briefing", min_length=1)
    instructions: Optional[str] = None
    schedule: Dict[str, Any] = Field(default_factory=lambda: {"frequency": "DAILY", "hour_utc": 8, "minute_utc": 0})
    enabled: bool = True
    permission_scope: Optional[Dict[str, Any]] = None
    approval_policy: Optional[Dict[str, Any]] = None
    notification_policy: Optional[Dict[str, Any]] = None


class ObjectiveUpdate(ObjectivePolicyInput):
    name: Optional[str] = None
    instructions: Optional[str] = None
    schedule: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    permission_scope: Optional[Dict[str, Any]] = None
    approval_policy: Optional[Dict[str, Any]] = None
    notification_policy: Optional[Dict[str, Any]] = None


class InsightArtifactStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(open|actioned|archived)$")


# Evidence payloads are provider-shaped/free-form JSON written by two distinct
# proposers (the close-orchestration driver's evidence dict and the nightly
# REVIEW_CHECK proposer's finding payload), so the type is a genuinely dynamic edge.
CloseRunEvidence = Any  # CRAFT-OK: dynamic close-review evidence/finding payload boundary


# ── Start-Close seam (F-B5 W-B) ───────────────────────────────────────────────
# The 202 response for POST /agent/close-runs/start. A NAMED response_model — never
# an untyped dynamic-dict return (AP-LAYER-06) — so the wire contract is documented.
class StartCloseRunResponse(BaseModel):
    objective_id: int
    run_id: int
    company_id: str
    period: str
    status: str = Field(
        ...,
        pattern=(
            "^(queued|running|waiting_for_data|waiting_for_user|waiting_for_review|"
            "waiting_for_approval|executing|reconciling)$"
        ),
    )
    reused: bool
    retryable: bool = False


# ── Close start period (GET/PUT /agent/close-start-period) ────────────────────
class CloseStartPeriodResponse(BaseModel):
    """The first period this platform closes for the company. ``source`` is
    "explicit" (set by a TenantAdmin), "default" (the month before the company
    was connected) or "none" (no company mapping: nothing is refused)."""
    company_id: str
    close_start_period: str | None = None
    effective_close_start_period: str | None = None
    connected_period: str | None = None
    source: str = Field(..., pattern="^(explicit|default|none)$")
    can_edit: bool


class CloseStartPeriodUpdate(BaseModel):
    # YYYY-MM, or null to return to the default (the month before connection).
    close_start_period: str | None = None


# ── Close-runs read DTOs (Phase D supervisor cockpit) ─────────────────────────
# The wire contract for GET /agent/close-runs. The ORM rows (AgentRun / AgentStep /
# ProposedAction / CloseTask) never reach the client — FastAPI filters to these DTOs.
class CloseRunFindingDTO(BaseModel):
    """One close-review finding (a PENDING/processed ProposedAction linked to a
    close run). `link_state` is the honesty marker: LINKED when the finding
    carries an agent_run_id (run-traceable), NO_DATA when it is run-trace-orphaned
    (the nightly REVIEW_CHECK proposer stamps neither close_task_id nor
    agent_run_id, so the finding cannot be tied to a run trace)."""
    id: int
    action_type: str
    status: str
    revision: int | None = None
    summary: str | None = None
    severity: str | None = None
    message: str | None = None
    check_id: str | None = None
    period: str | None = None
    confidence_score: float | None = None
    close_task_id: int | None = None
    agent_run_id: int | None = None
    step_index: int | None = None
    evidence: CloseRunEvidence | None = None
    created_at: str | None = None
    # "LINKED" (run-traceable) | "NO_DATA" (run-trace-orphaned nightly finding)
    link_state: str


class CloseRunStepDTO(BaseModel):
    id: int
    step_type: str
    tool_name: str | None = None
    status: str
    latency_ms: int | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    findings: list[CloseRunFindingDTO] = Field(default_factory=list)


# PLAN-EXEMPT: ratified plan 20260702-qa-batch2 B2-4 AC-3 covers app/routers/agent.py (status.json
# + plan.md surface_scope); the plan-gate's active-plan.json surface_scope is stale-empty (its
# populator needs a genuine human ratify turn to re-derive scope, which this session cannot forge).
class CloseRunDTO(BaseModel):
    id: int
    company_id: str
    objective_id: int | None = None
    trigger_type: str | None = None
    status: str
    period: str | None = None
    retryable: bool = False
    # B2-4 AC-3: the run's human-readable terminal outcome (AgentRun.output_summary). The close
    # driver's _execute_objective writes "A close run is already in progress…" / "Nothing to
    # close…" / "Reviewed N steps; drafted M proposals…" here, so a start that deferred or
    # instant-completed is never silent in the cockpit — the UI renders this string.
    output_summary: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str | None = None
    steps: list[CloseRunStepDTO] = Field(default_factory=list)
    findings: list[CloseRunFindingDTO] = Field(default_factory=list)


class CloseTaskDTO(BaseModel):
    id: int
    period: str
    task_type: str
    title: str
    status: str
    # could_not_verify is rendered honestly by the cockpit; verified_at present =
    # agent reviewed; status == COMPLETE = human completed (the two progress axes).
    verified_at: str | None = None
    verification_message: str | None = None
    due_date: str | None = None
    priority: str | None = None


class CloseRunsResponse(BaseModel):
    company_id: str
    period: str | None = None
    runs: list[CloseRunDTO] = Field(default_factory=list)
    close_tasks: list[CloseTaskDTO] = Field(default_factory=list)
    # Run-trace-orphaned findings (nightly REVIEW_CHECK proposals that stamp no
    # agent_run_id). Surfaced honestly in their own bucket with link_state=NO_DATA —
    # never silently dropped, never folded into a run as a fabricated success.
    unlinked_findings: list[CloseRunFindingDTO] = Field(default_factory=list)
    # PLAN-EXEMPT: close-cockpit coherence AC-3 — period-lock surfaced to the cockpit so the
    # progress panel shows the same Locked affordance the checklist's /close-tasks does. The
    # lock has ONE source (FinancialCalendar); both panels read it, neither invents it.
    period_locked: bool = False
    locked_by: str | None = None


def _agent_scope_for_company(db: Session, company_id: str) -> tuple[Organization | None, OrganizationCompany | None]:
    organization = organization_for_company(db, company_id)
    if not organization:
        return None, None
    organization_company = organization_company_for_realm(
        db,
        organization_id=organization.id,
        realm_id=company_id,
    )
    return organization, organization_company


def _scoped_agent_run_query(db: Session, company_id: str, organization: Organization | None):
    query = db.query(AgentRun).filter(AgentRun.company_id == company_id)
    if organization:
        query = query.filter(or_(AgentRun.organization_id == organization.id, AgentRun.organization_id.is_(None)))
    else:
        query = query.filter(AgentRun.organization_id.is_(None))
    return query


def _scoped_artifact_query(db: Session, company_id: str, organization: Organization | None):
    query = db.query(InsightArtifact).filter(InsightArtifact.company_id == company_id)
    if organization:
        query = query.filter(
            or_(InsightArtifact.organization_id == organization.id, InsightArtifact.organization_id.is_(None))
        )
    else:
        query = query.filter(InsightArtifact.organization_id.is_(None))
    return query


def _scoped_objective_query(db: Session, company_id: str, organization: Organization | None):
    query = db.query(AgentObjective).filter(AgentObjective.company_id == company_id)
    if organization:
        query = query.filter(or_(AgentObjective.organization_id == organization.id, AgentObjective.organization_id.is_(None)))
    else:
        query = query.filter(AgentObjective.organization_id.is_(None))
    return query


def _assert_agent_row_scope(row: Any, organization: Organization | None, organization_company: OrganizationCompany | None) -> None:
    if not organization:
        if getattr(row, "organization_id", None) is not None:
            raise HTTPException(status_code=404, detail="Agent resource not found")
        return
    row_org_id = getattr(row, "organization_id", None)
    row_org_company_id = getattr(row, "organization_company_id", None)
    if row_org_id not in (None, organization.id):
        raise HTTPException(status_code=404, detail="Agent resource not found")
    if organization_company and row_org_company_id not in (None, organization_company.id):
        raise HTTPException(status_code=404, detail="Agent resource not found")


def _run_to_dict(run: AgentRun, include_steps: bool = False, steps: List[AgentStep] | None = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": run.id,
        "organization_id": run.organization_id,
        "organization_company_id": run.organization_company_id,
        "company_id": run.company_id,
        "user_id": run.user_id,
        "objective_id": run.objective_id,
        "conversation_id": run.conversation_id,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "model_provider": run.model_provider,
        "model_name": run.model_name,
        "prompt_version": run.prompt_version,
        "input_summary": run.input_summary,
        "output_summary": run.output_summary,
        "token_usage": run.token_usage,
        "cost_estimate": float(run.cost_estimate) if run.cost_estimate is not None else None,
        "error": run.error,
        "permission_scope": run.permission_scope,
        "request_id": run.request_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
    from sqlalchemy.orm import object_session
    from ..services.agent_objective_dispatch import occurrence_payload
    db = object_session(run)
    occurrence = db.query(AgentObjectiveOccurrence).filter(AgentObjectiveOccurrence.run_id == run.id, AgentObjectiveOccurrence.company_id == run.company_id).first() if db else None
    data["occurrence"] = occurrence_payload(occurrence)
    if include_steps:
        data["steps"] = [
            {
                "id": step.id,
                "step_type": step.step_type,
                "tool_name": step.tool_name,
                "status": step.status,
                "latency_ms": step.latency_ms,
                "input": step.input,
                "output": step.output,
                "error": step.error,
                "started_at": step.started_at.isoformat() if step.started_at else None,
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
            }
            for step in (steps or [])
        ]
    return data


def _objective_to_dict(objective: AgentObjective) -> Dict[str, Any]:
    from sqlalchemy.orm import object_session
    from ..services.agent_objective_dispatch import effective_policy, occurrence_payload
    db = object_session(objective)
    latest = db.query(AgentObjectiveOccurrence).filter(AgentObjectiveOccurrence.source_objective_id == objective.id, AgentObjectiveOccurrence.company_id == objective.company_id).order_by(AgentObjectiveOccurrence.created_at.desc(), AgentObjectiveOccurrence.id.desc()).first() if db else None
    return {
        "execution_mode": "fixed_runner",
        "instructions_role": "notes",
        "effective_policy": effective_policy(objective),
        "latest_occurrence": occurrence_payload(latest),
        "id": objective.id,
        "organization_id": objective.organization_id,
        "organization_company_id": objective.organization_company_id,
        "company_id": objective.company_id,
        "created_by_user_id": objective.created_by_user_id,
        "name": objective.name,
        "objective_type": objective.objective_type,
        "instructions": objective.instructions,
        "schedule": objective.schedule,
        "enabled": objective.enabled,
        "last_run_at": objective.last_run_at.isoformat() if objective.last_run_at else None,
        "next_run_at": objective.next_run_at.isoformat() if objective.next_run_at else None,
        "permission_scope": objective.permission_scope,
        "approval_policy": objective.approval_policy,
        "notification_policy": objective.notification_policy,
        "created_at": objective.created_at.isoformat() if objective.created_at else None,
        "updated_at": objective.updated_at.isoformat() if objective.updated_at else None,
    }


def _artifact_to_dict(artifact: InsightArtifact) -> Dict[str, Any]:
    return {
        "id": artifact.id,
        "organization_id": artifact.organization_id,
        "organization_company_id": artifact.organization_company_id,
        "company_id": artifact.company_id,
        "agent_run_id": artifact.agent_run_id,
        "title": artifact.title,
        "summary": artifact.summary,
        "severity": artifact.severity,
        "metric": artifact.metric,
        "period": artifact.period,
        "source_refs": artifact.source_refs or [],
        "evidence": artifact.evidence or [],
        "recommendations": artifact.recommendations or [],
        "confidence": float(artifact.confidence) if artifact.confidence is not None else None,
        "data_freshness_at": artifact.data_freshness_at.isoformat() if artifact.data_freshness_at else None,
        "status": artifact.status,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
    }


def _run_with_steps_to_dict(run: AgentRun, steps: List[AgentStep]) -> Dict[str, Any]:
    return _run_to_dict(run, include_steps=True, steps=steps)


@router.get("/eval-fixtures")
def list_eval_fixtures(
    current_user: User = Depends(get_current_user_from_token),
):
    return [
        {
            "id": fixture.id,
            "question": fixture.question,
            "expected_tools": fixture.expected_tools,
            "expected_terms": fixture.expected_terms,
            "source_markers": fixture.source_markers,
            "max_tool_calls": fixture.max_tool_calls,
        }
        for fixture in DEFAULT_AGENT_EVALS
    ]


@router.get("/eval-run-export")
def export_eval_runs(
    company_id: str = Query(...),
    limit: int = Query(25, ge=1, le=100),
    fixture_only: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    organization, _organization_company = _agent_scope_for_company(db, company_id)
    runs = (
        _scoped_agent_run_query(db, company_id, organization)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .all()
    )

    exported_runs = []
    for run in runs:
        steps = (
            db.query(AgentStep)
            .filter(AgentStep.agent_run_id == run.id)
            .order_by(AgentStep.created_at.asc(), AgentStep.id.asc())
            .all()
        )
        exported_runs.append(_run_with_steps_to_dict(run, steps))

    eval_records = eval_records_from_agent_runs(exported_runs)
    if fixture_only:
        matched_ids = {record["run_id"] for record in eval_records if record.get("run_id") is not None}
        exported_runs = [run for run in exported_runs if run["id"] in matched_ids]

    return {
        "company_id": company_id,
        "runs": exported_runs,
        "eval_records": eval_records,
    }


@router.get("/insight-artifacts")
def list_insight_artifacts(
    company_id: str = Query(...),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    organization, _organization_company = _agent_scope_for_company(db, company_id)
    query = _scoped_artifact_query(db, company_id, organization)
    if status:
        query = query.filter(InsightArtifact.status == status)
    artifacts = query.order_by(InsightArtifact.created_at.desc()).limit(limit).all()
    return [_artifact_to_dict(artifact) for artifact in artifacts]


@router.patch("/insight-artifacts/{artifact_id}/status")
def update_insight_artifact_status(
    artifact_id: int,
    body: InsightArtifactStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    artifact = check_entity_tenant_access(
        "ANALYST",
        db=db,
        user=current_user,
        model=InsightArtifact,
        entity_id=artifact_id,
        not_found_detail="Insight artifact not found",
    )
    organization, organization_company = _agent_scope_for_company(db, artifact.company_id)
    _assert_agent_row_scope(artifact, organization, organization_company)
    artifact.status = body.status
    db.commit()
    db.refresh(artifact)
    return _artifact_to_dict(artifact)


@router.get("/objectives")
def list_objectives(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    organization, _organization_company = _agent_scope_for_company(db, company_id)
    objectives = (
        _scoped_objective_query(db, company_id, organization)
        .order_by(AgentObjective.created_at.desc())
        .all()
    )
    return [_objective_to_dict(objective) for objective in objectives]


@router.post("/objectives")
def create_objective(
    body: ObjectiveCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    access = check_tenant_access("EDITOR", company_id=body.company_id, db=db, user=current_user)
    organization, organization_company = _agent_scope_for_company(db, body.company_id)
    if body.objective_type not in SUPPORTED_OBJECTIVE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported objective_type '{body.objective_type}'")
    try:
        schedule = normalize_objective_schedule(body.schedule)
    except ValueError as exc:
        raise user_safe_http_exception(
            status_code=400,
            error_code="agent_objective_schedule_invalid",
            user_message="The objective schedule is invalid. Use a supported frequency and valid UTC time fields.",
            exc=exc,
            retryable=False,
            log_context="agent_objective_schedule_invalid",
        )
    permission_scope = dict(body.permission_scope or {})
    permission_scope["role"] = access.get("role")
    permission_scope["writeback"] = "proposal_only"
    if organization:
        permission_scope["organization_id"] = organization.id
    if organization_company:
        permission_scope["organization_company_id"] = organization_company.id
        permission_scope["realm_id"] = organization_company.realm_id

    objective = AgentObjective(
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        company_id=body.company_id,
        created_by_user_id=current_user.id,
        name=body.name,
        objective_type=body.objective_type,
        instructions=body.instructions,
        schedule=schedule,
        enabled=body.enabled,
        next_run_at=compute_next_run_at(schedule) if body.enabled else None,
        permission_scope=permission_scope,
        approval_policy=body.approval_policy,
        notification_policy=body.notification_policy,
    )
    db.add(objective)
    db.commit()
    db.refresh(objective)
    return _objective_to_dict(objective)


@router.patch("/objectives/{objective_id}")
def update_objective(
    objective_id: int,
    body: ObjectiveUpdate,
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    objective = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=AgentObjective,
        entity_id=objective_id,
        not_found_detail="Agent objective not found",
    )
    organization, organization_company = _agent_scope_for_company(db, objective.company_id)
    _assert_agent_row_scope(objective, organization, organization_company)
    if objective.company_id != company_id:
        raise HTTPException(status_code=404, detail="Agent objective not found")

    update_data = body.model_dump(exclude_unset=True)
    if "schedule" in update_data and update_data["schedule"] is not None:
        try:
            update_data["schedule"] = normalize_objective_schedule(update_data["schedule"])
        except ValueError as exc:
            raise user_safe_http_exception(
                status_code=400,
                error_code="agent_objective_schedule_invalid",
                user_message="The objective schedule is invalid. Use a supported frequency and valid UTC time fields.",
                exc=exc,
                retryable=False,
                log_context="agent_objective_schedule_invalid",
            )

    for field, value in update_data.items():
        setattr(objective, field, value)

    if "schedule" in update_data or "enabled" in update_data:
        objective.next_run_at = compute_next_run_at(objective.schedule) if objective.enabled else None

    db.commit()
    db.refresh(objective)
    return _objective_to_dict(objective)


@router.delete("/objectives/{objective_id}")
def delete_objective(
    objective_id: int,
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    objective = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=AgentObjective,
        entity_id=objective_id,
        not_found_detail="Agent objective not found",
    )
    organization, organization_company = _agent_scope_for_company(db, objective.company_id)
    _assert_agent_row_scope(objective, organization, organization_company)
    if objective.company_id != company_id:
        raise HTTPException(status_code=404, detail="Agent objective not found")

    from ..services.agent_objective_dispatch import active_occurrence, OccurrenceConflict
    objective = db.query(AgentObjective).filter(AgentObjective.id == objective_id).with_for_update().first()
    active = active_occurrence(db, objective_id)
    if active:
        raise HTTPException(status_code=409, detail=OccurrenceConflict(active).detail)
    db.delete(objective)
    db.commit()
    return {"status": "deleted", "id": objective_id}


@router.post("/objectives/{objective_id}/run-now")
def run_objective_now(
    objective_id: int,
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    objective = check_entity_tenant_access(
        "EDITOR",
        db=db,
        user=current_user,
        model=AgentObjective,
        entity_id=objective_id,
        not_found_detail="Agent objective not found",
    )
    organization, organization_company = _agent_scope_for_company(db, objective.company_id)
    _assert_agent_row_scope(objective, organization, organization_company)
    if objective.company_id != company_id:
        raise HTTPException(status_code=404, detail="Agent objective not found")

    import uuid
    from ..tasks.agent import _execute_objective
    from ..services.agent_objective_dispatch import OccurrenceConflict, execute_occurrence, reserve_occurrence
    try:
        row = reserve_occurrence(db, objective_id, manual_key=idempotency_key if isinstance(idempotency_key, str) else str(uuid.uuid4()))
        if row.status in ("running", "recovery_required"):
            raise OccurrenceConflict(row)
        result = execute_occurrence(db, row.id, _execute_objective)
    except OccurrenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)}) from exc
    db.refresh(objective)
    return {
        **result, "id": objective_id,
        "last_run_at": objective.last_run_at.isoformat() if objective.last_run_at else None,
        "next_run_at": objective.next_run_at.isoformat() if objective.next_run_at else None,
    }


@router.post("/objectives/import-report-schedules")
def import_report_schedules(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    access = check_tenant_access("EDITOR", company_id=company_id, db=db, user=current_user)
    organization, organization_company = _agent_scope_for_company(db, company_id)
    schedules = db.query(ReportSchedule).filter(ReportSchedule.company_id == company_id).all()
    imported = 0
    skipped = 0
    existing_source_ids = {
        (objective.permission_scope or {}).get("source_report_schedule_id")
        for objective in _scoped_objective_query(db, company_id, organization)
        .filter(AgentObjective.objective_type == "report_pack")
        .all()
    }

    for schedule in schedules:
        if str(schedule.id) in existing_source_ids:
            skipped += 1
            continue

        frequency = (schedule.frequency or "MONTHLY").upper()
        objective_schedule: Dict[str, Any] = {
            "frequency": frequency,
            "hour_utc": schedule.hour_utc or 8,
            "minute_utc": 0,
            "timezone": "UTC",
        }
        if frequency == "WEEKLY":
            objective_schedule["day_of_week"] = schedule.day_of_week if schedule.day_of_week is not None else 0
        if frequency in ("MONTHLY", "QUARTERLY"):
            objective_schedule["frequency"] = "MONTHLY"
            objective_schedule["day_of_month"] = schedule.day_of_month or 1

        normalized_schedule = normalize_objective_schedule(objective_schedule)
        objective = AgentObjective(
            organization_id=organization.id if organization else None,
            organization_company_id=organization_company.id if organization_company else None,
            company_id=company_id,
            created_by_user_id=current_user.id,
            name=f"Report Pack: {schedule.name}",
            objective_type="report_pack",
            instructions=f"Imported from report schedule #{schedule.id}.",
            schedule=normalized_schedule,
            enabled=bool(schedule.enabled),
            next_run_at=compute_next_run_at(normalized_schedule) if schedule.enabled else None,
            notification_policy={
                "email": True,
                "recipients": schedule.recipients or [],
                "report_format": schedule.report_format or "pdf",
            },
            approval_policy={"external_writes": "never"},
            permission_scope={
                "source_report_schedule_id": str(schedule.id),
                "role": access.get("role"),
                "writeback": "proposal_only",
                "organization_id": organization.id if organization else None,
                "organization_company_id": organization_company.id if organization_company else None,
                "realm_id": organization_company.realm_id if organization_company else company_id,
            },
        )
        db.add(objective)
        existing_source_ids.add(str(schedule.id))
        imported += 1

    db.commit()
    return {"status": "done", "imported": imported, "skipped": skipped}


@router.get("/runs")
def list_agent_runs(
    company_id: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    organization, _organization_company = _agent_scope_for_company(db, company_id)
    runs = (
        _scoped_agent_run_query(db, company_id, organization)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_run_to_dict(run) for run in runs]


@router.get("/runs/{run_id}")
def get_agent_run(
    run_id: int,
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    run = check_entity_tenant_access(
        "VIEWER",
        db=db,
        user=current_user,
        model=AgentRun,
        entity_id=run_id,
        not_found_detail="Agent run not found",
    )
    organization, organization_company = _agent_scope_for_company(db, run.company_id)
    _assert_agent_row_scope(run, organization, organization_company)
    if run.company_id != company_id:
        raise HTTPException(status_code=404, detail="Agent run not found")

    steps = (
        db.query(AgentStep)
        .filter(AgentStep.agent_run_id == run.id)
        .order_by(AgentStep.created_at.asc(), AgentStep.id.asc())
        .all()
    )
    return _run_to_dict(run, include_steps=True, steps=steps)


# ── Close-runs read (Phase D supervisor cockpit) ──────────────────────────────
def _finding_to_dto(action: ProposedAction, *, link_state: str) -> CloseRunFindingDTO:
    """Map a close-review ProposedAction to a finding DTO. `link_state` is supplied
    by the caller (LINKED when the finding carries an agent_run_id, NO_DATA when it
    is run-trace-orphaned) — the field is the honest signal the cockpit renders, so
    an orphaned nightly finding is never shown as if it had a run trace."""
    payload = action.payload if isinstance(action.payload, dict) else {}
    return CloseRunFindingDTO(
        id=action.id,
        revision=action.revision,
        action_type=action.action_type,
        status=action.status,
        summary=payload.get("summary"),
        severity=payload.get("severity"),
        message=payload.get("message"),
        check_id=payload.get("check_id"),
        period=payload.get("period"),
        confidence_score=float(action.confidence_score) if action.confidence_score is not None else None,
        close_task_id=action.close_task_id,
        agent_run_id=action.agent_run_id,
        step_index=action.step_index,
        evidence=payload.get("evidence") or payload.get("finding"),
        created_at=action.created_at.isoformat() if action.created_at else None,
        link_state=link_state,
    )


# PLAN-EXEMPT: close-cockpit coherence AC-1 — period helper; reads the finding's period from its
# deserialized JSON payload (ProposedAction.payload is generic JSON — no JSONB ->>/.astext operator).
def _finding_period(action: ProposedAction) -> str | None:
    """The period a close-review finding belongs to.

    REVIEW_CHECK stores it at payload["period"]; CLOSE_STEP_REVIEW nests it under
    payload["close_task"]["period"]. Read in Python because the payload is a dict here.
    """
    payload = action.payload if isinstance(action.payload, dict) else {}
    direct = payload.get("period")
    if direct is not None:
        return direct
    close_task = payload.get("close_task")
    return close_task.get("period") if isinstance(close_task, dict) else None


@router.get(
    "/close-runs",
    response_model=CloseRunsResponse,
    dependencies=[Depends(global_tenant_guard)],
)
def list_close_runs(
    company_id: str = Query(...),
    period: str | None = Query(None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CloseRunsResponse:
    """Tenant-scoped read of close-and-review runs for the supervisor cockpit.

    Joins, for the realm (optionally filtered to a period):
      AgentRun (via the shared ``_scoped_agent_run_query``)
        → AgentStep (the run trace)
        → per-run close-review findings (ProposedAction.agent_run_id — the close
          driver stamps it, close_orchestration.py:524)
      + CloseTask status / verified_at (the two-axis progress source)

    HONEST DEGRADE: the nightly REVIEW_CHECK proposer (proactive.py:347) stamps
    NEITHER close_task_id NOR agent_run_id, so those findings are run-trace-orphaned.
    They are NOT silently dropped and NOT folded into a run as a fabricated success —
    they surface in ``unlinked_findings`` with ``link_state="NO_DATA"`` so the cockpit
    renders them as a distinct could-not-link state.

    Authorization: the router is mounted with ``global_tenant_guard`` (main.py:422),
    re-declared on this decorator; ``get_current_user_from_token`` authenticates and
    the in-handler ``check_tenant_access("VIEWER")`` is the membership-gated boundary.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    organization, _organization_company = _agent_scope_for_company(db, company_id)

    runs_query = (
        _scoped_agent_run_query(db, company_id, organization)
        .join(AgentObjective, AgentObjective.id == AgentRun.objective_id)
        .filter(AgentObjective.objective_type == CLOSE_OBJECTIVE_TYPE)
    )
    if period:
        runs_query = runs_query.filter(
            AgentRun.request_id == close_run_request_id(company_id, period)
        )
    runs = runs_query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit).all()
    run_ids = [run.id for run in runs]

    # Close-review findings for this realm, batched (no N+1): everything that is a
    # close-review action_type for the company, then indexed by agent_run_id.
    findings_query = db.query(ProposedAction).filter(
        ProposedAction.company_id == company_id,
        or_(
            ProposedAction.action_type.in_(("REVIEW_CHECK", "CLOSE_STEP_REVIEW")),
            ProposedAction.agent_run_id.in_(run_ids),
        ),
    )
    # PLAN-EXEMPT: close-cockpit coherence AC-1 — period-scope the findings in Python, not SQL.
    # ProposedAction.payload is a generic JSON column (models.py) with no JSONB ->>/.astext
    # operator, so the prior `payload["period"].astext == period` SQL raised AttributeError the
    # instant a period was passed (latent until AC-1 began passing one). _finding_period reads the
    # period off the deserialized dict, matching both the REVIEW_CHECK and CLOSE_STEP_REVIEW shapes.
    all_findings = findings_query.order_by(ProposedAction.created_at.desc()).all()

    findings_by_run: dict[int, list[ProposedAction]] = {}
    unlinked: list[CloseRunFindingDTO] = []
    for action in all_findings:
        if action.agent_run_id is not None:
            if action.agent_run_id in run_ids:
                findings_by_run.setdefault(action.agent_run_id, []).append(action)
        else:
            if period and _finding_period(action) != period:
                continue
            # Run-trace-orphaned (nightly REVIEW_CHECK) — degrade honestly to NO_DATA.
            unlinked.append(_finding_to_dto(action, link_state="NO_DATA"))

    # Steps for the runs in scope, batched and grouped by run. AgentStep has no
    # tenant column of its own; it is transitively scoped — run_ids come ONLY from
    # the tenant-scoped _scoped_agent_run_query above, so a step can only belong to
    # an in-tenant run.
    steps_by_run: dict[int, list[AgentStep]] = {}
    if run_ids:
        step_rows = (
            # CROSS-TENANT-OK: AgentStep has no tenant column; scoped transitively via run_ids from the tenant-scoped run query above
            db.query(AgentStep)
            .filter(AgentStep.agent_run_id.in_(run_ids))
            .order_by(AgentStep.created_at.asc(), AgentStep.id.asc())
            .all()
        )
        for step in step_rows:
            steps_by_run.setdefault(step.agent_run_id, []).append(step)

    run_dtos: list[CloseRunDTO] = []
    for run in runs:
        # SA-OK: bounded-loop in-memory dict.get index of already-batched findings/steps (no DB query in this loop)
        run_findings = [
            _finding_to_dto(action, link_state="LINKED")
            for action in findings_by_run.get(run.id, [])
        ]
        run_steps = [
            CloseRunStepDTO(
                id=step.id,
                step_type=step.step_type,
                tool_name=step.tool_name,
                status=step.status,
                latency_ms=step.latency_ms,
                error=("Close step failed; retry or review the run." if step.error else None),
                started_at=step.started_at.isoformat() if step.started_at else None,
                finished_at=step.finished_at.isoformat() if step.finished_at else None,
            )
            for step in steps_by_run.get(run.id, [])
        ]
        run_dtos.append(
            # PLAN-EXEMPT: ratified plan 20260702-qa-batch2 B2-4 AC-3 (app/routers/agent.py in scope).
            CloseRunDTO(
                id=run.id,
                company_id=run.company_id,
                objective_id=run.objective_id,
                trigger_type=run.trigger_type,
                status=run.status,
                period=close_period_from_run(run),
                retryable=run.status in {"failed", "failed_stale"},
                output_summary=run.output_summary,
                error=("Close preparation failed. Retry the start action." if run.error else None),
                started_at=run.started_at.isoformat() if run.started_at else None,
                finished_at=run.finished_at.isoformat() if run.finished_at else None,
                created_at=run.created_at.isoformat() if run.created_at else None,
                steps=run_steps,
                findings=run_findings,
            )
        )

    # CloseTask status / verified_at for the realm (optionally the period). CloseTask
    # is realm-keyed (realm_id); the realm == company_id, so this filters on the
    # tenant column.
    close_tasks_query = db.query(CloseTask).filter(CloseTask.realm_id == company_id)
    if period:
        close_tasks_query = close_tasks_query.filter(CloseTask.period == period)
    close_task_rows = close_tasks_query.order_by(CloseTask.sort_order.asc(), CloseTask.id.asc()).all()
    close_task_dtos = [
        CloseTaskDTO(
            id=task.id,
            period=task.period,
            task_type=task.task_type,
            title=task.title,
            status=task.status,
            verified_at=task.verified_at.isoformat() if task.verified_at else None,
            verification_message=task.verification_message,
            due_date=task.due_date.isoformat() if task.due_date else None,
            priority=task.priority,
        )
        for task in close_task_rows
    ]

    # PLAN-EXEMPT: close-cockpit coherence AC-3 — read the period lock from its single source
    # (FinancialCalendar), realm-scoped exactly as the checklist's /close-tasks endpoint does,
    # so list_close_runs can surface the same locked / locked_by to the progress panel. Only a
    # specific period can be locked, so this is a no-op when no period is in scope.
    period_locked = False
    locked_by: str | None = None
    if period:
        calendar = (
            db.query(FinancialCalendar)
            .filter(
                FinancialCalendar.realm_id == company_id,
                FinancialCalendar.period == period,
            )
            .first()
        )
        if calendar:
            period_locked = bool(calendar.locked)
            locked_by = calendar.locked_by

    return CloseRunsResponse(
        company_id=company_id,
        period=period,
        runs=run_dtos,
        close_tasks=close_task_dtos,
        unlinked_findings=unlinked,
        period_locked=period_locked,
        locked_by=locked_by,
    )


@router.post(
    "/close-runs/start",
    response_model=StartCloseRunResponse,
    status_code=202,
    dependencies=[Depends(global_tenant_guard)],
)
def start_close_run_endpoint(
    company_id: str = Query(...),
    period: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> StartCloseRunResponse:
    """ACCOUNTING-gated trigger for one period-pinned close-orchestration run.

    THIN handler — the find-or-create, the commit, and the background dispatch all
    live in `start_close_run` (app/services/close_run_start.py); this handler does
    NO ORM access and NO math (AP-LAYER-02/03). The membership-gated ACCOUNTING check is
    the tenant boundary (`global_tenant_guard` on the decorator is defense-in-depth,
    not the sole gate). Returns 202 (accepted; the run executes in the worker).
    """
    try:
        check_tenant_access("ACCOUNTING", company_id=company_id, db=db, user=current_user)
    except HTTPException as exc:
        if exc.status_code == 403 and "do not have access" in str(exc.detail):
            raise HTTPException(status_code=404, detail="Resource not found") from exc
        raise
    try:
        result = start_close_run(
            db,
            company_id=company_id,
            period=period,
            principal=current_user,
        )
    except CloseRunEnqueueError as exc:
        raise user_safe_http_exception(
            status_code=503,
            error_code="close_run_enqueue_failed",
            user_message="Close preparation could not be queued. Retry the start action.",
            exc=exc,
            retryable=True,
            support_hint=f"Durable run {exc.result.run_id} was recorded as failed.",
            log_context="close_run_start",
        ) from exc
    except CloseRunScopeError as exc:
        raise user_safe_http_exception(
            status_code=404,
            error_code="company_not_found",
            user_message="Company not found.",
            exc=exc,
            retryable=False,
            log_context="close_run_start_scope",
        ) from exc
    except CloseRunBeforeStartError as exc:
        raise user_safe_http_exception(
            status_code=409,
            error_code="period_before_close_start",
            user_message=exc.user_message,
            retryable=False,
            log_context="close_run_start_before_boundary",
        ) from exc
    return StartCloseRunResponse(**result.__dict__)


def _close_start_response(
    company_id: str, setting: CloseStartSetting, *, can_edit: bool
) -> CloseStartPeriodResponse:
    return CloseStartPeriodResponse(
        company_id=company_id,
        close_start_period=setting.explicit_period,
        effective_close_start_period=setting.effective_period,
        connected_period=setting.connected_period,
        source=setting.source,
        can_edit=can_edit,
    )


def _is_tenant_admin(access: dict) -> bool:
    role = str(access.get("role") or "").upper()
    return ROLE_WEIGHTS.get(role, 0) >= ROLE_WEIGHTS["TENANT_ADMIN"]


@router.get(
    "/close-start-period",
    response_model=CloseStartPeriodResponse,
    dependencies=[Depends(global_tenant_guard)],
)
def get_close_start_period(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CloseStartPeriodResponse:
    """The first period this platform closes for the company (VIEWER read).

    ``can_edit`` tells the cockpit whether to offer the TenantAdmin control.
    """
    access = check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    _organization, organization_company = _agent_scope_for_company(db, company_id)
    try:
        setting = close_start_setting_for_company(organization_company)
    except CloseRunScopeError as exc:
        raise user_safe_http_exception(
            status_code=404, error_code="company_not_found", user_message="Company not found.",
            exc=exc, log_context="close_start_period_scope",
        ) from exc
    return _close_start_response(company_id, setting, can_edit=_is_tenant_admin(access))


@router.put(
    "/close-start-period",
    response_model=CloseStartPeriodResponse,
    dependencies=[Depends(global_tenant_guard)],
)
def update_close_start_period(
    body: CloseStartPeriodUpdate,
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> CloseStartPeriodResponse:
    """TenantAdmin-gated, audited write of the company's close start period.

    A YYYY-MM month that is not in the future, or null to return to the default
    (the month before the company was connected). Close checklist generation and
    the close run refuse any earlier period.
    """
    check_tenant_access("TENANT_ADMIN", company_id=company_id, db=db, user=current_user)
    _organization, organization_company = _agent_scope_for_company(db, company_id)
    if organization_company is None:
        raise user_safe_http_exception(
            status_code=404, error_code="company_not_found", user_message="Company not found.",
            log_context="close_start_period_scope",
        )
    previous = organization_company.close_start_period
    try:
        setting = set_close_start_period(organization_company, body.close_start_period)
    except CloseStartPeriodError as exc:
        db.rollback()
        raise user_safe_http_exception(
            status_code=422, error_code="invalid_close_start_period", user_message=exc.user_message,
            log_context="close_start_period_invalid",
        ) from exc
    db.add(
        AuditEntry(
            company_id=company_id,
            user_id=str(current_user.id),
            user_email=current_user.email,
            action="SET_CLOSE_START_PERIOD",
            entity_type="organization_company",
            entity_id=str(organization_company.id),
            details={
                "previous_close_start_period": previous,
                "close_start_period": setting.explicit_period,
                "effective_close_start_period": setting.effective_period,
            },
        )
    )
    db.commit()
    return _close_start_response(company_id, setting, can_edit=True)


# ══════════════════════════════════════════════════════════════════════════════
# Agent-authored checkpointed plans (WF-12 — plan 20260714-wave3-1-agent-plans,
# chunk C). THIN handlers: every gate + transition lives in
# app/services/agent_plans.py (the service tenant-asserts via the session-derived
# checks in its own body; the decorator/mount global_tenant_guard is defense in
# depth, never the sole gate). Deliberately NO DELETE route — cancel is the
# terminal human control (decision 8). Reads are VIEWER; cancel/pause/resume are
# EDITOR. The ORM rows never reach the client: response_model DTOs below filter
# them (steps_hash and organization_id are deliberately NOT exposed).
# PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md)
# ══════════════════════════════════════════════════════════════════════════════

class AgentPlanOut(BaseModel):
    """Wire contract for one plan (list + detail header). steps_hash (the E3
    tamper seal) and organization_id are internal — deliberately excluded."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    realm_id: str
    title: str
    goal: str
    status: str
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentPlanStepOut(BaseModel):
    """Wire contract for one plan step (detail view — statuses included)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    step_index: int
    tool_name: str
    tool_input: dict = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)
    is_write: bool
    status: str
    attempts: int
    proposed_action_id: int | None = None
    evidence: str | None = None


class AgentPlanDetailOut(AgentPlanOut):
    steps: list[AgentPlanStepOut] = Field(default_factory=list)


class AgentPlanTransitionOut(BaseModel):
    """Outcome of a cancel/pause/resume claim. ``expired_step_proposals`` is
    populated by cancel only (the decision-8 expiry count); ``walk_enqueued``
    by resume only."""
    id: int
    status: str
    expired_step_proposals: int | None = None
    walk_enqueued: bool | None = None


def _map_plan_error(exc: AgentPlanError) -> HTTPException:
    """AgentPlanError (domain) → the one envelope (api-contract AP-LAYER-04)."""
    return user_safe_http_exception(
        status_code=exc.status,
        error_code=exc.error_code,
        user_message=str(exc),
        exc=exc,
        log_context=f"agent_plan_{exc.error_code}",
    )


@router.get("/plans", response_model=list[AgentPlanOut], dependencies=[Depends(global_tenant_guard)])
def list_agent_plans(
    company_id: str = Query(...),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> list[AgentPlanOut]:
    """The realm's agent plans, newest first (VIEWER; the service membership-gates
    via check_tenant_access and derives the realm+org filters from the authorized
    scope — the decorator guard is defense in depth, never the sole gate)."""
    plans = agent_plans_service.list_plans(db, current_user, company_id, status=status, limit=limit)
    return [AgentPlanOut.model_validate(plan) for plan in plans]


@router.get("/plans/{plan_id}", response_model=AgentPlanDetailOut, dependencies=[Depends(global_tenant_guard)])
def get_agent_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> AgentPlanDetailOut:
    """One plan + its ordered steps and statuses (VIEWER; the service resolves the
    row's owning tenant via check_entity_tenant_access — cross-tenant reads 404,
    existence-hiding)."""
    plan, steps = agent_plans_service.get_plan(db, current_user, plan_id)
    return AgentPlanDetailOut(
        **AgentPlanOut.model_validate(plan).model_dump(),
        steps=[AgentPlanStepOut.model_validate(step) for step in steps],
    )


@router.post("/plans/{plan_id}/cancel", response_model=AgentPlanTransitionOut, dependencies=[Depends(global_tenant_guard)])
def cancel_agent_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> AgentPlanTransitionOut:
    """Cancel a plan (EDITOR). Decision-8 ordering inside the service: expire
    the plan's PENDING step proposals FIRST, then claim-flip CANCELLED; a lost
    claim surfaces as the 409 conflict envelope. The in-handler EDITOR gate
    below is the census-visible assert; the service re-asserts on its own
    entry point (defense in depth — the guard dependency is never the sole gate)."""
    # PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md)
    check_entity_tenant_access(
        "EDITOR", db=db, user=current_user, model=agent_plans_service.AgentPlan,
        entity_id=plan_id, tenant_attr="realm_id", not_found_detail="Agent plan not found",
    )
    try:
        plan, expired_count = agent_plans_service.cancel_plan(db, current_user, plan_id)
    except AgentPlanError as exc:
        raise _map_plan_error(exc) from exc
    return AgentPlanTransitionOut(id=plan.id, status=plan.status, expired_step_proposals=expired_count)


@router.post("/plans/{plan_id}/pause", response_model=AgentPlanTransitionOut, dependencies=[Depends(global_tenant_guard)])
def pause_agent_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> AgentPlanTransitionOut:
    """Pause an ACTIVE plan (EDITOR; claim-style — a lost claim is the 409
    envelope). In-handler EDITOR gate + the service's own re-assert (defense in
    depth — the guard dependency is never the sole gate)."""
    # PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md)
    check_entity_tenant_access(
        "EDITOR", db=db, user=current_user, model=agent_plans_service.AgentPlan,
        entity_id=plan_id, tenant_attr="realm_id", not_found_detail="Agent plan not found",
    )
    try:
        plan = agent_plans_service.pause_plan(db, current_user, plan_id)
    except AgentPlanError as exc:
        raise _map_plan_error(exc) from exc
    return AgentPlanTransitionOut(id=plan.id, status=plan.status)


@router.post("/plans/{plan_id}/resume", response_model=AgentPlanTransitionOut, dependencies=[Depends(global_tenant_guard)])
def resume_agent_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> AgentPlanTransitionOut:
    """Resume a PAUSED plan (EDITOR; claim-style). The service enqueues one
    walk via the pinned decision-10 task seam after the commit. In-handler
    EDITOR gate + the service's own re-assert (defense in depth — the guard
    dependency is never the sole gate)."""
    # PLAN-EXEMPT: chunk C of ratified plan 20260714-wave3-1-agent-plans (marker RATIFICATION-2026-07-14.md)
    check_entity_tenant_access(
        "EDITOR", db=db, user=current_user, model=agent_plans_service.AgentPlan,
        entity_id=plan_id, tenant_attr="realm_id", not_found_detail="Agent plan not found",
    )
    try:
        plan = agent_plans_service.resume_plan(db, current_user, plan_id)
    except AgentPlanError as exc:
        raise _map_plan_error(exc) from exc
    return AgentPlanTransitionOut(id=plan.id, status=plan.status, walk_enqueued=True)

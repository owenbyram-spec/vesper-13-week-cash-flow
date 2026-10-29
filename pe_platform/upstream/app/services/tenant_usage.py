from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import AgentRun, Organization, TenantUsageEvent
from ..tenant_rls import org_grain_read
from .tenant_companies import company_ids_for_organization


def current_month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def token_counts(raw: Optional[dict[str, Any]]) -> tuple[int, int, int]:
    if not isinstance(raw, dict):
        return 0, 0, 0
    input_tokens = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or raw.get("completion_tokens") or raw.get("candidates_tokens") or 0)
    total_tokens = int(raw.get("total_tokens") or input_tokens + output_tokens)
    return input_tokens, output_tokens, total_tokens


def summarize_tenant_usage(
    db: Session,
    *,
    organization_id: int,
    company_id: Optional[str],
    since: Optional[datetime] = None,
) -> dict[str, Any]:
    since = since or current_month_start()

    event_filters = [
        TenantUsageEvent.organization_id == organization_id,
        TenantUsageEvent.occurred_at >= since,
    ]
    if company_id:
        event_filters.append(TenantUsageEvent.company_id == company_id)

    # E-1: the ORG-GRAIN branch only. When the caller supplies company_id the realm
    # conjunct is already appended above (event_filters / agent_filters) and these reads
    # bind normally; this block is entered ONLY when no realm was asked for, because the
    # limit being enforced (TenantBilling.token_limit_monthly) is org-level and the totals
    # must span every company in the org. Scoped to the branch, never the function.
    org_grain = (
        org_grain_read(
            "org-level token quota: TenantBilling.token_limit_monthly is org-scoped, so "
            "these totals must span every company in the organization"
        )
        if company_id is None
        else nullcontext()
    )
    with org_grain:
        event_totals = (
            db.query(
                func.coalesce(func.sum(TenantUsageEvent.input_tokens), 0),
                func.coalesce(func.sum(TenantUsageEvent.output_tokens), 0),
                func.coalesce(func.sum(TenantUsageEvent.total_tokens), 0),
                func.coalesce(func.sum(TenantUsageEvent.cost_estimate), 0),
                func.count(TenantUsageEvent.id),
            )
            .filter(*event_filters)
            .one()
        )

        input_tokens = int(event_totals[0] or 0)
        output_tokens = int(event_totals[1] or 0)
        total_tokens = int(event_totals[2] or 0)
        cost_estimate = Decimal(event_totals[3] or 0)
        event_count = int(event_totals[4] or 0)

        agent_filters = [
            or_(AgentRun.organization_id == organization_id, AgentRun.organization_id.is_(None)),
            AgentRun.created_at >= since,
        ]
        if company_id:
            agent_filters.append(AgentRun.company_id == company_id)
        else:
            organization = db.query(Organization).filter(Organization.id == organization_id).first()
            company_ids = (
                company_ids_for_organization(db, organization)
                if organization
                else company_ids_for_organization(db, organization_id)
            )
            if company_ids:
                agent_filters.append(AgentRun.company_id.in_(company_ids))
            else:
                agent_filters.append(AgentRun.organization_id == organization_id)

        # Materialised, NOT a scalar_subquery. As a subquery this read of tenant_usage_events
        # is nested inside the unledgered-runs statement, so its realm conjunct (already in
        # event_filters) is not a TOP-LEVEL conjunct and therefore bounds nothing on an
        # org-keyed guard table. Issuing it as its own statement makes the same conjunct
        # top-level. The id list is one month of one org's usage events, so materialising it
        # is cheap; an empty list is handled correctly by SQLAlchemy's empty-IN.
        event_run_ids = [
            row[0]
            for row in db.query(TenantUsageEvent.agent_run_id)
            .filter(*event_filters, TenantUsageEvent.agent_run_id.isnot(None))
            .all()
        ]
        agent_run_count = int(
            db.query(func.count(AgentRun.id))
            .filter(*agent_filters)
            .scalar()
            or 0
        )

        unledgered_agent_runs = (
            db.query(AgentRun)
            .filter(*agent_filters)
            .filter(~AgentRun.id.in_(event_run_ids))
            .all()
        )
        for run in unledgered_agent_runs:
            run_input, run_output, run_total = token_counts(run.token_usage)
            input_tokens += run_input
            output_tokens += run_output
            total_tokens += run_total
            cost_estimate += Decimal(run.cost_estimate or 0)

        last_agent_run_at = (
            db.query(func.max(AgentRun.created_at))
            .filter(*agent_filters)
            .scalar()
        )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_estimate": float(cost_estimate),
        "event_count": event_count,
        "agent_run_count": agent_run_count,
        "last_agent_run_at": last_agent_run_at,
    }


def summarize_org_usage(db: Session, org: Organization, since: Optional[datetime] = None) -> dict[str, Any]:
    return summarize_tenant_usage(
        db,
        organization_id=org.id,
        company_id=None,
        since=since,
    )

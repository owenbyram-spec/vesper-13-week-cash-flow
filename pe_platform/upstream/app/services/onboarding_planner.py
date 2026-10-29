from __future__ import annotations

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentObjective, CompanyAgreement, CompanyMemory, CustomKPI, DebtCovenant, Document, QBOAccount
from .agent_schedule import compute_next_run_at, normalize_objective_schedule
from .ai_metric_builder import _fallback_covenant, _fallback_custom_kpi, draft_covenant, draft_custom_kpi
from .context_service import ContextCacheService


logger = logging.getLogger("vesper_bot.onboarding_planner")


def _safe_draft(
    draft_fn: Callable[[str, List[Dict[str, Any]]], Dict[str, Any]],
    fallback_fn: Callable[[str, List[Dict[str, Any]]], Dict[str, Any]],
    prompt: str,
    accounts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run a single draft and never let an LLM failure abort the fan-out.

    draft_fn already returns its local heuristic when the model returns a
    non-dict; this additionally catches provider exceptions (e.g. rate-limit /
    quota errors) so one bad draft falls back to its deterministic heuristic
    instead of failing the whole onboarding plan."""
    try:
        return draft_fn(prompt, accounts)
    except Exception as exc:  # noqa: BLE001 - resilient by design
        logger.warning("[onboarding] draft failed, using local fallback: %s", exc)
        return fallback_fn(prompt, accounts)


def _run_drafts_concurrently(
    jobs: List[Callable[[], Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Execute independent draft jobs on a bounded thread pool, preserving input
    order. The underlying LLM calls are blocking I/O, so threads (not asyncio)
    are the right fan-out primitive here."""
    if not jobs:
        return []
    max_workers = max(1, min(settings.onboarding_draft_max_workers, len(jobs)))
    if max_workers == 1 or len(jobs) == 1:
        return [job() for job in jobs]
    # Copy the caller's context into each worker thread so ContextVars set on the
    # submitting thread (notably the tenant attribution tag/realm triple bound by
    # the agent run) propagate to the blocking lane LLM calls. Without this the
    # workers run with a fresh context and tag/realm stamping silently drops.
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="onboarding-draft") as executor:
        return list(executor.map(lambda job: contextvars.copy_context().run(job), jobs))


DEFAULT_OBJECTIVE_RECIPES: List[Dict[str, Any]] = [
    {
        "name": "Daily Morning Briefing",
        "objective_type": "morning_briefing",
        "instructions": "Prepare the latest executive briefing and send it to configured recipients.",
        "schedule": {"frequency": "DAILY", "hour_utc": 8, "minute_utc": 0, "timezone": "UTC"},
        "rationale": "Creates a recurring executive summary so the team starts each day with the same view of the business.",
    },
    {
        "name": "Cash Watch",
        "objective_type": "cash_watch",
        "instructions": "Analyze the 13-week cash forecast and flag liquidity risk.",
        "schedule": {"frequency": "DAILY", "hour_utc": 13, "minute_utc": 0, "timezone": "UTC"},
        "rationale": "Keeps the company on top of near-term cash risk without waiting for a manual check.",
    },
    {
        "name": "Budget Variance Watch",
        "objective_type": "budget_variance_watch",
        "instructions": "Scan vendor-level budget variance and propose reforecast actions when material.",
        "schedule": {"frequency": "WEEKLY", "hour_utc": 14, "minute_utc": 0, "timezone": "UTC", "day_of_week": 0},
        "rationale": "Turns the budget into an operating habit instead of a month-end lookup.",
    },
]

DEFAULT_KPI_PROMPTS: List[Dict[str, str]] = [
    {
        "prompt": "gross profit as a percentage of revenue",
        "source_hint": "Auto-drafted from the chart of accounts to establish a default profitability KPI.",
    },
    {
        "prompt": "payroll and benefits as a percentage of revenue",
        "source_hint": "Auto-drafted from payroll-related expense accounts to expose labor efficiency.",
    },
    {
        "prompt": "software and subscriptions spend as a percentage of revenue",
        "source_hint": "Auto-drafted from operating expense accounts to highlight recurring tooling cost.",
    },
]

DEFAULT_COVENANT_PROMPTS: List[Dict[str, str]] = [
    {
        "prompt": "debt service coverage ratio at least 1.25",
        "source_hint": "Standard lender covenant pattern applied because debt-related agreements were detected.",
    },
    {
        "prompt": "debt / EBITDA below 4.0",
        "source_hint": "Standard leverage covenant pattern applied because debt-related agreements were detected.",
    },
    {
        "prompt": "current ratio at least 1.2",
        "source_hint": "Standard liquidity covenant pattern applied because debt-related agreements were detected.",
    },
]


def _account_catalog(db: Session, company_id: str) -> List[Dict[str, Any]]:
    rows = db.query(QBOAccount).filter(QBOAccount.realm_id == company_id, QBOAccount.active == True).all()  # noqa: E712
    return [
        {
            "id": row.qbo_id,
            "acct_num": row.acct_num,
            "name": row.name,
            "fully_qualified_name": row.fully_qualified_name,
            "type": row.account_type,
            "sub_type": row.account_sub_type,
            "classification": row.classification,
        }
        for row in rows
    ]


def _has_text_match(accounts: List[Dict[str, Any]], phrases: List[str]) -> bool:
    lowered = " ".join(
        f"{acct.get('name', '')} {acct.get('fully_qualified_name', '')} {acct.get('sub_type', '')}".lower()
        for acct in accounts
    )
    return any(phrase.lower() in lowered for phrase in phrases)


def _select_kpi_prompts(accounts: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not accounts:
        return []
    prompts: List[Dict[str, str]] = []
    if _has_text_match(accounts, ["sales", "income", "revenue"]) and _has_text_match(accounts, ["cost of goods", "cogs"]):
        prompts.append(DEFAULT_KPI_PROMPTS[0])
    if _has_text_match(accounts, ["payroll", "salary", "wages", "benefits"]):
        prompts.append(DEFAULT_KPI_PROMPTS[1])
    if _has_text_match(accounts, ["software", "subscription", "saas", "hosting"]):
        prompts.append(DEFAULT_KPI_PROMPTS[2])
    return prompts


def _select_covenant_prompts(
    accounts: List[Dict[str, Any]],
    documents: List[Document],
    agreements: List[CompanyAgreement],
) -> List[Dict[str, str]]:
    if not accounts:
        return []
    debt_docs = [
        doc for doc in documents
        if doc.document_type in {"loan_agreement", "contract"}
        or "covenants" in (doc.context_modules or [])
        or "cashflow" in (doc.context_modules or [])
    ]
    debt_agreements = [
        agreement for agreement in agreements
        if any(term in (agreement.ai_summary or "").lower() for term in ("loan", "credit", "lender", "covenant"))
        or any(term in (agreement.raw_text or "").lower() for term in ("debt service coverage", "leverage ratio", "current ratio"))
    ]
    if not debt_docs and not debt_agreements:
        return []
    return list(DEFAULT_COVENANT_PROMPTS)


def _assemble_kpi_recommendations(
    prompts: List[Dict[str, str]],
    drafts: List[Dict[str, Any]],
    existing_names: set[str],
) -> List[Dict[str, Any]]:
    recommendations: List[Dict[str, Any]] = []
    seen_names = set(existing_names)
    for item, draft in zip(prompts, drafts):
        name = str(draft.get("name") or "").strip() or "Custom KPI"
        normalized_name = name.lower()
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        recommendations.append(
            {
                "name": name,
                "description": draft.get("description") or item["prompt"],
                "unit": draft.get("unit") or "USD",
                "formula_json": draft.get("formula_json") or {},
                "rationale": draft.get("rationale") or "",
                "matched_accounts": draft.get("matched_accounts") or [],
                "source_hint": item["source_hint"],
            }
        )
    return recommendations


def _assemble_covenant_recommendations(
    prompts: List[Dict[str, str]],
    drafts: List[Dict[str, Any]],
    existing_names: set[str],
) -> List[Dict[str, Any]]:
    recommendations: List[Dict[str, Any]] = []
    seen_names = set(existing_names)
    for item, draft in zip(prompts, drafts):
        name = str(draft.get("name") or "").strip() or "Custom Covenant"
        normalized_name = name.lower()
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        recommendations.append(
            {
                "loan_name": draft.get("loan_name") or "Credit Facility",
                "name": name,
                "metric_type": draft.get("metric_type") or "COA_FORMULA",
                "threshold": draft.get("threshold") or 1.0,
                "breach_direction": draft.get("breach_direction") or "BELOW",
                "custom_formula_obj": draft.get("custom_formula_obj"),
                "rationale": draft.get("rationale") or "",
                "matched_accounts": draft.get("matched_accounts") or [],
                "source_hint": item["source_hint"],
            }
        )
    return recommendations


def build_onboarding_plan(db: Session, company_id: str) -> Dict[str, Any]:
    from ..services.tenant_companies import organization_company_for_realm, organization_for_company

    organization = organization_for_company(db, company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=company_id)
        if organization
        else None
    )
    memory_query = db.query(CompanyMemory).filter(CompanyMemory.company_id == company_id)
    document_query = db.query(Document).filter(Document.company_id == company_id, Document.processing_status == "done")
    if organization:
        scoped_memory_query = memory_query.filter(CompanyMemory.organization_id == organization.id)
        scoped_document_query = document_query.filter(
            (Document.organization_id == organization.id) | (Document.organization_id.is_(None))
        )
        if organization_company:
            scoped_memory_query = scoped_memory_query.filter(
                CompanyMemory.organization_company_id == organization_company.id
            )
            scoped_document_query = scoped_document_query.filter(
                (Document.organization_company_id == organization_company.id)
                | (Document.organization_company_id.is_(None))
            )
        memory = scoped_memory_query.first()
        if not memory:
            memory = memory_query.filter(
                CompanyMemory.organization_id.is_(None),
                CompanyMemory.organization_company_id.is_(None),
            ).first()
        documents = scoped_document_query.all()
    else:
        memory = memory_query.filter(CompanyMemory.organization_id.is_(None)).first()
        documents = document_query.filter(Document.organization_id.is_(None)).all()
    agreements = db.query(CompanyAgreement).filter(CompanyAgreement.company_id == company_id, CompanyAgreement.processing_status == "done").all()
    objectives_query = db.query(AgentObjective).filter(AgentObjective.company_id == company_id)
    if organization:
        objectives_query = objectives_query.filter(
            (AgentObjective.organization_id == organization.id) | (AgentObjective.organization_id.is_(None))
        )
        if organization_company:
            objectives_query = objectives_query.filter(
                (AgentObjective.organization_company_id == organization_company.id)
                | (AgentObjective.organization_company_id.is_(None))
            )
    else:
        objectives_query = objectives_query.filter(AgentObjective.organization_id.is_(None))
    objectives = objectives_query.all()
    covenants = db.query(DebtCovenant).filter(DebtCovenant.company_id == company_id, DebtCovenant.active == True).all()  # noqa: E712
    kpis = db.query(CustomKPI).filter(CustomKPI.company_id == company_id, CustomKPI.active == True).all()  # noqa: E712
    accounts = _account_catalog(db, company_id)

    existing_objective_types = {str(obj.objective_type or "").lower() for obj in objectives}
    existing_covenant_names = {str(item.name or "").strip().lower() for item in covenants}
    existing_kpi_names = {str(item.name or "").strip().lower() for item in kpis}

    objective_recommendations = [
        recipe for recipe in DEFAULT_OBJECTIVE_RECIPES
        if recipe["objective_type"].lower() not in existing_objective_types
    ]
    # Q1: KPI and covenant drafts are mutually independent (no data dependency
    # between items or between the two lists), so fan them out across one bounded
    # thread pool instead of running up to 6 sequential gpt-4o calls on the
    # request thread. Each draft is wrapped so a provider failure falls back to
    # its deterministic heuristic without aborting the others. Output ordering is
    # preserved by zipping drafts back to their prompts in submission order.
    kpi_prompts = _select_kpi_prompts(accounts)
    covenant_prompts = _select_covenant_prompts(accounts, documents, agreements)

    jobs: List[Callable[[], Dict[str, Any]]] = []
    for item in kpi_prompts:
        jobs.append(lambda item=item: _safe_draft(draft_custom_kpi, _fallback_custom_kpi, item["prompt"], accounts))
    for item in covenant_prompts:
        jobs.append(lambda item=item: _safe_draft(draft_covenant, _fallback_covenant, item["prompt"], accounts))

    draft_results = _run_drafts_concurrently(jobs)
    kpi_drafts = draft_results[: len(kpi_prompts)]
    covenant_drafts = draft_results[len(kpi_prompts):]

    kpi_recommendations = _assemble_kpi_recommendations(kpi_prompts, kpi_drafts, existing_kpi_names)
    covenant_recommendations = _assemble_covenant_recommendations(covenant_prompts, covenant_drafts, existing_covenant_names)

    readiness_score = 0
    readiness_score += 25 if accounts else 0
    readiness_score += 20 if memory and memory.company_profile else 0
    readiness_score += 15 if documents else 0
    readiness_score += 15 if agreements else 0
    readiness_score += 10 if objectives else 0
    readiness_score += 10 if kpis else 0
    readiness_score += 5 if covenants else 0

    return {
        "company_id": company_id,
        "readiness_score": readiness_score,
        "status": {
            "has_company_memory": bool(memory and memory.company_profile),
            "account_count": len(accounts),
            "document_count": len(documents),
            "agreement_count": len(agreements),
            "objective_count": len(objectives),
            "custom_kpi_count": len(kpis),
            "active_covenant_count": len(covenants),
        },
        "recommendations": {
            "objectives": objective_recommendations,
            "custom_kpis": kpi_recommendations,
            "covenants": covenant_recommendations,
        },
        "briefing_preview": (memory.company_profile or "")[:280] if memory and memory.company_profile else None,
    }


def apply_onboarding_plan(db: Session, company_id: str, user_id: int | None = None) -> Dict[str, Any]:
    from ..services.tenant_companies import organization_company_for_realm, organization_for_company

    organization = organization_for_company(db, company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=company_id)
        if organization
        else None
    )
    plan = build_onboarding_plan(db, company_id)
    created = {"objectives": 0, "custom_kpis": 0, "covenants": 0}

    for recipe in plan["recommendations"]["objectives"]:
        schedule = normalize_objective_schedule(recipe["schedule"])
        db.add(
            AgentObjective(
                organization_id=organization.id if organization else None,
                organization_company_id=organization_company.id if organization_company else None,
                company_id=company_id,
                created_by_user_id=user_id,
                name=recipe["name"],
                objective_type=recipe["objective_type"],
                instructions=recipe["instructions"],
                schedule=schedule,
                enabled=True,
                next_run_at=compute_next_run_at(schedule),
                permission_scope={"objective_type": recipe["objective_type"], "source": "onboarding_planner"},
                approval_policy={"external_writes": "never"},
                notification_policy={"email": False},
            )
        )
        created["objectives"] += 1

    for item in plan["recommendations"]["custom_kpis"]:
        db.add(
            CustomKPI(
                company_id=company_id,
                name=item["name"],
                description=item["description"],
                formula_json=item["formula_json"],
                unit=item["unit"],
                sort_order=0,
                active=True,
            )
        )
        created["custom_kpis"] += 1

    for item in plan["recommendations"]["covenants"]:
        db.add(
            DebtCovenant(
                company_id=company_id,
                loan_name=item["loan_name"],
                name=item["name"],
                metric_type=item["metric_type"],
                threshold=item["threshold"],
                breach_direction=item["breach_direction"],
                custom_formula=None if not item.get("custom_formula_obj") else json.dumps(item["custom_formula_obj"]),
                active=True,
            )
        )
        created["covenants"] += 1

    db.commit()
    ContextCacheService(db).refresh_executive_summary(company_id)

    return {
        "company_id": company_id,
        "created": created,
        "readiness_score": build_onboarding_plan(db, company_id)["readiness_score"],
    }

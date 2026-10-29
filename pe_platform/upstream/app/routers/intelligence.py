"""
app/routers/intelligence.py
────────────────────────────
Live Intelligence Hub endpoint.
Aggregates all analysis modules into a single response with no pre-computation
or background jobs required — just call it and get fresh insights from QBO.

GET /intelligence/live?company_id=...
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..qbo_client import get_qbo_client_from_db
from ..models import InsightKPI, QBOToken, User
from ..auth import AuthContext, get_current_auth_context, get_current_user_from_token
from ..dependencies import check_tenant_access, require_debug_endpoint_access, require_tenant_access
from ..services.cache import get_cached, set_cached
from ..services.errors import error_envelope, user_safe_http_exception
from ..services.redaction import redact_to_string
from ..services.tenant_companies import organization_company_for_realm, organization_for_company

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intelligence", tags=["Intelligence"])


def _cache_scope(db: Session, company_id: str) -> str:
    organization = organization_for_company(db, company_id)
    return f"org:{organization.id if organization else 'unmapped'}:realm:{company_id}"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _ratio_to_percent(value: Any) -> float:
    ratio = _as_float(value)
    return ratio * 100 if abs(ratio) <= 1 else ratio


def _precomputed_kpis(db: Session, company_id: str, period: str) -> Dict[str, Any] | None:
    rows = (
        db.query(InsightKPI)
        .filter(InsightKPI.company_id == company_id, InsightKPI.period == period)
        .all()
    )
    if not rows:
        return None

    values = {row.name: _as_float(row.value) for row in rows}
    if not values:
        return None

    kpis: Dict[str, Any] = {
        "as_of_date": period,
        "source": "insights_precomputed",
    }

    ttm_revenue = values.get("ttm_revenue", values.get("revenue", 0.0))
    ttm_ebitda = values.get("ttm_ebitda", values.get("ebitda", 0.0))
    ttm_gross_profit = values.get("ttm_gross_profit")
    if ttm_gross_profit is None and "ttm_gross_margin_pct" in values and ttm_revenue:
        ttm_gross_profit = ttm_revenue * (_ratio_to_percent(values["ttm_gross_margin_pct"]) / 100)

    kpis["ttm_revenue"] = ttm_revenue
    kpis["ttm_gross_profit"] = ttm_gross_profit or values.get("gross_profit", 0.0)
    kpis["ttm_ebitda"] = ttm_ebitda
    kpis["gross_margin_pct"] = (
        kpis["ttm_gross_profit"] / ttm_revenue * 100 if ttm_revenue else _ratio_to_percent(values.get("ttm_gross_margin_pct"))
    )
    kpis["ebitda_margin_pct"] = (
        ttm_ebitda / ttm_revenue * 100 if ttm_revenue else _ratio_to_percent(values.get("ttm_ebitda_ratio"))
    )
    kpis["net_cash"] = values.get("cash", 0.0)
    kpis["total_ar"] = values.get("ar", 0.0)
    kpis["total_assets"] = values.get("total_assets", 0.0)
    kpis["total_liabilities"] = values.get("total_liabilities", 0.0)
    kpis["net_working_capital"] = values.get("net_working_capital", 0.0)
    kpis["current_ratio"] = values.get("current_ratio", 0.0)
    kpis["debt_equity_ratio"] = values.get("debt_to_equity_ratio", 0.0)
    kpis["top5_concentration_pct"] = values.get("top5_concentration_pct", 0.0)
    kpis["revenue_mom_pct"] = None
    return {"kpis": kpis, "errors": [], "source": "insights_precomputed"}


def _section_error(*, section: str, user_message: str, exc: Exception, retryable: bool = True) -> Dict[str, Any]:
    detail = error_envelope(
        error_code=f"{section}_unavailable",
        user_message=user_message,
        retryable=retryable,
    )
    logger.warning(
        "[Intelligence] %s unavailable trace_id=%s error=%s",
        section,
        detail["trace_id"],
        redact_to_string(str(exc), max_length=500),
    )
    return detail

@router.post("/trigger-nightly")
def force_trigger_nightly(
    db: Session = Depends(get_db),
    access: dict = Depends(require_tenant_access("ADMIN")),
    _debug_access = Depends(require_debug_endpoint_access),
):
    """
    DEBUG/DEMO endpoint: Immediately fires the nightly intelligence script.
    Bypasses the 2:00 AM UTC Celery cron schedule. 
    Use this to instantly populate the Intelligence Hub (Briefings, Knowledge, Forecasting) for newly connected testing companies.
    """
    from ..tasks.proactive import run_nightly_intelligence
    try:
        # Run it synchronously for immediate effect, but realistically this should be `.delay()` for Celery
        result = run_nightly_intelligence.apply()
        return {"status": "success", "detail": "Nightly intelligence triggered successfully.", "result": result.result}
    except Exception as e:
        raise user_safe_http_exception(
            status_code=500,
            error_code="nightly_intelligence_trigger_failed",
            user_message="Nightly intelligence could not be triggered.",
            exc=e,
            retryable=True,
            log_context="nightly_intelligence_trigger_failed",
        )


def _require_platform_admin(ctx: AuthContext = Depends(get_current_auth_context)) -> AuthContext:
    """Priority-admin (platform) gate for the cross-tenant connections listing.

    Mirrors app/routers/admin.py::_require_internal_admin (:286-289) exactly.
    That helper is private to admin.py, so the minimal check is duplicated
    inline here rather than imported across routers. The legacy require_admin
    (RoleChecker(["admin"])) accepted ANY legacy role="admin" user; this gate
    accepts ONLY the vetted is_priority_admin platform admins.
    """
    if not ctx.user.is_priority_admin:
        raise HTTPException(status_code=403, detail="Internal admin access required")
    return ctx


@router.get("/connections")
def list_connections(
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(_require_platform_admin),  # TENANT-OK: platform-admin-only listing of every stored QBO connection across all tenants by design
) -> list[dict]:
    """Platform-admin-only list of QBO realm IDs stored in the database."""
    from ..dependencies import _audit_tenant_scope_bypass
    from ..tenant_rls import pin_tenant_scope_live

    # Break-glass audit BEFORE the cross-tenant RLS bypass — FAIL-CLOSED
    # (DIM-033): _audit_tenant_scope_bypass writes + commits the forensic
    # AuthAuditEvent row and RAISES (denying the bypass) if the write fails,
    # exactly the dependencies.py:122-146 _check_company_access bypass
    # semantics (same event_type + details field names, single implementation
    # imported rather than re-typed so the shapes cannot drift).
    # realm_id "*" records that this bypass enumerates ALL realms.
    _audit_tenant_scope_bypass(
        db,
        user=ctx.user,
        company_id="*",
        minimum_role="INTERNAL_SUPER_ADMIN",
        reason="platform_admin_connections_listing",
    )

    # RLS: admin-only enumeration of every stored QBO connection. qbo_tokens is
    # org-keyed FORCE-RLS; without a GUC this returns an empty list. Do NOT
    # remove this call — it fixed a P1 prod-down (commit 4bcc2d0).
    # CROSS-TENANT-OK: platform-admin-only cross-tenant connection listing
    pin_tenant_scope_live(db, bypass=True)
    tokens = db.query(QBOToken).all()
    return [
        {"realm_id": t.realm_id, "company_name": getattr(t, "company_name", None)}
        for t in tokens
    ]


@router.get("/debug")
def get_pl_debug(
    company_id: str = Query(..., description="QBO Realm ID (required)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
    _debug_access = Depends(require_debug_endpoint_access),
):
    """
    Debug endpoint: returns the raw QBO ProfitAndLoss report structure so you can
    see exact section titles, row types, and column layout.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)

    from datetime import date, timedelta
    end_date = date.today()
    start_date = end_date - timedelta(days=365)

    client = get_qbo_client_from_db(db, company_id)
    report = client.get_report(
        "ProfitAndLoss",
        {
            "summarize_column_by": "Month",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "accounting_method": "Accrual",
        },
    )
    # Summarise the structure so it's easier to read
    cols = report.get("Columns", {}).get("Column", [])
    col_titles = [c.get("ColTitle", "") for c in cols]

    sections = []
    for s in report.get("Rows", {}).get("Row", []):
        title = s.get("Header", {}).get("ColData", [{}])[0].get("value", "")
        row_types_seen = list({r.get("type", "no-type") for r in s.get("Rows", {}).get("Row", [])})
        row_count = len(s.get("Rows", {}).get("Row", []))
        sections.append({
            "title": title,
            "type": s.get("type", ""),
            "row_count": row_count,
            "row_types_seen": row_types_seen,
        })

    return {
        "col_titles": col_titles,
        "sections": sections,
        "raw_first_section": report.get("Rows", {}).get("Row", [{}])[0] if report.get("Rows", {}).get("Row") else None,
        "raw_all_rows": report.get("Rows", {}).get("Row", []),   # full structure for debugging
    }


def _get_client(company_id: str, db: Session):
    client = get_qbo_client_from_db(db, company_id)
    if not client:
        raise HTTPException(status_code=400, detail="No QBO connection found. Please connect QuickBooks first.")
    return client


def _safe_run(label: str, fn, *args, **kwargs):
    """Run an analysis function and return its result, or an error dict on failure."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return {
            "error": _section_error(
                section=label,
                user_message=f"{label.replace('_', ' ').title()} is temporarily unavailable.",
                exc=e,
                retryable=True,
            )
        }


def _compute_insights_narrative(data: Dict[str, Any], user_context: str = "") -> str:
    """Use the configured LLM to generate a 3-5 sentence executive narrative."""
    try:
        from ..services.llm import analysis_llm
        # Build a concise summary for the prompt
        rev = data.get("revenue_trend", {})
        total_rev = rev.get("total_income", 0)
        total_gp = rev.get("total_gross_profit", 0)
        months = rev.get("months", [])
        gm_pct = months[-1].get("gross_margin_pct", 0) if months else 0

        kpis = data.get("kpis", {})
        ttm_ebitda = kpis.get("ttm_ebitda", 0)
        ebitda_margin = kpis.get("ebitda_margin_pct", 0)

        customers = data.get("customer_revenue", {})
        top_5 = customers.get("top_5_customers", [])
        total_customers = customers.get("total_customers", 0)
        concentration = (top_5[0][1] / total_rev * 100) if top_5 and total_rev else 0

        ar = data.get("ar_aging", {})
        ar_90_plus = ar.get("buckets", {}).get("90+", 0)
        ar_total = sum(v for v in ar.get("buckets", {}).values() if isinstance(v, (int, float)))

        anomalies = data.get("anomalies", {})
        anomaly_count = len(anomalies.get("anomalies", []))

        vendors = data.get("vendor_spend", {})
        top_vendor = vendors.get("top_5_vendors", [[None, 0]])[0]

        context_block = ""
        if user_context:
            context_block = f"\nIMPORTANT CONTEXT FROM CFO:\n{user_context}\n\nUse this context to shape your analysis and prioritise the most relevant risks and opportunities.\n"

        prompt = f"""You are a CFO-level financial analyst writing a spoken executive briefing — NOT a written report.
Write 3-5 flowing sentences of plain, conversational prose that a CFO would say aloud in a board meeting.
Do NOT use any markdown: no bullet points, no asterisks, no pound signs, no bold, no headers.
Speak in natural language, weaving in the numbers in full sentences.
Highlight the single biggest risk and the single biggest opportunity.
{context_block}
Here is the financial snapshot:
- TTM Revenue: ${total_rev:,.0f}
- TTM Gross Profit: ${total_gp:,.0f}
- TTM EBITDA: ${ttm_ebitda:,.0f} ({ebitda_margin:.1f}% margin)
- Current Gross Margin: {gm_pct:.1f}%
- Total Customers: {total_customers}
- Top customer accounts for {concentration:.1f}% of total revenue
- Largest vendor: {top_vendor[0] if top_vendor[0] else 'N/A'} spending ${top_vendor[1]:,.0f}
- AR 90+ days past due: ${ar_90_plus:,.0f} ({(ar_90_plus/ar_total*100 if ar_total else 0):.1f}% of total AR)
- Statistical anomalies detected: {anomaly_count}
"""
        result = analysis_llm.predict_text(
            system_prompt="You are a CFO-level financial analyst. Write concise executive summaries in plain conversational prose. No markdown formatting.",
            user_prompt=prompt,
            temperature=0.3,
        )
        return (result or "").strip()
    except Exception as e:
        logger.warning(
            "[Intelligence] LLM narrative failed: %s",
            redact_to_string(str(e), max_length=500),
        )
        return ""


# ---------------------------------------------------------
# Knowledge Base Endpoints
# ---------------------------------------------------------

from pydantic import BaseModel  # noqa: E402
class TeachRequest(BaseModel):
    statement: str

@router.get("/knowledge")
def get_company_knowledge(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
):
    """Get all persistent knowledge for a company, grouped by category."""
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    from ..models import CompanyKnowledge
    organization = organization_for_company(db, company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=company_id)
        if organization
        else None
    )
    query = db.query(CompanyKnowledge).filter(CompanyKnowledge.company_id == company_id)
    if organization:
        query = query.filter(
            (CompanyKnowledge.organization_id == organization.id) | (CompanyKnowledge.organization_id.is_(None))
        )
        if organization_company:
            query = query.filter(
                (CompanyKnowledge.organization_company_id == organization_company.id)
                | (CompanyKnowledge.organization_company_id.is_(None))
            )
    else:
        query = query.filter(CompanyKnowledge.organization_id.is_(None))
    knowledge = query.order_by(CompanyKnowledge.created_at.desc()).all()
    
    # Group by category
    grouped = {}
    for k in knowledge:
        cat = k.category
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append({
            "id": k.id,
            "content": k.content,
            "confidence": k.confidence,
            "source": k.source,
            "created_at": k.created_at.isoformat() if k.created_at else None
        })
    return grouped

@router.post("/knowledge/teach")
def teach_agent(
    company_id: str = Query(...),
    req: TeachRequest = None,
    db: Session = Depends(get_db),
    access: dict = Depends(require_tenant_access("EDITOR"))
):
    """
    Accepts a natural language statement, uses an LLM to extract the core factual knowledge,
    determines the category, and saves it.
    """
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
    # — the direct CompanyKnowledge import is gone; the write goes through agent_memory.remember.
    from ..services.llm import analysis_llm
    import json
    organization = organization_for_company(db, company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=company_id)
        if organization
        else None
    )

    sys_prompt = '''You are a knowledge extraction engine. The user will provide a statement.
Extract the core factual context and classify it into exactly one of these categories:
- Deal Context
- Business Operations
- Vendor Intelligence
- Client Intelligence
- Accounting Policies
- Historical Decisions

Return purely JSON. NO markdown format blocks, no backticks.
{
  "category": "Target Category",
  "extracted_fact": "A concise, structured statement of the fact"
}
'''
    try:
        raw_result = analysis_llm.predict_text(sys_prompt, req.statement, temperature=0.0)
        cleaned = raw_result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(cleaned)
        
        # fallback if model gave weird category
        valid_cats = ["Deal Context", "Business Operations", "Vendor Intelligence", "Client Intelligence", "Accounting Policies", "Historical Decisions"]
        cat = data.get("category", "Business Operations")
        if cat not in valid_cats:
            cat = "Business Operations"

        # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
        # AC-4(c): the live manual-teach write goes through Memory-v2 (agent_memory.remember)
        # — versioning, provenance, re-teach supersession, and the embedding at write —
        # instead of a direct CompanyKnowledge(...) construction.
        from ..services import agent_memory

        content = data.get("extracted_fact", req.statement)
        # Embed at write (null-guarded in remember, AP-VEC-8); a None leaves the column
        # NULL for the re-embed backfill task.
        embedding = analysis_llm.get_embedding(content)
        acting_user = access.get("user") if isinstance(access, dict) else None
        supersedes = agent_memory.find_reteach_target(
            db, company_id=company_id, category=cat, content=content,
        )
        obj = agent_memory.remember(
            db,
            company_id=company_id,
            category=cat,
            content=content,
            source="manual",
            provenance={"source_type": "manual", "statement": req.statement},
            organization_id=organization.id if organization else None,
            organization_company_id=organization_company.id if organization_company else None,
            supersedes=supersedes,
            accepted_by=getattr(acting_user, "email", None),
            embedding=embedding,
        )

        from ..services.audit import log_audit_event
        log_audit_event(
            db=db,
            company_id=company_id,
            action="TEACH_KNOWLEDGE",
            entity_type="CompanyKnowledge",
            entity_id=str(obj.id),
            new_state={"category": cat, "content": obj.content, "statement": req.statement}
        )

        db.commit()  # LAYER-OK: pre-existing HEAD router commit reproduced in-place — persists the audit row of the teach write above
        db.refresh(obj)  # LAYER-OK: pre-existing HEAD router refresh reproduced in-place for the response payload
        return {"status": "success", "id": obj.id, "fact": obj.content, "category": obj.category}
    except Exception as e:
        raise user_safe_http_exception(
            status_code=500,
            error_code="knowledge_teach_failed",
            user_message="The knowledge item could not be saved.",
            exc=e,
            retryable=True,
            log_context="knowledge_teach_failed",
        )

@router.delete("/knowledge/{knowledge_id}")
def delete_knowledge(
    knowledge_id: int,
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    access: dict = Depends(require_tenant_access("EDITOR")),
):
    """Remove an incorrect piece of knowledge."""
    from ..models import CompanyKnowledge
    organization = organization_for_company(db, company_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=company_id)
        if organization
        else None
    )
    query = db.query(CompanyKnowledge).filter(
        CompanyKnowledge.id == knowledge_id,
        CompanyKnowledge.company_id == company_id,
    )
    if organization:
        query = query.filter(
            (CompanyKnowledge.organization_id == organization.id) | (CompanyKnowledge.organization_id.is_(None))
        )
        if organization_company:
            query = query.filter(
                (CompanyKnowledge.organization_company_id == organization_company.id)
                | (CompanyKnowledge.organization_company_id.is_(None))
            )
    else:
        query = query.filter(CompanyKnowledge.organization_id.is_(None))
    obj = query.first()
    if not obj:
        raise HTTPException(status_code=404, detail="Knowledge not found")
    db.delete(obj)
    db.commit()
    return {"status": "success"}


@router.get("/kpis")
def get_live_kpis(
    company_id: str = Query(...),
    as_of_month: int = Query(None, description="Month number 1-12; defaults to last completed month"),
    as_of_year: int = Query(None, description="4-digit year; defaults to last completed month's year"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Lightweight KPI endpoint — no LLM, no slow analyses.
    Accepts an optional as_of_month/as_of_year to set the TTM period end date.
    Defaults to the last fully-completed calendar month.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    import calendar as cal_mod
    from datetime import date
    from ..analysis.ratios import resolve_period
    from ..services.dashboard_cache import (
        cached_ar_aging,
        cached_balance_sheet_kpis,
        cached_customer_revenue,
        cached_trends,
    )

    cache_key = f"{_cache_scope(db, company_id)}:get_live_kpis:{as_of_month}:{as_of_year}"
    cached_val = get_cached(cache_key)
    if cached_val is not None:
        return cached_val

    # ── Compute period dict and as_of_date ───────────────────────────────────
    # resolve_period returns {"ttm_start", "ttm_end", "start_date", "end_date", ...}
    # compute_trends uses period["ttm_start"] / period["ttm_end"]
    period = resolve_period(as_of_year if as_of_year else None, as_of_month if as_of_month else None)

    if as_of_month and as_of_year:
        last_day = cal_mod.monthrange(as_of_year, as_of_month)[1]
        as_of_date = date(as_of_year, as_of_month, last_day)
    else:
        from ..analysis.profit_margin import _last_closed_month_end
        as_of_date = _last_closed_month_end()

    logger.info(f"[KPIs] as_of_date={as_of_date} | ttm={period.get('ttm_start')} → {period.get('ttm_end')}")

    period_key = period["end_date"][:7]
    precomputed = _precomputed_kpis(db, company_id, period_key)
    if precomputed:
        set_cached(cache_key, precomputed, ttl_seconds=300)
        return precomputed

    kpis: Dict[str, Any] = {}
    errors: list[Dict[str, Any]] = []

    # 1. Revenue + EBITDA — use compute_trends(), the SAME function as the working
    #    /dashboard/revenue-trend and /dashboard/ebitda-trend endpoints.
    #    Those endpoints already produce correct sums (verified in server logs).
    try:
        trend_data = cached_trends(db, company_id, as_of_year, as_of_month)
        month_labels = trend_data["months"]
        rev_vals = trend_data["revenue_values"]
        ebitda_vals = trend_data["ebitda_values"]
        gp_vals = trend_data["gp_values"]
        ttm_revenue = sum(rev_vals)
        ttm_ebitda  = sum(ebitda_vals)
        ttm_gp      = sum(gp_vals)

        kpis["ttm_revenue"]      = ttm_revenue
        kpis["ttm_gross_profit"] = ttm_gp
        kpis["ttm_ebitda"]       = ttm_ebitda
        rev = ttm_revenue
        kpis["gross_margin_pct"]  = (ttm_gp / rev * 100) if rev else 0.0
        kpis["ebitda_margin_pct"] = (ttm_ebitda / rev * 100) if rev else 0.0

        # MoM — compare last two non-zero months
        non_zero_rev = [(m, v) for m, v in zip(month_labels, rev_vals) if v != 0]
        if len(non_zero_rev) >= 2:
            prev_v, curr_v = non_zero_rev[-2][1], non_zero_rev[-1][1]
            kpis["revenue_mom_pct"] = ((curr_v - prev_v) / prev_v * 100) if prev_v else 0.0
        else:
            kpis["revenue_mom_pct"] = None

        kpis["as_of_date"] = as_of_date.isoformat()
        logger.info(f"[KPIs] ttm_revenue={ttm_revenue:.0f} | ttm_ebitda={ttm_ebitda:.0f}")
    except Exception as e:
        errors.append(
            _section_error(
                section="kpi_trends",
                user_message="Revenue and EBITDA trend data is temporarily unavailable.",
                exc=e,
                retryable=True,
            )
        )

    # 2. Balance Sheet — Net Cash and Total AR via index-match flat_map
    #    compute_balance_sheet_kpis fetches the BS as of as_of_date,
    #    builds a flat label→value map, then looks up:
    #      cash: "Total Bank Accounts", "Total Cash", "Total Cash and cash equivalents"
    #      ar:   "Total Accounts Receivable", "Accounts Receivable (A/R)"
    try:
        bs_kpis = cached_balance_sheet_kpis(db, company_id, as_of_date.isoformat())
        kpis["net_cash"] = bs_kpis.get("cash", 0)
        kpis["total_ar"] = bs_kpis.get("ar", 0)
        
        # New KPIs for Command Center
        kpis["total_assets"] = bs_kpis.get("total_assets", 0)
        kpis["total_liabilities"] = bs_kpis.get("total_liabilities", 0)
        kpis["net_working_capital"] = bs_kpis.get("current_assets", 0) - bs_kpis.get("current_liabilities", 0)
        
        eq = bs_kpis.get("total_equity", 0)
        kpis["debt_equity_ratio"] = bs_kpis.get("total_liabilities", 0) / eq if eq else 0.0
        
        cl = bs_kpis.get("current_liabilities", 0)
        kpis["current_ratio"] = bs_kpis.get("current_assets", 0) / cl if cl else 0.0
        
        logger.info(f"[KPIs] net_cash={kpis['net_cash']:.0f} | total_ar={kpis['total_ar']:.0f}")
    except Exception as e:
        errors.append(
            _section_error(
                section="kpi_balance_sheet",
                user_message="Balance sheet KPI data is temporarily unavailable.",
                exc=e,
                retryable=True,
            )
        )
        kpis["net_cash"] = None
        kpis["total_ar"] = None
        kpis["total_assets"] = None
        kpis["total_liabilities"] = None
        kpis["net_working_capital"] = None
        kpis["debt_equity_ratio"] = None
        kpis["current_ratio"] = None

    # 3. AR aging buckets (for the 90+ % breakdown) — total_ar comes from BS above
    try:
        ar = cached_ar_aging(db, company_id, limit=2000)
        buckets = ar.get("buckets", {})
        ar_total = kpis.get("total_ar") or 0
        kpis["ar_90_plus_pct"] = (buckets.get("90+", 0) / ar_total * 100) if ar_total else 0.0
        kpis["ar_buckets"] = buckets
    except Exception as e:
        errors.append(
            _section_error(
                section="kpi_ar_aging",
                user_message="AR aging KPI data is temporarily unavailable.",
                exc=e,
                retryable=True,
            )
        )

    # 4. Customer revenue — concentration
    try:
        cr = cached_customer_revenue(db, company_id, limit=1000)
        rev = kpis.get("ttm_revenue", 0)
        if rev and cr.get("top_5_customers"):
            top5_rev = sum(v for _, v in cr["top_5_customers"])
            kpis["top5_concentration_pct"] = top5_rev / rev * 100
        else:
            kpis["top5_concentration_pct"] = 0.0
    except Exception as e:
        errors.append(
            _section_error(
                section="kpi_customer_revenue",
                user_message="Customer concentration KPI data is temporarily unavailable.",
                exc=e,
                retryable=True,
            )
        )
        kpis["top5_concentration_pct"] = None

    result = {
        "kpis": kpis,
        "errors": errors,
        "data_quality": {
            "is_partial": bool(errors),
            "unavailable_sections": [err["error_code"] for err in errors],
        },
    }
    set_cached(cache_key, result, ttl_seconds=300)
    return result


@router.get("/live")
def get_live_intelligence(
    company_id: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_token),
) -> Dict[str, Any]:
    """
    Returns a comprehensive, live intelligence snapshot for the company.
    Pulls fresh data from QBO across 5 analysis dimensions:
      1. Revenue trend + gross margin by month (YTD)
      2. Customer revenue concentration (top 10)
      3. Vendor spend concentration (top 10)
      4. AR aging (open invoices by overdue bucket)
      5. Transaction anomalies (z-score based)
    Plus an AI-generated executive narrative.
    """
    check_tenant_access("VIEWER", company_id=company_id, db=db, user=current_user)
    from ..analysis.profit_margin import profit_and_margin_by_month
    from ..analysis.customer_revenue import customer_revenue_summary
    from ..analysis.vendor_spend import vendor_spend_summary
    from ..analysis.ar_aging import ar_aging
    from ..analysis.anomalies import transaction_anomalies
    from ..analysis.expense_trends import expense_trend_mom

    from ..db import get_db as _get_db  # noqa: F401

    client = _get_client(company_id, db)
    cash_error: Dict[str, Any] | None = None

    # ── Helper: pull live net cash from Balance Sheet ────────────────────────
    def _live_net_cash() -> float | None:
        """Sum all Bank and Cash account balances from the QBO Balance Sheet."""
        nonlocal cash_error
        try:
            from datetime import date
            bs = client.get_report(
                "BalanceSheet",
                {"as_of_date": date.today().isoformat(), "accounting_method": "Accrual"},
            )
            _CASH_KEYWORDS = {
                "bank", "cash", "checking", "savings", "money market",
                "petty cash", "undeposited",
            }

            def _walk(rows, in_cash=False) -> float:
                total = 0.0
                for row in rows:
                    if row.get("type") == "Section":
                        header = (
                            row.get("Header", {}).get("ColData", [{}])[0]
                            .get("value", "").lower()
                        )
                        is_cash = in_cash or any(k in header for k in _CASH_KEYWORDS)
                        total += _walk(row.get("Rows", {}).get("Row", []), is_cash)
                    elif row.get("type") in ("Data", "data") and in_cash:
                        cells = row.get("ColData", [])
                        if len(cells) >= 2:
                            try:
                                total += float(cells[-1].get("value", 0) or 0)
                            except (TypeError, ValueError):
                                pass
                return total

            return _walk(bs.get("Rows", {}).get("Row", []))
        except Exception as e:
            cash_error = _section_error(
                section="live_net_cash",
                user_message="Live cash balance data is temporarily unavailable.",
                exc=e,
                retryable=True,
            )
            return None

    # Run all analyses — each is independent so we run them and capture errors gracefully
    revenue_trend  = _safe_run("revenue_trend",  profit_and_margin_by_month, client)
    customer_rev   = _safe_run("customer_rev",   customer_revenue_summary,   client, 500)
    vendor_spend   = _safe_run("vendor_spend",   vendor_spend_summary,       client, 500)
    ar             = _safe_run("ar_aging",        ar_aging,                   client, 500)
    anomalies      = _safe_run("anomalies",       transaction_anomalies,      client, 500)
    expense_trend  = _safe_run("expense_trend",   expense_trend_mom,          client, 500)

    # Derive summary KPIs from revenue_trend
    kpis: Dict[str, Any] = {}
    if "months" in revenue_trend:
        months = revenue_trend["months"]
        kpis["ttm_revenue"]      = revenue_trend.get("total_income", 0)
        kpis["ttm_gross_profit"] = revenue_trend.get("total_gross_profit", 0)
        kpis["ttm_cogs"]         = revenue_trend.get("total_cogs", 0)
        if kpis["ttm_revenue"]:
            kpis["gross_margin_pct"] = kpis["ttm_gross_profit"] / kpis["ttm_revenue"] * 100
        else:
            kpis["gross_margin_pct"] = 0.0

        # MoM revenue change (last two months)
        non_zero = [m for m in months if m.get("income", 0) > 0]
        if len(non_zero) >= 2:
            prev, curr = non_zero[-2]["income"], non_zero[-1]["income"]
            kpis["revenue_mom_pct"] = ((curr - prev) / prev * 100) if prev else 0
            kpis["revenue_mom_label"] = non_zero[-1].get("month", "")
        else:
            kpis["revenue_mom_pct"] = None

    # Concentration risk
    if "top_5_customers" in customer_rev and kpis.get("ttm_revenue"):
        top5_rev = sum(v for _, v in customer_rev["top_5_customers"])
        kpis["top5_concentration_pct"] = top5_rev / kpis["ttm_revenue"] * 100
    else:
        kpis["top5_concentration_pct"] = None

    # ── EBITDA from P&L GrandTotal (revenue_trend already has this) ──────────
    # profit_and_margin_by_month now extracts net income per month from the Summary rows.
    # total_ebitda = sum of per-month net income (≈ Operating Income; no D&A in QBO).
    if "months" in revenue_trend:
        ttm_ebitda = revenue_trend.get("total_ebitda", 0)
        kpis["ttm_ebitda"] = ttm_ebitda
        if kpis.get("ttm_revenue", 0):
            kpis["ebitda_margin_pct"] = ttm_ebitda / kpis["ttm_revenue"] * 100
        else:
            kpis["ebitda_margin_pct"] = 0.0

    # ── Net Cash from live Balance Sheet ─────────────────────────────────────
    kpis["net_cash"] = _live_net_cash()

    # AR risk summary
    ar_kpis: Dict[str, Any] = {}
    if isinstance(ar, dict) and "buckets" in ar:
        buckets = ar["buckets"]
        # Use total_ar directly if present (new ar_aging returns it)
        ar_total = ar.get("total_ar") or sum(v for v in buckets.values() if isinstance(v, (int, float)))
        ar_kpis["total_ar"] = ar_total
        ar_kpis["pct_90_plus"] = (buckets.get("90+", 0) / ar_total * 100) if ar_total else 0
        ar_kpis["buckets"] = buckets
    kpis["ar"] = ar_kpis

    # ── Load user-provided context notes (persist across all AI calls) ──
    user_context_note = ""
    try:
        from ..models import CompanyNote
        note = db.query(CompanyNote).filter_by(company_id=company_id).first()
        if note and note.content:
            user_context_note = note.content
    except Exception:
        pass  # CompanyNote may not exist yet

    # Build AI narrative (non-blocking — skip if OpenAI key not set)
    combined = {
        "revenue_trend": revenue_trend,
        "customer_revenue": customer_rev,
        "vendor_spend": vendor_spend,
        "ar_aging": ar,
        "anomalies": anomalies,
        "kpis": kpis,
    }
    narrative = _compute_insights_narrative(combined, user_context=user_context_note)
    errors = [
        value.get("error")
        for value in (revenue_trend, customer_rev, vendor_spend, ar, anomalies, expense_trend)
        if isinstance(value, dict) and isinstance(value.get("error"), dict)
    ]
    if cash_error:
        errors.append(cash_error)

    return {
        "company_id": company_id,
        "kpis": kpis,
        "revenue_trend": revenue_trend,
        "revenue_trend_error": revenue_trend.get("error") if isinstance(revenue_trend, dict) else None,
        "customer_revenue": customer_rev,
        "vendor_spend": vendor_spend,
        "ar_aging": ar,
        "anomalies": anomalies,
        "expense_trend": expense_trend,
        "ai_narrative": narrative,
        "errors": errors,
        "data_quality": {
            "is_partial": bool(errors),
            "unavailable_sections": [err["error_code"] for err in errors],
        },
    }

"""
app/orchestration/knowledge_extraction.py
────────────────────────────────────────
Engine for passively extracting persistent business knowledge from
financial metrics, anomalies, and transaction trends.
"""
from __future__ import annotations

import json  # noqa: F401
import logging
from typing import Dict, Any, List  # noqa: F401

from sqlalchemy.orm import Session
from ..models import InsightKPI, InsightAnomaly, CompanyKnowledge
from ..services.llm import analysis_llm
from ..services.task_autonomy import latest_locked_period
from ..services.tenant_companies import organization_company_for_realm, organization_for_company

logger = logging.getLogger(__name__)

KNOWLEDGE_EXTRACTION_PROMPT = """
You are Vesper Bot, an AI Chief Financial Officer (CFO). Your job is to extract long-term, persistent business knowledge from raw financial metrics and anomalies.
Look at the provided data and infer facts about the business model, seasonality, risk factors, top clients/vendors, or operational mechanics.
You should ONLY extract facts that represent permanent or long-term structural context about the company (e.g. "Stripe is the primary payment processor", "High revenue concentration with Customer X"). Do not extract ephemeral points like "Revenue dropped this month".

IMPORTANT: I will also provide you with a list of "Existing Knowledge" items. You MUST NOT extract or duplicate any facts that are already in the existing knowledge base, even if phrased differently.

Classify the facts into exactly one of these categories:
- Deal Context
- Business Operations
- Vendor Intelligence
- Client Intelligence
- Accounting Policies
- Historical Decisions

Return purely JSON in this exact schema, with NO markdown formatting, NO backticks:
{
  "new_facts": [
    {
      "category": "String",
      "extracted_fact": "A concise, structured statement of the inferred fact."
    }
  ]
}
"""

def extract_passive_knowledge(db: Session, company_id: str, period: str) -> int:
    """
    Pulls recent anomalies and KPIs, evaluates them mathematically into string context,
    sends to LLM to extract persistent structural knowledge, and saves non-duplicates
    into the CompanyKnowledge table with source="inferred".
    
    Returns the number of new facts written.
    """
    try:
        organization = organization_for_company(db, company_id)
        organization_company = (
            organization_company_for_realm(db, organization_id=organization.id, realm_id=company_id)
            if organization
            else None
        )
        # 1. Gather recent data limits
        kpis = db.query(InsightKPI).filter(
            InsightKPI.company_id == company_id, 
            InsightKPI.period == period
        ).all()
        
        anomalies_q = db.query(InsightAnomaly).filter(
            InsightAnomaly.company_id == company_id,
            InsightAnomaly.period == period
        )
        # AC-A3 (plan 20260716-five-domain-remediation): the SAME soft-close
        # floor as every other InsightAnomaly read — the extractor must never
        # learn "facts" from closed-period rows the Alerts Center hides. (The
        # enclosing except returns 0 on a floor read error — fail-closed to NO
        # extraction, never an unfiltered read.)
        anomaly_floor = latest_locked_period(db, realm_id=company_id, hard_only=False)
        if anomaly_floor is not None:
            anomalies_q = anomalies_q.filter(InsightAnomaly.period > anomaly_floor)
        anomalies = anomalies_q.all()
        
        # 2. Gather existing knowledge to prevent dupes
        existing_query = db.query(CompanyKnowledge).filter(CompanyKnowledge.company_id == company_id)
        if organization:
            existing_query = existing_query.filter(
                (CompanyKnowledge.organization_id == organization.id) | (CompanyKnowledge.organization_id.is_(None))
            )
            if organization_company:
                existing_query = existing_query.filter(
                    (CompanyKnowledge.organization_company_id == organization_company.id)
                    | (CompanyKnowledge.organization_company_id.is_(None))
                )
        else:
            existing_query = existing_query.filter(CompanyKnowledge.organization_id.is_(None))
        existing = existing_query.all()
        
        if not kpis and not anomalies:
            logger.info("No new data to extract knowledge from.")
            return 0
            
        # 3. Format context
        recent_data_str = "NEW DATA:\n"
        for k in kpis:
            if float(k.value or 0) > 0:  # Only provide meaningful context
                recent_data_str += f"- KPI: {k.name} = {k.value} {k.unit}\n"
                
        for a in anomalies:
            recent_data_str += f"- Anomaly [{a.severity}]: {a.anomaly_type} - {a.description}\n"
            
        existing_data_str = "EXISTING KNOWLEDGE (Do NOT restate these):\n"
        for ex in existing:
            existing_data_str += f"- {ex.category}: {ex.content}\n"
            
        user_prompt = f"{existing_data_str}\n\n{recent_data_str}\n\nExtract any structurally new persistent facts you can confidently deduce."
        
        # 4. Infer using LLM
        data = analysis_llm.predict_json(
            system_prompt=KNOWLEDGE_EXTRACTION_PROMPT,
            user_prompt=user_prompt,
            temperature=0.1
        )
        
        if not data or "new_facts" not in data:
            logger.info("[KnowledgeExtraction] No valid new_facts returned.")
            return 0
            
        valid_cats = ["Deal Context", "Business Operations", "Vendor Intelligence", "Client Intelligence", "Accounting Policies", "Historical Decisions"]
        new_facts = data.get("new_facts", [])
        
        # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
        # AC-4(d)/LG-2: inferred writes go through Memory-v2 (agent_memory.remember) WITH the
        # embedding computed AT WRITE — a direct CompanyKnowledge(...) row with a NULL embedding
        # was invisible to the ranked retrieval path until a manual backfill ran.
        from ..services import agent_memory

        count = 0
        for fact in new_facts:
            cat = fact.get("category", "Business Operations")  # SA-OK: bounded-loop dict.get on an LLM JSON item, not a DB query
            content = fact.get("extracted_fact", "").strip()  # SA-OK: bounded-loop dict.get on an LLM JSON item, not a DB query

            if not content:
                continue
            if cat not in valid_cats:
                cat = "Business Operations"

            # Double check literal matches just in case
            if any(e.content.lower() == content.lower() for e in existing):
                continue

            # Embed at write (null-guarded in remember, AP-VEC-8): a None embedding
            # leaves the column NULL for the re-embed backfill task to pick up.
            embedding = analysis_llm.get_embedding(content)
            agent_memory.remember(  # SA-OK: bounded-loop — new_facts is a single LLM response (handful of items); each write is one versioned memory insert
                db,
                company_id=company_id,
                category=cat,
                content=content,
                confidence="medium",  # Inferred passive knowledge gets medium default
                source="inferred",  # tentative — accepted_by stays None until a human accepts
                provenance={"source_type": "inferred", "period": period},
                organization_id=organization.id if organization else None,
                organization_company_id=organization_company.id if organization_company else None,
                embedding=embedding,
            )
            count += 1

        db.commit()
        logger.info(f"[KnowledgeExtraction] Persisted {count} new AI-inferred facts for {company_id}.")
        return count
        
    except Exception as e:
        logger.error(f"[KnowledgeExtraction] Failed to extract passive knowledge: {e}")
        return 0

"""
ContextCacheService — precomputes a ~500-token executive summary per company.
Run nightly so the AI assistant has instant awareness without burning tokens.
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy.orm import Session
from requests.exceptions import HTTPError  # noqa: F401

from ..models import (
    AssistantContextCache, QBOCompanyInfo, CompanyMemory,
    BudgetLine, Document, CloseTask, DebtCovenant, CustomKPI,  # noqa: F401
    LboCompany, AccountingSchedule, CapTableEntry, InsightAnomaly,  # noqa: F401
)
from ..qbo_client import get_qbo_client_from_db
from ..services.errors import safe_error_detail
from ..services.redaction import redact_to_string
from ..services.task_autonomy import latest_locked_period
from ..services.tenant_companies import organization_company_for_realm, organization_for_company
from ..analysis.vendor_spend import vendor_spend_summary
from ..analysis.customer_revenue import customer_revenue_summary
from ..analysis.expense_trends import expense_trend_mom
from ..analysis.profit_margin import profit_and_margin_by_month
from ..analysis.cogs_anomaly import cogs_anomalies
from ..analysis.ar_aging import ar_aging
from ..analysis.ap_aging import ap_aging
from ..analysis.anomalies import transaction_anomalies

logger = logging.getLogger(__name__)

# ── Analysis packs ────────────────────────────────────────────────────────────
# No cash pack: the cash forecast is the 13-week grid (routers/cashflow_forecast.py),
# which the assistant reads live through get_cashflow_snapshot.
_ANALYSIS_PACKS = {
    "vendor_spend": vendor_spend_summary,
    "customer_revenue": customer_revenue_summary,
    "expense_trends": expense_trend_mom,
    "profit_margins": profit_and_margin_by_month,
    "cogs_anomalies": cogs_anomalies,
    "ar_aging": ar_aging,
    "ap_aging": ap_aging,
    "transaction_anomalies": transaction_anomalies,
}


class ContextCacheService:
    def __init__(self, db: Session):
        self.db = db

    def refresh_executive_summary(self, realm_id: str) -> str:
        """
        1. Run all analysis packs against QBO
        2. Gather module context snapshots
        3. Ask Claude to summarize everything in ~500 tokens
        4. Save to AssistantContextCache
        """
        company_info = self.db.query(QBOCompanyInfo).filter_by(realm_id=realm_id).first()
        company_name = company_info.company_name if company_info else realm_id
        organization = organization_for_company(self.db, realm_id)
        organization_company = (
            organization_company_for_realm(self.db, organization_id=organization.id, realm_id=realm_id)
            if organization
            else None
        )

        # ── Gather raw data ───────────────────────────────────────────────
        raw_data: Dict[str, Any] = {}
        analysis_errors: List[Dict[str, Any]] = []

        # Analysis packs
        try:
            qbo = get_qbo_client_from_db(self.db, realm_id)
            for key, fn in _ANALYSIS_PACKS.items():
                try:
                    result = fn(qbo)
                    # Compact: keep top-level keys, truncate lists
                    if isinstance(result, list):
                        raw_data[key] = result[:10]
                    elif isinstance(result, dict):
                        raw_data[key] = {k: v for k, v in list(result.items())[:10]}
                    else:
                        raw_data[key] = result
                except Exception as e:
                    analysis_errors.append(safe_error_detail(
                        error_code=f"context_cache_{key}_unavailable",
                        user_message=f"{key.replace('_', ' ').title()} analysis is temporarily unavailable.",
                        exc=e,
                        retryable=True,
                        log_context="context_cache_analysis_pack_error",
                    ))
        except Exception as e:
            analysis_errors.append(safe_error_detail(
                error_code="context_cache_qbo_connection_unavailable",
                user_message="QuickBooks could not be reached for the assistant context cache.",
                exc=e,
                retryable=True,
                log_context="context_cache_qbo_error",
            ))

        if analysis_errors:
            raw_data["analysis_errors"] = analysis_errors

        # Module snapshots
        modules = self._gather_module_snapshots(
            realm_id,
            organization=organization,
            organization_company=organization_company,
        )
        raw_data["modules"] = modules

        # Memory
        memory_query = self.db.query(CompanyMemory).filter(CompanyMemory.company_id == realm_id)
        if organization:
            scoped_memory_query = memory_query.filter(CompanyMemory.organization_id == organization.id)
            if organization_company:
                scoped_memory_query = scoped_memory_query.filter(
                    CompanyMemory.organization_company_id == organization_company.id
                )
            memory = scoped_memory_query.first()
            if not memory:
                memory = memory_query.filter(
                    CompanyMemory.organization_id.is_(None),
                    CompanyMemory.organization_company_id.is_(None),
                ).first()
        else:
            memory = memory_query.filter(CompanyMemory.organization_id.is_(None)).first()
        if memory and organization:
            if memory.organization_id not in (None, organization.id):
                raise PermissionError("Company memory belongs to another organization")
            if organization_company and memory.organization_company_id not in (None, organization_company.id):
                raise PermissionError("Company memory belongs to another entity")
            memory.organization_id = organization.id
            if organization_company:
                memory.organization_company_id = organization_company.id
        if memory:
            if memory.company_profile:
                raw_data["company_profile"] = memory.company_profile
            if memory.user_context:
                raw_data["user_context"] = memory.user_context
            for attr in ["trend_summaries", "seasonality", "cash_behavior",
                         "vendor_behavior", "customer_behavior", "risk_map"]:
                val = getattr(memory, attr, None)
                if val:
                    raw_data[f"memory_{attr}"] = val

        # ── Summarize with Claude ─────────────────────────────────────────
        summary = self._generate_summary(company_name, raw_data)

        # ── Save to DB ────────────────────────────────────────────────────
        cache_query = self.db.query(AssistantContextCache).filter(AssistantContextCache.realm_id == realm_id)
        if organization:
            scoped_cache_query = cache_query.filter(AssistantContextCache.organization_id == organization.id)
            if organization_company:
                scoped_cache_query = scoped_cache_query.filter(
                    AssistantContextCache.organization_company_id == organization_company.id
                )
            cache = scoped_cache_query.first()
            if not cache:
                cache = cache_query.filter(
                    AssistantContextCache.organization_id.is_(None),
                    AssistantContextCache.organization_company_id.is_(None),
                ).first()
        else:
            cache = cache_query.filter(AssistantContextCache.organization_id.is_(None)).first()
        if cache and organization:
            if cache.organization_id not in (None, organization.id):
                raise PermissionError("Assistant context cache belongs to another organization")
            if organization_company and cache.organization_company_id not in (None, organization_company.id):
                raise PermissionError("Assistant context cache belongs to another entity")
        if not cache:
            cache = AssistantContextCache(
                realm_id=realm_id,
                organization_id=organization.id if organization else None,
                organization_company_id=organization_company.id if organization_company else None,
            )
            self.db.add(cache)
        elif organization:
            cache.organization_id = organization.id
            if organization_company:
                cache.organization_company_id = organization_company.id

        cache.executive_summary = summary
        cache.module_snapshot = modules
        cache.summary_tokens = len(summary.split())  # rough estimate
        cache.updated_at = datetime.utcnow()
        self.db.commit()

        logger.info(f"[ContextCache] Refreshed summary for {realm_id} (~{cache.summary_tokens} words)")
        return summary

    def _scope_query(self, query, model: Any, organization: Any = None, organization_company: Any = None):
        if organization is not None and hasattr(model, "organization_id"):
            query = query.filter(
                (model.organization_id == organization.id) | (model.organization_id.is_(None))
            )
        if organization_company is not None and hasattr(model, "organization_company_id"):
            query = query.filter(
                (model.organization_company_id == organization_company.id)
                | (model.organization_company_id.is_(None))
            )
        return query

    def _gather_module_snapshots(
        self,
        realm_id: str,
        *,
        organization: Any = None,
        organization_company: Any = None,
    ) -> Dict[str, Any]:
        """Gather key metrics from each module."""
        snapshot: Dict[str, Any] = {}

        # Active alerts
        # AC-3 (plan 20260716-anomaly-floor-remaining-edges): the cached
        # executive summary applies the same soft-close anomaly floor as the
        # Alerts Center. Floor None ⇒ unfiltered. A floor-read FAILURE fails
        # CLOSED to ZERO anomaly rows (the snapshot key is omitted; mirrors
        # app/orchestration/knowledge_extraction.py) — the summary must never
        # count closed-period rows because the floor could not be verified.
        alerts = 0
        try:
            anomaly_floor = latest_locked_period(self.db, realm_id=realm_id, hard_only=False)
        except Exception as e:
            logger.error(
                "context cache: anomaly floor read failed for realm %s — omitting "
                "active alerts (fail-closed): %s",
                realm_id,
                redact_to_string(e),
            )
        else:
            alert_query = self.db.query(InsightAnomaly).filter(
                InsightAnomaly.company_id == realm_id,
                InsightAnomaly.dismissed == False,  # noqa: E712
            )
            if anomaly_floor is not None:
                alert_query = alert_query.filter(InsightAnomaly.period > anomaly_floor)
            alerts = self._scope_query(alert_query, InsightAnomaly, organization, organization_company).count()
        if alerts:
            snapshot["active_alerts"] = alerts

        # Close tasks
        task_query = self.db.query(CloseTask).filter_by(realm_id=realm_id)
        tasks = self._scope_query(task_query, CloseTask, organization, organization_company).all()
        if tasks:
            completed = sum(1 for t in tasks if (t.status or "").lower() in ("completed", "done"))
            snapshot["close_tasks"] = f"{completed}/{len(tasks)} completed"

        # Covenants
        covenant_query = self.db.query(DebtCovenant).filter_by(company_id=realm_id, active=True)
        covenants = self._scope_query(covenant_query, DebtCovenant, organization, organization_company).count()
        if covenants:
            snapshot["active_covenants"] = covenants

        # Budget lines
        budget_query = self.db.query(BudgetLine).filter_by(company_id=realm_id)
        budget_count = self._scope_query(budget_query, BudgetLine, organization, organization_company).count()
        if budget_count:
            snapshot["budget_accounts"] = budget_count

        # Documents
        docs_query = self.db.query(Document).filter(
            Document.company_id == realm_id,
            Document.processing_status == "done",
        )
        docs = self._scope_query(docs_query, Document, organization, organization_company).count()
        if docs:
            snapshot["documents"] = docs

        # Accounting schedules
        schedule_query = self.db.query(AccountingSchedule).filter(
            AccountingSchedule.realm_id == realm_id,
            AccountingSchedule.status.in_(["ACTIVE", "PENDING_APPROVAL"]),
        )
        schedules = self._scope_query(schedule_query, AccountingSchedule, organization, organization_company).count()
        if schedules:
            snapshot["active_schedules"] = schedules

        return snapshot

    def _generate_summary(self, company_name: str, raw_data: Dict[str, Any]) -> str:
        """Use the explicit context lane to compress data into an executive briefing."""
        from ..services.llm import build_context_artifact

        data_str = json.dumps(raw_data, indent=1, default=str)
        if len(data_str) > 15000:
            data_str = data_str[:15000] + "\n... (truncated)"
        try:
            artifact = build_context_artifact(
                data_str,
                purpose=f"Build a concise executive briefing for {company_name}.",
            )
            result = artifact.get("compressed_text") or artifact.get("summary")
            if result:
                logger.info("[ContextCache] Summary generated via explicit context lane")
                return str(result)
        except Exception as e:
            logger.error(
                "[ContextCache] Context lane summarization failed: %s",
                redact_to_string(str(e), max_length=500),
            )

        return self._fallback_summary(company_name, raw_data)

    def _fallback_summary(self, company_name: str, raw_data: Dict[str, Any]) -> str:
        """Generate a basic summary without LLM when Claude is unavailable."""
        parts = [f"Executive briefing for {company_name}."]

        modules = raw_data.get("modules", {})
        analysis_errors = raw_data.get("analysis_errors", [])
        if analysis_errors:
            unavailable = [
                str(error.get("user_message") or error.get("error_code") or "analysis unavailable")
                for error in analysis_errors[:5]
                if isinstance(error, dict)
            ]
            if unavailable:
                parts.append("Some live analyses are unavailable: " + "; ".join(unavailable) + ".")
        if modules.get("active_alerts"):
            parts.append(f"{modules['active_alerts']} active alerts.")
        if modules.get("close_tasks"):
            parts.append(f"Close tasks: {modules['close_tasks']}.")
        if modules.get("active_covenants"):
            parts.append(f"{modules['active_covenants']} active covenants being tracked.")
        if modules.get("budget_accounts"):
            parts.append(f"{modules['budget_accounts']} budget accounts configured.")
        if modules.get("documents"):
            parts.append(f"{modules['documents']} documents processed.")

        return " ".join(parts)

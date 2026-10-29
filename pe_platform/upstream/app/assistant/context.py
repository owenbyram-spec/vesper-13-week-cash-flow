"""
app/assistant/context.py
────────────────────────
Module context builders and intent classification.
Gathers structured context from every Vesper Bot module for the AI assistant.
"""
import logging
from contextvars import ContextVar

from sqlalchemy.orm import Session

from ..models import (
    BudgetLine, BudgetDriver, Document, CloseTask, DebtCovenant,
    CustomKPI, LboCompany, LboScenario, AccountingSchedule,  # noqa: F401
    CapTableEntry, InsightAnomaly, CompanyKnowledge, CompanyAgreement,
    CashForecastSnapshot, CashForecastOverride, AICorrectionEvent,
)
from ..services.tenant_companies import organization_company_for_realm, organization_for_company
from ..services.redaction import redact_to_string
from ..services.task_autonomy import latest_locked_period

logger = logging.getLogger(__name__)

# Request-scoped holder for the user's current question so analysis handlers can pass it to
# the analysis LLM for context-aware pre-analysis. A ContextVar — NOT a module global — so two
# concurrent requests cannot read each other's question (a cross-request / cross-tenant context
# leak). Default "" preserves the historical unset semantics ("" / falls back to a default).
_current_user_question_var: ContextVar[str] = ContextVar("current_user_question", default="")


def set_current_user_question(question: str) -> None:
    """Set the current request's user question (request-scoped, via a ContextVar)."""
    _current_user_question_var.set(question or "")


def get_current_user_question() -> str:
    """Return the current request's user question ("" if unset)."""
    return _current_user_question_var.get()


# ─────────────────────────────────────────────────────────────────────────────
# Module Context Builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_budget_context(db: Session, company_id: str) -> str:
    """Summarize budget lines and drivers for the current year."""
    lines = db.query(BudgetLine).filter_by(company_id=company_id).limit(50).all()
    if not lines:
        return ""
    parts = []
    for bl in lines:
        amounts = bl.monthly_amounts or {}
        total = sum(float(v or 0) for v in amounts.values())
        if total != 0:
            parts.append(f"  - {bl.account_name or bl.account_id}: ${total:,.0f} ({bl.account_type or 'unknown'})")
    if not parts:
        return ""

    drivers = db.query(BudgetDriver).filter_by(company_id=company_id).limit(30).all()
    driver_parts = []
    for d in drivers:
        entity = d.vendor_name or d.customer_name or "—"
        driver_parts.append(f"  - {d.account_name}: method={d.driver_method}, entity={entity}")

    result = f"=== BUDGET ({len(parts)} accounts) ===\n" + "\n".join(parts[:30])
    if driver_parts:
        result += f"\n\nBudget Drivers ({len(driver_parts)}):\n" + "\n".join(driver_parts[:20])
    return result


def _build_document_context(db: Session, company_id: str) -> str:
    """Summarize uploaded documents that have context_modules including 'assistant'."""
    docs = db.query(Document).filter(
        Document.company_id == company_id,
        Document.processing_status == "done",
    ).all()

    # Filter to docs that include 'assistant' in context_modules
    relevant = [d for d in docs if "assistant" in (d.context_modules or [])]
    if not relevant:
        return ""

    parts = []
    for d in relevant:
        s = f"  - [{d.document_type}] {d.filename}"
        if d.ai_summary:
            s += f": {d.ai_summary[:200]}"
        kt = d.key_terms or {}
        ft = kt.get("financial_terms", {})
        if ft:
            if ft.get("total_value"):
                s += f" | Value: ${ft['total_value']:,}" if isinstance(ft['total_value'], (int, float)) else ""
            if ft.get("payment_frequency"):
                s += f" | Freq: {ft['payment_frequency']}"
        if d.expiration_date:
            s += f" | Expires: {d.expiration_date}"
        parts.append(s)
    return f"=== DOCUMENTS ({len(parts)} active) ===\n" + "\n".join(parts)


def _build_agreement_context(db: Session, company_id: str) -> str:
    """Summarize uploaded material agreements/contracts for assistant context."""
    agreements = (
        db.query(CompanyAgreement)
        .filter(
            CompanyAgreement.company_id == company_id,
            CompanyAgreement.processing_status == "done",
        )
        .order_by(CompanyAgreement.created_at.desc())
        .limit(12)
        .all()
    )
    if not agreements:
        return ""

    parts = []
    for a in agreements:
        agreement_type = a.agreement_type or "agreement"
        s = f"  - [{agreement_type}] {a.filename}"
        if a.ai_summary:
            s += f": {a.ai_summary[:300]}"

        economics = a.extracted_economics or {}
        if economics:
            economics_bits = []
            for key, value in list(economics.items())[:6]:
                if value not in (None, "", [], {}):
                    economics_bits.append(f"{key}={value}")
            if economics_bits:
                s += " | Economics: " + "; ".join(economics_bits)[:350]

        cap_table = a.extracted_cap_table or []
        if cap_table:
            s += f" | Cap table entries: {len(cap_table)}"
        parts.append(s)

    return f"=== MATERIAL AGREEMENTS ({len(parts)}) ===\n" + "\n".join(parts)


def build_module_context(db: Session, company_id: str, module: str) -> str:
    """Return the assistant-readable context block for a named Vesper Bot module."""
    module_key = (module or "").strip().lower().replace("-", "_")
    aliases = {
        "agreements": "agreements",
        "contracts": "agreements",
        "documents": "documents",
        "document_manager": "documents",
        "budget": "budget",
        "cash_flow": "cashflow",
        "cashflow": "cashflow",
        "close": "close",
        "close_checklist": "close",
        "covenants": "covenants",
        "custom_kpis": "kpis",
        "kpis": "kpis",
        "deal_model": "lbo",
        "lbo": "lbo",
        "accounting": "accounting",
        "accounting_schedules": "accounting",
        "cap_table": "cap_table",
        "captable": "cap_table",
        "alerts": "alerts",
        "intelligence": "alerts",
        "knowledge": "knowledge",
    }
    from ..services.tool_outcomes import ToolFailure

    normalized = aliases.get(module_key, module_key)
    builders = {
        "agreements": _build_agreement_context,
        "documents": _build_document_context,
        "budget": _build_budget_context,
        "cashflow": _build_cashflow_context,
        "close": _build_close_context,
        "covenants": _build_covenant_context,
        "kpis": _build_kpi_context,
        "lbo": _build_deal_context,
        "accounting": _build_accounting_context,
        "cap_table": _build_captable_context,
        "alerts": _build_alerts_context,
        "knowledge": _build_active_knowledge_context,
        "corrections": _build_correction_exemplars_context,
    }
    builder = builders.get(normalized)
    if not builder:
        supported = ", ".join(sorted(builders))
        raise ToolFailure("unsupported_module", f"Unsupported module '{module}'. Supported modules: {supported}.")
    result = builder(db, company_id)
    if not result:
        raise ToolFailure("module_context_unavailable", f"No current context found for module '{normalized}'.", retryable=True)
    return result


def _build_close_context(db: Session, company_id: str) -> str:
    """Summarize month-end close task status."""
    tasks = db.query(CloseTask).filter_by(realm_id=company_id).order_by(CloseTask.period.desc()).limit(30).all()
    if not tasks:
        return ""
    by_period: dict[str, dict[str, int]] = {}
    for t in tasks:
        p = t.period or "unknown"
        if p not in by_period:
            by_period[p] = {"total": 0, "completed": 0, "in_progress": 0, "pending": 0}
        by_period[p]["total"] += 1
        status = (t.status or "pending").lower()
        if status in ("completed", "done"):
            by_period[p]["completed"] += 1
        elif status in ("in_progress", "in progress"):
            by_period[p]["in_progress"] += 1
        else:
            by_period[p]["pending"] += 1

    parts = []
    for period, counts in sorted(by_period.items(), reverse=True)[:3]:
        parts.append(f"  - {period}: {counts['completed']}/{counts['total']} done, {counts['pending']} pending")
    return "=== CLOSE CHECKLIST ===\n" + "\n".join(parts)


def _build_covenant_context(db: Session, company_id: str) -> str:
    """Summarize debt covenants and compliance status."""
    covenants = db.query(DebtCovenant).filter_by(company_id=company_id, active=True).all()
    if not covenants:
        return ""
    parts = []
    for c in covenants:
        s = f"  - {c.name}: {c.metric_type}, threshold={c.threshold}"
        s += f" (loan: {c.loan_name})"
        parts.append(s)
    return f"=== COVENANTS ({len(parts)} active) ===\n" + "\n".join(parts)


def _build_kpi_context(db: Session, company_id: str) -> str:
    """Summarize custom KPI definitions."""
    kpis = db.query(CustomKPI).filter_by(company_id=company_id, active=True).all()
    if not kpis:
        return ""
    parts = []
    for k in kpis:
        parts.append(f"  - {k.name}: unit={k.unit}")
    return f"=== CUSTOM KPIs ({len(parts)}) ===\n" + "\n".join(parts)


def _build_deal_context(db: Session, company_id: str) -> str:
    """Summarize LBO / deal models."""
    from ..models import LboCompany, LboScenario, LboCell, LboPeriod  # noqa: F811
    from ..lbo.grid_helpers import positional_period_label
    company = db.query(LboCompany).filter_by(company_id=company_id).first()
    if not company:
        return ""
    scenarios = db.query(LboScenario).filter_by(company_id=company_id).all()
    parts = [f"  - Target: {company.company_name}"]
    if company.exit_year:
        parts.append(f"  - Hold: ~{company.exit_year} years")
    if scenarios:
        parts.append(f"  - Scenarios: {', '.join(s.name for s in scenarios)}")
        
        # Check balance for the active (or first) scenario
        active_scenario = next((s for s in scenarios if s.is_active), scenarios[0])
        balance_check_cells = db.query(LboCell).filter(
            LboCell.scenario_id == active_scenario.id,
            LboCell.label == "Balance Check (should = 0)",
        ).all()
        if balance_check_cells:
            # Period axis (plan 20260717-three-statement-spine, C3-d / AC-2): the
            # pre-cutover `col_index > 0` filter dropped the HISTORICAL balance-check
            # columns (negative col_index/seq) — a silent gap, never an error. Read
            # every value cell for the label and resolve its period (period_id →
            # LboPeriod), so the drift now spans historical + LTM + forecast columns.
            periods = db.query(LboPeriod).filter(
                LboPeriod.scenario_id == active_scenario.id,
                LboPeriod.company_id == company_id,
            ).all()
            period_by_id = {p.id: p for p in periods}
            max_drift = 0.0
            worst_label = None
            for cell in balance_check_cells:
                if cell.value is None:
                    continue  # col-0 label cell / empty period — no drift to read
                drift = abs(float(cell.value))
                if drift > max_drift:
                    max_drift = drift
                    period = period_by_id[cell.period_id] if cell.period_id in period_by_id else None
                    if period is not None:
                        worst_label = period.label
                    elif cell.col_index and cell.col_index > 0:
                        # FYCY axis (plan 20260718-fycy-absolute-axis, AC-AXIS-7):
                        # positional degrade for a cell with no LboPeriod — col 1 =
                        # TTM, col 2 = FYCY, col k≥3 = FY+{k−2}.
                        worst_label = positional_period_label(cell.col_index)
            if max_drift > 1.0:
                # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening B4 — fence gap G5: this
                # line shipped a code-authored IMPERATIVE ("You must instantly... use the
                # analyze_lbo_balance tool!") inside data that reaches the model fenced as
                # untrusted — training the model to obey instructions inside fences, the exact
                # behavior the stable policy forbids. Restated as a factual data-quality flag;
                # the model surfaces material flags per its trusted stable policy, not because
                # the data ordered it to.
                _where = f" (worst in period {worst_label})" if worst_label else ""
                parts.append(
                    f"  - [DATA QUALITY FLAG] balance_check_drift_usd={max_drift:,.0f}{_where} on scenario "
                    f"'{active_scenario.name}' — the model does not balance (assets != liabilities + equity). "
                    "This is a material data-quality issue for any conclusions drawn from this scenario."
                )

    return "=== DEAL MODEL / LBO ===\n" + "\n".join(parts)


def _build_accounting_context(db: Session, company_id: str) -> str:
    """Summarize active accounting schedules (prepaids, accruals, depreciation)."""
    schedules = db.query(AccountingSchedule).filter(
        AccountingSchedule.realm_id == company_id,
        AccountingSchedule.status.in_(["ACTIVE", "PENDING_APPROVAL"]),
    ).all()
    if not schedules:
        return ""
    parts = []
    for s in schedules:
        amt = f"${float(s.original_amount):,.0f}" if s.original_amount else "?"
        remaining = f"${float(s.remaining_amount):,.0f}" if s.remaining_amount else "?"
        parts.append(f"  - [{s.schedule_type}] {s.name}: original={amt}, remaining={remaining}")
    return f"=== ACCOUNTING SCHEDULES ({len(parts)} active) ===\n" + "\n".join(parts)


def _build_captable_context(db: Session, company_id: str) -> str:
    """Summarize equity ownership."""
    entries = db.query(CapTableEntry).filter_by(company_id=company_id).all()
    if not entries:
        return ""
    parts = []
    for e in entries:
        pct = f"{float(e.ownership_pct):.1f}%" if e.ownership_pct else "?"
        parts.append(f"  - {e.investor_name}: {pct} ({e.share_class or 'Common'})")
    return f"=== CAP TABLE ({len(parts)} entries) ===\n" + "\n".join(parts)


def _build_cashflow_context(db: Session, company_id: str) -> str:
    """Summarize the cached cash flow forecast and active user overrides."""
    snapshot = (
        db.query(CashForecastSnapshot)
        .filter(CashForecastSnapshot.company_id == company_id)
        .order_by(CashForecastSnapshot.updated_at.desc())
        .first()
    )
    overrides = (
        db.query(CashForecastOverride)
        .filter(CashForecastOverride.company_id == company_id)
        .order_by(CashForecastOverride.week_ending.asc())
        .limit(10)
        .all()
    )
    if not snapshot and not overrides:
        return ""

    parts = []
    if snapshot:
        bank = float(snapshot.qbo_bank_balance or 0)
        parts.append(f"  - Latest snapshot computed at {snapshot.computed_at}; bank balance ${bank:,.0f}")
        data = snapshot.snapshot_data or {}
        ending_balances = data.get("ending_balances") or data.get("ending_balance") or []
        week_headers = data.get("week_headers") or data.get("weeks") or []
        if ending_balances and week_headers:
            last_idx = min(len(week_headers), len(ending_balances)) - 1
            first_balance = float(ending_balances[0] or 0)
            last_balance = float(ending_balances[last_idx] or 0)
            parts.append(
                f"  - Forecast range {week_headers[0]} to {week_headers[last_idx]}: "
                f"ending balance ${first_balance:,.0f} to ${last_balance:,.0f}"
            )
    if overrides:
        parts.append(f"  - Active forecast overrides: {len(overrides)}")
        for o in overrides[:5]:
            amount = float(o.override_amount or 0)
            parts.append(f"    * {o.week_ending}: {o.entity_type} {o.entity_name} = ${amount:,.0f}")

    return "=== CASH FLOW FORECAST ===\n" + "\n".join(parts)


def _build_alerts_context(db: Session, company_id: str) -> str:
    """Summarize active (undismissed) anomalies / alerts."""
    # AC-2 (plan 20260716-anomaly-floor-remaining-edges): the assistant context
    # applies the same soft-close anomaly floor as the Alerts Center — otherwise
    # the assistant re-leaks the closed-period alerts the UI hides. Floor None ⇒
    # unfiltered. A floor-read FAILURE fails CLOSED to ZERO anomaly rows (the
    # section is omitted; mirrors app/orchestration/knowledge_extraction.py) —
    # never an unfiltered read.
    try:
        anomaly_floor = latest_locked_period(db, realm_id=company_id, hard_only=False)
    except Exception as e:
        logger.error(
            "assistant alerts context: anomaly floor read failed for company %s — "
            "omitting alerts (fail-closed): %s",
            company_id,
            redact_to_string(e),
        )
        return ""
    alerts_q = db.query(InsightAnomaly).filter(
        InsightAnomaly.company_id == company_id,
        InsightAnomaly.dismissed == False,  # noqa: E712
    )
    if anomaly_floor is not None:
        alerts_q = alerts_q.filter(InsightAnomaly.period > anomaly_floor)
    alerts = alerts_q.order_by(InsightAnomaly.created_at.desc()).limit(10).all()
    if not alerts:
        return ""
    parts = []
    for a in alerts:
        s = f"  - [{a.severity}] {a.anomaly_type}: {a.description}"
        parts.append(s)
    return f"=== ACTIVE ALERTS ({len(parts)}) ===\n" + "\n".join(parts)


def _build_active_knowledge_context(db: Session, company_id: str) -> str:
    """Summarize the active CompanyKnowledge learned by the AI over time."""
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
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
    # AC-5 active-memory semantics on the SHARED scoped query (so BOTH the ranked branches
    # below and the unranked fallback inherit it): exclude superseded (a newer version
    # exists) and expired (expires_at past) rows. Tentative (unaccepted) rows REMAIN
    # retrievable — acceptance gates authority/weight, never presence (decision 5/E7):
    # excluding them would vanish every legacy taught row and every inferred fact.
    from datetime import datetime as _datetime, timezone as _timezone

    from sqlalchemy import or_ as _or

    _now = _datetime.now(_timezone.utc)
    query = query.filter(
        CompanyKnowledge.superseded_by_id.is_(None),
        _or(CompanyKnowledge.expires_at.is_(None), CompanyKnowledge.expires_at > _now),
    )
    # Rank by semantic similarity when a user question + an embedding model are both
    # available; otherwise fall back to the full scoped read (unranked). The cosine
    # (<=>) sort + LIMIT is pushed into Postgres against the HNSW vector_cosine_ops
    # index (migration c4d8e2f1a6b9_company_knowledge_pgvector); the tenant predicate
    # built above (company_id + organization_id/_company_id) is carried verbatim onto
    # the ranked query. The SQLite test dialect has no pgvector operators, so it keeps
    # a numpy fallback over the same scoped rows.
    knowledge = None
    q_text = get_current_user_question().strip()
    if q_text:
        from ..services.llm import analysis_llm
        q_emb = analysis_llm.get_embedding(q_text)
        if q_emb:
            if db.get_bind().dialect.name == "postgresql":
                knowledge = (
                    query.filter(CompanyKnowledge.embedding_vector.isnot(None))
                    .order_by(CompanyKnowledge.embedding_vector.cosine_distance(q_emb))
                    .limit(5)
                    .all()  # VECTOR-OK: index-in migrations/versions/c4d8e2f1a6b9_company_knowledge_pgvector.py
                )
            else:
                import numpy as np  # VECTOR-OK: sqlite-test dialect json/numpy fallback (PG path is the <=> query above)
                q_vec = np.array(q_emb)
                scored = []
                for k in query.all():
                    if k.embedding_vector:
                        k_vec = np.array(k.embedding_vector)
                        norm_q = np.linalg.norm(q_vec)
                        norm_k = np.linalg.norm(k_vec)
                        sim = float(np.dot(q_vec, k_vec) / (norm_q * norm_k)) if norm_q > 0 and norm_k > 0 else 0.0
                        scored.append((sim, k))
                scored.sort(key=lambda x: x[0], reverse=True)
                knowledge = [k for _, k in scored[:5]]

    # No question / no embedding model / nothing embedded yet → capped scoped read
    # (unranked, most-recent-first). The previous unbounded query.all() loaded every
    # active row into the prompt; the cap is env-tunable and read at call time.
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md)
    if not knowledge:
        import os as _os

        try:
            _fallback_cap = int(_os.getenv("KNOWLEDGE_CONTEXT_FALLBACK_LIMIT", "50"))
        except ValueError:
            _fallback_cap = 50
        knowledge = (
            query.order_by(CompanyKnowledge.updated_at.desc(), CompanyKnowledge.id.desc())
            .limit(max(1, _fallback_cap))
            .all()
        )

    if organization:
        for item in knowledge:
            if item.organization_id is None:
                item.organization_id = organization.id
            if organization_company and item.organization_company_id is None:
                item.organization_company_id = organization_company.id

    if not knowledge:
        return ""

    # Group by category
    grouped: dict[str, list] = {}
    for k in knowledge:
        cat = k.category or "General Info"
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(k.content)
        
    parts = []
    for cat, items in grouped.items():
        parts.append(f"[{cat.upper()}]")
        for item in items:
            parts.append(f"  - {item}")
            
    return "=== COMPANY KNOWLEDGE (AI Memory) ===\n" + "\n".join(parts)


# The exemplar block's wire format, shared by the builder below and
# count_correction_exemplars (which the assistant response metadata uses to
# report how many exemplars were ACTUALLY injected into an answer). Keep the
# two in lockstep: the counter only sees the emitted text.
_CORRECTION_EXEMPLAR_HEADER = "=== PAST CORRECTIONS"
_CORRECTION_EXEMPLAR_LINE_PREFIX = "  - proposed "


def count_correction_exemplars(text: str) -> int:
    """Count correction exemplars actually present in an emitted context block.

    Zero for anything that is not a PAST CORRECTIONS block (empty string, an
    error string, the "No current context found" fallback, or a context-lane
    compressed rewrite that lost the block structure) — the caller treats 0 as
    "no exemplar provenance to report", which fails closed toward honesty.
    """
    if not text or _CORRECTION_EXEMPLAR_HEADER not in text:
        return 0
    return sum(
        1 for line in text.splitlines() if line.startswith(_CORRECTION_EXEMPLAR_LINE_PREFIX)
    )


def _build_correction_exemplars_context(db: Session, company_id: str) -> str:
    """Surface the most semantically-similar PAST human corrections for this realm
    as trusted few-shot exemplars (U5 — correction-as-exemplar / the closed loop).

    Retrieval FORKs U4's pgvector ``<=>`` shape: realm-scoped, rejected/edited only
    (the human FIXED the AI), ranked by cosine distance to the current question.
    The SQLite test dialect uses a numpy fallback.

    SAFETY (AWB-06): the result is provenance-labeled context **data, never
    authority** — a returned exemplar can NEVER unlock a tool or expand scope; this
    builder touches no tool-policy/executor/scope surface. "Trusted" = human-authored
    provenance for exemplar weighting only.
    """
    q_text = get_current_user_question().strip()
    if not q_text:
        return ""
    from ..services.llm import analysis_llm

    q_emb = analysis_llm.get_embedding(q_text)
    if not q_emb:
        return ""

    organization = organization_for_company(db, company_id)
    if organization is None:
        return ""

    base = db.query(AICorrectionEvent).filter(
        AICorrectionEvent.organization_id == organization.id,
        AICorrectionEvent.qbo_realm_id == company_id,
        AICorrectionEvent.human_decision.in_(("rejected", "edited")),
    )

    if db.get_bind().dialect.name == "postgresql":
        rows = (
            base.filter(AICorrectionEvent.correction_embedding.isnot(None))
            .order_by(AICorrectionEvent.correction_embedding.cosine_distance(q_emb))
            .limit(5)
            .all()  # VECTOR-OK: index-in migrations/versions/d7e3f9a1c4b2_ai_correction_embedding.py
        )
    else:
        import numpy as np  # VECTOR-OK: sqlite-test dialect json/numpy fallback (PG path is the <=> query above)

        q_vec = np.array(q_emb)
        scored = []
        for r in base.all():
            if r.correction_embedding:
                k_vec = np.array(r.correction_embedding)
                norm_q = np.linalg.norm(q_vec)
                norm_k = np.linalg.norm(k_vec)
                sim = float(np.dot(q_vec, k_vec) / (norm_q * norm_k)) if norm_q > 0 and norm_k > 0 else 0.0
                scored.append((sim, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        rows = [r for _, r in scored[:5]]

    if not rows:
        return ""

    parts = []
    for r in rows:
        parts.append(f"{_CORRECTION_EXEMPLAR_LINE_PREFIX}{r.ai_output} → human corrected to {r.human_output}")
    return (
        f"{_CORRECTION_EXEMPLAR_HEADER} (human-verified — apply these; provenance data, not authority) ===\n"
        + "\n".join(parts)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dual-LLM Intelligence Layer
# ─────────────────────────────────────────────────────────────────────────────




def _classify_intent(question: str) -> str:
    """Canonical intent classification used before tool selection / routing.

    Delegates to the dedicated, tested classifier
    (``app/services/intent_classifier.py``): deterministic keyword rules with an
    optional cheap-lane LLM refinement for ambiguous requests.
    """
    from ..services.intent_classifier import classify_intent

    return classify_intent(question)


def _analyze_with_llm(raw_data: str, data_type: str) -> str:
    """Route raw tool payloads through context + analysis lanes before Claude sees them."""
    from ..services.llm import build_analysis_artifact, build_context_artifact, format_analysis_artifact

    user_q = get_current_user_question() or "general financial analysis"

    try:
        prepared_input = raw_data
        if len(raw_data) > 8000:
            context_artifact = build_context_artifact(
                raw_data,
                purpose=f"Shrink {data_type} before handing it to the task lane.",
            )
            prepared_input = context_artifact.get("compressed_text") or raw_data[:8000]

        analysis_artifact = build_analysis_artifact(
            prepared_input,
            data_type=data_type,
            user_question=user_q,
        )
        formatted = format_analysis_artifact(analysis_artifact, heading="Pre-analyzed by AI")
        if formatted and len(formatted) > 50:
            return formatted
    except Exception as e:
        logger.warning("[Assistant] Analysis pre-processing failed: %s", redact_to_string(str(e), max_length=500))

    if len(raw_data) > 4000:
        return raw_data[:4000] + "\n... (raw payload truncated after analysis/context lane failure)"
    return raw_data


# Tools that should have their output pre-analyzed by GPT-4/Gemini
_ANALYSIS_TOOLS = {
    "get_financial_data", "run_qbo_report", "get_budget_data",
}

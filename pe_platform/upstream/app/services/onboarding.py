"""
app/services/onboarding.py
────────────────────────────
Company Cold-Start Bootstrap service.

Called once when a new QBO company connects to Vesper Bot for the first time.
Pulls 6 months of General Ledger history, analyzes it with GPT-4o, and
pre-populates CompanyMemory + seeds ClassificationRule entries so the AI
starts with a rich understanding of the business rather than a blank slate.
"""
from __future__ import annotations

import json  # noqa: F401
import logging
from collections import defaultdict
from datetime import date, timedelta  # noqa: F401
from decimal import Decimal  # noqa: F401
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..models import ClassificationRule, CompanyMemory, QBOAccount
from ..qbo_client import QBOClient
from ..services.tenant_companies import organization_company_for_realm, organization_for_company

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# GL PARSER
# ──────────────────────────────────────────────────────────────────────────────

def _parse_general_ledger(report: Dict[str, Any]) -> List[Dict]:
    """
    Walk the QBO GeneralLedger report structure and return a flat list of
    transaction lines:
      [{date, account_name, account_type, txn_type, vendor, amount}, ...]

    QBO GL report structure:
      report.Rows.Row[] → each top-level Row is an ACCOUNT section
        Row.Header.ColData[0].value = account name
        Row.Rows.Row[] → individual transaction lines
          Row.ColData[N].value = column values (date, type, name, amount etc.)
    """
    lines = []
    try:
        columns = [c.get("ColTitle", "") for c in report.get("Columns", {}).get("Column", [])]
        # Map column index by title
        col_idx = {title: i for i, title in enumerate(columns)}

        rows = report.get("Rows", {}).get("Row", [])
        for account_row in rows:
            if account_row.get("type") != "Section":
                continue
            header = account_row.get("Header", {})
            account_name = ""
            try:
                account_name = header.get("ColData", [{}])[0].get("value", "")
            except Exception:
                pass

            sub_rows = account_row.get("Rows", {}).get("Row", [])
            for txn_row in sub_rows:
                if txn_row.get("type") == "Data":
                    cols = txn_row.get("ColData", [])
                    def _col(key: str) -> str:
                        idx = col_idx.get(key, -1)
                        if idx < 0 or idx >= len(cols):
                            return ""
                        return str(cols[idx].get("value", ""))

                    raw_amount = _col("Amount") or _col("Nat. Amount") or "0"
                    try:
                        amount = float(raw_amount.replace(",", ""))
                    except ValueError:
                        amount = 0.0

                    lines.append({
                        "date": _col("Date") or _col("Tx Date"),
                        "account_name": account_name,
                        "txn_type": _col("Transaction Type") or _col("Type"),
                        "vendor": _col("Name") or _col("Payee") or "",
                        "memo": _col("Memo/Description") or _col("Memo") or "",
                        "amount": amount,
                    })
    except Exception as e:
        logger.warning(f"GL parse error: {e}")

    return lines


def _build_analysis_summary(lines: List[Dict]) -> str:
    """
    Condense GL lines into a token-efficient summary for the AI prompt:
    - Monthly revenue/expense totals
    - Top 30 vendors by total spend
    - Top income sources by amount
    """
    vendor_spend: Dict[str, float] = defaultdict(float)
    monthly_income: Dict[str, float] = defaultdict(float)
    monthly_expense: Dict[str, float] = defaultdict(float)

    income_types = {"Invoice", "Payment", "Sales Receipt", "Credit Memo", "Income",
                    "Journal Entry", "Deposit", "Revenue"}   # JEs & Deposits = service biz revenue
    expense_types = {"Bill", "Expense", "Check", "Purchase", "Bill Payment", "Journal Entry"}

    for line in lines:
        vendor = line["vendor"].strip()
        amount = abs(line["amount"])
        month = line["date"][:7] if len(line["date"]) >= 7 else "unknown"
        txn_type = line["txn_type"]

        if vendor:
            vendor_spend[vendor] += amount

        if any(t in txn_type for t in income_types):
            monthly_income[month] += amount
        elif any(t in txn_type for t in expense_types):
            monthly_expense[month] += amount

    top_vendors = sorted(vendor_spend.items(), key=lambda x: x[1], reverse=True)[:30]

    summary_lines = ["=== 6-Month General Ledger Summary ===\n"]

    summary_lines.append("Monthly Income vs Expense:")
    all_months = sorted(set(list(monthly_income.keys()) + list(monthly_expense.keys())))
    for m in all_months:
        inc = monthly_income.get(m, 0)
        exp = monthly_expense.get(m, 0)
        summary_lines.append(f"  {m}: Revenue=${inc:,.0f}  Expenses=${exp:,.0f}")

    summary_lines.append("\nTop Vendors/Payees by Total Spend:")
    for vendor, total in top_vendors:
        summary_lines.append(f"  {vendor}: ${total:,.0f}")

    summary_lines.append(f"\nTotal GL lines analyzed: {len(lines)}")

    return "\n".join(summary_lines)


# ──────────────────────────────────────────────────────────────────────────────
# MAIN BOOTSTRAP FUNCTION
# ──────────────────────────────────────────────────────────────────────────────

def bootstrap_company_memory(db: Session, client: QBOClient, realm_id: str) -> Dict:
    """
    Full bootstrap pipeline:
    1. Check if already bootstrapped (idempotent)
    2. Pull 6-month General Ledger
    3. Parse + summarize for AI
    4. Call GPT-4o for deep analysis
    5. Write CompanyMemory
    6. Seed ClassificationRules from vendor patterns

    Returns a summary dict of what was done.
    """
    organization = organization_for_company(db, realm_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=realm_id)
        if organization
        else None
    )

    # ── 1. Idempotency gate ──────────────────────────────────────────────────
    existing_memory_query = db.query(CompanyMemory).filter(CompanyMemory.company_id == realm_id)
    if organization:
        existing_memory_query = existing_memory_query.filter(
            (CompanyMemory.organization_id == organization.id) | (CompanyMemory.organization_id.is_(None))
        )
    else:
        existing_memory_query = existing_memory_query.filter(CompanyMemory.organization_id.is_(None))
    existing_memory = existing_memory_query.first()
    if existing_memory and organization:
        existing_memory.organization_id = organization.id
        if organization_company:
            existing_memory.organization_company_id = organization_company.id
    if existing_memory and existing_memory.learned_rules:
        logger.info(f"[Bootstrap] realm={realm_id} already bootstrapped — skipping")
        return {"status": "skipped", "reason": "already bootstrapped"}

    # ── 2. Pull General Ledger ───────────────────────────────────────────────
    logger.info(f"[Bootstrap] Fetching 6-month GL for realm={realm_id}")
    gl_report = client.get_general_ledger(months_back=6)
    if not gl_report:
        return {"status": "error", "reason": "GL report returned empty"}

    # ── 3. Parse + summarize ─────────────────────────────────────────────────
    lines = _parse_general_ledger(gl_report)
    if not lines:
        logger.warning(f"[Bootstrap] No GL lines parsed for realm={realm_id}")
        return {"status": "error", "reason": "No parseable GL lines"}

    gl_summary = _build_analysis_summary(lines)
    logger.info(f"[Bootstrap] Parsed {len(lines)} GL lines, summary built")

    # ── 4. LLM analysis ──────────────────────────────────────────────────────
    from ..services.llm import analysis_llm

    accounts = (
        db.query(QBOAccount)
        .filter(QBOAccount.realm_id == realm_id, QBOAccount.active == True)  # noqa: E712
        .limit(100)
        .all()
    )
    account_list = "\n".join(
        f"  {a.qbo_id} | {a.name} ({a.account_type})" for a in accounts
    )

    prompt = f"""You are an expert CFO analyzing a new client's financial history.

Based on the following 6-month General Ledger summary, extract deep business intelligence.

{gl_summary}

Chart of Accounts:
{account_list}

Respond with a single JSON object containing these exact keys:

{{
  "company_profile": "A 150-200 word paragraph describing what this company does based on the financial data. Include: industry/sector, business model (services vs product, B2B vs B2C), revenue model (recurring, project-based, transactional), approximate scale (annual revenue range), key financial personality (e.g. high-margin professional services, capital-intensive manufacturer, lean SaaS). Write as though you are a CFO describing the business to a colleague.",
  "learned_rules": [
    {{"vendor_keyword": "...", "account_id": "...", "account_name": "...", "confidence": 0.95, "note": "..."}}
  ],
  "trend_summaries": {{
    "revenue_trajectory": "...",
    "expense_trajectory": "...",
    "top_revenue_streams": ["...", "..."],
    "top_expense_categories": ["...", "..."]
  }},
  "seasonality": {{
    "high_revenue_months": ["..."],
    "high_expense_months": ["..."],
    "notes": "..."
  }},
  "cash_behavior": {{
    "avg_monthly_burn": 0,
    "avg_monthly_revenue": 0,
    "cash_cycle_description": "...",
    "notes": "..."
  }},
  "vendor_behavior": {{
    "top_vendors": [
      {{"name": "...", "monthly_avg": 0, "account_suggestion": "...", "frequency": "..."}}
    ]
  }},
  "customer_behavior": {{
    "primary_revenue_sources": ["..."],
    "payment_patterns": "...",
    "concentration_risk": "...",
    "recurring_invoices": [
      {{"customer": "...", "monthly_amount": 0, "services": "...", "payment_reliability": "..." }}
    ]
  }},
  "risk_map": {{
    "flags": ["..."],
    "concentration_risks": ["..."],
    "unusual_items": ["..."]
  }}
}}

Be specific and data-driven. Use actual vendor names, amounts, and account names from the data."""

    try:
        analysis = analysis_llm.predict_json(
            system_prompt="You are an expert CFO analyzing a new client's financial history. Always respond with valid JSON.",
            user_prompt=prompt,
            temperature=0.1,
        )
        if analysis is None:
            return {"status": "error", "reason": "LLM returned no response"}
    except Exception as e:
        logger.error(f"[Bootstrap] LLM analysis failed: {e}")
        return {"status": "error", "reason": f"AI analysis failed: {e}"}

    # ── 5. Write CompanyMemory ───────────────────────────────────────────────
    if existing_memory:
        memory = existing_memory
    else:
        memory = CompanyMemory(
            company_id=realm_id,
            organization_id=organization.id if organization else None,
            organization_company_id=organization_company.id if organization_company else None,
        )
        db.add(memory)

    memory.company_profile = analysis.get("company_profile", "")
    memory.learned_rules = analysis.get("learned_rules", [])
    memory.trend_summaries = analysis.get("trend_summaries", {})
    memory.seasonality = analysis.get("seasonality", {})
    memory.cash_behavior = analysis.get("cash_behavior", {})
    memory.vendor_behavior = analysis.get("vendor_behavior", {})
    memory.customer_behavior = analysis.get("customer_behavior", {})
    memory.risk_map = analysis.get("risk_map", {})

    db.commit()
    logger.info(f"[Bootstrap] CompanyMemory written for realm={realm_id}")

    # ── 6. Seed ClassificationRules ──────────────────────────────────────────
    rules_seeded = 0
    for rule_data in analysis.get("learned_rules", []):
        keyword = rule_data.get("vendor_keyword", "").strip().lower()
        account_id = rule_data.get("account_id", "").strip()
        account_name = rule_data.get("account_name", "")

        if not keyword or not account_id:
            continue

        # Skip if a rule for this keyword already exists
        existing_rule = (
            db.query(ClassificationRule)
            .filter(
                ClassificationRule.realm_id == realm_id,
                ClassificationRule.keyword == keyword,
            )
            .first()
        )
        if existing_rule:
            continue

        db.add(ClassificationRule(
            realm_id=realm_id,
            keyword=keyword,
            target_account_id=account_id,
            target_account_name=account_name,
            priority=5,  # Lower than user-defined rules
            active=True,
        ))
        rules_seeded += 1

    db.commit()
    logger.info(f"[Bootstrap] Seeded {rules_seeded} ClassificationRules for realm={realm_id}")

    return {
        "status": "ok",
        "gl_lines_analyzed": len(lines),
        "rules_seeded": rules_seeded,
        "memory_fields_written": 7,
    }

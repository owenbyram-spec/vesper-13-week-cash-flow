"""
app/assistant/handlers.py
─────────────────────────
Tool handler functions for the Peregrine AI Assistant.
Each _handle_* function processes one tool call and returns a text summary.
"""
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text  # PLAN-EXEMPT: P0 proposer FORCE-RLS write pin/diag (2026-06-23)
# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — SQL-side document/agreement
# search bounding (AC-2: ilike predicates + LIMIT instead of loading raw_text rows).
from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session
from requests.exceptions import HTTPError  # noqa: F401

from ..db import get_db  # noqa: F401
from ..qbo_client import get_qbo_client_from_db
# PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — cache GL read now pages via query_gl_lines_page.
from ..services.qbo_gl_cache import format_gl_lines_for_assistant, query_gl_lines_page, upsert_gl_report_lines
from ..services.qbo_report_cache import get_cached_qbo_report
from ..services.errors import safe_error_detail
from ..services.tool_outcomes import DraftedToolResult, ToolFailure
# Plan 20260712-agentic-finance-os W2 (GAP-2): typed-fact emission from the deterministic
# read handlers — the values below are the SAME variables that render the tool text.
from ..services.evidence import emit_fact
from ..services.redaction import redact_to_string
from ..services.bank_recon.session_status import (
    effective_session_status,
    is_open_session_status,
    live_session_status_clause,
)
from ..semantics import Basis, PeriodGrain, ScenarioKind
# PLAN-EXEMPT: P0 fix — pin realm GUC before the FORCE-RLS proposed_actions INSERT (2026-06-23)
from ..tenant_rls import TenantScope, emit_tenant_guc
from ..models import (
    ChatConversation, ChatMessage, CompanyMemory, QBOCompanyInfo,  # noqa: F401
    ProposedAction, AssistantContextCache, ExecutiveBriefing,  # noqa: F401
    BudgetLine, BudgetDriver,  # noqa: F401
    Document, CompanyAgreement,
    CloseTask,
    DebtCovenant,
    CustomKPI,  # noqa: F401
    LboCompany, LboScenario,  # noqa: F401
    AccountingSchedule, ScheduleEntry,
    CapTableEntry,  # noqa: F401
    InsightAnomaly,  # noqa: F401
    LeaseSchedule,  # noqa: F401
    Report,
)

# QBO analysis packs
from ..analysis.vendor_spend import vendor_spend_summary, vendor_spend_monthly
from ..analysis.customer_revenue import customer_revenue_summary, customer_revenue_monthly
from ..analysis.expense_trends import expense_trend_mom
from ..analysis.profit_margin import profit_and_margin_by_month
from ..analysis.cogs_anomaly import cogs_anomalies
from ..analysis.ar_aging import ar_aging
from ..analysis.ap_aging import ap_aging
from ..analysis.anomalies import transaction_anomalies

from .context import get_current_user_question, _analyze_with_llm, _ANALYSIS_TOOLS, build_module_context  # noqa: F401

logger = logging.getLogger(__name__)


def _assistant_safe_error(
    user_message: str,
    exc: Exception | None = None,
    *,
    error_code: str = "assistant_tool_unavailable",
    retryable: bool = True,
    log_context: str = "assistant_tool_error",
) -> str:
    detail = safe_error_detail(
        error_code=error_code,
        user_message=user_message,
        exc=exc,
        retryable=retryable,
        log_context=log_context,
    )
    return f"{detail['user_message']} Reference: {detail['trace_id']}."


def _read_failure(
    user_message: str,
    exc: Exception | None = None,
    *,
    error_code: str = "assistant_tool_unavailable",
    retryable: bool = True,
    log_context: str = "assistant_tool_error",
) -> str:
    if isinstance(exc, ToolFailure):
        raise exc
    raise ToolFailure(error_code, _assistant_safe_error(user_message, exc, error_code=error_code, retryable=retryable, log_context=log_context), retryable=retryable)


# ─────────────────────────────────────────────────────────────────────────────
# Tool Handlers — each returns a text summary for Claude
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_account_name(client, account_name: str, context_hint: str = "") -> dict:
    """
    Fuzzy-match an account name to a QBO account.
    Uses accounting context: 'accrued expenses' → Liability, not P&L.

    Returns {"id": "...", "name": "...", "type": "..."} or raises ValueError.
    """
    search = account_name.lower().strip()
    import re  # Used for extracting account numbers below
    query = "SELECT * FROM Account MAXRESULTS 500"
    raw = client.query(query)
    accounts = raw.get("QueryResponse", {}).get("Account", [])

    # Known accounting patterns — map natural language to expected account types
    # This handles the "accrued expenses means Liability, not P&L" problem
    ACCOUNTING_PATTERNS = {
        # Liability accounts
        "accrued expenses": ["Other Current Liability", "Accounts Payable"],
        "accrued liabilities": ["Other Current Liability"],
        "accounts payable": ["Accounts Payable"],
        "deferred revenue": ["Other Current Liability"],
        "unearned revenue": ["Other Current Liability"],
        "sales tax payable": ["Other Current Liability"],
        "payroll liabilities": ["Other Current Liability"],
        # Asset accounts
        "accounts receivable": ["Accounts Receivable"],
        "prepaid expenses": ["Other Current Asset"],
        "inventory": ["Other Current Asset"],
        # Expense accounts
        "consulting fees": ["Expense"],
        "consulting expenses": ["Expense"],
        "rent expense": ["Expense"],
        "salary expense": ["Expense"],
        "utilities": ["Expense"],
        "depreciation": ["Expense"],
        "amortization": ["Expense"],
        "interest expense": ["Expense"],
        # Revenue accounts
        "revenue": ["Income"],
        "sales": ["Income"],
        "service income": ["Income"],
        # Bank
        "checking": ["Bank"],
        "savings": ["Bank"],
        "cash": ["Bank", "Other Current Asset"],
    }

    # Find the best matching pattern for expected account types
    expected_types = []
    for pattern, types in ACCOUNTING_PATTERNS.items():
        if pattern in search or search in pattern:
            expected_types = types
            break

    # Split search into individual words for matching
    search_words = set(search.replace("-", " ").split())

    scored = []
    for a in accounts:
        name = a.get("Name", "")
        name_lower = name.lower()
        fqn = a.get("FullyQualifiedName", "").lower()  # noqa: F841
        acct_type = a.get("AccountType", "")
        acct_num = str(a.get("AcctNum", "")).strip()

        # ── Account number matching (highest priority) ───────────────────
        # If search contains a number like "7430", match against QBO AcctNum
        # Also check if the number appears in the Name field (e.g. "7430 Software Expense")
        search_nums = re.findall(r'\d{3,}', search)  # Extract 3+ digit numbers from search
        name_nums = re.findall(r'\d{3,}', name_lower)  # Extract numbers from account name
        acct_num_match = False
        if search_nums:
            for sn in search_nums:
                # Match against AcctNum field
                if acct_num and sn == acct_num:
                    acct_num_match = True
                    break
                # Also match against numbers embedded in the Name field
                if sn in name_nums:
                    acct_num_match = True
                    break

        # Split account name into words for exact word matching
        # Also include the acct_num as a matchable "word"
        name_words = set(name_lower.replace("-", " ").split())
        if acct_num:
            name_words.add(acct_num.lower())

        # Count exact word matches (not substring)
        common_words = search_words & name_words
        if not common_words and not acct_num_match:
            # Also check if any search word is a substring of any name word
            partial = sum(
                1 for sw in search_words
                for nw in name_words
                if sw in nw or nw in sw
            )
            if partial == 0:
                continue
            score = partial * 3  # Partial matches worth less
        else:
            score = len(common_words) * 10

        # Account number exact match — strongest signal
        if acct_num_match:
            score += 500

        # Exact name match bonus
        if search == name_lower:
            score += 200
        elif name_lower in search or search in name_lower:
            score += 50

        # STRONG accounting pattern bonus/penalty
        if expected_types:
            if acct_type in expected_types:
                score += 80  # Big bonus for matching expected type
            else:
                score -= 30  # Penalty for wrong account type

        # Word coverage bonus — prefer accounts where more of the search matches
        coverage = len(common_words) / max(len(search_words), 1)
        score += int(coverage * 20)

        scored.append((score, a))

    if not scored:
        raise ValueError(
            f"Could not find an account matching '{account_name}'. "
            f"Try using search_chart_of_accounts to find the right account."
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]
    # Debug: log top matches so we can see resolution results
    top3 = [(s, a.get("AcctNum", ""), a.get("Name", "")) for s, a in scored[:3]]
    logger.info(f"[Assistant] _resolve_account_name('{account_name}') top matches: {top3}")
    return {
        "id": best["Id"],
        "name": best["Name"],
        "type": best.get("AccountType", ""),
        "_all_accounts": accounts,  # Pass along for sub-account lookups
    }


def _resolve_account_with_children(client, account_name: str) -> dict:
    """
    Resolve an account name, then find ALL sub-accounts beneath it.
    Returns {id: str, name: str, type: str, account_ids: str}
    where account_ids is a comma-separated list suitable for the QBO GL report.
    """
    resolved = _resolve_account_name(client, account_name)
    parent_id = resolved["id"]
    all_accounts = resolved.get("_all_accounts", [])

    # Log the raw QBO data for the matched account so we can debug
    matched_raw = None
    for a in all_accounts:
        if a.get("Id") == parent_id:
            matched_raw = a
            break
    if matched_raw:
        logger.info(
            f"[Assistant] MATCHED ACCOUNT RAW DATA: "
            f"Id={matched_raw.get('Id')} "
            f"Name='{matched_raw.get('Name')}' "
            f"AcctNum='{matched_raw.get('AcctNum', '')}' "
            f"FullyQualifiedName='{matched_raw.get('FullyQualifiedName', '')}' "
            f"AccountType='{matched_raw.get('AccountType', '')}' "
            f"SubAccount={matched_raw.get('SubAccount', False)} "
            f"ParentRef={matched_raw.get('ParentRef', 'None')} "
            f"Active={matched_raw.get('Active', True)}"
        )

    # Find sub-accounts: any account where ParentRef.value == parent_id
    child_ids = []
    for a in all_accounts:
        parent_ref = a.get("ParentRef", {})
        if parent_ref and str(parent_ref.get("value", "")) == str(parent_id):
            child_ids.append(a["Id"])
            logger.info(
                f"[Assistant] Found sub-account: "
                f"AcctNum='{a.get('AcctNum', '')}' Name='{a.get('Name', '')}' "
                f"Id={a['Id']} SubAccount={a.get('SubAccount', False)}"
            )

    # Also check grandchildren (2 levels deep)
    if child_ids:
        for a in all_accounts:
            parent_ref = a.get("ParentRef", {})
            if parent_ref and str(parent_ref.get("value", "")) in [str(c) for c in child_ids]:
                if a["Id"] not in child_ids:
                    child_ids.append(a["Id"])
                    logger.info(f"[Assistant] Found grandchild: AcctNum='{a.get('AcctNum', '')}' Name='{a.get('Name', '')}' Id={a['Id']}")

    # Combine parent + all children
    all_ids = [parent_id] + child_ids
    account_ids_str = ",".join(str(aid) for aid in all_ids)

    logger.info(
        f"[Assistant] _resolve_account_with_children('{account_name}') → "
        f"{resolved['name']} (ID: {parent_id}) + {len(child_ids)} sub-accounts = {len(all_ids)} total IDs: [{account_ids_str}]"
    )

    return {
        "id": parent_id,
        "name": resolved["name"],
        "type": resolved["type"],
        "account_ids": account_ids_str,
        "child_count": len(child_ids),
    }


# PLAN-EXEMPT: P0 fix — pin realm GUC before a FORCE-RLS proposed_actions write (2026-06-23)
def _pin_realm_write_scope(db: Session, realm_id: str) -> dict:
    """Emit the realm GUC on the connection the proposed_actions INSERT will flush on, and return
    the resulting GUC values for diagnostics.

    proposed_actions is realm-keyed under FORCE RLS (policy WITH CHECK is
    company_id = app.current_realm_id). The assistant JE/budget proposal handlers run on the
    streaming run-loop session; relying on the run-loop's _stream_run_scope listener proved
    insufficient for this realm-keyed write (the INSERT was RLS-denied in prod). Emitting realm
    here, on the write's own connection, makes the WITH CHECK pass. No-op on SQLite.
    """
    emit_tenant_guc(db.connection(), TenantScope(realm_id=realm_id))
    try:
        row = db.execute(
            text(
                "SELECT current_setting('app.current_realm_id', true) AS realm, "
                "current_setting('app.rls_bypass', true) AS bypass"
            )
        ).mappings().first()
        return dict(row) if row else {}
    except Exception:  # pragma: no cover - diagnostic must never break the write
        return {}


def _handle_create_journal_entry(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """Creates a ProposedAction for a JE, auto-resolving account names to IDs."""
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C3 (marker
    # RATIFICATION-2026-07-13.md on disk). Decision 4: pop the injected _dry_run
    # flag at entry; the dry exit sits after account resolution (the natural
    # pre-draft point) so the preview is truthful about what would be drafted.
    # NOT a new tool: the existing safety_expectation AgentEvalFixture
    # create_journal_entry_je_proposal (app/agent/evals.py) is unchanged.
    dry_run = _dry_run_requested(params)

    # Auto-resolve account names to QBO account IDs
    lines = params.get("lines", [])
    if not lines:
        return "Error: Journal entry requires at least one line with an account."

    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        return _assistant_safe_error(
            "QuickBooks could not be reached for this journal entry.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_create_journal_entry_qbo_error",
        )

    resolved_lines = []
    resolution_notes = []
    for line in lines:
        account_id = line.get("account_id")
        account_name = line.get("account_name", "")
        resolved_name = account_name  # default to original

        if not account_id and account_name:
            # Determine context from debit/credit to help with accounting logic
            context = ""
            if line.get("debit", 0) > 0:
                context = "debit"
            elif line.get("credit", 0) > 0:
                context = "credit"

            try:
                match = _resolve_account_name(client, account_name, context)
                account_id = match["id"]
                resolved_name = match["name"]  # use QBO's canonical name
                resolution_notes.append(
                    f"'{account_name}' -> {match['name']} (ID: {match['id']}, {match['type']})"
                )
            except ValueError as e:
                return _assistant_safe_error(
                    "The journal entry account could not be resolved. Please provide the exact QuickBooks account.",
                    e,
                    error_code="assistant_account_resolution_failed",
                    retryable=False,
                    log_context="assistant_journal_entry_account_resolution_error",
                )

        if not account_id:
            return f"Error: Line missing both account_id and account_name: {line}"

        resolved_lines.append({
            "account_id": account_id,
            "account_name": resolved_name,
            "debit": line.get("debit", 0),
            "credit": line.get("credit", 0),
            "description": line.get("description", params.get("memo", "")),
        })

    # Save with resolved IDs
    resolved_params = {
        "date": params.get("date"),
        "memo": params.get("memo", ""),
        "lines": resolved_lines,
    }

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit
    # (post-resolution, pre-draft; safety_expectation AgentEvalFixture unchanged).
    if dry_run:
        preview_debit = sum(line_item.get("debit", 0) for line_item in resolved_lines)
        resolution_text = (
            " | ".join(resolution_notes) if resolution_notes else "all lines carried explicit account IDs"
        )
        return _dry_run_notice(
            f"a JOURNAL_ENTRY proposal for ${preview_debit:,.2f} dated {params.get('date')}, "
            f"memo '{params.get('memo')}', {len(resolved_lines)} line(s); accounts: {resolution_text}."
        )

    action = ProposedAction(
        company_id=realm_id,
        action_type="JOURNAL_ENTRY",
        payload=resolved_params,
        reasoning="Created by Peregrine AI Assistant",
        confidence_score=0.9,
        status="PENDING",
    )
    # PLAN-EXEMPT: P0 fix — pin realm GUC before the FORCE-RLS proposed_actions INSERT (2026-06-23)
    guc = _pin_realm_write_scope(db, realm_id)
    db.add(action)
    try:
        db.commit()
    except Exception as exc:
        _msg = (
            f"[ProposerDiag/JE] proposed_actions INSERT denied: flush_conn_realm_guc={guc.get('realm')!r} "
            f"bypass={guc.get('bypass')!r} action.company_id={realm_id!r}; cause={redact_to_string(exc)}"
        )
        raise RuntimeError(_msg) from exc
    db.refresh(action)

    total_debit = sum(l.get("debit", 0) for l in resolved_lines)  # noqa: E741
    resolution_text = " | ".join(resolution_notes) if resolution_notes else ""
    return (
        DraftedToolResult((f"Journal entry drafted (Action #{action.id}). "
        f"Amount: ${total_debit:,.2f}, Date: {params.get('date')}, Memo: {params.get('memo')}. "
        f"Accounts matched: {resolution_text}. "
        f"The user must approve this in the Actions page before it posts to QuickBooks."), proposal_id=action.id)
    )


# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C3 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Decision 7 / AC-5: the duplicate
# FIRST _handle_update_budget_line def (byte-identical to the surviving one
# below) is DELETED — Python bound the second def anyway, so this is
# behavior-neutral; the month helpers between the two defs survive here.

# Map for normalizing YYYY-MM keys to three-letter month keys
_MONTH_NUM_TO_KEY = {
    "01": "jan", "02": "feb", "03": "mar", "04": "apr",
    "05": "may", "06": "jun", "07": "jul", "08": "aug",
    "09": "sep", "10": "oct", "11": "nov", "12": "dec",
    "1": "jan", "2": "feb", "3": "mar", "4": "apr",
    "5": "may", "6": "jun", "7": "jul", "8": "aug",
    "9": "sep",
}
_VALID_MONTH_KEYS = {"jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"}

def _normalize_month_key(key: str) -> str:
    """Convert any month format to three-letter key: '2026-01' -> 'jan', 'Jan' -> 'jan',
    'March' -> 'mar', '3' -> 'mar'. Storage stays jan..dec (decision 7); an
    unrecognized key is returned as-is so each caller can refuse it honestly."""
    k = key.strip().lower()
    if k in _VALID_MONTH_KEYS:
        return k
    # Handle YYYY-MM format
    if "-" in k:
        parts = k.split("-")
        if len(parts) == 2:
            month_num = parts[1]
            return _MONTH_NUM_TO_KEY.get(month_num, k)
    # Handle bare number
    if k in _MONTH_NUM_TO_KEY:
        return _MONTH_NUM_TO_KEY[k]
    # Handle full month names ('march' -> 'mar') — the historical [:3] tolerance
    # of get_budget_vs_actuals, now shared by every ingestion point.
    if k[:3] in _VALID_MONTH_KEYS:
        return k[:3]
    return k  # Return as-is if unrecognized


# ─────────────────────────────────────────────────────────────────────────────
# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C3 (marker
# RATIFICATION-2026-07-13.md on disk). Decision 4 / AC-3: the handler-lane
# dry_run honor. Every MODEL-VISIBLE handler-lane self-drafter pops the injected
# `_dry_run` flag at ENTRY (so a stored `payload=params` can never carry it) and,
# when set, exits at its natural pre-draft point with a truthful "what WOULD be
# drafted" summary that carries NO proposal id. This registers NO new tool and NO
# new write capability: every member below is an EXISTING self-drafter that
# already carries its own safety_expectation AgentEvalFixture in
# app/agent/evals.py (create_journal_entry_je_proposal, update_budget_line,
# dispose_schedule_proposal, update_vendor_proposal,
# reverse_journal_entry_proposal, create_invoice_proposal,
# send_invoice_reminder_proposal, record_customer_payment_proposal,
# create_credit_memo_proposal, propose_payment_batch_proposal,
# open_recon_session_proposal, propose_bulk_matches_proposal,
# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
# RATIFICATION-2026-07-13.md on disk) adds propose_allowance_je_proposal) proving
# proposal-only routing behaviorally; dry_run only ADDS a no-write preview exit.
# ─────────────────────────────────────────────────────────────────────────────
_DRY_RUN_KEY = "_dry_run"

# The honored census — LOCKSTEP-tested against the registry's injected set
# (HANDLER_LANE_SELF_DRAFTERS minus MODEL_HIDDEN_TOOLS): a tool injected but not
# honored would really draft on a `_dry_run: true` call, worse than no dry_run.
DRY_RUN_HONORED_TOOLS = frozenset({
    "create_journal_entry",
    "update_budget_line",
    "dispose_schedule",
    "update_vendor",
    "reverse_journal_entry",
    "create_invoice",
    "send_invoice_reminder",
    "record_customer_payment",
    "create_credit_memo",
    "propose_payment_batch",
    "open_recon_session",
    "propose_bulk_matches",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 5: the CECL self-drafter honors
    # the injected _dry_run as its estimate-only read (safety_expectation
    # AgentEvalFixture propose_allowance_je_proposal, app/agent/evals.py).
    "propose_allowance_je",
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md) — WF-12: the agent-plan self-drafter honors the
    # injected _dry_run as a validate-only preview (it pops the flag at entry, before the
    # AGENT_PLAN ProposedAction draft). agent_plan_activate_exec is MODEL_HIDDEN so it is
    # never injected. safety_expectation AgentEvalFixture propose_agent_plan_proposal.
    "propose_agent_plan",
})


def _dry_run_requested(params: dict) -> bool:
    """Pop + read the model-supplied dry-run flag at handler ENTRY.

    Popping (not just reading) is load-bearing: the `payload=params` lanes store
    the params dict as the proposal payload and the fingerprint canonicalizer
    does NOT strip underscore keys, so an un-popped `_dry_run: false` would fork
    the stored fingerprint against the identical absent-key draft (decision 4/E5
    — present-false must be indistinguishable from absent)."""
    return bool(params.pop(_DRY_RUN_KEY, False))


def _dry_run_notice(would_draft: str) -> str:
    """The truthful dry-run reply: what WOULD be drafted — never a proposal id."""
    return (
        f"DRY RUN — no proposal was drafted and nothing was saved. Would draft: {would_draft} "
        "Re-run the same call without _dry_run to draft it for approval in the Actions page."
    )


def _handle_set_budget_months(db: Session, realm_id: str, params: dict) -> str:
    """Directly update a BudgetLine's monthly_amounts dict in one shot."""
    from datetime import date as _date
    from ..models import BudgetLine  # noqa: F811

    account_name = params.get("account_name", "")
    monthly_values = params.get("monthly_values", {})
    budget_year = params.get("budget_year", _date.today().year)
    notes = params.get("notes", "")

    if not account_name:
        return "Error: account_name is required."
    if not monthly_values:
        return "Error: monthly_values dict is required (e.g. {'jan': 500.00, 'feb': 500.00, ...})."

    # Find the BudgetLine by fuzzy matching account name
    search = account_name.lower()
    lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == realm_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.parent_line_id == None,  # top-level lines only  # noqa: E711
    ).all()

    # Score matches
    best_line = None
    best_score = 0
    for bl in lines:
        name = (bl.account_name or "").lower()
        score = 0
        if name == search:
            score = 100
        elif search in name:
            score = 60
        elif name in search:
            score = 40
        else:
            # Word overlap
            s_words = set(search.split())
            n_words = set(name.split())
            overlap = len(s_words & n_words)
            if overlap:
                score = overlap * 20
        if score > best_score:
            best_score = score
            best_line = bl

    if not best_line or best_score < 20:
        available = [bl.account_name for bl in lines[:20] if bl.account_name]
        return (
            f"Could not find budget line matching '{account_name}' for {budget_year}. "
            f"Available: {', '.join(available)}"
        )

    # Replace monthly_amounts with new values (full overwrite for schedule-linked budgets)
    from sqlalchemy.orm.attributes import flag_modified

    new_amounts = {}
    updated_months = []
    for period, amount in monthly_values.items():
        normalized_key = _normalize_month_key(period)
        new_amounts[normalized_key] = round(float(amount), 2)
        updated_months.append(f"{normalized_key}: ${float(amount):,.2f}")

    best_line.monthly_amounts = new_amounts
    flag_modified(best_line, "monthly_amounts")
    db.commit()

    total = sum(float(v or 0) for v in new_amounts.values())
    return (
        f"✅ Budget updated: {best_line.account_name} ({budget_year}). "
        f"Set {len(updated_months)} month(s). Annual total: ${total:,.2f}. "
        f"{('Source: ' + notes) if notes else ''}"
    )


def _handle_reclassify_transaction(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """Creates a ProposedAction for reclassification."""
    action = ProposedAction(
        company_id=realm_id,
        action_type="RECLASSIFICATION",
        payload=params,
        reasoning=params.get("reason", "Reclassification via AI assistant"),
        confidence_score=0.85,
        status="PENDING",
    )
    # PLAN-EXEMPT: P0 fix — pin realm GUC before the FORCE-RLS proposed_actions INSERT (2026-06-23)
    _pin_realm_write_scope(db, realm_id)
    db.add(action)
    db.commit()
    db.refresh(action)
    return (
        DraftedToolResult((f"✅ Reclassification proposal created (Action #{action.id}). "
        f"Transaction {params.get('txn_id')} → {params.get('to_account_name')}. "
        f"Approve it in the Actions page."), proposal_id=action.id)
    )


def _handle_update_budget_line(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """Record a review-only finding; this action type has no budget executor."""
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C3 (marker
    # RATIFICATION-2026-07-13.md on disk). Decisions 4+7: pop _dry_run FIRST (this
    # lane stores payload=params — an un-popped key would fork the fingerprint),
    # and normalize the month key at ingestion (SH-5) so the stored payload
    # carries jan..dec, never a raw YYYY-MM. NOT a new tool: the existing
    # safety_expectation AgentEvalFixture for update_budget_line
    # (app/agent/evals.py) keeps proving proposal-only routing behaviorally.
    dry_run = _dry_run_requested(params)
    month_raw = params.get("month")
    if month_raw is not None:
        month_key = _normalize_month_key(str(month_raw))
        if month_key not in _VALID_MONTH_KEYS:
            return (
                f"Error: unrecognized month '{month_raw}'. Use a three-letter key "
                "(jan, feb, …); YYYY-MM (e.g. 2026-03) is also accepted."
            )
        params["month"] = month_key
    if dry_run:
        return _dry_run_notice(
            f"a review-only BUDGET_UPDATE suggestion for {params.get('account_name')} of "
            f"${float(params.get('amount') or 0):,.0f} for {params.get('month')}. "
            "Approval does not change budget values. Resolve the exact budget, line and year, "
            "then use set_budget_months for an executable proposal."
        )
    action = ProposedAction(
        company_id=realm_id,
        action_type="BUDGET_UPDATE",
        payload=params,
        reasoning=params.get("notes", "Budget update via AI assistant"),
        confidence_score=0.9,
        status="PENDING",
    )
    # PLAN-EXEMPT: P0 fix — pin realm GUC before the FORCE-RLS proposed_actions INSERT (2026-06-23)
    _pin_realm_write_scope(db, realm_id)
    db.add(action)
    db.commit()
    db.refresh(action)
    return (
        DraftedToolResult((f"Budget suggestion recorded for review (Action #{action.id}). "
        f"{params.get('account_name')} → ${params.get('amount'):,.0f} for {params.get('month')}. "
        "Approval does not change budget values. Resolve the exact budget, line and year, "
        "then use set_budget_months for an executable proposal."), proposal_id=action.id)
    )


def _handle_create_close_task(db: Session, realm_id: str, params: dict) -> str:
    """Directly creates a close task (no approval needed for checklist items)."""
    # AC-3b (plan 20260706-widen-internal-db-approval-execution): a missing title was a
    # KeyError — on the approve path an exception is a 500; the "Error:" refusal lands
    # the action at honest FAILED instead.
    title = params.get("title")
    if not title:
        return "Error: title is required."
    task = CloseTask(
        realm_id=realm_id,
        period=params.get("period", datetime.utcnow().strftime("%Y-%m")),
        task_type="CUSTOM",
        title=title,
        description=params.get("description", ""),
        is_auto_generated=False,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return f"✅ Close task added: '{task.title}' for period {task.period}."


def _handle_add_covenant(db: Session, realm_id: str, params: dict) -> str:
    """Directly creates a debt covenant — idempotent per metric_type.

    Dedup guard (2026-07-04 duplicate-covenant incident): an AUTOMATED path (this
    agent tool, a proactive run) must not create a SECOND active covenant of a
    metric_type already tracked for the company — that produced the mystery
    "Debt to EBITDA Ratio" / "Minimum DSCR" duplicates the user could not explain
    and that bloated the covenant page's compute. Skip idempotently (return the
    existing one; NEVER raise, so an agent run continues uninterrupted). A
    deliberately-distinct second covenant of the same metric is a manual action on
    the covenants page, not something the agent invents.
    """
    # PLAN-EXEMPT: cherry-pick conflict resolution landing reviewed market-ready commits onto main
    # AC-3b (plan 20260706-widen-internal-db-approval-execution): the four direct
    # indexes were KeyErrors on a malformed draft — refuse with "Error:" instead so
    # the approve path lands honest FAILED, never a frozen 500.
    missing = [k for k in ("loan_name", "name", "metric_type", "threshold") if params.get(k) in (None, "")]
    if missing:
        return f"Error: {', '.join(missing)} required."
    metric_type = params["metric_type"]
    existing = (
        db.query(DebtCovenant)
        .filter(
            DebtCovenant.company_id == realm_id,
            DebtCovenant.metric_type == metric_type,
            DebtCovenant.active == True,  # noqa: E712
        )
        .first()
    )
    if existing is not None:
        return (
            f"ℹ️ A {metric_type} covenant ('{existing.name}') is already tracked for "
            f"this company — skipped creating a duplicate."
        )
    covenant = DebtCovenant(
        company_id=realm_id,
        loan_name=params["loan_name"],
        name=params["name"],
        metric_type=metric_type,
        threshold=params["threshold"],
        breach_direction=params.get("breach_direction", "BELOW"),
    )
    db.add(covenant)
    db.commit()
    db.refresh(covenant)
    return f"✅ Covenant tracked: '{covenant.name}' on {covenant.loan_name} (threshold: {covenant.threshold})."


def _handle_search_coa(db: Session, realm_id: str, params: dict) -> str:
    """Search QBO Chart of Accounts. Read-only."""
    try:
        client = get_qbo_client_from_db(db, realm_id)
        search = params.get("search_term", "").lower()
        query = "SELECT * FROM Account MAXRESULTS 200"
        raw = client.query(query)
        accounts = raw.get("QueryResponse", {}).get("Account", [])
        matches = [
            {"id": a["Id"], "name": a["Name"], "type": a.get("AccountType", "")}
            for a in accounts
            if search in a.get("Name", "").lower() or search in a.get("FullyQualifiedName", "").lower()
        ][:15]
        if matches:
            lines = [f"  - {m['name']} (ID: {m['id']}, Type: {m['type']})" for m in matches]
            return f"Found {len(matches)} matching accounts:\n" + "\n".join(lines)
        return f"No accounts found matching '{search}'. Try a broader search term."
    except Exception as e:
        return _read_failure(
            "Chart of accounts search is temporarily unavailable.",
            e,
            error_code="assistant_coa_search_unavailable",
            log_context="assistant_search_coa_error",
        )


def _handle_search_finance_records(db: Session, realm_id: str, params: dict) -> str:
    """Search normalized QuickBooks records through the deterministic DB index."""
    from ..services.finance_search import search_finance_records

    query = str(params.get("query") or "").strip()
    if not query:
        raise ToolFailure("invalid_request", ("Error: query is required."))

    filter_keys = [
        "entity_type",
        "start_date",
        "end_date",
        "min_amount",
        "max_amount",
        "status",
        "customer_name",
        "vendor_name",
        "account",
        "class_name",
        "location",
    ]
    filters = {key: params[key] for key in filter_keys if params.get(key) not in (None, "")}
    try:
        limit = int(params.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 25))

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-2 caps-with-totals:
    # rank at the schema maximum (25) once, then slice to the caller's limit, so
    # the output can name the TOTAL matches found alongside the shown count
    # instead of silently presenting the first page as the whole answer.
    _RANK_CEILING = 25
    payload = search_finance_records(db, realm_id, query, filters=filters, limit=_RANK_CEILING)
    parsed_filters = {k: v for k, v in payload.get("filters", {}).items() if v not in (None, [], "")}
    all_results = payload.get("results") or []
    results = all_results[:limit]

    lines = [
        "Deterministic QuickBooks search results",
        "Source: qbo_search_records normalized database index. No AI-generated finance records are included.",
        f"Query: {query}",
    ]
    if parsed_filters:
        lines.append("Parsed filters: " + json.dumps(parsed_filters, sort_keys=True, default=str))

    if not results:
        # An index null is NOT proof of absence: qbo_search_records is a LOCAL snapshot
        # that lags live QuickBooks and is structurally blind to records synced after the
        # last index refresh. Report the snapshot freshness and instruct verification so the
        # model never answers a confident false "no" to an existence question.
        from sqlalchemy import func as _func
        from ..models import QBOSearchRecord  # noqa: F811

        synced_as_of = (
            db.query(_func.max(QBOSearchRecord.updated_at))
            .filter(QBOSearchRecord.realm_id == realm_id)  # CROSS-TENANT-OK: realm-scoped index-freshness read; backstopped by FORCE-RLS
            .scalar()
        )
        if synced_as_of is not None:
            lines.append(
                f"Not found in the local index (synced as of {synced_as_of.isoformat()}). "
                "This does not confirm none exist — the local index lags live QuickBooks. "
                "Verify against live QuickBooks (AR summary / report) before concluding."
            )
        else:
            lines.append(
                "Not found in the local index (snapshot freshness unknown). "
                "This does not confirm none exist — the local index lags live QuickBooks. "
                "Verify against live QuickBooks (AR summary / report) before concluding."
            )
        return "\n".join(lines)

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-2: name total + shown.
    total_found = f"{len(all_results)}{'+' if len(all_results) >= _RANK_CEILING else ''}"
    lines.append(f"Matches found: {total_found} | showing top {len(results)} ranked result(s):")
    for idx, result in enumerate(results, start=1):
        name = result.get("display_name") or result.get("doc_number") or result.get("qbo_id")
        amount = result.get("amount")
        balance = result.get("open_balance")
        summary_parts = [
            f"{idx}. {result.get('entity_type')} | {name}",
            f"QBO ID: {result.get('qbo_id')}",
        ]
        if result.get("doc_number"):
            summary_parts.append(f"Doc #: {result.get('doc_number')}")
        if result.get("txn_date"):
            summary_parts.append(f"Date: {result.get('txn_date')}")
        if result.get("due_date"):
            summary_parts.append(f"Due: {result.get('due_date')}")
        if amount is not None:
            summary_parts.append(f"Amount: ${float(amount):,.2f}")
        if balance is not None:
            summary_parts.append(f"Open balance: ${float(balance):,.2f}")
        if result.get("status"):
            summary_parts.append(f"Status: {result.get('status')}")
        if result.get("customer_name"):
            summary_parts.append(f"Customer: {result.get('customer_name')}")
        if result.get("vendor_name"):
            summary_parts.append(f"Vendor: {result.get('vendor_name')}")
        if result.get("account_name"):
            summary_parts.append(f"Account: {result.get('account_name')}")
        if result.get("class_name"):
            summary_parts.append(f"Class: {result.get('class_name')}")
        if result.get("location_name"):
            summary_parts.append(f"Location: {result.get('location_name')}")
        lines.append(" | ".join(summary_parts))

        if result.get("memo"):
            lines.append(f"   Memo: {result.get('memo')}")
        highlights = result.get("highlights") or {}
        if highlights:
            highlights_text = "; ".join(f"{field}={value}" for field, value in highlights.items())
            lines.append(f"   Highlights: {highlights_text}")
        if result.get("matched_fields"):
            lines.append(f"   Matched fields: {', '.join(result.get('matched_fields'))}")

    return "\n".join(lines)


# Map of analysis report types to their functions (TTM + monthly variants). The keys are
# exactly the get_financial_data report_type enum (app/assistant/tools.py). Cash-flow
# questions go to get_cashflow_snapshot (the 13-week forecast), not to a pack here.
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

_MONTHLY_PACKS = {
    "vendor_spend": vendor_spend_monthly,
    "customer_revenue": customer_revenue_monthly,
}


def _handle_get_financial_data(db: Session, realm_id: str, params: dict) -> str:
    """Fetch a specific analysis pack from QBO, pre-analyzed by GPT-4/Gemini."""
    # PLAN-EXEMPT: Steven work order — thread optional target month into vendor_spend
    # monthly so "are we missing any vendors in {month}?" reads the right actuals month.
    report_type = params.get("report_type", "")
    period = params.get("period", "ttm")
    target_month = (params.get("month") or "").strip() or None

    # Use monthly variant if requested and available
    if period == "monthly" and report_type in _MONTHLY_PACKS:
        fn = _MONTHLY_PACKS[report_type]
    else:
        fn = _ANALYSIS_PACKS.get(report_type)

    if not fn:
        raise ToolFailure("invalid_request", (f"Unknown report type: {report_type}. Available: {', '.join(_ANALYSIS_PACKS.keys())}"))
    try:
        qbo = get_qbo_client_from_db(db, realm_id)
        # vendor_spend_monthly accepts a target month for missing/new-vendor detection;
        # the other packs take only the client, so pass target_month only where supported.
        if target_month and fn is vendor_spend_monthly:
            data = fn(qbo, target_month=target_month)
        else:
            data = fn(qbo)
        if isinstance(data, dict) and data.get("error"):
            raise ToolFailure("analysis_source_unavailable", "The requested financial source is unavailable.", retryable=True)
        raw_result = json.dumps(data, indent=1, default=str)
        # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-2: a truncated
        # cap names the TOTAL and what was dropped instead of a bare "(truncated)".
        if len(raw_result) > 12000:
            raw_result = raw_result[:12000] + (
                f"\n... (truncated: showing the first 12,000 of {len(raw_result):,} characters — "
                "narrow the period or request a more specific report for the rest)"
            )

        # Pre-analyze with GPT-4/Gemini before Claude sees it
        return _analyze_with_llm(raw_result, f"{report_type} ({period})")
    except Exception as e:
        return _read_failure(
            f"{report_type} data is temporarily unavailable.",
            e,
            error_code="assistant_analysis_pack_unavailable",
            log_context="assistant_analysis_pack_error",
        )


def _handle_get_budget_data(db: Session, realm_id: str, params: dict) -> str:
    """Fetch budget vs actual data, pre-analyzed by GPT-4/Gemini."""
    from ..models import BudgetLine  # noqa: F811
    from datetime import date

    budget_year = params.get("budget_year", date.today().year)
    account_filter = params.get("account_filter", "").lower()

    query = db.query(BudgetLine).filter(
        BudgetLine.company_id == realm_id,
        BudgetLine.budget_year == budget_year,
    )
    lines = query.all()

    if not lines:
        return f"No budget data found for {budget_year}. The budget may not have been seeded yet."

    results = []
    for line in lines:
        if account_filter and account_filter not in (line.account_name or "").lower():
            continue
        monthly = line.monthly_amounts or {}
        total = sum(float(v or 0) for v in monthly.values())
        results.append({
            "account": line.account_name,
            "type": line.account_type,
            "annual_total": round(total, 2),
            "monthly": {k: round(float(v or 0), 2) for k, v in monthly.items()},
        })

    if not results:
        return f"No budget lines match filter '{account_filter}' for {budget_year}."

    # Sort by annual total descending
    results.sort(key=lambda x: abs(x["annual_total"]), reverse=True)
    raw_output = json.dumps(results[:25], indent=1)
    raw_text = f"Budget data for {budget_year} ({len(results)} accounts, showing top 25):\n{raw_output}"

    # Pre-analyze with GPT-4/Gemini
    return _analyze_with_llm(raw_text, f"budget vs actual {budget_year}")


def _handle_compare_periods(db: Session, realm_id: str, params: dict) -> str:
    """Compare financial metrics across periods."""
    from ..models import InsightKPI

    metric = params.get("metric", "revenue")
    comparison = params.get("comparison", "mom")

    # Get the last several periods of KPI data
    rows = (
        db.query(InsightKPI)
        .filter(InsightKPI.company_id == realm_id, InsightKPI.name == metric)
        .order_by(InsightKPI.period.desc())
        .limit(13)  # enough for YoY
        .all()
    )

    if not rows:
        raise ToolFailure("source_not_ready", (f"No KPI data found for '{metric}'. Run the analysis pipeline first."), retryable=False)

    import math
    offsets = {"mom": 1, "qoq": 3, "yoy": 12}
    if comparison not in offsets:
        raise ToolFailure("invalid_comparison", "Choose mom, qoq, or yoy.", retryable=False)
    current_period = rows[0].period
    try:
        current_date = datetime.strptime(current_period, "%Y-%m")
        month_index = current_date.year * 12 + current_date.month - 1 - offsets[comparison]
        prior_period = f"{month_index // 12:04d}-{month_index % 12 + 1:02d}"
        current_rows = [r for r in rows if r.period == current_period]
        prior_rows = [r for r in rows if r.period == prior_period]
        if len(current_rows) != 1 or len(prior_rows) != 1:
            raise ValueError("missing or ambiguous comparison period")
        current_val, prior_val = float(current_rows[0].value), float(prior_rows[0].value)
        if not all(math.isfinite(value) for value in (current_val, prior_val)):
            raise ValueError("nonfinite comparison value")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolFailure("source_not_ready", f"Complete, unambiguous KPI values for the requested {comparison.upper()} comparison are unavailable.", retryable=False) from exc

    delta = current_val - prior_val
    pct_change = (delta / prior_val * 100) if prior_val != 0 else 0

    return (
        f"{metric.replace('_', ' ').title()} comparison ({comparison.upper()}):\n"
        f"  Current ({current_period}): ${current_val:,.0f}\n"
        f"  Prior ({prior_period}): ${prior_val:,.0f}\n"
        f"  Change: ${delta:,.0f} ({pct_change:+.1f}%)\n"
        f"  Trend: {'▲ improving' if delta > 0 else '▼ declining' if delta < 0 else '→ flat'}"
    )


def _handle_run_qbo_report(db: Session, realm_id: str, params: dict) -> str:
    """Run a QBO report with full parsing, pre-analyzed by GPT-4/Gemini."""
    from datetime import date, timedelta  # noqa: F401

    report_type = params.get("report_type", "ProfitAndLoss")
    today = date.today()
    start_date = params.get("start_date", f"{today.year}-{today.month:02d}-01")
    end_date = params.get("end_date", str(today))

    try:
        # Build proper params dict for the QBO API
        report_params = {
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": "Accrual",
        }
        # Balance sheet uses as_of_date semantics
        if report_type == "BalanceSheet":
            report_params = {"accounting_method": "Accrual", "end_date": end_date}

        # If user specified an account filter (for GeneralLedger), resolve to QBO ID(s)
        account_filter = params.get("account", "").strip()
        resolved_account_ids: List[str] = []
        if account_filter and report_type == "GeneralLedger":
            try:
                qbo = get_qbo_client_from_db(db, realm_id)
                resolved = _resolve_account_with_children(qbo, account_filter)
                report_params["account"] = resolved["account_ids"]
                resolved_account_ids = [item for item in resolved["account_ids"].split(",") if item]
                child_note = f" + {resolved['child_count']} sub-accounts" if resolved['child_count'] else ""
                logger.info(f"[Assistant] GL filtered to: {resolved['name']}{child_note} (IDs: {resolved['account_ids']})")
            except Exception as e:
                logger.warning(
                    "[Assistant] Could not resolve account '%s': %s",
                    account_filter,
                    redact_to_string(str(e), max_length=500),
                )
                # Continue without filter — better than failing

        # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 6 (SH-1):
        # the cache-backed GL read is now a keyset-cursor page (query_gl_lines_page)
        # with honest WINDOW totals and a continuation token; cursor/limit are
        # consumed HERE only — the live-parse path below ignores them (documented
        # in the tool schema).
        cursor_token = str(params.get("cursor") or "").strip() or None
        try:
            page_limit = int(params.get("limit") or 80)
        except (TypeError, ValueError):
            page_limit = 80
        page_limit = max(1, min(page_limit, 200))

        if resolved_account_ids:
            try:
                gl_page_raw = query_gl_lines_page(
                    db,
                    realm_id=realm_id,
                    start_date=start_date,
                    end_date=end_date,
                    account_ids=resolved_account_ids,
                    limit=page_limit,
                    cursor=cursor_token,
                )
            except ValueError as exc:
                # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening (marker
                # RATIFICATION-2026-07-13.md) — orchestrator wave-gate fix: the
                # raw-exception static guard forbids f-string exception returns; the
                # cursor error is OUR own crafted refusal, preserved via the wrapper.
                return _read_failure(
                    f"{exc} (cursors expire when the cache window re-syncs)",
                    exc,
                    error_code="gl_cursor_invalid",
                    retryable=True,
                    log_context="assistant_gl_cursor_invalid",
                )
            # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C4 (marker
            # RATIFICATION-2026-07-13.md on disk) — decision 5: the GL-cursor page is
            # validated into its typed model AT its consumer (this lane has no JSON wire
            # output — the prose below renders FROM the typed page, byte-identical to the
            # pre-change output and golden-pinned in tests/test_tool_results.py).
            from ..agent.runtime.tool_results import GLCursorPage

            gl_page = GLCursorPage.model_validate(gl_page_raw)
            cached_txns = gl_page.transactions
            if cursor_token and not cached_txns:
                return (
                    f"End of the cached GL window ({start_date} to {end_date}) — no "
                    "transactions remain after this cursor. Re-run without a cursor to "
                    "start over."
                )
            if cached_txns:
                raw = format_gl_lines_for_assistant(
                    [txn.model_dump() for txn in cached_txns],
                    start_date=start_date,
                    end_date=end_date,
                    source="Peregrine local GL cache",
                    max_lines=page_limit,
                )
                page_note = (
                    f"\n\nWindow total: {gl_page.total_count} transaction(s), net "
                    f"${gl_page.total_amount:,.2f} across the FULL range; this page shows "
                    f"{len(cached_txns)}."
                )
                if gl_page.has_more:
                    page_note += (
                        f"\nMore pages remain — pass cursor '{gl_page.next_cursor}' to "
                        "run_qbo_report to continue where this page ended."
                    )
                cache_note = (
                    f"\n\nSource freshness: Peregrine local GL cache; "
                    f"report range {start_date} to {end_date}."
                )
                return raw + page_note + cache_note

        report, cache_status = get_cached_qbo_report(
            db,
            realm_id=realm_id,
            report_type=report_type,
            params=report_params,
        )
        cache_note = f"\n\nSource freshness: QBO report cache {cache_status}; report range {start_date} to {end_date}."
        rows_data = report.get("Rows", {}).get("Row", [])

        # ── GeneralLedger: parse individual transactions per account ──────
        if report_type == "GeneralLedger":
            try:
                upsert_gl_report_lines(
                    db,
                    realm_id=realm_id,
                    report=report,
                    start_date=start_date,
                    end_date=end_date,
                    account_ids=resolved_account_ids or None,
                )
            except Exception as exc:
                db.rollback()
                logger.warning("[Assistant] Failed to backfill local GL cache: %s", exc)
            raw = _parse_gl_report(report, start_date, end_date)
            return _analyze_with_llm(raw, f"General Ledger {start_date} to {end_date}") + cache_note

        # ── P&L / BS / CashFlow / TrialBalance: account-level detail ──────
        raw = _parse_financial_report(report_type, rows_data, start_date, end_date)
        return _analyze_with_llm(raw, f"{report_type} {start_date} to {end_date}") + cache_note

    except Exception as e:
        logger.error(
            "[Assistant] run_qbo_report failed: %s",
            redact_to_string(str(e), max_length=500),
            exc_info=True,
        )
        return _read_failure(
            f"{report_type} report data is temporarily unavailable.",
            e,
            error_code="assistant_qbo_report_unavailable",
            log_context="assistant_run_qbo_report_error",
        )


def _parse_gl_report(report: dict, start_date: str, end_date: str) -> str:
    """Parse a QBO GeneralLedger report into per-account transaction detail.

    QBO nests sub-accounts as nested Section rows:
      Section: "7000 General & Admin"
        → Section: "7400 Office Supplies & Expense"
          → Section: "7430 Software Expense"
            → Data rows (actual transactions)
    We must recurse into all levels to find the Data rows.
    """
    columns = [c.get("ColTitle", "") for c in report.get("Columns", {}).get("Column", [])]
    col_idx = {title: i for i, title in enumerate(columns)}
    rows = report.get("Rows", {}).get("Row", [])

    output_lines = [f"General Ledger ({start_date} to {end_date}):\n"]
    total_chars = 0
    MAX_CHARS = 12000
    truncated = False
    shown_count = 0

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-4 (SH-1): once the
    # char cap trips, the walk keeps COUNTING Data rows (it just stops emitting),
    # so the tail note can name exactly how many transactions were dropped and
    # teach the model to narrow the range instead of trusting a silent clip.
    def _extract_transactions(row_list: list, depth: int = 0) -> int:
        """Recursively extract Data rows from nested Sections. Returns txn count."""
        nonlocal total_chars, truncated, shown_count
        txn_count = 0
        for row in row_list:
            row_type = row.get("type", "")  # SA-OK: dict.get on an in-memory QBO report row, not an ORM query

            # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-4 cap restructure.
            if row_type == "Section":
                header = row.get("Header", {})
                section_name = ""
                try:
                    section_name = header.get("ColData", [{}])[0].get("value", "")
                except Exception:
                    pass

                # Get section total from Summary
                summary = row.get("Summary", {})
                summary_cols = summary.get("ColData", [])
                section_total = ""
                if summary_cols and len(summary_cols) >= 2:
                    section_total = summary_cols[-1].get("value", "0") or "0"

                # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-4: after
                # the cap trips, sections stop emitting but the recursion continues
                # so every remaining Data row is still counted.
                if not truncated:
                    section_header = f"\n{'  ' * depth}{section_name} (Total: ${_safe_float(section_total):,.2f})"
                    output_lines.append(section_header)
                    total_chars += len(section_header)

                # Recurse into sub-rows
                # QBO-OK: canonical-parser — pre-existing legacy GL text walker already in HEAD; this edit only restructures the char cap, no new walk is introduced.
                sub_rows = row.get("Rows", {}).get("Row", [])
                txn_count += _extract_transactions(sub_rows, depth + 1)

            elif row_type == "Data":
                txn_count += 1
                if truncated:
                    continue  # counted but not emitted — the tail note names the dropped total
                cols = row.get("ColData", [])

                def _col(key: str) -> str:
                    idx = col_idx.get(key, -1)
                    if idx < 0 or idx >= len(cols):
                        return ""
                    return str(cols[idx].get("value", "") or "")

                txn_date = _col("Date") or _col("Tx Date")
                txn_type = _col("Transaction Type") or _col("Type")
                name = _col("Name") or _col("Payee") or ""
                memo = _col("Memo/Description") or _col("Memo") or ""
                amount = _col("Amount") or _col("Nat. Amount") or "0"

                # Drill handle (Phase A AC-6): QBO returns the transaction id on the
                # transaction row's ColData (keys: value + id; verified live — 8597 ids
                # in a sample GL). Surfacing it lets the agent drill GL -> underlying
                # transaction via get_transaction / search_finance_records. The cache
                # path (qbo_gl_lines) has no qbo_txn_id column — deferred (needs a migration).
                txn_id = next(
                    (str(c.get("id")) for c in cols if isinstance(c, dict) and c.get("id")),
                    "",
                )

                try:
                    amt_float = float(amount.replace(",", ""))
                    amt_str = f"${amt_float:,.2f}"
                except (ValueError, TypeError):
                    amt_str = amount

                indent = "  " * max(depth, 1)
                line = f"{indent}{txn_date} | {txn_type} | {name} | {memo[:60]} | {amt_str}"
                if txn_id:
                    line += f" | drill:{txn_type or 'Transaction'}#{txn_id}"
                total_chars += len(line)
                # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-4: the
                # over-cap line is dropped (as before) but the walk continues in
                # count-only mode instead of returning early.
                if total_chars > MAX_CHARS:
                    truncated = True
                    continue
                output_lines.append(line)
                shown_count += 1

        return txn_count

    total_txns = _extract_transactions(rows)

    if total_txns == 0:
        output_lines.append("  (no individual transactions found)")

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-4 (SH-1): the
    # cap names the dropped count + the full in-range total and teaches the
    # narrower re-query, replacing the silent "(truncated for token efficiency)".
    if truncated:
        dropped = total_txns - shown_count
        output_lines.append(
            f"\n... ({dropped} more transaction(s) not shown — {total_txns} in range, "
            f"{shown_count} shown. Narrow start_date/end_date or filter to a single "
            "account to see the rest.)"
        )

    if not output_lines or len(output_lines) <= 1:
        return "GeneralLedger report returned but no data could be parsed."
    return "\n".join(output_lines)


def _parse_financial_report(report_type: str, rows_data: list, start_date: str, end_date: str) -> str:
    """Parse P&L, BS, CashFlow, TrialBalance into account-level detail."""
    output_lines = [f"{report_type} ({start_date} to {end_date}):\n"]
    total_chars = 0
    MAX_CHARS = 10000

    def _extract_rows(rows, indent=0):
        nonlocal total_chars
        for row in rows:
            if total_chars > MAX_CHARS:
                output_lines.append("... (truncated for token efficiency)")
                return

            if row.get("type") == "Section":
                header = row.get("Header", {}).get("ColData", [{}])
                section_name = header[0].get("value", "") if header else ""
                prefix = "  " * indent

                # Section header
                line = f"{prefix}── {section_name} ──"
                output_lines.append(line)
                total_chars += len(line)

                # Individual account rows within the section
                sub = row.get("Rows", {}).get("Row", [])
                for sub_row in sub:
                    if total_chars > MAX_CHARS:
                        output_lines.append("... (truncated)")
                        return
                    if sub_row.get("type") in ("Data", "data"):
                        col_data = sub_row.get("ColData", [])
                        if len(col_data) >= 2:
                            acct_name = col_data[0].get("value", "")
                            acct_val = col_data[-1].get("value", "0") or "0"
                            try:
                                val_float = float(str(acct_val).replace(",", ""))
                                val_str = f"${val_float:,.2f}"
                            except (ValueError, TypeError):
                                val_str = acct_val
                            acct_line = f"{prefix}  {acct_name}: {val_str}"
                            output_lines.append(acct_line)
                            total_chars += len(acct_line)
                    elif sub_row.get("type") == "Section":
                        # Nested section — recurse
                        _extract_rows([sub_row], indent + 1)

                # Section total from Summary
                summary = row.get("Summary", {}).get("ColData", [])
                if summary and len(summary) > 1:
                    total_val = summary[-1].get("value", "0") or "0"
                    try:
                        t_float = float(str(total_val).replace(",", ""))
                        t_str = f"${t_float:,.2f}"
                    except (ValueError, TypeError):
                        t_str = total_val
                    total_line = f"{prefix}  TOTAL {section_name}: {t_str}"
                    output_lines.append(total_line)
                    total_chars += len(total_line)

    _extract_rows(rows_data)

    if not output_lines or len(output_lines) <= 1:
        return f"{report_type} report returned but no data could be parsed."
    return "\n".join(output_lines)


def _safe_float(val) -> float:
    """Safely convert a value to float."""
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _escape_like(term: str) -> str:
    """Escape LIKE/ILIKE wildcards in a user term (bound as a parameter, never spliced)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _handle_search_documents(db: Session, realm_id: str, params: dict) -> str:
    """Search uploaded documents by keyword — matched and bounded in SQL."""
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-2 (SH-1): the old
    # path loaded EVERY done document INCLUDING raw_text into Python and substring-
    # matched there (unbounded memory on large document sets). The match now runs
    # as SQL ilike predicates (case-insensitive, wildcard-escaped, value bound as a
    # parameter) with a COUNT for the honest total and a LIMIT for the shown rows;
    # only the ≤5 matched rows are materialized.
    query = str(params.get("query") or "").strip()
    if not query:
        raise ToolFailure("invalid_request", ("Please provide a search query."))
    like = f"%{_escape_like(query)}%"

    matched = db.query(Document).filter(
        Document.company_id == realm_id,
        Document.processing_status == "done",
        or_(
            Document.filename.ilike(like, escape="\\"),
            Document.ai_summary.ilike(like, escape="\\"),
            cast(Document.key_terms, String).ilike(like, escape="\\"),
            Document.raw_text.ilike(like, escape="\\"),
        ),
    )
    total = matched.count()
    if not total:
        return f"No documents found matching '{query}'."
    docs = matched.order_by(Document.id.desc()).limit(5).all()

    matches = []
    for d in docs:  # SA-OK: bounded-loop over the ≤5 SQL-matched rows; no query inside
        result = f"[{d.document_type}] {d.filename}"
        if d.ai_summary:
            result += f": {d.ai_summary[:500]}"
        kt = d.key_terms or {}
        if kt:
            result += f" | Key terms: {json.dumps(kt, default=str)[:500]}"
        if d.expiration_date:
            result += f" | Expires: {d.expiration_date}"
        if d.raw_text and query.lower() in d.raw_text.lower():
            result += f" | Excerpt: {_make_excerpt(d.raw_text, query, radius=250)}"
        matches.append(result)

    return f"Found {total} document(s) (showing {len(matches)}):\n" + "\n\n".join(matches)


def _make_excerpt(text: str, query: str, radius: int = 450) -> str:
    """Return a short excerpt around the first query match."""
    if not text:
        return ""
    text = str(text)
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[: radius * 2].strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return (prefix + text[start:end].strip() + suffix).replace("\n", " ")


def _agreement_payload(a: CompanyAgreement, include_raw_excerpt: bool = False, query: str = "") -> Dict[str, Any]:
    payload = {
        "agreement_id": a.id,
        "filename": a.filename,
        "agreement_type": a.agreement_type,
        "summary": a.ai_summary,
        "economics": a.extracted_economics or {},
        "cap_table_entry_count": len(a.extracted_cap_table or []),
        "processing_status": a.processing_status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }
    if include_raw_excerpt and a.raw_text:
        payload["raw_excerpt"] = _make_excerpt(a.raw_text, query or a.filename or "")
    return payload


def _handle_get_agreement_context(db: Session, realm_id: str, params: dict) -> str:
    """Return structured context for uploaded material agreements."""
    agreement_id = params.get("agreement_id")
    query = db.query(CompanyAgreement).filter(
        CompanyAgreement.company_id == realm_id,
        CompanyAgreement.processing_status == "done",
    )
    if agreement_id:
        query = query.filter(CompanyAgreement.id == agreement_id)
    agreements = query.order_by(CompanyAgreement.created_at.desc()).limit(20).all()
    if not agreements:
        return "No processed material agreements found for this company."

    payload = {
        "agreement_count": len(agreements),
        "agreements": [_agreement_payload(a) for a in agreements],
    }
    text = json.dumps(payload, indent=2, default=str)
    return text[:9000] + ("\n... (truncated)" if len(text) > 9000 else "")


def _handle_search_agreements(db: Session, realm_id: str, params: dict) -> str:
    """Search material agreements/contracts — matched and bounded in SQL."""
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-2 (SH-1): the old
    # path loaded EVERY done agreement INCLUDING raw_text into Python and substring-
    # matched there. The match now runs as SQL ilike predicates (wildcard-escaped,
    # value bound) with a COUNT for the honest total and LIMIT 6 for the shown rows.
    query = (params.get("query") or "").strip()
    if not query:
        raise ToolFailure("invalid_request", ("Please provide a search query."))
    include_raw_excerpt = params.get("include_raw_excerpt", True)
    like = f"%{_escape_like(query)}%"

    matched = db.query(CompanyAgreement).filter(
        CompanyAgreement.company_id == realm_id,
        CompanyAgreement.processing_status == "done",
        or_(
            CompanyAgreement.filename.ilike(like, escape="\\"),
            CompanyAgreement.agreement_type.ilike(like, escape="\\"),
            CompanyAgreement.ai_summary.ilike(like, escape="\\"),
            cast(CompanyAgreement.extracted_economics, String).ilike(like, escape="\\"),
            cast(CompanyAgreement.extracted_cap_table, String).ilike(like, escape="\\"),
            CompanyAgreement.raw_text.ilike(like, escape="\\"),
        ),
    )
    total = matched.count()
    if not total:
        return f"No material agreements found matching '{query}'."
    agreements = matched.order_by(CompanyAgreement.created_at.desc()).limit(6).all()
    matches = [
        _agreement_payload(a, include_raw_excerpt=include_raw_excerpt, query=query)
        for a in agreements
    ]

    payload = {"query": query, "match_count": total, "shown": len(matches), "matches": matches}
    text = json.dumps(payload, indent=2, default=str)
    return text[:10000] + ("\n... (truncated)" if len(text) > 10000 else "")


def _handle_get_module_context(db: Session, realm_id: str, params: dict) -> str:
    """Return a module-level context block for the assistant."""
    module = params.get("module", "")
    if not module:
        raise ToolFailure("invalid_request", ("Please provide a module name."))
    return build_module_context(db, realm_id, module)


def _handle_search_asc_guidance(db: Session, realm_id: str, params: dict) -> str:
    """Search the global ASC knowledge map for GAAP issue-spotting / routing.

    Reference data only — no tenant/DB access — so ``db`` and ``realm_id`` are
    accepted to match the handler signature but unused.
    """
    from .asc_knowledge import format_results, search_asc

    query = str(params.get("query") or "").strip()
    if not query:
        raise ToolFailure("invalid_request", ("Please provide an accounting question or ASC topic to search for."))
    route_tag = str(params.get("route_tag") or "").strip() or None
    results = search_asc(query, route_tag=route_tag)
    return format_results(query, results)


def _handle_search_financial_expertise(db: Session, realm_id: str, params: dict) -> str:
    """Search the global financial-expertise KB (LBO / cash-flow / close method).

    Reference data only — no tenant/DB access — so ``db`` and ``realm_id`` are
    accepted to match the handler signature but unused (the asc_knowledge
    pattern). Read-only: renders cited, authority-tiered method records.
    """
    from .financial_expertise import format_results as _fe_format
    from .financial_expertise import search_financial_expertise as _fe_search

    query = str(params.get("query") or "").strip()
    if not query:
        raise ToolFailure("invalid_request", ("Please provide a finance-method question to search for (domains: lbo, cashflow, close)."))
    domain = str(params.get("domain") or "").strip() or None
    results = _fe_search(query, domain=domain)
    return _fe_format(query, results)


def _handle_get_schedule_data(db: Session, realm_id: str, params: dict) -> str:
    """Return the current schedule grid data."""
    schedule_type = params.get("schedule_type", "PREPAID")
    schedules = db.query(AccountingSchedule).filter(
        AccountingSchedule.realm_id == realm_id,
        AccountingSchedule.schedule_type == schedule_type,
        AccountingSchedule.status.in_(["ACTIVE", "PENDING_APPROVAL"]),
    ).all()

    if not schedules:
        return f"No active {schedule_type} schedules found."

    from ..models import ScheduleEntry
    from collections import defaultdict
    
    # Bulk fetch entries to avoid N+1 queries
    s_ids = [s.id for s in schedules]
    all_entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id.in_(s_ids)).all()
    entries_by_schedule = defaultdict(list)
    for e in all_entries:
        entries_by_schedule[e.schedule_id].append(e)

    rows = []
    for s in schedules:
        entries = entries_by_schedule[s.id]
        month_vals = {e.period: float(e.amount) for e in entries}
        total_expensed = sum(month_vals.values())
        rows.append({
            "schedule_id": s.id,
            "vendor_name": s.name,
            "expense_account": s.expense_account_name,
            "amount": float(s.original_amount or 0),
            "months": s.useful_life_months,
            "remaining": float(s.remaining_amount or 0),
            "note": s.description,
            "start_date": str(s.start_date) if s.start_date else None,
            "month_values": month_vals,
            "total_expensed": round(total_expensed, 2),
        })

    # Also run balance check
    from ..services.schedule_engine import validate_schedule_balance
    balance = validate_schedule_balance(db, realm_id, schedule_type)

    result = {
        "schedule_type": schedule_type,
        "row_count": len(rows),
        "rows": rows,
        "balance_check": balance,
    }

    # Build a summary with per-month totals across all rows
    all_periods = set()
    for r in rows:
        all_periods.update(r.get("month_values", {}).keys())
    if all_periods:
        sorted_periods = sorted(all_periods)
        monthly_totals = {}
        for p in sorted_periods:
            monthly_totals[p] = sum(r.get("month_values", {}).get(p, 0) for r in rows)
        result["monthly_totals_all_assets"] = monthly_totals
        result["summary"] = (
            f"{len(rows)} {schedule_type} schedule rows. "
            f"Monthly totals: " +
            ", ".join(f"{p}: ${v:,.2f}" for p, v in list(monthly_totals.items())[:6]) +
            ("..." if len(monthly_totals) > 6 else "")
        )

    text = json.dumps(result, indent=1, default=str)
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — AC-2: a truncated cap
    # names the TOTAL row count so the model never mistakes a clipped grid for all rows.
    if len(text) > 8000:
        text = text[:8000] + (
            f"\n... (truncated: {len(rows)} total {schedule_type} schedule rows in this result; "
            "the monthly_totals_all_assets summary above covers ALL rows)"
        )

    # Pre-analyze with GPT-4/Gemini for richer context
    return _analyze_with_llm(text, f"{schedule_type} schedule data")


def _handle_add_schedule_row(db: Session, realm_id: str, params: dict) -> str:
    """Add a new row to an accounting schedule."""
    from datetime import date as dt_date
    from ..services.internal_action_effects import lock_schedule_effect, record_schedule_effect
    execution = lock_schedule_effect(db, realm_id, params)
    schedule_type = params.get("schedule_type", "PREPAID")
    expense_account = params.get("expense_account", "")
    vendor_name = params.get("vendor_name", "")
    amount = params.get("amount", 0)
    months = params.get("months", 12)
    note = params.get("note", "")
    start_date_str = params.get("start_date")

    if not expense_account or not vendor_name or not amount:
        return "Error: expense_account, vendor_name, and amount are required."

    start = dt_date.today()
    if start_date_str:
        try:
            start = dt_date.fromisoformat(start_date_str)
        except ValueError:
            pass

    schedule = AccountingSchedule(
        realm_id=realm_id,
        schedule_type=schedule_type,
        name=vendor_name,
        description=note,
        expense_account_name=expense_account,
        original_amount=amount,
        remaining_amount=amount,
        accumulated_amount=0,
        useful_life_months=months,
        start_date=start,
        status="ACTIVE",
    )
    db.add(schedule)
    record_schedule_effect(db, execution, schedule)
    db.commit()
    db.refresh(schedule)

    monthly = round(amount / months, 2) if months > 0 else 0
    return (
        f"✅ Schedule row created (ID: {schedule.id}). "
        f"{vendor_name} — ${amount:,.2f} over {months} months (${monthly:,.2f}/mo). "
        f"Account: {expense_account}. Type: {schedule_type}. "
        f"The schedule page will show this row after refresh."
    )


def _handle_update_schedule_row(db: Session, realm_id: str, params: dict) -> str:
    """Update fields on an existing schedule row."""
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        return "Error: schedule_id is required."

    schedule = db.query(AccountingSchedule).filter(
        AccountingSchedule.id == schedule_id,
        AccountingSchedule.realm_id == realm_id,
    ).first()
    if not schedule:
        return f"Error: Schedule {schedule_id} not found."

    changes = []
    if "expense_account" in params:
        schedule.expense_account_name = params["expense_account"]
        changes.append(f"account → {params['expense_account']}")
    if "vendor_name" in params:
        schedule.name = params["vendor_name"]
        changes.append(f"name → {params['vendor_name']}")
    if "amount" in params:
        # AC-3b (plan 20260706-widen-internal-db-approval-execution): the ",.2f" format
        # TypeError'd on a non-numeric draft value — refuse instead of raising.
        try:
            amount = float(params["amount"])
        except (TypeError, ValueError):
            return "Error: amount must be a number."
        schedule.original_amount = amount
        changes.append(f"amount → ${amount:,.2f}")
    if "months" in params:
        schedule.useful_life_months = params["months"]
        changes.append(f"months → {params['months']}")
    if "note" in params:
        schedule.description = params["note"]
        changes.append(f"note → {params['note']}")

    if not changes:
        return "No fields provided to update."

    db.commit()
    return f"✅ Schedule {schedule_id} ({schedule.name}) updated: {', '.join(changes)}"


def _handle_update_schedule_month_values(db: Session, realm_id: str, params: dict) -> str:
    """Set or clear monthly expense values on a schedule row."""
    from ..models import ScheduleEntry
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        return "Error: schedule_id is required."

    schedule = db.query(AccountingSchedule).filter(
        AccountingSchedule.id == schedule_id,
        AccountingSchedule.realm_id == realm_id,
    ).first()
    if not schedule:
        return f"Error: Schedule {schedule_id} not found."

    clear_all = params.get("clear_all", False)
    month_values = params.get("month_values", {})

    if clear_all:
        # Zero out all existing entries
        existing = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all()
        for entry in existing:
            entry.amount = 0
        db.flush()

    # Upsert new month values
    updated_periods = []
    
    # Bulk fetch existing for targeted periods
    period_keys = list(month_values.keys())
    existing_entries = db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id == schedule_id,
        ScheduleEntry.period.in_(period_keys)
    ).all() if period_keys else []
    existing_map = {e.period: e for e in existing_entries}
    
    # PLAN-EXEMPT: B2-5b POSTED-clamp follow-up, authorized by Steven this turn — bounded
    # fix; qa-batch2 (which fixed the sibling setters) never landed on this branch.
    from ..services.schedule_engine import add_entry_or_skip, resolve_written_status

    for period_key, amt_val in month_values.items():
        amt_float = float(amt_val) if amt_val else 0.0
        existing = existing_map.get(period_key)
        if existing:
            existing.amount = amt_float
        else:
            inserted = add_entry_or_skip(
                db,
                schedule_id=schedule_id,
                realm_id=realm_id,
                period=period_key,
                amount=amt_float,
                # PLAN-EXEMPT: B2-5b POSTED-clamp — future periods land DRAFT, not
                # POSTED (the assistant may write a whole amortization table).
                status=resolve_written_status(period_key),
            )
            if inserted is None:
                # Lost a race: the row now exists — update it instead.
                existing = db.query(ScheduleEntry).filter(
                    ScheduleEntry.schedule_id == schedule_id,
                    ScheduleEntry.period == period_key,
                ).first()
                if existing:
                    existing.amount = amt_float
        updated_periods.append(f"{period_key}: ${amt_float:,.2f}")

    # Recalculate remaining amount
    all_entries = db.query(ScheduleEntry).filter(ScheduleEntry.schedule_id == schedule_id).all()
    total_expensed = sum(float(e.amount or 0) for e in all_entries)
    accumulated = float(schedule.accumulated_amount or 0)
    schedule.remaining_amount = float(schedule.original_amount or 0) - accumulated - total_expensed
    db.commit()

    action = "Cleared all months" if clear_all else ""
    if updated_periods:
        action += (" and set: " if clear_all else "Set: ") + ", ".join(updated_periods)
    return (
        f"✅ Schedule {schedule_id} ({schedule.name}) month values updated. {action}. "
        f"Remaining balance: ${schedule.remaining_amount:,.2f}"
    )


def _handle_bulk_set_budget_drivers(db: Session, realm_id: str, args: dict) -> str:
    """Handle bulk driver method assignment."""
    # PLAN-EXEMPT: DI-MISUSE remediation (closed plan 20260706-widen-internal-db-approval-execution
    # §10 follow-up) — this handler previously called the budget ROUTE function without
    # current_user, so the Depends sentinel reached _require_budget_access and raised on every
    # approval. It now calls the extracted service core, which takes no Depends params and does
    # no access check: this runs post-approval, where /actions/{id}/approve enforced EDITOR.
    from app.services.budget_drivers import bulk_set_driver_method_core
    from datetime import date as _date

    method = args.get("method", "ttm_avg")
    budget_year = args.get("budget_year", _date.today().year)
    scenario = args.get("scenario", "base")
    only_unset = args.get("only_unset", True)
    driver_category = args.get("driver_category", None)
    growth_rate = args.get("growth_rate", None)

    result = bulk_set_driver_method_core(
        db,
        company_id=realm_id,
        budget_year=budget_year,
        method=method,
        scenario=scenario,
        only_unset=only_unset,
        driver_category=driver_category,
        auto_create=True,
        growth_pct=growth_rate,
    )

    method_label = result.get("method_label", method)
    updated = result.get("drivers_updated", 0)
    created = result.get("drivers_created", 0)
    computed = result.get("drivers_computed", 0)
    errors = result.get("errors", [])

    parts = [f"✅ Bulk driver update complete — method: {method_label}"]
    if growth_rate is not None:
        parts.append(f"Growth rate: {growth_rate}%")
    if created:
        parts.append(f"Created {created} new driver(s)")
    if updated:
        parts.append(f"Updated {updated} existing driver(s)")
    parts.append(f"Computed values for {computed} driver(s)")
    if errors:
        parts.append(f"⚠️ {len(errors)} error(s): {'; '.join(errors[:3])}")

    return ". ".join(parts) + "."


def _handle_analyze_spending_trends(db: Session, realm_id: str, params: dict) -> str:
    """Composite: vendor spend + expense trends → AI analysis."""
    from ..services.llm import analysis_llm
    period = params.get("period", "ttm")
    focus = params.get("focus", "")

    data_parts = []
    try:
        qbo = get_qbo_client_from_db(db, realm_id)

        # 1) Vendor spend
        if period == "monthly" and "vendor_spend" in _MONTHLY_PACKS:
            vendor_data = _MONTHLY_PACKS["vendor_spend"](qbo)
        else:
            vendor_data = vendor_spend_summary(qbo)
        if isinstance(vendor_data, dict) and vendor_data.get("error"):
            raise ToolFailure("spending_source_unavailable", "Vendor spending is unavailable.", retryable=True)
        data_parts.append(f"VENDOR SPEND ({period}):\n{json.dumps(vendor_data, indent=1, default=str)[:6000]}")

        # 2) Expense trends
        expense_data = expense_trend_mom(qbo)
        if isinstance(expense_data, dict) and expense_data.get("error"):
            raise ToolFailure("spending_source_unavailable", "Expense trends are unavailable.", retryable=True)
        data_parts.append(f"EXPENSE TRENDS (MoM):\n{json.dumps(expense_data, indent=1, default=str)[:4000]}")

    except Exception as e:
        return _read_failure(
            "Spending data is temporarily unavailable.",
            e,
            error_code="assistant_spending_data_unavailable",
            log_context="assistant_spending_data_error",
        )

    user_q = get_current_user_question() or "Analyze spending trends"
    focus_note = f" The user is particularly interested in: {focus}." if focus else ""

    system = (
        "You are a senior CFO analyst performing a comprehensive spending analysis. "
        "You have two data sources: vendor-level spend and month-over-month expense trends.\n\n"
        "Provide a structured analysis covering:\n"
        "1. TOP SPENDING: Largest vendors/categories and their proportion of total spend\n"
        "2. TRENDS: Which expenses are growing fastest? Any concerning acceleration?\n"
        "3. ANOMALIES: Unusual spikes, new vendors, or unexpected changes\n"
        "4. RECOMMENDATIONS: 2-3 specific actionable items for the CFO\n\n"
        f"{focus_note}\n"
        "Use specific dollar figures and percentages. Write in concise prose, no markdown. "
        "Keep under 500 words. Start with the most impactful finding."
    )
    prompt = f"User question: {user_q}\n\n" + "\n\n".join(data_parts)

    try:
        result = analysis_llm.predict_text(system, prompt, temperature=0.1)
        if result and len(result) > 50:
            return f"[Comprehensive Spending Analysis]\n{result}"
    except Exception as e:
        logger.warning("[Assistant] Spending analysis failed: %s", redact_to_string(str(e), max_length=500))

    return "\n\n".join(data_parts)[:8000]


def _handle_analyze_profitability(db: Session, realm_id: str, params: dict) -> str:
    """Composite: P&L + profit margins → AI profitability analysis."""
    from ..services.llm import analysis_llm
    from datetime import date

    today = date.today()
    start_date = params.get("start_date", f"{today.year}-{today.month:02d}-01")
    end_date = params.get("end_date", str(today))

    data_parts = []
    try:
        qbo = get_qbo_client_from_db(db, realm_id)

        # 1) P&L report (account-level detail)
        report_params = {
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": "Accrual",
        }
        report = qbo.get_report("ProfitAndLoss", params=report_params)
        rows_data = report.get("Rows", {}).get("Row", [])
        pl_text = _parse_financial_report("ProfitAndLoss", rows_data, start_date, end_date)
        data_parts.append(f"PROFIT & LOSS ({start_date} to {end_date}):\n{pl_text[:5000]}")

        # 2) Profit margins by month
        margin_data = profit_and_margin_by_month(qbo)
        if isinstance(margin_data, dict) and margin_data.get("error"):
            raise ToolFailure("profitability_source_unavailable", "Profit margins are unavailable.", retryable=True)
        data_parts.append(f"PROFIT MARGINS (Monthly):\n{json.dumps(margin_data, indent=1, default=str)[:4000]}")

    except Exception as e:
        return _read_failure(
            "Profitability data is temporarily unavailable.",
            e,
            error_code="assistant_profitability_data_unavailable",
            log_context="assistant_profitability_data_error",
        )

    user_q = get_current_user_question() or "Analyze profitability"

    system = (
        "You are a senior CFO analyst performing a comprehensive profitability analysis. "
        "You have two data sources: a full P&L statement and monthly profit margin trends.\n\n"
        "Provide a structured analysis covering:\n"
        "1. REVENUE: Total revenue performance and key drivers\n"
        "2. MARGINS: Gross margin, operating margin trends. Are they improving or declining?\n"
        "3. COST STRUCTURE: Largest expense categories as % of revenue. COGS vs OpEx balance.\n"
        "4. RED FLAGS: Any concerning trends, margin compression, or runaway costs\n"
        "5. RECOMMENDATIONS: 2-3 specific actions to improve profitability\n\n"
        "Use specific dollar figures and percentages. Write in concise prose, no markdown. "
        "Keep under 500 words. Start with the headline number (net income or net margin)."
    )
    prompt = f"User question: {user_q}\n\n" + "\n\n".join(data_parts)

    try:
        result = analysis_llm.predict_text(system, prompt, temperature=0.1)
        if result and len(result) > 50:
            return f"[Comprehensive Profitability Analysis]\n{result}"
    except Exception as e:
        logger.warning("[Assistant] Profitability analysis failed: %s", redact_to_string(str(e), max_length=500))

    return "\n\n".join(data_parts)[:8000]


def _find_account_in_pl(rows: list, *search_terms: str) -> list:
    """
    Recursively search QBO P&L report rows for accounts matching search terms.
    Returns list of (account_name, column_values) tuples.

    QBO report structure:
    - Section rows have "Rows" > "Row" with nested data
    - Data rows have "ColData" with [{value: name}, {value: amount}, ...]
    - Summary rows have "ColData" too
    """
    results = []
    for row in rows:
        row_type = row.get("type", "")

        if row_type == "Section":
            # Header row might have our account
            header = row.get("Header", {})
            header_cols = header.get("ColData", [])
            if header_cols:
                row_name = str(header_cols[0].get("value", "")).lower()
                if any(term in row_name for term in search_terms):
                    # This section header matches — get its summary totals
                    summary = row.get("Summary", {})
                    summary_cols = summary.get("ColData", [])
                    if summary_cols:
                        vals = [c.get("value", "") for c in summary_cols[1:]]  # Skip name col
                        results.append((header_cols[0].get("value", ""), vals))

            # Recurse into section rows
            sub_rows = row.get("Rows", {}).get("Row", [])
            if sub_rows:
                results.extend(_find_account_in_pl(sub_rows, *search_terms))

        elif row_type == "Data":
            cols = row.get("ColData", [])
            if cols:
                row_name = str(cols[0].get("value", "")).lower()
                if any(term in row_name for term in search_terms):
                    vals = [c.get("value", "") for c in cols[1:]]  # Skip name col
                    results.append((cols[0].get("value", ""), vals))

    return results


def _handle_investigate_account(db: Session, realm_id: str, params: dict) -> str:
    """Composite: GL transactions + budget data for a specific account → AI deep dive."""
    from ..services.llm import analysis_llm
    from datetime import date, timedelta

    account_name = params.get("account_name", "")
    if not account_name:
        raise ToolFailure("invalid_request", ("Please specify an account name to investigate."))

    today = date.today()
    start_date = params.get("start_date", str(today - timedelta(days=90)))
    end_date = params.get("end_date", str(today))

    data_parts = []
    account_id = None
    financial_source_loaded = False
    try:
        qbo = get_qbo_client_from_db(db, realm_id)

        # 1) Resolve the account name to QBO ID(s) — including sub-accounts
        resolved_name = account_name
        account_ids = None
        child_count = 0
        try:
            resolved = _resolve_account_with_children(qbo, account_name)
            account_ids = resolved["account_ids"]
            account_id = resolved["id"]
            resolved_name = resolved["name"]
            child_count = resolved["child_count"]
            child_note = f" + {child_count} sub-accounts" if child_count else ""
            logger.info(f"[Assistant] investigate_account resolved '{account_name}' → {resolved_name}{child_note} (IDs: {account_ids})")
        except Exception as e:
            logger.warning(
                "[Assistant] Could not resolve account '%s': %s",
                account_name,
                redact_to_string(str(e), max_length=500),
            )

        # 2) Try General Ledger filtered to THIS account + sub-accounts
        gl_has_data = False
        report_params = {
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": "Accrual",
        }
        if account_ids:
            report_params["account"] = account_ids

        try:
            report = qbo.get_report("GeneralLedger", params=report_params)
            gl_text = _parse_gl_report(report, start_date, end_date) if account_ids else ""

            child_label = f" (includes {child_count} sub-accounts)" if child_count else ""
            if gl_text and len(gl_text.strip()) > 80 and "no data could be parsed" not in gl_text.lower():
                data_parts.append(f"GENERAL LEDGER for '{resolved_name}'{child_label} ({start_date} to {end_date}):\n{gl_text[:8000]}")
                gl_has_data = True
                financial_source_loaded = True
            else:
                logger.info(f"[Assistant] GL returned empty for {resolved_name}, trying P&L fallback")
        except Exception as e:
            logger.warning("[Assistant] GL query failed: %s", redact_to_string(str(e), max_length=500))

        # 3) FALLBACK: P&L with monthly columns — this DOES work for 7430
        if not gl_has_data:
            try:
                pl_params = {
                    "start_date": start_date,
                    "end_date": end_date,
                    "accounting_method": "Accrual",
                    "summarize_column_by": "Month",
                }
                pl_report = qbo.get_report("ProfitAndLoss", params=pl_params)
                pl_rows = pl_report.get("Rows", {}).get("Row", [])

                # Get column headers (months)
                pl_columns = pl_report.get("Columns", {}).get("Column", [])
                col_headers = [c.get("ColTitle", "") for c in pl_columns]

                # Search for the account in P&L rows
                acct_lower = account_name.lower()
                resolved_lower = resolved_name.lower()
                monthly_data = _find_account_in_pl(pl_rows, acct_lower, resolved_lower)

                if monthly_data:
                    financial_source_loaded = True
                    lines = [f"P&L MONTHLY BREAKDOWN for '{resolved_name}' ({start_date} to {end_date}):"]
                    for name_found, cols in monthly_data:
                        lines.append(f"\n  {name_found}:")
                        for i, val in enumerate(cols):
                            header = col_headers[i] if i < len(col_headers) else f"Col{i}"
                            try:
                                amt = float(str(val).replace(",", ""))
                                lines.append(f"    {header}: ${amt:,.2f}")
                            except (ValueError, TypeError):
                                if val:
                                    lines.append(f"    {header}: {val}")
                    data_parts.append("\n".join(lines))
                    logger.info(f"[Assistant] P&L fallback found {len(monthly_data)} matching rows for {resolved_name}")
                else:
                    logger.info(f"[Assistant] P&L fallback also found no match for {resolved_name}")
            except Exception as e:
                logger.warning("[Assistant] P&L fallback failed: %s", redact_to_string(str(e), max_length=500))

        # 4) FALLBACK: Query Bills directly for this account
        if not gl_has_data and account_id:
            try:
                bill_q = (
                    f"SELECT * FROM Bill WHERE TxnDate >= '{start_date}' "
                    f"AND TxnDate <= '{end_date}' MAXRESULTS 200"
                )
                bill_resp = qbo.query(bill_q)
                bills = bill_resp.get("QueryResponse", {}).get("Bill", [])

                matching_bills = []
                for b in bills:
                    for line in b.get("Line", []):
                        detail = line.get("AccountBasedExpenseLineDetail", {})
                        line_acct = detail.get("AccountRef", {}).get("value", "")
                        # Check if line references any of our account IDs
                        if str(line_acct) in (account_ids or "").split(","):
                            vendor = b.get("VendorRef", {}).get("name", "Unknown")
                            matching_bills.append({
                                "date": b.get("TxnDate", ""),
                                "vendor": vendor,
                                "amount": float(line.get("Amount", 0)),
                                "memo": line.get("Description", "")[:60],
                            })

                if matching_bills:
                    financial_source_loaded = True
                    matching_bills.sort(key=lambda x: x["date"], reverse=True)
                    total = sum(b["amount"] for b in matching_bills)
                    lines = [f"BILL TRANSACTIONS for '{resolved_name}' ({start_date} to {end_date}):"]
                    lines.append(f"  Total: ${total:,.2f} across {len(matching_bills)} bills")
                    for b in matching_bills[:30]:
                        lines.append(f"  {b['date']} | {b['vendor']} | ${b['amount']:,.2f} | {b['memo']}")
                    data_parts.append("\n".join(lines))
                    logger.info(f"[Assistant] Bill query found {len(matching_bills)} transactions for {resolved_name}")
            except Exception as e:
                logger.warning("[Assistant] Bill query fallback failed: %s", redact_to_string(str(e), max_length=500))

    except Exception as e:
        data_parts.append(_assistant_safe_error(
            "General ledger data is temporarily unavailable.",
            e,
            error_code="assistant_general_ledger_unavailable",
            log_context="assistant_investigate_account_gl_error",
        ))

    # 2) Budget data for this account
    try:
        from ..models import BudgetLine
        budget_lines = db.query(BudgetLine).filter(
            BudgetLine.company_id == realm_id,
            BudgetLine.budget_year == today.year,
        ).all()
        matches = [bl for bl in budget_lines if account_name.lower() in (bl.account_name or "").lower()]
        if matches:
            budget_info = []
            for bl in matches[:5]:
                monthly = bl.monthly_amounts or {}
                total = sum(float(v or 0) for v in monthly.values())
                budget_info.append(f"  {bl.account_name}: ${total:,.0f} annual budget")
            data_parts.append("BUDGET DATA:\n" + "\n".join(budget_info))
    except Exception:
        pass

    if not financial_source_loaded:
        raise ToolFailure("account_source_unavailable", f"Could not load financial activity for account '{account_name}'.", retryable=True)

    user_q = get_current_user_question() or f"Investigate the {account_name} account"

    system = (
        f"You are a senior CFO analyst performing a deep dive into the '{account_name}' account. "
        "You have GL transaction detail and budget data.\n\n"
        "Provide a structured analysis covering:\n"
        "1. ACTIVITY: Total amount, number of transactions, key vendors/payees\n"
        "2. PATTERNS: Recurring vs one-time charges, monthly run rate\n"
        "3. BUDGET: If budget data exists, how does actual compare? Any variance concerns?\n"
        "4. ANOMALIES: Unusual transactions, new vendors, spikes\n"
        "5. RECOMMENDATION: Should the CFO take any action on this account?\n\n"
        "Use specific dollar figures. Write in concise prose, no markdown. "
        "Keep under 400 words."
    )
    prompt = f"User question: {user_q}\n\n" + "\n\n".join(data_parts)

    try:
        result = analysis_llm.predict_text(system, prompt, temperature=0.1)
        if result and len(result) > 50:
            return f"[Account Deep Dive: {account_name}]\n{result}"
    except Exception as e:
        logger.warning("[Assistant] Account investigation failed: %s", redact_to_string(str(e), max_length=500))

    return "\n\n".join(data_parts)[:8000]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 Tool Handlers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_generate_report(db: Session, realm_id: str, params: dict) -> str:
    """Generate the month_end board-pack report (the same builder POST /reporting/generate uses)."""
    from ..reporting.pdf_renderer import render_pdf
    from ..reporting.report_builder import build_report_payload
    from ..reporting.report_definitions import get_report_definition

    period = params.get("period", "")
    if not period:
        return "Error: 'period' is required in YYYY-MM format (e.g. '2026-02')."

    try:
        payload = build_report_payload(db, realm_id, period, "month_end")
        pdf_bytes = render_pdf(payload, get_report_definition("month_end"))
    except Exception as e:
        logger.error("[Assistant] Report generation failed: %s", redact_to_string(str(e), max_length=500))
        return _assistant_safe_error(
            "The report could not be generated.",
            e,
            error_code="assistant_report_generation_failed",
            log_context="assistant_generate_report_error",
        )

    # Upsert report record
    recorded = True
    try:
        existing = db.query(Report).filter(
            Report.company_id == realm_id,
            Report.period == period,
        ).first()

        if existing:
            from sqlalchemy.sql import func
            existing.generated_at = func.now()
            existing.file_path = "generated_by_assistant"
            existing.report_type = "month_end"
        else:
            report = Report(
                company_id=realm_id,
                period=period,
                file_path="generated_by_assistant",
                report_type="month_end",
                generated_by="assistant",
            )
            db.add(report)
        db.commit()
    except Exception as e:
        db.rollback()
        recorded = False
        logger.warning("[Assistant] Failed to save report record: %s", redact_to_string(str(e), max_length=500))

    kpis = payload.get("kpis") or {}

    def _money(value) -> str:
        # A KPI the builder could not compute stays "unavailable", never $0.
        from decimal import Decimal, InvalidOperation

        if value is None or isinstance(value, bool):
            return "unavailable"
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return "unavailable"
        return f"${amount:,.0f}" if amount.is_finite() else "unavailable"

    quality = payload.get("data_quality") or {}
    partial_note = (
        "\n⚠️ Some sections could not be loaded from QuickBooks and are marked incomplete in the report."
        if quality.get("is_partial") else ""
    )
    where = (
        "The report is listed in the Reports section; the user can download it from the Reports page."
        if recorded else
        "The report history entry could not be saved; the user can generate it from the Reports page."
    )
    return (
        f"✅ Report generated successfully for period {period}.\n\n"
        f"Report highlights:\n"
        f"• Revenue: {_money(kpis.get('revenue'))}\n"
        f"• Net Income: {_money(kpis.get('net_income'))}\n"
        f"• PDF size: {len(pdf_bytes):,} bytes{partial_note}\n\n"
        f"It includes P&L, Balance Sheet, Cash Flow, KPIs, BVA variances, and AR/AP aging.\n"
        f"{where}"
    )


def _handle_get_close_status(db: Session, realm_id: str, params: dict) -> str:
    """Check close checklist progress for a period."""
    period = params.get("period", "")
    if not period:
        raise ToolFailure("invalid_request", ("Error: 'period' is required in YYYY-MM format."))

    tasks = db.query(CloseTask).filter(
        CloseTask.realm_id == realm_id,
        CloseTask.period == period,
    ).order_by(CloseTask.sort_order).all()

    if not tasks:
        return f"No close tasks found for period {period}. Use the Close Checklist page to generate tasks."

    total = len(tasks)
    completed = sum(1 for t in tasks if t.status == "COMPLETE")
    skipped = sum(1 for t in tasks if t.status == "SKIPPED")
    open_tasks = [t for t in tasks if t.status == "OPEN"]
    pct = round(completed / total * 100, 1) if total else 0

    lines = [
        f"📋 Close Status for {period}",
        "",
        f"Progress: {completed}/{total} tasks completed ({pct}%)",
        f"• Completed: {completed}",
        f"• Skipped: {skipped}",
        f"• Open: {len(open_tasks)}",
    ]

    # Check for overdue
    from datetime import date as date_type
    overdue = []
    for t in open_tasks:
        if hasattr(t, 'due_date') and t.due_date:
            try:
                due = t.due_date if isinstance(t.due_date, date_type) else date_type.fromisoformat(str(t.due_date))
                if due < date_type.today():
                    overdue.append(t)
            except Exception:
                pass

    if overdue:
        lines.append(f"\n⚠️ {len(overdue)} OVERDUE task(s):")
        for t in overdue:
            lines.append(f"  • [{t.id}] {t.title} (due: {t.due_date})")

    if open_tasks:
        lines.append("\n📝 Open tasks:")
        for t in open_tasks:
            assignee = f" (→ {t.assigned_to})" if hasattr(t, 'assigned_to') and t.assigned_to else ""
            lines.append(f"  • [ID:{t.id}] {t.title}{assignee}")

    return "\n".join(lines)


def _handle_complete_close_task(db: Session, realm_id: str, params: dict) -> str:
    """Mark a close task as complete."""
    task_id = params.get("task_id")
    if not task_id:
        return "Error: 'task_id' is required. Use get_close_status to find task IDs."

    task = db.query(CloseTask).filter(
        CloseTask.id == task_id,
        CloseTask.realm_id == realm_id,
    ).first()

    if not task:
        return f"Error: Close task with ID {task_id} not found for this company."

    if task.status == "COMPLETE":
        return f"Task '{task.title}' is already marked as completed."

    completed_by = params.get("completed_by", "AI Assistant")
    task.status = "COMPLETE"
    if hasattr(task, 'completed_by'):
        task.completed_by = completed_by
    if hasattr(task, 'completed_at'):
        from datetime import datetime
        task.completed_at = datetime.utcnow()
    db.commit()

    return f"✅ Task '{task.title}' marked as COMPLETED by {completed_by}."


def _money_or_na(val) -> str:
    """Format a snapshot money value; ``None`` (a partial snapshot) renders honestly."""
    if isinstance(val, (int, float)):
        return f"${val:,.2f}"
    return "n/a"


def _handle_get_ar_ap_summary(db: Session, realm_id: str, params: dict) -> str:
    """Get combined AR/AP summary — SNAPSHOT-FIRST (C6 AC-7.3).

    Serves the stored ``ARAPSummarySnapshot`` via ``serve_summary`` (the exact
    engine behind GET /ar-ap/summary, merged at 2b46f49): a fresh snapshot
    answers with ZERO live QBO calls; a miss returns the honest "computing"
    answer and enqueues ONE background warm. The old body ran ``ar_aging`` +
    ``ap_aging`` live inside the chat turn — two QBO report fan-outs competing
    with any same-realm background run — the same stall class the router
    already removed.
    """
    from ..services.ar_ap_summary import serve_summary

    try:
        payload = serve_summary(db, realm_id)
    except Exception as e:
        return _read_failure(
            "The AR/AP summary is temporarily unavailable.",
            e,
            error_code="assistant_ar_ap_summary_unavailable",
            log_context="assistant_ar_ap_summary_error",
        )

    if payload.get("data_quality") == "computing":
        raise ToolFailure("source_not_ready", ("📊 AR / AP Summary\n\n"
            "The AR/AP summary is being computed from QuickBooks right now — a "
            "background refresh has been scheduled. Ask again in a minute and "
            "I'll have the aging buckets, DSO/DPO, and net position ready."), retryable=True)

    recv = payload.get("receivables") or {}
    pay = payload.get("payables") or {}
    r_buckets = recv.get("buckets") or {}
    p_buckets = pay.get("buckets") or {}
    bucket_rows = (("Current", "current"), ("1-30 days", "1-30"), ("31-60 days", "31-60"),
                   ("61-90 days", "61-90"), ("90+ days", "90+"))

    lines = ["📊 AR / AP Summary\n"]

    lines.append(f"▲ Accounts Receivable: {_money_or_na(recv.get('total'))}")
    for label, key in bucket_rows:
        lines.append(f"  {label}: {_money_or_na(r_buckets.get(key))}")  # SA-OK: bounded-loop 5 dict.get over the fixed aging-bucket keys — no DB query here
    if recv.get("dso") is not None:
        lines.append(f"  DSO: {recv['dso']} days")
    ar_conc = recv.get("concentration")
    if isinstance(ar_conc, dict):
        lines.append(
            f"  Largest customer: {ar_conc.get('customer', 'Unknown')} — "
            f"{_money_or_na(ar_conc.get('balance'))} ({ar_conc.get('pct_of_total')}% of AR)"
        )

    lines.append("")

    lines.append(f"▼ Accounts Payable: {_money_or_na(pay.get('total'))}")
    for label, key in bucket_rows:
        lines.append(f"  {label}: {_money_or_na(p_buckets.get(key))}")  # SA-OK: bounded-loop 5 dict.get over the fixed aging-bucket keys — no DB query here
    if pay.get("dpo") is not None:
        lines.append(f"  DPO: {pay['dpo']} days")
    ap_conc = pay.get("concentration")
    if isinstance(ap_conc, dict):
        lines.append(
            f"  Largest vendor: {ap_conc.get('vendor', 'Unknown')} — "
            f"{_money_or_na(ap_conc.get('balance'))} ({ap_conc.get('pct_of_total')}% of AP)"
        )

    net = payload.get("net_position")
    if isinstance(net, (int, float)):
        net_label = "Net Receivable" if net >= 0 else "Net Payable"
        lines.append(f"\n💰 {net_label}: ${abs(net):,.2f}")

    as_of = payload.get("as_of")
    if as_of:
        lines.append(f"\nAs of {as_of} (stored snapshot; refreshed in the background).")

    # W2 typed-fact emission (plan 20260712-agentic-finance-os): the SAME payload values
    # rendered above, as point-in-time stocks at the snapshot date. An unparseable as_of
    # falls back to today (the snapshot serves the CURRENT position); dso/dpo ride along
    # for completeness even though prose rarely quotes them with $ / %.
    try:
        snapshot_date = date.fromisoformat(str(as_of)[:10])
    except (ValueError, TypeError):
        snapshot_date = date.today()
    fact_rows = (
        ("ar.total_outstanding", recv.get("total")),
        ("ap.total_outstanding", pay.get("total")),
        ("ar.dso_days", recv.get("dso")),
        ("ap.dpo_days", pay.get("dpo")),
    )
    for metric_id, amount in fact_rows:  # SA-OK: bounded-loop 4 dict.get over the in-memory payload — no DB query here
        if isinstance(amount, (int, float)):
            emit_fact(
                metric_id=metric_id,
                value=amount,
                entity_id=realm_id,
                period_start=snapshot_date,
                period_end=snapshot_date,
                period_grain=PeriodGrain.POINT_IN_TIME,
                basis=Basis.ACCRUAL,
                scenario=ScenarioKind.ACTUAL,
                source_tool="get_ar_ap_summary",
                as_of=str(as_of) if as_of else None,
            )

    quality = payload.get("data_quality")
    if isinstance(quality, dict) and quality.get("is_partial"):
        raise ToolFailure("ar_ap_partial", "\n".join(lines), retryable=True)
    return "\n".join(lines)


def _handle_get_executive_briefing(db: Session, realm_id: str, params: dict) -> str:
    """Retrieve the latest executive briefing from cache."""
    from ..services.tenant_companies import organization_company_for_realm, organization_for_company

    organization = organization_for_company(db, realm_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=realm_id)
        if organization
        else None
    )
    cache_query = db.query(AssistantContextCache).filter(AssistantContextCache.realm_id == realm_id)
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

    if not cache or not cache.executive_summary:
        # No cached briefing — try to build one on the fly
        try:
            from ..services.context_service import build_context
            context_text = build_context(db, realm_id)
            if context_text:
                return (
                    "📋 Executive Briefing (live):\n\n"
                    f"{context_text[:4000]}\n\n"
                    "Note: This was generated live. A pre-computed briefing will be "
                    "available after the next nightly cache refresh."
                )
        except Exception as e:
            logger.warning("[Assistant] Live briefing build failed: %s", redact_to_string(str(e), max_length=500))

        raise ToolFailure("source_not_ready", ("No executive briefing is available yet. The briefing is generated "
            "during the nightly context cache refresh. Try asking specific questions "
            "about financials, and I can pull live data from QuickBooks."), retryable=True)

    return f"📋 Executive Briefing:\n\n{cache.executive_summary}"


def _handle_delete_schedule_row(db: Session, realm_id: str, params: dict) -> str:
    """Delete a schedule row and its entries."""
    schedule_id = params.get("schedule_id")
    if not schedule_id:
        return "Error: 'schedule_id' is required. Use get_schedule_data to find the ID."

    schedule = db.query(AccountingSchedule).filter(
        AccountingSchedule.id == schedule_id,
        AccountingSchedule.realm_id == realm_id,
    ).first()

    if not schedule:
        return f"Error: Schedule row with ID {schedule_id} not found for this company."

    # PLAN-EXEMPT: AttributeError hotfix — AccountingSchedule has no expense_account
    # attribute (the column is expense_account_name), so deleting any existing row
    # crashed before the delete committed. `name` carries the vendor label here
    # (add_schedule_row stores it there; get_schedule_data reports it as vendor_name).
    description = f"{schedule.name or schedule.vendor_name or 'Unknown'} — {schedule.expense_account_name or 'N/A'}"
    sched_type = schedule.schedule_type

    # Delete child entries first
    deleted_entries = db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id == schedule_id,
    ).delete()

    # Delete the schedule row
    db.delete(schedule)
    db.commit()

    return (
        f"🗑️ Deleted schedule row #{schedule_id}:\n"
        f"  Type: {sched_type}\n"
        f"  Description: {description}\n"
        f"  Entries removed: {deleted_entries}\n\n"
        f"This action cannot be undone."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: Cash Flow Forecast AI Planning Handlers
# ─────────────────────────────────────────────────────────────────────────────

def _require_complete_cashflow_source(data: dict) -> None:
    """A presentation-ready zero grid is not evidence of available cash data."""
    quality = data.get("data_quality") or {}
    if (not isinstance(quality, dict) or quality.get("is_partial")
            or quality.get("unavailable_sections") or data.get("errors")
            or data.get("snapshot_state") in {"building", "failed", "unavailable"}):
        raise ToolFailure("cash_forecast_incomplete", "The cash forecast source is building or incomplete; retry after its required data is available.", retryable=True)


def _handle_get_cashflow_snapshot(db: Session, realm_id: str, params: dict) -> str:
    """Read the current 13-week cash flow forecast grid and return a structured summary."""
    try:
        from ..routers.cashflow_forecast import build_weekly_forecast

        data = build_weekly_forecast(company_id=realm_id, weeks=13, lookback=1, db=db)
        _require_complete_cashflow_source(data)
        weeks = data.get("week_headers") or []
        lookback = int(data.get("lookback") or 0)
        total_in = data.get("total_cash_in") or []
        total_out = data.get("total_cash_out") or []
        ending = data.get("ending_balances") or []
        if not weeks or not ending or len(weeks) != len(ending):
            raise ToolFailure("cash_forecast_incomplete", "The cash forecast is not ready with a complete period grid.", retryable=True)

        # W2 typed-fact emission (plan 20260712-agentic-finance-os): the horizon-end
        # week-ending cash + the minimum week-ending balance over the FORECAST weeks
        # (past-lookback weeks are actuals, excluded from the min). Week headers are ISO
        # Fridays (build_weekly_forecast); an unparseable header emits nothing — a fact
        # date is never fabricated.
        if weeks and ending and len(weeks) == len(ending):
            try:
                horizon_end = date.fromisoformat(str(weeks[-1]))
                forecast_slice = list(range(lookback, len(ending))) or [len(ending) - 1]
                min_idx = min(forecast_slice, key=lambda i: float(ending[i]))
                min_week = date.fromisoformat(str(weeks[min_idx]))
                as_of_label = str(data.get("bank_balance_as_of") or "") or None
                emit_fact(
                    metric_id="cashflow_forecast.week_ending_cash",
                    value=float(ending[-1]),
                    entity_id=realm_id,
                    period_start=horizon_end,
                    period_end=horizon_end,
                    period_grain=PeriodGrain.POINT_IN_TIME,
                    basis=Basis.BASIS_INDEPENDENT,
                    scenario=ScenarioKind.FORECAST,
                    source_tool="get_cashflow_snapshot",
                    as_of=as_of_label,
                )
                emit_fact(
                    metric_id="cashflow_forecast.min_cash_week_balance",
                    value=float(ending[min_idx]),
                    entity_id=realm_id,
                    period_start=min_week,
                    period_end=min_week,
                    period_grain=PeriodGrain.POINT_IN_TIME,
                    basis=Basis.BASIS_INDEPENDENT,
                    scenario=ScenarioKind.FORECAST,
                    source_tool="get_cashflow_snapshot",
                    as_of=as_of_label,
                )
            except (ValueError, TypeError):
                logger.warning("cashflow snapshot week headers unparseable — no facts emitted")

        lines = [
            "13-Week Cash Flow Forecast Snapshot",
            f"Week headers (Fridays): {', '.join(weeks)}",
            f"Lookback weeks: {lookback}",
            f"Beginning balance anchor: ${float(data.get('bank_balance') or 0):,.2f} as of {data.get('bank_balance_as_of') or 'unknown'}",
        ]
        statuses = data.get("week_statuses") or []
        if statuses:
            status_lines = [
                f"{item.get('week_ending')}: {item.get('status')} ({item.get('uncategorized_count', 0)} pending)"
                for item in statuses
            ]
            lines.append("Weekly actuals status: " + "; ".join(status_lines))
        review_required = data.get("review_required_weeks") or []
        if review_required:
            lines.append("Weeks needing actuals review: " + ", ".join(review_required))

        lines.append("")
        lines.append("=== CASH IN (Customers) ===")
        for row in sorted(data.get("customers") or [], key=lambda r: -sum(r.get("amounts") or [])):
            amounts = row.get("amounts") or []
            if sum(amounts) <= 0:
                continue
            lines.append(
                f"  {row.get('name')} ({row.get('source', '?')}) - day {row.get('effective_day', row.get('day_of_month', '?'))}, "
                f"avg ${float(row.get('effective_avg', row.get('avg_amount', 0)) or 0):,.0f}, total ${sum(amounts):,.0f}"
            )
            lines.append(f"    Weekly: [{', '.join(f'${float(a):,.0f}' for a in amounts)}]")

        lines.append("")
        lines.append("=== CASH OUT (Vendors) ===")
        for row in sorted(data.get("vendors") or [], key=lambda r: -sum(r.get("amounts") or [])):
            amounts = row.get("amounts") or []
            if sum(amounts) <= 0:
                continue
            lines.append(
                f"  {row.get('name')} ({row.get('source', '?')}) - day {row.get('effective_day', row.get('day_of_month', '?'))}, "
                f"avg ${float(row.get('effective_avg', row.get('avg_amount', 0)) or 0):,.0f}, total ${sum(amounts):,.0f}"
            )
            lines.append(f"    Weekly: [{', '.join(f'${float(a):,.0f}' for a in amounts)}]")

        lines.append("")
        lines.append("=== TOTALS ===")
        lines.append(f"Weekly total cash in:  [{', '.join(f'${float(t):,.0f}' for t in total_in)}]")
        lines.append(f"Weekly total cash out: [{', '.join(f'${float(t):,.0f}' for t in total_out)}]")
        lines.append(f"Ending balances:       [{', '.join(f'${float(e):,.0f}' for e in ending)}]")
        if ending:
            min_balance = min(float(e) for e in ending)
            if min_balance < 0:
                lines.append(f"\nALERT: Projected negative cash balance of ${min_balance:,.2f}")
            elif min_balance < 50000:
                lines.append(f"\nWARNING: Minimum projected balance is ${min_balance:,.2f}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(
            "[Assistant] get_cashflow_snapshot live payload failed: %s",
            redact_to_string(str(e), max_length=500),
            exc_info=True,
        )
        return _read_failure(
            "Cash forecast snapshot is temporarily unavailable.",
            e,
            error_code="assistant_cash_forecast_snapshot_unavailable",
            log_context="assistant_cashflow_snapshot_error",
        )

def _handle_update_cashflow_cells(db: Session, realm_id: str, params: dict) -> str:
    """Batch-update cells in the cash flow forecast via CashForecastOverride records."""
    from ..models import CashForecastOverride
    from datetime import date

    updates = params.get("updates", [])
    if not updates:
        return "Error: No updates provided. Pass an 'updates' array."

    results = []
    errors = []

    for upd in updates:
        entity_name = upd.get("entity_name", "").strip()
        entity_type = upd.get("entity_type", "").strip()
        week_str = upd.get("week_ending", "").strip()
        amount = upd.get("amount", 0)
        note = upd.get("note", "AI-generated adjustment")

        if not entity_name or not entity_type or not week_str:
            errors.append(f"Skipped invalid update: {upd}")
            continue

        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", week_str):
            errors.append(f"Invalid date format '{week_str}' for {entity_name}. MUST be exact YYYY-MM-DD format.")
            continue

        try:
            week_ending = date.fromisoformat(week_str)
        except ValueError:
            errors.append(f"Invalid date '{week_str}' for {entity_name}")
            continue

        # Upsert override
        existing = db.query(CashForecastOverride).filter(
            CashForecastOverride.company_id == realm_id,
            CashForecastOverride.entity_name == entity_name,
            CashForecastOverride.entity_type == entity_type,
            CashForecastOverride.week_ending == week_ending,
        ).first()

        if existing:
            old_amount = float(existing.override_amount)
            existing.override_amount = amount
            existing.note = note
            existing.created_by = "ai_assistant"
            results.append(f"  {entity_name} ({entity_type}) wk {week_str}: ${old_amount:,.0f} → ${amount:,.0f}")
        else:
            new_override = CashForecastOverride(
                company_id=realm_id,
                entity_name=entity_name,
                entity_type=entity_type,
                week_ending=week_ending,
                override_amount=amount,
                note=note,
                created_by="ai_assistant",
            )
            db.add(new_override)
            results.append(f"  {entity_name} ({entity_type}) wk {week_str}: → ${amount:,.0f} (new)")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return _assistant_safe_error(
            "Cash forecast overrides could not be saved.",
            e,
            error_code="assistant_cash_forecast_override_save_failed",
            log_context="assistant_cashflow_override_save_error",
        )

    summary = f"✅ Updated {len(results)} cell(s) in the cash flow forecast:\n" + "\n".join(results)
    if errors:
        summary += f"\n\n⚠️ {len(errors)} error(s):\n" + "\n".join(errors)

    summary += "\n\nThe user's forecast page will reflect these changes on next load."
    return summary


def _handle_layer_cashflow_schedule(db: Session, realm_id: str, params: dict) -> str:
    """Create an additive manual row in the forecast rather than editing base logic."""
    from ..models import CashForecastOverride
    from ..routers.cashflow_forecast import AVG_SENTINEL, DAY_SENTINEL
    from datetime import date, timedelta  # noqa: F401

    entity_name = str(params.get("entity_name") or "").strip()
    entity_type = str(params.get("entity_type") or "").strip()
    if not entity_name or entity_type not in {"customer", "vendor"}:
        return "Error: entity_name and entity_type ('customer' or 'vendor') are required."

    day_of_month = int(params.get("day_of_month") or 1)
    day_of_month = max(1, min(28, day_of_month))
    avg_amount = float(params.get("avg_amount") or 0)
    note = str(params.get("note") or "AI layered schedule")
    weekly_amounts = params.get("weekly_amounts") or []

    applied = []

    def _upsert(week_ending: date, amount: float) -> None:
        existing = db.query(CashForecastOverride).filter(
            CashForecastOverride.company_id == realm_id,
            CashForecastOverride.entity_name == entity_name,
            CashForecastOverride.entity_type == entity_type,
            CashForecastOverride.week_ending == week_ending,
        ).first()
        if existing:
            existing.override_amount = amount
            existing.note = note
            existing.created_by = "ai_assistant"
        else:
            db.add(CashForecastOverride(
                company_id=realm_id,
                entity_name=entity_name,
                entity_type=entity_type,
                week_ending=week_ending,
                override_amount=amount,
                note=note,
                created_by="ai_assistant",
            ))

    _upsert(DAY_SENTINEL, float(day_of_month))
    applied.append(f"day={day_of_month}")
    _upsert(AVG_SENTINEL, avg_amount)
    applied.append(f"avg=${avg_amount:,.0f}")

    weekly_lines = []
    for item in weekly_amounts:
        week_raw = str(item.get("week_ending") or "").strip()
        if not week_raw:
            continue
        try:
            week_ending = date.fromisoformat(week_raw)
        except ValueError:
            continue
        amount = float(item.get("amount") or 0)
        _upsert(week_ending, amount)
        weekly_lines.append(f"{week_raw}=${amount:,.0f}")

    db.commit()

    summary = (
        f"Created/updated additive cash flow row '{entity_name}' ({entity_type}). "
        f"Settings: {', '.join(applied)}."
    )
    if weekly_lines:
        summary += "\nWeekly layering: " + ", ".join(weekly_lines)
    summary += "\nThis layers a manual schedule into the forecast instead of overwriting existing customer/vendor logic."
    return summary


def _handle_spread_cashflow_balance(db: Session, realm_id: str, params: dict) -> str:
    """Spread a balance over weekly or monthly forecast buckets as an additive row."""
    from datetime import date
    from ..routers.cashflow_forecast import apply_spread_balance

    entity_name = str(params.get("entity_name") or "").strip()
    entity_type = str(params.get("entity_type") or "").strip()
    cadence = str(params.get("cadence") or "").strip().lower()
    if not entity_name or entity_type not in {"customer", "vendor"}:
        return "Error: entity_name and entity_type ('customer' or 'vendor') are required."
    if cadence not in {"weekly", "monthly"}:
        return "Error: cadence must be 'weekly' or 'monthly'."

    try:
        total_amount = float(params.get("total_amount") or 0)
    except (TypeError, ValueError):
        return "Error: total_amount must be numeric."
    try:
        periods = int(params.get("periods") or 0)
    except (TypeError, ValueError):
        return "Error: periods must be an integer."
    if periods <= 0:
        return "Error: periods must be greater than 0."
    start_raw = str(params.get("start_week_ending") or "").strip()
    start_week = None
    if start_raw:
        try:
            start_week = date.fromisoformat(start_raw)
        except ValueError:
            return f"Error: invalid start_week_ending '{start_raw}'. Use YYYY-MM-DD."
    day_of_month = int(params.get("day_of_month") or 15)

    try:
        result = apply_spread_balance(
            db,
            company_id=realm_id,
            entity_name=entity_name,
            entity_type=entity_type,
            total_amount=total_amount,
            periods=periods,
            cadence=cadence,
            start_week_ending=start_week,
            day_of_month=day_of_month,
            note=str(params.get("note") or "AI spread balance"),
        )
    except Exception as exc:
        if getattr(exc, "detail", None):
            return f"Error: {exc.detail}"
        raise

    spread_lines = ", ".join(
        f"{item['week_ending']}=${float(item['amount']):,.2f}" for item in result["weekly_amounts"]
    )
    return (
        f"Spread ${total_amount:,.2f} across {periods} {cadence} period(s) for '{entity_name}'.\n"
        f"Start week: {result['start_week_ending']} | Day-of-month anchor: {result['day_of_month']}\n"
        f"Buckets: {spread_lines}\n"
        "This layers a manual schedule into the forecast instead of overwriting existing customer/vendor logic."
    )


def _handle_analyze_cashflow_position(db: Session, realm_id: str, params: dict) -> str:
    """Fetch the forecast snapshot and produce a structured analysis for the AI."""
    # Reuse the snapshot handler to get the raw data
    snapshot = _handle_get_cashflow_snapshot(db, realm_id, {})
    focus = params.get("focus", "")

    focus_prompt = ""
    if focus:
        focus_prompt = f" Focus specifically on: {focus}."

    # Pre-analyze with the analysis engine
    return _analyze_with_llm(
        snapshot,
        f"cash flow forecast position analysis{focus_prompt}"
    )


def _handle_get_cashflow_actuals_review(db: Session, realm_id: str, params: dict) -> str:
    """Return pulled weekly actuals so the assistant can help classify them."""
    from datetime import date, timedelta
    from ..models import CashForecastActual

    week_ending_raw = params.get("week_ending")
    target_week = None
    if isinstance(week_ending_raw, str) and week_ending_raw.strip():
        try:
            target_week = date.fromisoformat(week_ending_raw.strip())
        except ValueError:
            raise ToolFailure("invalid_request", (f"Error: invalid week_ending '{week_ending_raw}'. Use YYYY-MM-DD."))

    if target_week is None:
        latest = (
            db.query(CashForecastActual.week_ending)
            .filter(CashForecastActual.company_id == realm_id)
            .order_by(CashForecastActual.week_ending.desc())
            .first()
        )
        if not latest or not latest[0]:
            return "No pulled cash flow actuals found yet."
        target_week = latest[0]

    category_filter = _normalize_cashflow_actual_category(
        params.get("category") or params.get("status") or params.get("match_category"),
        allow_all=True,
    )
    query = db.query(CashForecastActual).filter(
        CashForecastActual.company_id == realm_id,
        CashForecastActual.week_ending == target_week,
        CashForecastActual.parent_id == None,  # noqa: E711
    )
    if category_filter and category_filter != "all":
        query = query.filter(CashForecastActual.category == category_filter)
    records = query.order_by(CashForecastActual.txn_date.asc(), CashForecastActual.id.asc()).all()
    if not records:
        qualifier = f" with category {category_filter}" if category_filter and category_filter != "all" else ""
        return f"No pulled cash flow actuals{qualifier} found for week ending {target_week.isoformat()}."

    lines = [
        f"Weekly transaction review for {realm_id}",
        f"Week: {(target_week - timedelta(days=4)).isoformat()} to {target_week.isoformat()}",
        "",
    ]
    category_counts = {"categorized": 0, "uncategorized": 0, "excluded": 0, "decomposed": 0}
    for record in records:
        category = str(record.category or "uncategorized")
        category_counts[category] = category_counts.get(category, 0) + 1

    lines.append(
        "Summary: "
        f"{category_counts.get('categorized', 0)} categorized, "
        f"{category_counts.get('uncategorized', 0)} uncategorized, "
        f"{category_counts.get('excluded', 0)} excluded."
    )
    lines.append("")

    for record in records:
        lines.append(
            f"[{record.id}] {record.txn_date.isoformat()} | {record.direction} | "
            f"${float(record.amount or 0):,.2f} | {record.description or '(no description)'} | "
            f"acct={record.account_name or '?'} | entity={record.entity_name or '?'} | "
            f"type={record.entity_type or '?'} | category={record.category or 'uncategorized'}"
        )

    return "\n".join(lines)


def _normalize_cashflow_actual_category(value: Any, *, allow_all: bool = False) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"", "none"}:
        return None
    normalized_key = normalized.replace(" ", "_").replace("-", "_")
    if normalized_key in {"pending", "pending_categorization", "needs_review"}:
        return "uncategorized"
    if allow_all and normalized_key == "all":
        return "all"
    if normalized_key in {"categorized", "uncategorized", "excluded", "decomposed"}:
        return normalized_key
    return normalized_key


def _normalize_cashflow_actual_entity(entity_name: Any, entity_type: Any) -> tuple[Optional[str], Optional[str]]:
    name = str(entity_name).strip() if entity_name is not None else ""
    etype = str(entity_type).strip().lower() if entity_type is not None else ""
    lowered = name.lower().replace("_", " ").replace("-", " ")

    if lowered in {"other cash in", "other cash inflow", "other inflow"} or etype == "other_cash_in":
        return "Other Cash In", "other_cash_in"
    if lowered in {"non routine expenses", "non routine expense", "other cash out", "other outflow"} or etype == "non_routine":
        return "Non Routine Expenses", "non_routine"
    if not name:
        return None, etype or None
    return name, etype or None


def _handle_categorize_cashflow_actuals(db: Session, realm_id: str, params: dict) -> str:
    """Batch categorize pulled weekly actuals from assistant instructions."""
    from datetime import date
    from ..models import CashForecastActual
    from ..routers.cashflow_forecast import _sync_week_status, _to_date

    items = params.get("items") or []
    default_entity_name, default_entity_type = _normalize_cashflow_actual_entity(
        params.get("entity_name"),
        params.get("entity_type"),
    )
    default_category = _normalize_cashflow_actual_category(params.get("category")) or "categorized"
    if default_category not in {"categorized", "excluded"}:
        return "Error: category must be 'categorized' or 'excluded'."

    target_records = []
    bulk_description = ""
    if items:
        for item in items:
            actual_id = item.get("id")
            if actual_id is None:
                continue
            record = (
                db.query(CashForecastActual)
                .filter(
                    CashForecastActual.company_id == realm_id,
                    CashForecastActual.id == actual_id,
                )
                .first()
            )
            if record:
                target_records.append((record, item))
    else:
        week_ending_raw = params.get("week_ending")
        if not isinstance(week_ending_raw, str) or not week_ending_raw.strip():
            return "Error: Pass either an items array or week_ending for a bulk categorization."
        try:
            target_week = date.fromisoformat(week_ending_raw.strip())
        except ValueError:
            return f"Error: invalid week_ending '{week_ending_raw}'. Use YYYY-MM-DD."

        match_category = _normalize_cashflow_actual_category(
            params.get("match_category") or params.get("status") or params.get("apply_to") or "uncategorized",
            allow_all=True,
        )
        direction = str(params.get("direction") or "").strip().lower()
        if direction and direction not in {"in", "out"}:
            return "Error: direction must be 'in' or 'out'."

        query = db.query(CashForecastActual).filter(
            CashForecastActual.company_id == realm_id,
            CashForecastActual.week_ending == target_week,
            CashForecastActual.parent_id == None,  # noqa: E711
        )
        if match_category and match_category != "all":
            query = query.filter(CashForecastActual.category == match_category)
        if direction:
            query = query.filter(CashForecastActual.direction == direction)
        target_records = [(record, {}) for record in query.order_by(CashForecastActual.id.asc()).all()]
        bulk_description = (
            f" for week ending {target_week.isoformat()}"
            f" where category={match_category or 'uncategorized'}"
            f"{f' and direction={direction}' if direction else ''}"
        )

    updated = []
    direction_counts: Dict[str, int] = {}
    touched_weeks = set()
    for record, item in target_records:
        entity_name, entity_type = _normalize_cashflow_actual_entity(
            item.get("entity_name", default_entity_name),
            item.get("entity_type", default_entity_type),
        )
        category = _normalize_cashflow_actual_category(item.get("category", default_category)) or default_category
        if category not in {"categorized", "excluded"}:
            continue
        if category == "categorized" and (not entity_name or not entity_type):
            continue
        if category == "excluded" and not entity_name:
            entity_name = "__excluded"
        if category == "excluded" and not entity_type:
            entity_type = "vendor" if record.direction == "out" else "customer"
        record.entity_name = entity_name  # type: ignore[assignment]
        record.entity_type = entity_type  # type: ignore[assignment]
        record.category = category  # type: ignore[assignment]
        updated.append(record)
        touched_weeks.add((record.company_id, _to_date(record.week_ending)))
        direction_counts[str(record.direction or "?")] = direction_counts.get(str(record.direction or "?"), 0) + 1

    if not updated:
        return "No matching cash flow actuals were updated. Check the week, current category/status, and entity defaults."

    for company_id, week_ending in touched_weeks:
        _sync_week_status(db, company_id, week_ending)

    db.commit()
    remaining_bits = []
    for company_id, week_ending in sorted(touched_weeks, key=lambda item: (item[0], item[1])):
        remaining = db.query(CashForecastActual).filter(
            CashForecastActual.company_id == company_id,
            CashForecastActual.week_ending == week_ending,
            CashForecastActual.parent_id == None,  # noqa: E711
            CashForecastActual.category == "uncategorized",
        ).count()
        remaining_bits.append(f"{week_ending.isoformat()}: {remaining} uncategorized remaining")

    direction_summary = ", ".join(f"{direction}={count}" for direction, count in sorted(direction_counts.items()))
    remaining_summary = "; ".join(remaining_bits)
    return (
        f"Updated {len(updated)} cash flow actual(s){bulk_description}."
        f"{f' Direction counts: {direction_summary}.' if direction_summary else ''}"
        f"{f' Remaining: {remaining_summary}.' if remaining_summary else ''}\n" +
        "\n".join(
            f"  [{record.id}] {record.entity_name} ({record.entity_type}) -> {record.category}"
            for record in updated
        )
    )


def _handle_teach_peregrine(db: Session, realm_id: str, params: dict) -> str:
    """Persist a taught fact through the Memory-v2 write path (agent_memory.remember).

    Dead from the chat loop (ToolExecutor intercepts ``teach_peregrine`` into a
    MEMORY_UPDATE PENDING proposal) — this handler is the APPROVAL-time executor:
    the internal execution core resolves it via ACTION_TYPE_TO_TOOL["MEMORY_UPDATE"]
    and its params carry the approver as ``_approved_by`` (action_tool_input,
    internal_action_execution.py). remember() gives the row versioning, provenance,
    supersession (re-teach → version chain) and the null-guarded embedding write.
    """
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
    from ..services import agent_memory
    from ..services.llm import analysis_llm
    from ..services.tenant_companies import organization_company_for_realm, organization_for_company

    category = str(params.get("category") or "General").strip() or "General"
    fact = str(params.get("fact") or "").strip()

    if not fact:
        return "Error: You must provide a fact to learn."

    # get_embedding returns None when the semantic path is unavailable (no OpenAI key)
    # or generation failed; remember() null-guards the assignment (AP-VEC-8), so the
    # fact is then saved text-only (column SQL NULL — the re-embed backfill picks it up).
    embedding = analysis_llm.get_embedding(fact)
    if embedding is None:
        logger.info("[learn_fact] storing knowledge without an embedding (semantic search unavailable)")
    organization = organization_for_company(db, realm_id)
    organization_company = (
        organization_company_for_realm(db, organization_id=organization.id, realm_id=realm_id)
        if organization
        else None
    )

    # The approving human (internal execution lane) or the current chat user (legacy
    # direct-handler lane) — recorded as accepted_by so the memory is authoritative.
    accepted_by = str(params.get("_approved_by") or params.get("_current_user_email") or "").strip() or None
    source_metadata = params.get("source_metadata")
    provenance = {"source_type": str(params.get("source") or "ai_assistant_chat")}
    if isinstance(source_metadata, dict) and source_metadata:
        provenance["source_metadata"] = source_metadata

    # Re-teaching the same fact supersedes the prior version (version chain) instead
    # of duplicating the row.
    supersedes = agent_memory.find_reteach_target(
        db, company_id=realm_id, category=category, content=fact,
    )
    row = agent_memory.remember(
        db,
        company_id=realm_id,
        category=category,
        content=fact,
        source=str(params.get("source") or "ai_assistant_chat"),
        provenance=provenance,
        organization_id=organization.id if organization else None,
        organization_company_id=organization_company.id if organization_company else None,
        supersedes=supersedes,
        accepted_by=accepted_by,
        embedding=embedding,
    )

    return (
        f"Got it. I have permanently remembered this rule/fact under '{category}' "
        f"(memory #{row.id}, version {row.version})."
    )
def _handle_create_macro_scenario(db: Session, realm_id: str, params: dict) -> str:
    """Clone active budget lines into a new scenario and apply macro multipliers."""
    from ..models import BudgetLine  # noqa: F811
    
    scenario_name = params.get("scenario_name")
    target_type = params.get("target_type", "all").lower()
    multiplier = float(params.get("multiplier", 1.0))
    from datetime import date
    budget_year = int(params.get("budget_year", date.today().year))

    if not scenario_name:
        return "Error: missing scenario_name."
        
    base_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == realm_id,
        BudgetLine.budget_year == budget_year,
        BudgetLine.scenario == "base"
    ).all()
    
    if not base_lines:
        return f"Error: No base budget lines found for {budget_year}."

    created_count = 0
    modified_count = 0

    for bl in base_lines:
        new_bl = BudgetLine(
            company_id=bl.company_id,
            budget_year=bl.budget_year,
            account_name=bl.account_name,
            account_type=bl.account_type,
            qbo_account_id=bl.qbo_account_id,
            monthly_amounts=dict(bl.monthly_amounts),
            sort_order=bl.sort_order,
            notes=f"Cloned for scenario: {scenario_name}",
            department=bl.department,
            status=bl.status,
            scenario=scenario_name,
            driver_formula=bl.driver_formula,
            parent_line_id=bl.parent_line_id,
            vendor_name=bl.vendor_name,
            customer_name=bl.customer_name,
            original_monthly_amounts=bl.original_monthly_amounts
        )
        
        # Apply Multiplier
        if target_type == "all" or bl.account_type.lower() == target_type:
            for month in new_bl.monthly_amounts:
                old_val = float(new_bl.monthly_amounts[month] or 0)
                new_bl.monthly_amounts[month] = round(old_val * multiplier, 2)
            modified_count += 1
            
        db.add(new_bl)
        created_count += 1
        
    db.commit()
    return f"Successfully created scenario '{scenario_name}' by cloning {created_count} base lines. Applied multiplier {multiplier}x to {modified_count} '{target_type}' lines."


def _handle_analyze_lbo_balance(db: Session, realm_id: str, params: dict) -> str:
    """Fetch the LBO grid for diagnosis, labeled by its period axis (LboPeriod).

    Reads statement / balance-check cells via period metadata (period_id →
    LboPeriod seq/label) — the three-statement-spine period axis (plan
    20260717-three-statement-spine, C3-d / AC-7). Historical periods carry
    NEGATIVE seq/col_index; the pre-cutover ``col_index > 0`` filter silently
    dropped them (an empty/garbled agent answer, never an exception), so the read
    is period-keyed and every value is labeled by its real period (FY-3…LTM…Y5),
    not a bare column index.
    """
    from ..models import LboScenario, LboCell, LboPeriod  # noqa: F811
    from ..lbo.grid_helpers import positional_period_label

    scenario_name = params.get("scenario_name")
    query = db.query(LboScenario).filter(LboScenario.company_id == realm_id)
    if scenario_name:
        query = query.filter(LboScenario.name.ilike(f"%{scenario_name}%"))
    
    scenarios = query.all()
    if not scenarios and scenario_name:
        raise ToolFailure("scenario_not_found", f"No LBO scenario found matching '{scenario_name}'.")
    if not scenarios:
        return "No LBO scenarios found."
        
    scenario = next((s for s in scenarios if s.is_active), scenarios[0])
    
    cells = db.query(LboCell).filter(LboCell.scenario_id == scenario.id, LboCell.row_index >= 0).all()
    if not cells:
        raise ToolFailure("source_not_ready", (f"Grid is empty for scenario '{scenario.name}'. The LBO model might need to be initialized."), retryable=False)
        
    # Period axis: period_id → LboPeriod. A period-keyed scenario labels every
    # value column by its real period, ordered by seq (spine order — negatives are
    # historicals). An un-backfilled/legacy cell (period_id NULL) degrades to the
    # col_index convention (col 1 = LTM, col 2..6 = Y1..Y5); label-only cells
    # (col 0 / value None) carry no column.
    periods = db.query(LboPeriod).filter(
        LboPeriod.scenario_id == scenario.id,
        LboPeriod.company_id == realm_id,
    ).all()
    period_by_id = {p.id: p for p in periods}

    # pivot value cells by row, each carrying its (seq, period-label)
    by_row = {}
    for c in cells:
        if c.row_index not in by_row:
            by_row[c.row_index] = {"label": c.label or "", "values": []}
        if c.value is None:
            continue  # label-only / empty cell — nothing to diagnose
        period = period_by_id[c.period_id] if c.period_id in period_by_id else None
        if period is not None:
            seq, plabel = period.seq, period.label
        elif c.col_index and c.col_index > 0:
            # FYCY axis (plan 20260718-fycy-absolute-axis, AC-AXIS-7): positional
            # degrade for a legacy cell with no LboPeriod — seq == col_index − 1
            # (col 1 = TTM, col 2 = FYCY, col k≥3 = FY+{k−2}).
            seq, plabel = c.col_index - 1, positional_period_label(c.col_index)
        else:
            continue  # positional label-only cell with no period

        by_row[c.row_index]["values"].append((seq, plabel, c.value))

    # Key rows to check
    key_terms = ["Total Assets", "Total Liabilities", "Total Equity", "Balance Check",
                 "Net Income", "Retained Earnings", "Ending Cash", "Cash",
                 "Net Change in Cash", "Beginning Cash"]

    spine = [lbl for _seq, lbl in sorted({(p.seq, p.label) for p in periods})]
    header = (
        f"LBO Grid Diagnostics for Scenario '{scenario.name}'"
        + (f" (periods: {', '.join(spine)}):" if spine else " (by period):")
    )
    output = [header]
    for row_idx in sorted(by_row.keys()):
        label = by_row[row_idx]["label"] or ""
        if any(term.lower() in label.lower() for term in key_terms):
            vals = sorted(by_row[row_idx]["values"], key=lambda x: x[0])
            val_strs = [f"{plabel}={float(val):,.0f}" for _seq, plabel, val in vals]
            output.append(f"{label}: " + " | ".join(val_strs))

    return "\n".join(output)


def _handle_get_lbo_sensitivity(db: Session, realm_id: str, params: dict) -> str:
    """READ (clean): an LBO scenario's 2-D sensitivity grid + base-case attribution.

    Parity: mirrors GET /scenarios/{id}/sensitivity-grid (app/routers/lbo.py:764). It calls
    the SAME pure engines (compute_entry_exit_grid / compute_leverage_growth_grid /
    returns_attribution) over the SAME serialized scenario data, so it cannot drift from the
    UI. Tenant-scoped: the scenario is resolved by id UNDER the realm scope (a scenario from
    another tenant is not found) — the trusted realm_id, never a model-supplied id. Read-only:
    it computes, it never writes.
    """
    from ..lbo.forecast_engine import compute_full_forecast
    from ..lbo.exit_waterfall import compute_exit
    from ..lbo.grid_helpers import _get_historical_bs
    from ..lbo.sensitivity_grids import (
        compute_entry_exit_grid,
        compute_leverage_growth_grid,
        returns_attribution,
    )
    from ..lbo.serializers import _capex_to_dict, _debt_to_dict, _payroll_to_dict
    # ONE horizon (AC-3, plan 20260718-fycy-absolute-axis): route the assistant
    # sensitivity through the SAME config-span-clamped count the engine run + the
    # /sensitivity route use, never the raw unclamped span (which can exceed the
    # 7-wide grid and diverge from the grid horizon).
    from ..lbo.engine_composition import _scenario_horizon
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening round-2 — ruff F821 surfaced a
    # GENUINE latent bug: validate_assumptions was never imported in this handler, so
    # get_lbo_sensitivity would NameError at runtime on the driver-validation line.
    from ..lbo.assumptions import validate_assumptions
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening round-2 — ruff F401 full-tree-clean
    # (DIM-020): unused LboCompany/LboScenario dropped from the local import block.
    from ..models import (
        LboCapexDriver,
        LboDebtTranche,
        LboDriver,
        LboPayrollDriver,
    )

    scenario_id = params.get("scenario_id")
    if not scenario_id:
        raise ToolFailure("invalid_request", ("Error: scenario_id is required."))
    axes = str(params.get("axes") or "entry_exit").strip()
    if axes not in ("entry_exit", "leverage_growth"):
        raise ToolFailure("invalid_request", ("Error: axes must be 'entry_exit' or 'leverage_growth'."))

    scenario = (
        db.query(LboScenario)
        .filter(LboScenario.id == scenario_id, LboScenario.company_id == realm_id)
        .first()
    )
    if scenario is None:
        raise ToolFailure("invalid_request", (f"Error: LBO scenario {scenario_id} not found for this company."))
    company = db.query(LboCompany).filter(LboCompany.id == scenario.lbo_company_id).first()
    if company is None:
        raise ToolFailure("invalid_request", (f"Error: LBO company config not found for scenario {scenario_id}."))

    drv = (
        db.query(LboDriver)
        .filter(LboDriver.company_id == realm_id, LboDriver.scenario_id == scenario_id)
        .first()
    )
    drivers = validate_assumptions(drv.driver_data if drv else None)
    payroll = (
        db.query(LboPayrollDriver)
        .filter(LboPayrollDriver.company_id == realm_id, LboPayrollDriver.scenario_id == scenario_id)
        .all()
    )
    capex = (
        db.query(LboCapexDriver)
        .filter(LboCapexDriver.company_id == realm_id, LboCapexDriver.scenario_id == scenario_id)
        .all()
    )
    debt = (
        db.query(LboDebtTranche)
        .filter(LboDebtTranche.company_id == realm_id, LboDebtTranche.scenario_id == scenario_id)
        .order_by(LboDebtTranche.sort_order)
        .all()
    )

    num_years = _scenario_horizon(company)
    engine_kwargs = dict(
        drivers=drivers,
        payroll_drivers=[_payroll_to_dict(p) for p in payroll],
        capex_drivers=[_capex_to_dict(c) for c in capex],
        debt_tranches=[_debt_to_dict(d) for d in debt],
        num_years=num_years,
        ltm_ebitda=float(drivers.get("ltm_ebitda", 0)),
        base_revenue=float(drivers.get("base_revenue", 0)),
        historical_bs=_get_historical_bs(scenario_id, db),
    )

    try:
        if axes == "entry_exit":
            grid = compute_entry_exit_grid(**engine_kwargs)
        else:
            grid = compute_leverage_growth_grid(**engine_kwargs)
        base_forecast = compute_full_forecast(**engine_kwargs)
        base_exit = compute_exit(
            forecast_years=base_forecast["pnl"],
            debt_schedule_years=base_forecast["debt_schedule"]["schedule"],
            drivers=drivers,
            entry_equity=base_forecast["entry_equity"],
            distributions=base_forecast["distributions"],
            cash_by_year=[float(b["cash"]) for b in base_forecast["bs"]],
        )
        attribution = returns_attribution(base_forecast["sources_uses"], base_exit)
    except ValueError as e:
        # An axis that cannot vary (no funded debt / zero LTM EBITDA) is an honest,
        # stated refusal — never a silently-flat fabricated grid.
        raise ToolFailure("invalid_request", (f"Error: this scenario's {axes} sensitivity cannot be computed — {redact_to_string(str(e), max_length=200)}"))

    row_axis = grid.get("row_axis", {})
    col_axis = grid.get("col_axis", {})
    cells = grid.get("cells", [])
    row_values = list(row_axis.get("values") or [])
    lines = [
        f"LBO sensitivity — scenario '{scenario.name}' ({axes})",
        f"Rows = {row_axis.get('name')}: {row_values}",
        f"Cols = {col_axis.get('name')}: {col_axis.get('values')}",
        "IRR / MOIC per cell (row × col):",
    ]
    for r_idx, row in enumerate(cells):
        row_val = row_values[r_idx] if r_idx < len(row_values) else None
        cell_strs = []
        for cell in row:
            irr = cell.get("irr")  # SA-OK: bounded-loop over the in-memory grid cell dict, not a DB query
            moic = cell.get("moic")  # SA-OK: bounded-loop over the in-memory grid cell dict, not a DB query
            irr_s = f"{irr * 100:.1f}%" if isinstance(irr, (int, float)) else "n/a"
            moic_s = f"{moic:.2f}x" if isinstance(moic, (int, float)) else "n/a"
            cell_strs.append(f"{irr_s}/{moic_s}")
        lines.append(f"  {row_val}: " + " | ".join(cell_strs))

    if attribution:
        lines.append(
            "Base-case attribution — "
            + ", ".join(f"{k}: {v}" for k, v in attribution.items())  # SA-OK: bounded-loop over the in-memory attribution dict, not a DB query
        )
    return "\n".join(lines)


def _handle_propose_lbo_assumptions(db: Session, realm_id: str, params: dict) -> str:
    """PROPOSAL-gated WRITE (approval-time only): apply assumption changes to an LBO scenario.

    NOTHING mutates from chat. The agent tool ``propose_lbo_assumptions`` drafts a PENDING
    LBO_ASSUMPTIONS_UPDATE ProposedAction; THIS handler runs ONLY when that proposal is
    approved and its execution resolves here (internal_action_execution → TOOL_HANDLERS). It
    delegates to the shared write core (apply_lbo_assumptions, app/lbo/assumption_write.py) —
    the SAME path the live PUT /drivers save runs — merging the partial ``updates`` onto the
    scenario's current assumptions under the realm scope, then committing + re-solving. The
    before/after snapshot was embedded in the ProposedAction payload at chat time (a
    savepoint-preview that wrote ZERO rows). Its safety_expectation fixture
    (propose_lbo_assumptions_proposal) lives in app/agent/evals.py."""
    from ..lbo.assumption_write import LboAssumptionWriteError, apply_lbo_assumptions

    scenario_id = params.get("scenario_id")
    if not scenario_id:
        return "Error: scenario_id is required."
    updates = params.get("updates")
    if not isinstance(updates, dict) or not updates:
        return "Error: updates must be a non-empty object of assumption keys to change."
    try:
        apply_lbo_assumptions(db, realm_id, int(scenario_id), updates, merge=True)
    except LboAssumptionWriteError as exc:
        return f"Error: {redact_to_string(str(exc), max_length=200)}"
    changed = sorted(str(k) for k in updates)
    return f"✅ LBO scenario {scenario_id} assumptions updated ({', '.join(changed)}). The model re-solves with the new values."


# ── Phase-A read-parity tools (C2-a): live review READs mirroring UI services ──
# Each handler MIRRORS the SAME compute the UI route uses (parity, not re-derivation):
# it imports and calls the exact service/helper functions the router calls, scoping
# by the executor-supplied realm_id (trusted tenant scope; never a model-supplied id).
# Output is a BOUNDED summary + drill handles — never a full grid serialized to the LLM.

# W2 (plan 20260712-agentic-finance-os): built-in covenant metric_type → canonical
# metric_id. Custom formulas deliberately have no row (no canonical identity to claim).
_COVENANT_METRIC_IDS = {
    "DSCR": "covenant.dscr",
    "DEBT_EBITDA": "covenant.debt_to_ebitda",
    "CURRENT_RATIO": "covenant.current_ratio",
    "MIN_CASH": "covenant.min_cash",
}


def _handle_get_covenants_status(db: Session, realm_id: str, params: dict) -> str:
    """Live covenant pass/warn/breach status.

    Parity: mirrors GET /covenants/status (app/routers/covenants.py:192-285). Reuses the
    SAME breach math — compute_builtin / compute_coa_formula (app/services/coa_formula.py:34,61)
    for the metric value and _compute_status / _merge_overall_status
    (app/routers/covenants.py:123,141) for pass/warn/breach — so it cannot drift from the UI.
    """
    from ..routers.covenants import _compute_status, _merge_overall_status, _get_date_range
    from ..services.coa_formula import compute_builtin, compute_coa_formula

    covenants = db.query(DebtCovenant).filter(
        DebtCovenant.company_id == realm_id,
        DebtCovenant.active == True,  # noqa: E712
    ).all()
    if not covenants:
        return "No active debt covenants are being tracked for this company."

    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        client = None
        _ = e  # connection failure degrades each covenant to UNKNOWN below (UI parity)

    start_date, end_date = _get_date_range()
    lines = [f"🏦 Covenant Status ({start_date} → {end_date})"]
    overall_status = "PASS"
    rows: list[tuple[str, str]] = []
    status_icons = {"BREACH": "🔴", "WARN": "🟡", "PASS": "🟢"}

    for cov in covenants:
        value: float | None = None
        try:
            if not client:
                value = None
            elif cov.custom_formula:
                formula = json.loads(cov.custom_formula)
                value, _ = compute_coa_formula(formula, client, start_date, end_date)
            else:
                # PLAN-EXEMPT: item-3 add-backs — breach summary uses bank EBITDA too (ratified 2026-06-25)
                value, _ = compute_builtin(
                    cov.metric_type, client, start_date, end_date, db=db, realm_id=client.realm_id,
                    addbacks=json.loads(cov.ebitda_addbacks) if cov.ebitda_addbacks else None,
                )
        except Exception:
            value = None

        status = _compute_status(value, float(cov.threshold), cov.breach_direction)
        overall_status = _merge_overall_status(overall_status, status)
        # W2 typed-fact emission (plan 20260712-agentic-finance-os): built-in covenant
        # values only (a custom_formula has no canonical metric_id). Stock metrics
        # (min-cash, current ratio) are as-of the window end; flow-window metrics
        # (DSCR, leverage) carry the closed-month TTM span _get_date_range computed.
        if value is not None and not cov.custom_formula:
            fact_metric_id = _COVENANT_METRIC_IDS.get((cov.metric_type or "").upper())
            if fact_metric_id:
                from ..semantics import ValueKind, get_metric

                definition = get_metric(fact_metric_id)
                is_stock = definition is not None and definition.value_kind == ValueKind.STOCK
                emit_fact(
                    metric_id=fact_metric_id,
                    value=value,
                    entity_id=realm_id,
                    period_start=date.fromisoformat(end_date if is_stock else start_date),
                    period_end=date.fromisoformat(end_date),
                    period_grain=(
                        PeriodGrain.POINT_IN_TIME if is_stock
                        else PeriodGrain.TTM
                    ),
                    basis=Basis.ACCRUAL,
                    scenario=ScenarioKind.ACTUAL,
                    source_tool="get_covenants_status",
                    as_of=end_date,
                )
        rows.append((cov.name, status))
        icon = status_icons[status] if status in status_icons else "⚪️"
        val_str = f"{value:,.2f}" if value is not None else "n/a"
        lines.append(
            f"  {icon} {cov.name} ({cov.loan_name}): {status} — "
            f"current {val_str} vs threshold {float(cov.threshold):,.2f} ({cov.breach_direction})"
        )

    lines.insert(1, f"Overall: {overall_status} across {len(covenants)} covenant(s).")
    breaches = [n for n, s in rows if s == "BREACH"]
    if breaches:
        lines.append(f"\n⚠️ In breach: {', '.join(breaches)}. Open the Covenants page for the full breakdown.")
    if any(status == "UNKNOWN" for _, status in rows):
        raise ToolFailure("covenants_partial", "\n".join(lines), retryable=True)
    return "\n".join(lines)


def _handle_get_weekly_review(db: Session, realm_id: str, params: dict) -> str:
    """Weekly cash-review summary (current week vs prior 4-week average + anomalies).

    Parity: mirrors GET /weekly-review/summary (app/routers/weekly_review.py:166-258). Reuses
    the SAME helpers — _fetch_transactions_for_period / _week_totals / _detect_anomalies
    (app/routers/weekly_review.py:32,117,124) — for identical inflow/outflow/anomaly math.
    """
    from datetime import date, timedelta
    from ..routers.weekly_review import (
        _fetch_transactions_for_period, _week_totals, _detect_anomalies,
    )

    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        return _read_failure(
            "QuickBooks could not be reached for the weekly review.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_weekly_review_qbo_error",
        )

    today = date.today()
    current_start, current_end = today - timedelta(days=7), today
    prior_start, prior_end = today - timedelta(days=35), today - timedelta(days=8)

    def fetch_complete(start, end):
        # The shared UI helper tolerates failed sources. An agent prerequisite
        # must instead account for every fetched row and every required query.
        import math

        class RequiredSources:
            failed = False
            row_count = 0

            def query(self, query):
                try:
                    response = client.query(query)
                    entity = query.split(" FROM ", 1)[1].split(" ", 1)[0]
                    payload = response["QueryResponse"]
                    if not isinstance(payload, dict) or response.get("Fault"):
                        raise ValueError("Invalid transaction response")
                    batch = payload.get(entity, [])
                    if not isinstance(batch, list):
                        raise ValueError("Invalid transaction rows")
                    for row in batch:
                        if (not isinstance(row, dict) or not row.get("Id")
                                or isinstance(row.get("TotalAmt"), bool)
                                or not math.isfinite(float(row["TotalAmt"]))):
                            raise ValueError("Incomplete transaction amount or identity")
                    self.row_count += len(batch)
                    return response
                except Exception:
                    self.failed = True
                    raise

        source = RequiredSources()
        transactions = _fetch_transactions_for_period(source, start, end)
        if source.failed or len(transactions) != source.row_count:
            raise ToolFailure("weekly_review_incomplete", "Required weekly transaction sources are incomplete; no cash totals were reported.", retryable=True)
        return transactions

    try:
        current_txns = fetch_complete(current_start, current_end)
        prior_txns = fetch_complete(prior_start, prior_end)
    except Exception as e:
        return _read_failure(
            "Weekly QuickBooks transactions could not be loaded.",
            e,
            error_code="assistant_weekly_review_query_failed",
            log_context="assistant_weekly_review_query_error",
        )

    cur = _week_totals(current_txns)
    prior_inflow = sum(t["amount"] for t in prior_txns if t["direction"] == "inflow")
    prior_outflow = sum(t["amount"] for t in prior_txns if t["direction"] == "outflow")
    avg_in, avg_out = prior_inflow / 4, prior_outflow / 4
    anomalies = _detect_anomalies(current_txns, prior_txns)

    def _pct(c, p):
        return f"{((c - p) / p * 100):+.0f}%" if p else "n/a"

    lines = [
        f"🗓️ Weekly Review ({current_start.isoformat()} → {current_end.isoformat()})",
        f"  Inflow:  ${cur['inflow']:,.2f} (vs 4-wk avg ${avg_in:,.2f}, {_pct(cur['inflow'], avg_in)})",
        f"  Outflow: ${cur['outflow']:,.2f} (vs 4-wk avg ${avg_out:,.2f}, {_pct(cur['outflow'], avg_out)})",
        f"  Net:     ${cur['net']:,.2f}",
        f"  Anomalies flagged: {len(anomalies)}",
    ]
    for a in anomalies[:5]:
        sev = str(a["severity"]).upper() if "severity" in a else "WARNING"
        label = a["name"] if "name" in a else (a["description"] if "description" in a else "txn")
        amt = a["amount"] if "amount" in a else 0
        zsc = a["z_score"] if "z_score" in a else "?"
        lines.append(f"    • {sev}: {label} ${amt:,.2f} (z={zsc})")
    if len(anomalies) > 5:
        lines.append(f"    …and {len(anomalies) - 5} more — open the Weekly Review page for the full list.")
    return "\n".join(lines)


def _handle_get_budget_vs_actuals(db: Session, realm_id: str, params: dict) -> str:
    """Live Budget-vs-Actuals variance for one month (live QBO actuals, not just BudgetLine rows).

    Parity: mirrors GET /budget/vs-actuals (app/routers/budget.py:408-...). Reuses the SAME budget
    service helpers the router calls — _parse_pl_single_month_entries / _actual_entries_by_name /
    _actual_entries_by_id (app/budget/qbo_reports.py) and _aggregate_budget_line_amount /
    _aggregate_actual_amount (app/budget/rollups.py) — for identical actual + variance math.
    """
    from collections import defaultdict
    from datetime import date
    from ..budget.constants import MONTHS
    from ..budget.qbo_reports import (
        _parse_pl_single_month_entries, _actual_entries_by_name, _actual_entries_by_id,
    )
    from ..budget.rollups import _aggregate_budget_line_amount, _aggregate_actual_amount

    budget_year = params.get("budget_year") or date.today().year
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 7 (SH-5):
    # accept BOTH forms via the shared normalizer ('mar', 'March', '2026-03', '3');
    # the old bare [:3] slice turned '2026-03' into '202' and errored.
    month_key = _normalize_month_key(str(params.get("month") or MONTHS[date.today().month - 1]))
    if month_key not in MONTHS:
        raise ToolFailure("invalid_request", (f"Error: invalid month '{params.get('month')}'. Use a three-letter key "
            "(jan, feb, …) or YYYY-MM (e.g. 2026-03)."))
    month_idx = MONTHS.index(month_key)

    all_lines = db.query(BudgetLine).filter(
        BudgetLine.company_id == realm_id,
        BudgetLine.budget_year == budget_year,
    ).order_by(BudgetLine.account_type, BudgetLine.sort_order, BudgetLine.account_name).all()
    if not all_lines:
        raise ToolFailure("source_not_ready", (f"No budget found for {month_key.upper()} {budget_year}. Build a budget on the Budget page first."), retryable=False)
    budget_lines = [bl for bl in all_lines if bl.parent_line_id is None]
    children_by_parent: dict[int, list[BudgetLine]] = defaultdict(list)
    for bl in all_lines:
        if bl.parent_line_id is not None:
            children_by_parent[bl.parent_line_id].append(bl)

    actuals_partial = False
    try:
        client = get_qbo_client_from_db(db, realm_id)
        month_num = month_idx + 1
        if month_num in (1, 3, 5, 7, 8, 10, 12):
            last_day = 31
        elif month_num in (4, 6, 9, 11):
            last_day = 30
        else:
            last_day = 29 if (budget_year % 4 == 0 and (budget_year % 100 != 0 or budget_year % 400 == 0)) else 28
        report = client.get_report("ProfitAndLoss", {
            "start_date": date(budget_year, month_num, 1).isoformat(),
            "end_date": date(budget_year, month_num, last_day).isoformat(),
            "accounting_method": "Accrual",
        })
        actual_entries = _parse_pl_single_month_entries(report, month_key=month_key)
        actuals = _actual_entries_by_name(actual_entries)
        actuals_by_id = _actual_entries_by_id(actual_entries)
        actual_names_without_id = {
            e["account_name"] for e in actual_entries if not e.get("qbo_account_id")
        }
    except Exception:
        actuals_partial = True
        actuals, actuals_by_id, actual_names_without_id = {}, {}, set()

    threshold_pct = 10.0
    flagged: list[tuple[str, float, float | None]] = []
    tot_budget = tot_actual = 0.0
    for line in budget_lines:
        b = _aggregate_budget_line_amount(line, month_key, children_by_parent)
        a = _aggregate_actual_amount(
            line, actuals, children_by_parent,
            actuals_by_id=actuals_by_id, actual_names_without_id=actual_names_without_id,
        )
        tot_budget += b
        tot_actual += a
        variance = round(a - b, 2)
        var_pct = round((variance / b * 100), 1) if b else None
        if var_pct is not None and abs(var_pct) > threshold_pct:
            flagged.append((line.account_name, variance, var_pct))

    tot_var = round(tot_actual - tot_budget, 2)
    lines = [
        f"📈 Budget vs Actuals — {month_key.upper()} {budget_year}",
        f"  Budget: ${tot_budget:,.2f}  |  Actual: ${tot_actual:,.2f}  |  Variance: ${tot_var:,.2f}",
        f"  Flagged lines (|var%| > {threshold_pct:.0f}%): {len(flagged)}",
    ]
    if actuals_partial:
        lines.append("  ⚠️ Live actuals from QuickBooks were unavailable — variance reflects budget only.")
    for name, var, vp in sorted(flagged, key=lambda x: abs(x[1]), reverse=True)[:8]:
        lines.append(f"    • {name}: variance ${var:,.2f} ({vp:+.1f}%)")
    if len(flagged) > 8:
        lines.append(f"    …and {len(flagged) - 8} more — open the Budget vs Actuals page for the full grid.")
    if actuals_partial:
        raise ToolFailure("budget_actuals_partial", "\n".join(lines), retryable=True)
    return "\n".join(lines)


def _handle_get_intelligence_snapshot(db: Session, realm_id: str, params: dict) -> str:
    """Consolidated 5-dimension live intelligence snapshot summary.

    Parity: mirrors GET /intelligence/live (app/routers/intelligence.py:644-...). Reuses the SAME
    analysis modules the router runs — profit_and_margin_by_month / customer_revenue_summary /
    vendor_spend_summary / ar_aging / transaction_anomalies — via the router's _safe_run wrapper
    (app/routers/intelligence.py:212), so each dimension and its error-degrade match the UI.
    """
    from ..routers.intelligence import _safe_run, _get_client

    try:
        client = _get_client(realm_id, db)
    except Exception as e:
        return _read_failure(
            "QuickBooks could not be reached for the intelligence snapshot.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_intelligence_qbo_error",
        )

    revenue_trend = _safe_run("revenue_trend", profit_and_margin_by_month, client)
    customer_rev = _safe_run("customer_rev", customer_revenue_summary, client, 500)
    vendor_spend = _safe_run("vendor_spend", vendor_spend_summary, client, 500)
    ar = _safe_run("ar_aging", ar_aging, client, 500)
    anomalies = _safe_run("anomalies", transaction_anomalies, client, 500)

    lines = ["🧭 Intelligence Snapshot (live, 5 dimensions)"]
    unavailable: list[str] = []

    if isinstance(revenue_trend, dict) and "months" in revenue_trend:
        ttm_rev = revenue_trend.get("total_income", 0) or 0
        ttm_gp = revenue_trend.get("total_gross_profit", 0) or 0
        gm = (ttm_gp / ttm_rev * 100) if ttm_rev else 0.0
        lines.append(f"  1) Revenue (TTM): ${ttm_rev:,.0f}  |  Gross margin: {gm:.1f}%")
    else:
        unavailable.append("revenue_trend")

    if isinstance(customer_rev, dict) and customer_rev.get("top_5_customers"):
        top = customer_rev["top_5_customers"][0]
        lines.append(f"  2) Top customer: {top[0]} (${top[1]:,.0f}); {len(customer_rev['top_5_customers'])} in top set")
    elif isinstance(customer_rev, dict) and customer_rev.get("top_5_customers") == []:
        lines.append("  2) No customer revenue in the source window.")
    else:
        unavailable.append("customer_revenue")

    if isinstance(vendor_spend, dict) and vendor_spend.get("top_5_vendors"):
        tv = vendor_spend["top_5_vendors"][0]
        name = tv[0] if isinstance(tv, (list, tuple)) else tv
        amt = tv[1] if isinstance(tv, (list, tuple)) and len(tv) > 1 else None
        lines.append(f"  3) Top vendor: {name}" + (f" (${amt:,.0f})" if amt is not None else " by spend"))
    elif not isinstance(vendor_spend, dict) or "error" in vendor_spend:
        unavailable.append("vendor_spend")

    if isinstance(ar, dict) and "buckets" in ar:
        total_ar = ar.get("total_ar") or sum(v for v in ar["buckets"].values() if isinstance(v, (int, float)))
        over90 = ar["buckets"].get("90+", 0)
        lines.append(f"  4) AR total: ${total_ar:,.0f}  |  90+ days: ${over90:,.0f}")
    else:
        unavailable.append("ar_aging")

    if isinstance(anomalies, dict) and isinstance(anomalies.get("anomalies"), list):
        lines.append(f"  5) Transaction anomalies flagged: {len(anomalies['anomalies'])}")
    elif isinstance(anomalies, list):
        lines.append(f"  5) Transaction anomalies flagged: {len(anomalies)}")
    else:
        unavailable.append("anomalies")

    if unavailable:
        lines.append(f"  ⚠️ Unavailable dimensions: {', '.join(unavailable)} (QBO data gap).")
    lines.append("  Open the Intelligence page for the full breakdown + AI narrative.")
    if unavailable:
        raise ToolFailure("intelligence_partial", "\n".join(lines), retryable=True)
    return "\n".join(lines)


# ── C3-TOOLS wave 3.1 (plan 20260706-cfo-grade-financial-workflows, gap-register rows 1-13) ──
# The internal-DB write handlers below follow the established _handle_* idiom
# (handler(db, realm_id, params) -> str; success iff the string does NOT start with "Error:";
# realm_id is action.company_id, server-written + trusted). Reads recompute-only (no proposal);
# writes are proposal-gated — drafted, never executed, from chat (Control 1/2, AWB SKILL).
# Every WRITE tool here lands WITH a matching AgentEvalFixture carrying a safety_expectation in
# app/agent/evals.py (proposal_only_for_financial_mutation); the scored agent-eval CI floor is the
# durable enforcer that a chat turn can never be injection-driven past the proposal gate (AP-T09).


def _handle_refresh_ar_ap_snapshot(db: Session, realm_id: str, params: dict) -> str:
    """Row 1 (READ, recompute-only): force the live AR/AP recompute + rewrite the read-path
    snapshot. Idempotent — invents no data; writes only the (company, period) summary cache
    that GET /ar-ap/summary already serves. Mirrors receivables_payables.refresh_ar_ap_summary."""
    from ..services.ar_ap_summary import serve_summary

    try:
        payload = serve_summary(db, realm_id, force_live=True)
    except Exception as e:
        return _read_failure(
            "The AR/AP snapshot could not be refreshed from QuickBooks.",
            e,
            error_code="assistant_ar_ap_refresh_unavailable",
            log_context="assistant_ar_ap_refresh_error",
        )
    quality = payload.get("data_quality")
    if quality == "computing" or (isinstance(quality, dict) and quality.get("is_partial")):
        raise ToolFailure("ar_ap_refresh_partial", "AR/AP refresh has unavailable required source data.", retryable=True)
    total_ar = payload.get("total_ar")
    total_ap = payload.get("total_ap")
    return (
        "🔄 AR/AP snapshot refreshed from QuickBooks. "
        f"Total AR: {_money_or_na(total_ar)}  |  Total AP: {_money_or_na(total_ap)}."
    )


def _handle_refresh_cashflow_forecast(db: Session, realm_id: str, params: dict) -> str:
    """Row 2 (READ, recompute-only): force a full QBO rebuild of the 13-week forecast and
    rewrite the serve snapshot (the exact recompute POST /cashflow-forecast/weekly/refresh
    runs). Idempotent — persists only the forecast cache, no user financial fact."""
    from ..routers.cashflow_forecast import _build_forecast_from_qbo, _save_snapshot

    try:
        snapshot_data, bank_balance, _as_of, _src, _diag = _build_forecast_from_qbo(
            realm_id, 13, 1, db
        )
        _require_complete_cashflow_source(snapshot_data)
        _save_snapshot(db, realm_id, snapshot_data, bank_balance)
    except Exception as e:
        return _read_failure(
            "The cash-flow forecast could not be refreshed from QuickBooks.",
            e,
            error_code="assistant_cashflow_refresh_unavailable",
            log_context="assistant_cashflow_refresh_error",
        )
    return (
        "🔄 13-week cash-flow forecast rebuilt from QuickBooks and re-snapshotted. "
        "Open the Cash Flow Forecast page for the refreshed grid."
    )


def _handle_refresh_covenant_status(db: Session, realm_id: str, params: dict) -> str:
    """Row 3 (READ, recompute-only): force the live two-lens covenant recompute and rewrite
    each (covenant, period) snapshot. Snapshot-append only — no user data. Mirrors
    covenants.refresh_covenant_status; the optional as_of selects a closed month."""
    from ..routers.covenants import _covenant_status_payload

    as_of = str(params.get("as_of") or "").strip() or None
    try:
        payload = _covenant_status_payload(db, realm_id, as_of, force_live=True)
    except Exception as e:
        return _read_failure(
            "Covenant status could not be refreshed from QuickBooks.",
            e,
            error_code="assistant_covenant_refresh_unavailable",
            log_context="assistant_covenant_refresh_error",
        )
    overall = payload.get("overall_status") or payload.get("overall") or "refreshed"
    if overall == "UNKNOWN" or any(row.get("status") == "UNKNOWN" for row in payload.get("covenants", [])):
        raise ToolFailure("covenants_partial", f"Covenant status is unavailable. Overall: {overall}.", retryable=True)
    return f"🔄 Covenant status recomputed from QuickBooks. Overall: {overall}."


def _handle_create_budget_line(db: Session, realm_id: str, params: dict) -> str:
    """Row 4 (WRITE, proposal-gated at approval): create a new budget line. Reproduces the
    budget.create_line ORM write + its no-duplicate guard (there is no DB uniqueness
    constraint — a duplicate parent double-renders the grid)."""
    account_name = str(params.get("account_name") or "").strip()
    account_type = str(params.get("account_type") or "").strip().lower()
    if not account_name:
        return "Error: account_name is required."
    if account_type not in {"revenue", "cogs", "opex"}:
        return "Error: account_type must be one of revenue, cogs, opex."
    budget_year = params.get("budget_year") or datetime.utcnow().year
    scenario = str(params.get("scenario") or "base").strip() or "base"
    monthly = params.get("monthly_amounts")
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 7 (SH-5), the
    # corruption-path close: a raw dict here went STRAIGHT into
    # BudgetLine.monthly_amounts, so a model-supplied YYYY-MM key ('2026-01')
    # persisted verbatim and the budget grid (which reads jan..dec) silently
    # dropped the month. Normalize every key at ingestion; refuse unrecognized
    # keys and non-numeric values instead of storing them.
    monthly_amounts: dict = {}
    if isinstance(monthly, dict):
        for period, amount in monthly.items():  # SA-OK: bounded-loop over the ≤12-key model-supplied month dict, no query inside
            month_key = _normalize_month_key(str(period))
            if month_key not in _VALID_MONTH_KEYS:
                return (
                    f"Error: unrecognized month key '{period}' in monthly_amounts. Use "
                    "three-letter keys (jan..dec); YYYY-MM keys are also accepted."
                )
            try:
                monthly_amounts[month_key] = round(float(amount), 2)
            except (TypeError, ValueError):
                return f"Error: monthly_amounts['{period}'] must be a number."

    dup = (
        db.query(BudgetLine.id)
        .filter(
            BudgetLine.company_id == realm_id,
            BudgetLine.budget_year == budget_year,
            BudgetLine.account_name == account_name,
            BudgetLine.scenario == scenario,
            BudgetLine.parent_line_id.is_(None),
        )
        .first()
    )
    if dup is not None:
        return (
            f"Error: a budget line for '{account_name}' already exists for {budget_year} "
            f"(scenario '{scenario}')."
        )
    line = BudgetLine(
        company_id=realm_id,
        budget_year=budget_year,
        account_name=account_name,
        account_type=account_type,
        scenario=scenario,
        monthly_amounts=monthly_amounts,
        notes=str(params.get("notes") or "") or None,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return f"✅ Budget line created: '{account_name}' ({account_type}) for {budget_year} [{scenario}]."


def _handle_create_custom_kpi(db: Session, realm_id: str, params: dict) -> str:
    """Row 5a (WRITE, proposal-gated): create a custom KPI. formula_json is a structured dict
    the KPI engine evaluates — NEVER an arbitrary eval string (no code execution)."""
    name = str(params.get("name") or "").strip()
    if not name:
        return "Error: name is required."
    formula_json = params.get("formula_json")
    if not isinstance(formula_json, dict) or not formula_json:
        return "Error: formula_json must be a non-empty object describing the KPI formula."
    kpi = CustomKPI(
        company_id=realm_id,
        name=name,
        description=str(params.get("description") or "") or None,
        formula_json=formula_json,
        unit=str(params.get("unit") or "USD"),
    )
    db.add(kpi)
    db.commit()
    db.refresh(kpi)
    return f"✅ Custom KPI created: '{name}' ({kpi.unit})."


def _handle_update_custom_kpi(db: Session, realm_id: str, params: dict) -> str:
    """Row 5b (WRITE, proposal-gated): update a custom KPI's editable fields. Realm-scoped
    lookup (a KPI from another tenant is not found). formula_json stays a structured dict."""
    kpi_id = params.get("kpi_id")
    if not kpi_id:
        return "Error: kpi_id is required."
    kpi = (
        db.query(CustomKPI)
        .filter(CustomKPI.id == kpi_id, CustomKPI.company_id == realm_id)
        .first()
    )
    if kpi is None:
        return f"Error: custom KPI {kpi_id} not found for this company."
    changed = []
    if params.get("name") is not None:
        kpi.name = str(params["name"]).strip()
        changed.append("name")
    if params.get("description") is not None:
        kpi.description = str(params["description"]) or None
        changed.append("description")
    if params.get("unit") is not None:
        kpi.unit = str(params["unit"])
        changed.append("unit")
    if params.get("formula_json") is not None:
        if not isinstance(params["formula_json"], dict) or not params["formula_json"]:
            return "Error: formula_json must be a non-empty object."
        kpi.formula_json = params["formula_json"]
        changed.append("formula")
    if not changed:
        return "Error: no updatable fields provided (name, description, unit, or formula_json)."
    db.commit()
    db.refresh(kpi)
    return f"✅ Custom KPI '{kpi.name}' updated ({', '.join(changed)})."


def _handle_update_close_task_state(db: Session, realm_id: str, params: dict) -> str:
    """Row 6 (WRITE, proposal-gated): move a close task through its state machine
    (skip/reopen/assign) — the reversible internal rows close_tasks skip/reopen/assign write.
    Legal states only; realm-scoped lookup (cross-tenant = not found)."""
    task_id = params.get("task_id")
    action = str(params.get("action") or "").strip().lower()
    if not task_id:
        return "Error: task_id is required."
    if action not in {"skip", "reopen", "assign"}:
        return "Error: action must be one of skip, reopen, assign."
    task = (
        db.query(CloseTask)
        .filter(CloseTask.id == task_id, CloseTask.realm_id == realm_id)
        .first()
    )
    if task is None:
        return f"Error: close task {task_id} not found for this company."

    if action == "skip":
        task.status = "SKIPPED"
        reason = str(params.get("reason") or "").strip()
        if reason:
            task.description = (task.description or "") + f"\n\nSkip reason: {reason}"
    elif action == "reopen":
        task.status = "OPEN"
        if hasattr(task, "completed_by"):
            task.completed_by = None
        if hasattr(task, "completed_at"):
            task.completed_at = None
    else:  # assign
        assigned_to = str(params.get("assigned_to") or "").strip()
        if not assigned_to or not hasattr(task, "assigned_to"):
            return "Error: assign requires assigned_to."
        task.assigned_to = assigned_to
        priority = str(params.get("priority") or "").strip().lower()
        if priority:
            if priority not in {"low", "normal", "high", "critical"}:
                return "Error: priority must be low, normal, high, or critical."
            if hasattr(task, "priority"):
                task.priority = priority
    db.commit()
    db.refresh(task)
    return f"✅ Close task '{task.title}' → {task.status} ({action})."


def _handle_dismiss_alert(db: Session, realm_id: str, params: dict) -> str:
    """Row 10a (WRITE, proposal-gated): dismiss (or un-dismiss) ONE anomaly alert by id.
    Single-id only — the model can never bulk-silence a company's alerts (never-add set: no
    /all or dismiss_all path)."""
    alert_id = params.get("alert_id")
    if not alert_id:
        return "Error: alert_id is required (single-alert dismissal only)."
    dismissed = params.get("dismissed")
    dismissed = True if dismissed is None else bool(dismissed)
    alert = (
        db.query(InsightAnomaly)
        .filter(InsightAnomaly.id == alert_id, InsightAnomaly.company_id == realm_id)
        .first()
    )
    if alert is None:
        return f"Error: alert {alert_id} not found for this company."
    alert.dismissed = dismissed
    alert.dismissed_at = datetime.utcnow() if dismissed else None
    db.commit()
    verb = "dismissed" if dismissed else "restored"
    return f"✅ Alert {alert_id} {verb}."


def _handle_acknowledge_watchdog(db: Session, realm_id: str, params: dict) -> str:
    """Row 10b (WRITE, proposal-gated): acknowledge ONE watchdog alert by id. Single-id only —
    no bulk-acknowledge path is reachable from chat (never-add set)."""
    from ..models import WatchdogAlert

    alert_id = params.get("alert_id")
    if not alert_id:
        return "Error: alert_id is required (single-alert acknowledgement only)."
    alert = (
        db.query(WatchdogAlert)
        .filter(WatchdogAlert.id == alert_id, WatchdogAlert.company_id == realm_id)
        .first()
    )
    if alert is None:
        return f"Error: watchdog alert {alert_id} not found for this company."
    alert.acknowledged = True
    alert.acknowledged_by = str(params.get("acknowledged_by") or "").strip() or "AI Assistant"
    alert.acknowledged_at = datetime.utcnow()
    db.commit()
    return f"✅ Watchdog alert {alert_id} acknowledged by {alert.acknowledged_by}."


def _handle_recon_confirm_match(db: Session, realm_id: str, params: dict) -> str:
    """Row 12a (WRITE, proposal-gated): confirm one proposed bank-rec pairing. Delegates to
    the match service (the exact bridge the human-gated POST /sessions/{id}/confirm calls);
    the match must belong to the session + realm.

    AC-6 (plan 20260713-wave2-3-bank-recon, decision 8 / E7): this handler executes at
    APPROVAL time (RECON_MATCH_CONFIRM → this handler), so it re-validates liveness with
    the SAME indexed checks + skip vocabulary as the bulk executor
    (``recon_bulk_confirm_exec``) before setting any flag — it previously set flags
    unconditionally, so approving a stale per-pair proposal could steal a clear."""
    from ..models import ReconciliationSession
    from ..services.bank_recon import agent_ops
    from ..services.bank_recon import match as match_service

    session_id = params.get("session_id")
    bank_item_id = params.get("bank_item_id")
    book_txn_id = params.get("book_txn_id")
    if not session_id or not bank_item_id or not book_txn_id:
        return "Error: session_id, bank_item_id, and book_txn_id are all required."
    session = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.id == session_id,
            ReconciliationSession.realm_id == realm_id,
        )
        .first()
    )
    if session is None:
        return f"Error: reconciliation session {session_id} not found for this company."
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A / AC-6 (marker
    # RATIFICATION-2026-07-13.md on disk) — execution-time steal-guard re-validation,
    # one vocabulary with the bulk lane. Covered by the safety_expectation
    # AgentEvalFixture recon_confirm_match_proposal (app/agent/evals.py) behaviorally.
    if not is_open_session_status(session.status):
        return (
            f"Error: reconciliation session {session_id} is "
            f"{effective_session_status(session.status)}, not OPEN — "
            f"the pair was skipped ({agent_ops.SKIP_SESSION_CLOSED}); nothing was confirmed."
        )
    try:
        bank_id = int(bank_item_id)
        book_id = int(book_txn_id)
    except (TypeError, ValueError):
        return "Error: bank_item_id and book_txn_id must be integer ids."
    liveness = agent_ops.check_bulk_pairs_liveness(
        db, realm_id=realm_id, session=session, pairs=[(bank_id, book_id)]
    )
    if liveness.skipped_stale:
        return (
            f"Error: the drafted pair (bank {bank_id} ↔ book {book_id}) is no longer live — "
            f"the bank or book row no longer exists ({agent_ops.SKIP_STALE}); nothing was confirmed."
        )
    if liveness.skipped_cleared_elsewhere:
        return (
            f"Error: the drafted pair (bank {bank_id} ↔ book {book_id}) is no longer live — "
            f"the book transaction is already cleared or the bank line is already matched "
            f"({agent_ops.SKIP_CLEARED_ELSEWHERE}); nothing was confirmed."
        )
    bank_item_id = bank_id
    book_txn_id = book_id
    try:
        match_service.confirm_match(
            db,
            realm_id=realm_id,
            session=session,
            bank_item_id=bank_item_id,
            book_txn_id=book_txn_id,
        )
        db.commit()
    except match_service.ConfirmationSessionNotOpenError:
        return (
            f"Error: reconciliation session {session_id} is no longer OPEN — "
            f"the pair was skipped ({agent_ops.SKIP_SESSION_CLOSED}); nothing was confirmed."
        )
    except (
        match_service.ConfirmationSessionNotFoundError,
        match_service.StatementLineNotInSessionError,
        match_service.BookTransactionNotInSessionError,
        match_service.MatchOutsideSessionWindowError,
    ):
        return (
            f"Error: the drafted pair (bank {bank_id} ↔ book {book_id}) is no longer "
            f"eligible for this session ({agent_ops.SKIP_STALE}); nothing was confirmed."
        )
    except match_service.CleanStartNotConfiguredError:
        return (
            "Error: this account no longer has a clean-start date; the pair was "
            f"skipped ({agent_ops.SKIP_STALE}) and nothing was confirmed."
        )
    except (
        match_service.BookTransactionClearedError,
        match_service.MatchClaimConflictError,
    ):
        return (
            f"Error: the drafted pair (bank {bank_id} ↔ book {book_id}) was claimed "
            f"before approval ({agent_ops.SKIP_CLEARED_ELSEWHERE}); nothing was confirmed."
        )
    except Exception as exc:
        if getattr(exc, "detail", None):
            return f"Error: {exc.detail}"
        return f"Error: could not confirm the match ({redact_to_string(str(exc), max_length=200)})."
    return f"✅ Reconciliation match confirmed in session {session_id}."


def _handle_recon_unconfirm(db: Session, realm_id: str, params: dict) -> str:
    """Row 12b (WRITE, proposal-gated): reverse a bank-rec confirmation. Delegates to the
    match service (POST /sessions/{id}/unconfirm); the match must belong to session + realm.

    OPEN-only, mirroring _handle_recon_confirm_match and the HTTP routes (plan
    20260715-single-confirm-open-guard): unconfirming on a COMPLETE session desyncs its
    stamped cleared_balance, and on an ABANDONED session the clears are already released —
    a completed reconciliation is corrected by reopening it, not by unconfirming pairs on
    a still-COMPLETE one."""
    from ..models import ReconciliationSession
    from ..services.bank_recon import match as match_service

    session_id = params.get("session_id")
    bank_item_id = params.get("bank_item_id")
    book_txn_id = params.get("book_txn_id")
    if not session_id or not bank_item_id or not book_txn_id:
        return "Error: session_id, bank_item_id, and book_txn_id are all required."
    session = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.id == session_id,
            ReconciliationSession.realm_id == realm_id,
        )
        .first()
    )
    if session is None:
        return f"Error: reconciliation session {session_id} not found for this company."
    if not is_open_session_status(session.status):
        return (
            f"Error: reconciliation session {session_id} is "
            f"{effective_session_status(session.status)}, not OPEN — "
            "reopen it first to change a confirmation; nothing was unconfirmed."
        )
    try:
        match_service.unconfirm_match(
            db,
            realm_id=realm_id,
            session=session,
            bank_item_id=bank_item_id,
            book_txn_id=book_txn_id,
        )
        db.commit()
    except Exception as exc:
        if getattr(exc, "detail", None):
            return f"Error: {exc.detail}"
        return f"Error: could not unconfirm the match ({redact_to_string(str(exc), max_length=200)})."
    return f"✅ Reconciliation match reversed in session {session_id}."


# ─────────────────────────────────────────────────────────────────────────────
# Wave 2.3 — agent-run bank reconciliation (plan 20260713-wave2-3-bank-recon,
# chunk A; marker RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar
# absent in this dispatched session — standing precedent).
#
# TWO-NAME ARCHITECTURE (decision 3, the E1 blocking fix): the VISIBLE tools
# (open_recon_session / propose_bulk_matches) are HANDLER_LANE_SELF_DRAFTERS —
# their handlers run at CHAT time, surface the fail-closed guards / run the
# suggest engine THEN, and draft ONE PENDING ProposedAction whose
# payload["tool_name"] is the paired HIDDEN execution-only tool
# (recon_session_open_exec / recon_bulk_confirm_exec — in no bundle, never
# offered to the model). The hidden handlers are the approval-time executors
# with guard re-checks + the E5 steal-guard liveness contract. Paired
# safety_expectation AgentEvalFixtures open_recon_session_proposal /
# propose_bulk_matches_proposal / recon_session_open_exec_fixture /
# recon_bulk_confirm_exec_fixture live in app/agent/evals.py (AP-T09 lockstep).
# ─────────────────────────────────────────────────────────────────────────────


def _handle_open_recon_session(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """Wave-2.3 visible SELF-DRAFTER (decisions 3/4): runs at CHAT time, surfaces the
    three fail-closed open guards honestly at DRAFT via agent_ops.check_session_openable
    (SAME primitives + SAME exception classes as the service — drift-pinned by tests),
    and drafts ONE PENDING RECON_SESSION_OPEN ProposedAction executed by the hidden
    recon_session_open_exec handler at approval. A doomed draft is refused with ZERO
    proposals. safety_expectation AgentEvalFixture: open_recon_session_proposal."""
    from decimal import Decimal, InvalidOperation

    from ..services.bank_recon import agent_ops
    from ..services.bank_recon import session as session_service
    from ..services.bank_recon.match import CleanStartNotConfiguredError
    from ..services.bank_recon.session import OpenSessionExistsError, PeriodAlreadyReconciledError

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # open_recon_session_proposal unchanged; dry exit after the open guards below).
    dry_run = _dry_run_requested(params)
    statement_import_id = params.get("statement_import_id")
    if (
        isinstance(statement_import_id, bool)
        or not isinstance(statement_import_id, int)
        or statement_import_id < 1
    ):
        return (
            "Error: statement_import_id is required and must identify the exact "
            "controlled parsed statement import."
        )
    account_id = str(params.get("account_id") or "").strip()
    if not account_id:
        return "Error: account_id is required (resolve it with search_chart_of_accounts)."
    account_name = str(params.get("account_name") or "").strip() or account_id
    try:
        period_start = date.fromisoformat(str(params.get("period_start") or ""))
        period_end = date.fromisoformat(str(params.get("period_end") or ""))
    except ValueError:
        return "Error: period_start and period_end must be YYYY-MM-DD dates."
    if period_end < period_start:
        return "Error: period_end must be on or after period_start."
    if params.get("ending_balance") is None:
        return "Error: ending_balance (the statement closing balance) is required."
    try:
        ending_balance = Decimal(str(params.get("ending_balance")))
    except InvalidOperation:
        return "Error: ending_balance must be a number."

    try:
        session_service.validate_statement_import_for_session(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
            ending_balance=ending_balance,
        )
    except session_service.StatementImportNotFoundError:
        return (
            "Error: that statement import was not found for this company. Nothing "
            "was drafted."
        )
    except session_service.StatementImportNotEligibleError:
        return (
            "Error: that statement import is not a controlled parsed statement with "
            "a passed certificate. Nothing was drafted."
        )
    except session_service.StatementImportMismatchError as exc:
        return (
            "Error: the selected statement import does not match the requested "
            f"{exc.field}. Nothing was drafted."
        )
    except session_service.StatementImportProjectionError:
        return (
            "Error: the selected statement import has incomplete or inconsistent "
            "controlled rows. Nothing was drafted."
        )

    try:
        agent_ops.check_session_openable(
            db,
            realm_id=realm_id,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
        )
    except OpenSessionExistsError as exc:
        return (
            f"An OPEN reconciliation session (#{exc.existing_session_id}) already exists for "
            f"account {account_name}. Nothing was drafted — resume that session (use its "
            f"session_id with list_unmatched_items / propose_bulk_matches / get_recon_tieout) "
            "or abandon it in the reconciliation UI first."
        )
    except PeriodAlreadyReconciledError as exc:
        return (
            f"Account {account_name} is already reconciled through "
            f"{exc.anchor_date.isoformat()}. Nothing was drafted — reopen the prior "
            "reconciliation instead of opening a new session for the same period."
        )
    except CleanStartNotConfiguredError:
        return (
            f"Account {account_name} has no clean-start anchor configured, so a reconciliation "
            "cannot start from a known baseline. Nothing was drafted — set the account's clean "
            "start (last reconciled balance + date) in the reconciliation UI first."
        )

    tool_input = {
        "statement_import_id": statement_import_id,
        "account_id": account_id,
        "account_name": account_name,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "ending_balance": str(ending_balance),
    }
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit
    # (post-guards, pre-draft; safety_expectation AgentEvalFixture unchanged).
    if dry_run:
        return _dry_run_notice(
            f"a RECON_SESSION_OPEN proposal for {account_name}, statement period "
            f"{period_start.isoformat()} – {period_end.isoformat()}, ending balance "
            f"${ending_balance:,.2f} (the open guards passed at preview time and are "
            "re-checked at draft and at execution)."
        )
    action = ProposedAction(
        company_id=realm_id,
        action_type="RECON_SESSION_OPEN",
        payload={
            "tool_name": "recon_session_open_exec",
            "summary": (
                f"Open bank-reconciliation session for {account_name} "
                f"({period_start.isoformat()} – {period_end.isoformat()})"
            ),
            "execution_mode": "internal_db",
            "approval_effect": (
                "Approving APPLIES this change to Peregrine's database (the reconciliation "
                "session opens); rejecting discards it."
            ),
            "tool_input": tool_input,
            "affected_entity": account_name,
            "before_state": {
                "status": "not_loaded",
                "note": "Reviewer should inspect the account's reconciliation state before approval.",
            },
            "after_state": tool_input,
            "source_data": {"source": "ai_assistant_chat", "tool_name": "open_recon_session"},
            "required_role": "EDITOR",
        },
        reasoning=(
            "Drafted by Peregrine assistant: open (or resume) a bank-reconciliation session. "
            "The open guards were checked at draft time and are re-checked at execution."
        ),
        confidence_score=0.85,
        status="PENDING",
        source="ai_assistant",
        category="proposal",
        priority="medium",
    )
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker on disk) —
    # pin realm GUC before the FORCE-RLS proposed_actions INSERT (the JE-handler precedent).
    _pin_realm_write_scope(db, realm_id)
    db.add(action)
    db.commit()
    db.refresh(action)
    return (
        DraftedToolResult((f"✅ Reconciliation session drafted (Action #{action.id}) for {account_name}, "
        f"statement period {period_start.isoformat()} – {period_end.isoformat()}, ending "
        f"balance ${ending_balance:,.2f}. Nothing opens from chat — the user must approve "
        "the proposal in the Actions page, and execution re-checks the open guards."), proposal_id=action.id)
    )


def _handle_recon_session_open_exec(db: Session, realm_id: str, params: dict) -> str:
    """Wave-2.3 HIDDEN executor (decisions 3/4, E4): runs ONLY at approval time
    (RECON_SESSION_OPEN → this handler via ACTION_TYPE_TO_TOOL); never offered to the
    model (no bundle + MODEL_HIDDEN_TOOLS). Re-checks the guards via create_session
    itself. E4 split: a SAME-period concurrent human open resumes idempotently (honest
    'resumed' message, no second row); a DIFFERENT-period OPEN session surfaces as an
    honest execution failure. safety_expectation AgentEvalFixture:
    recon_session_open_exec_fixture."""
    from decimal import Decimal, InvalidOperation

    from ..models import ReconciliationSession
    from ..services.bank_recon import session as session_service

    statement_import_id = params.get("statement_import_id")
    if (
        isinstance(statement_import_id, bool)
        or not isinstance(statement_import_id, int)
        or statement_import_id < 1
    ):
        return (
            "Error: statement_import_id is required and must identify the exact "
            "controlled parsed statement import."
        )
    account_id = str(params.get("account_id") or "").strip()
    if not account_id:
        return "Error: account_id is required."
    account_name = str(params.get("account_name") or "").strip() or account_id
    try:
        period_start = date.fromisoformat(str(params.get("period_start") or ""))
        period_end = date.fromisoformat(str(params.get("period_end") or ""))
    except ValueError:
        return "Error: period_start and period_end must be YYYY-MM-DD dates."
    if params.get("ending_balance") is None:
        return "Error: ending_balance is required."
    try:
        ending_balance = Decimal(str(params.get("ending_balance")))
    except InvalidOperation:
        return "Error: ending_balance must be a number."

    # SAME-primitive pre-read as create_session's idempotent same-period check, so an
    # idempotent return is reported honestly as a RESUME (E4), never as a fresh open.
    pre_existing = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.realm_id == realm_id,
            ReconciliationSession.account_id == account_id,
            ReconciliationSession.period_start == period_start,
            ReconciliationSession.period_end == period_end,
            live_session_status_clause(ReconciliationSession.status),
        )
        .first()
    )
    try:
        session = session_service.create_session(
            db,
            realm_id=realm_id,
            statement_import_id=statement_import_id,
            account_id=account_id,
            account_name=account_name,
            period_start=period_start,
            period_end=period_end,
            ending_balance=ending_balance,
        )
    except session_service.StatementImportNotFoundError:
        return "Error: that statement import was not found for this company."
    except session_service.StatementImportNotEligibleError:
        return (
            "Error: that statement import is not a controlled parsed statement with "
            "a passed certificate."
        )
    except session_service.StatementImportMismatchError as exc:
        return (
            "Error: the selected statement import does not match the requested "
            f"{exc.field}."
        )
    except session_service.StatementImportProjectionError:
        return (
            "Error: the selected statement import has incomplete or inconsistent "
            "controlled rows."
        )
    except session_service.PeriodAlreadyReconciledError as exc:
        # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon A (marker
        # RATIFICATION-2026-07-13.md) — orchestrator wave-gate fix: the raw-exception
        # static guard forbids f-string exception returns; str(exc) is OUR service's
        # crafted refusal (user-safe by construction), preserved via the house wrapper.
        return _assistant_safe_error(
            str(exc),
            exc,
            error_code="recon_period_already_reconciled",
            retryable=False,
            log_context="assistant_recon_session_open_refused",
        )
    except session_service.OpenSessionExistsError as exc:
        # create_session's same-period idempotent check ran FIRST, so this OPEN session
        # is for a DIFFERENT statement period — an honest execution failure (E4).
        return (
            f"Error: an OPEN reconciliation session (#{exc.existing_session_id}) already "
            "exists for this account for a different statement period — resume or abandon "
            "it before opening this one."
        )
    db.commit()
    if pre_existing is not None and pre_existing.id == session.id:
        return (
            f"✅ Resumed reconciliation session #{session.id} "
            f"({effective_session_status(session.status)}) for "
            f"{account_name} {period_start.isoformat()} – {period_end.isoformat()} — a "
            "session for this statement period already existed; nothing new was created."
        )
    return (
        f"✅ Reconciliation session #{session.id} opened for {account_name} "
        f"({period_start.isoformat()} – {period_end.isoformat()}), statement ending "
        f"balance ${ending_balance:,.2f}."
    )


def _handle_propose_bulk_matches(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """Visible SELF-DRAFTER: propose one explicit bounded suggestion page.

    The signed cursor is bound to this session's realm/import/lineage and complete
    unmatched-set hashes. No page is followed automatically. Every paid attempt
    has a write-ahead spend claim committed before its one permitted provider
    call; the returned pair list is still only a PENDING proposal and is
    revalidated by the hidden executor.
    safety_expectation AgentEvalFixture: propose_bulk_matches_proposal.
    """
    from ..models import ReconciliationSession
    from ..services.bank_recon import agent_ops, llm_suggest
    from ..services.bank_recon import match as match_service

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # propose_bulk_matches_proposal unchanged; dry exit after the pair build below).
    dry_run = _dry_run_requested(params)
    session_id = params.get("session_id")
    if not session_id:
        return "Error: session_id is required (open a session first with open_recon_session)."
    cursor = params.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
        return "Error: cursor must be the opaque non-empty string from a prior suggestion call."
    try:
        max_pairs = min(
            int(params.get("max_pairs") or agent_ops.MAX_BULK_PAIRS),
            agent_ops.MAX_BULK_PAIRS,
        )
    except (TypeError, ValueError):
        return "Error: max_pairs must be an integer."
    max_pairs = max(1, max_pairs)
    session = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.id == session_id,
            ReconciliationSession.realm_id == realm_id,
        )
        .first()
    )
    if session is None:
        return f"Error: reconciliation session {session_id} not found for this company."
    if not is_open_session_status(session.status):
        return (
            f"Error: reconciliation session {session_id} is "
            f"{effective_session_status(session.status)}, not OPEN — "
            "nothing was drafted."
        )
    try:
        result = match_service.find_matches(db, realm_id=realm_id, session=session)
    except match_service.CleanStartNotConfiguredError:
        return (
            "Error: this account has no clean-start anchor configured — set the clean start "
            "before matching. Nothing was drafted."
        )
    except match_service.StatementMembershipInvalidError:
        return (
            "Error: this reconciliation's frozen statement rows are incomplete or "
            "inconsistent. Review the controlled import before matching. Nothing was drafted."
        )

    scope = llm_suggest.SuggestionScope(
        realm_id=realm_id,
        session_id=session.id,
        statement_import_id=session.statement_import_id,
        lineage_status=session.lineage_status,
    )
    try:
        prepared = llm_suggest.prepare_suggestion_batch(
            db,
            scope=scope,
            bank_rows=result.in_bank_not_books,
            book_rows=result.in_books_not_bank,
            cursor=cursor,
            user_id=None,
        )
        if prepared.spend_claim_id is not None:
            if dry_run:
                db.rollback()
                return _dry_run_notice(
                    "a durable suggestion spend claim followed by one bounded "
                    f"provider call for reconciliation session {session.id}, page "
                    f"{prepared.state.page_index + 1} of {prepared.state.page_count}"
                )
            # Every initial/retry claim must survive a provider/process failure.
            db.commit()
        suggest = llm_suggest.execute_suggestion_batch(db, prepared)
    except llm_suggest.SuggestionCursorError:
        return (
            "Error: that suggestion cursor is invalid, stale, or belongs to another "
            "reconciliation. Restart suggestions from the first page. Nothing was drafted."
        )
    except llm_suggest.SuggestionRetryRefused:
        return (
            "Error: that suggestion retry is unavailable or was already used. Restart "
            "suggestions from the first page. Nothing was drafted."
        )
    except llm_suggest.SuggestionInputBudgetError:
        return (
            "Error: this suggestion page cannot fit the bounded provider input. Review "
            "the unmatched items manually. Nothing was drafted."
        )
    except llm_suggest.SuggestionRetryClaimNotCommitted:
        return (
            "Error: the suggestion attempt could not be secured before provider spend. "
            "Restart suggestions from the first page. Nothing was drafted."
        )

    page_note = f" Page {suggest.page_number} of {suggest.planned_pages}."
    cursor_notes: list[str] = []
    if suggest.retry_cursor is not None:
        cursor_notes.append(
            "Single-use retry cursor: "
            f"{json.dumps(suggest.retry_cursor)}. Submit it only in a new explicit "
            "propose_bulk_matches call."
        )
    if suggest.next_cursor is not None:
        cursor_notes.append(
            "Next-page cursor: "
            f"{json.dumps(suggest.next_cursor)}. Submit it in a new explicit "
            "propose_bulk_matches call to inspect the next bounded page."
        )
    if cursor_notes:
        cursor_notes.append("No retry or next page runs automatically.")
    cursor_note = " " + " ".join(cursor_notes) if cursor_notes else ""

    if not suggest.suggestions:
        if suggest.failure_code is not None:
            failure_message = (
                "The suggestion provider failed for this page"
                if suggest.failure_code == "provider_error"
                else "The suggestion provider returned an invalid response for this page"
            )
            return (
                f"Error: {failure_message}; nothing was drafted."
                f"{page_note}{cursor_note}"
            )
        return (
            f"No confident bulk-match suggestions were found on this page for session "
            f"{session.id} — "
            f"nothing was drafted. {len(result.in_bank_not_books)} bank line(s) and "
            f"{len(result.in_books_not_bank)} book transaction(s) remain unmatched; review "
            f"them with list_unmatched_items.{page_note}{cursor_note}"
        )
    seen_bank: set = set()
    seen_book: set = set()
    pairs: list = []
    display: list = []
    for s in suggest.suggestions:  # SA-OK: bounded-loop over in-memory suggestion DTOs (≤40/side), no query inside
        if s.bank_item_id in seen_bank or s.book_txn_id in seen_book:
            continue  # one clear per bank line and per book txn — mirrors the match engine's at-most-once rule
        seen_bank.add(s.bank_item_id)
        seen_book.add(s.book_txn_id)
        pairs.append({"bank_item_id": s.bank_item_id, "book_txn_id": s.book_txn_id})
        display.append(
            {
                "bank_item_id": s.bank_item_id,
                "book_txn_id": s.book_txn_id,
                "amount": str(s.amount) if s.amount is not None else None,
                "confidence": s.confidence,
                "reason": s.reason,
            }
        )
        if len(pairs) >= max_pairs:
            break

    tool_input = {"session_id": session.id, "pairs": pairs}
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit
    # (post-suggestion, pre-draft; safety_expectation AgentEvalFixture unchanged).
    if dry_run:
        return _dry_run_notice(
            f"a RECON_BULK_CONFIRM proposal with {len(pairs)} suggested pairing(s) for "
            f"reconciliation session {session.id} ({session.account_name}), page "
            f"{suggest.page_number} of {suggest.planned_pages}; each pair "
            "would be re-validated at approval time."
        )
    action = ProposedAction(
        company_id=realm_id,
        action_type="RECON_BULK_CONFIRM",
        payload={
            "tool_name": "recon_bulk_confirm_exec",
            "summary": (
                f"Bulk-confirm {len(pairs)} suggested bank-reconciliation pairing(s) in "
                f"session {session.id} ({session.account_name})"
            ),
            "execution_mode": "internal_db",
            "approval_effect": (
                "Approving confirms the still-live pairs (each is re-validated at execution; "
                "stale pairs are skipped, never force-confirmed); rejecting discards them all."
            ),
            "tool_input": tool_input,
            "affected_entity": f"reconciliation session {session.id}",
            "before_state": {
                "status": "not_loaded",
                "note": "Reviewer should inspect the session's unmatched items before approval.",
            },
            "after_state": {
                "pairs": display,
                "truncated": suggest.truncated,
                "page_number": suggest.page_number,
                "planned_pages": suggest.planned_pages,
                "has_more": suggest.has_more,
                "next_cursor": suggest.next_cursor,
                "retry_cursor": suggest.retry_cursor,
                "failure_code": suggest.failure_code,
            },
            "source_data": {"source": "ai_assistant_chat", "tool_name": "propose_bulk_matches"},
            "required_role": "EDITOR",
        },
        reasoning=(
            "LLM-suggested pairings over the session's unmatched buckets (ids validated "
            "against the real unmatched sets; the model cannot invent ids). Every pair is "
            "re-validated at approval time."
        ),
        confidence_score=0.8,
        status="PENDING",
        source="ai_assistant",
        category="proposal",
        priority="medium",
    )
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker on disk) —
    # pin realm GUC before the FORCE-RLS proposed_actions INSERT (the JE-handler precedent).
    _pin_realm_write_scope(db, realm_id)
    db.add(action)
    db.commit()
    db.refresh(action)
    return (
        DraftedToolResult((f"✅ Bulk match proposal drafted (Action #{action.id}): {len(pairs)} suggested "
        f"pairing(s) for reconciliation session {session.id}. Nothing is confirmed from "
        "chat — the user must approve it in the Actions page; each pair is re-validated at "
        f"approval and stale pairs are skipped, never force-confirmed.{page_note}{cursor_note}"), proposal_id=action.id)
    )


def _handle_recon_bulk_confirm_exec(db: Session, realm_id: str, params: dict) -> str:
    """Wave-2.3 HIDDEN executor (decision 5, E5): runs ONLY at approval time
    (RECON_BULK_CONFIRM → this handler); never offered to the model. Iterates ONLY the
    stored pair list with INDEXED liveness checks (session OPEN; book exists + cleared
    falsy + cleared_session_id null; bank item exists + no APPROVED match on its
    bank_txn_id) — it NEVER re-runs suggest_pairings/find_matches. The valid subset
    confirms via match_service.confirm_match; skips report distinctly. safety_expectation
    AgentEvalFixture: recon_bulk_confirm_exec_fixture."""
    from ..models import ReconciliationSession
    from ..services.bank_recon import agent_ops
    from ..services.bank_recon import match as match_service

    session_id = params.get("session_id")
    raw_pairs = params.get("pairs")
    if not session_id:
        return "Error: session_id is required."
    if not isinstance(raw_pairs, list) or not raw_pairs:
        return "Error: pairs must be a non-empty list of {bank_item_id, book_txn_id} objects."
    pairs: list = []
    for entry in raw_pairs:  # SA-OK: bounded-loop parsing the drafted pair list (≤40), pure in-memory
        if not isinstance(entry, dict):
            return "Error: pairs must be objects with bank_item_id and book_txn_id."
        try:
            pairs.append((int(entry.get("bank_item_id")), int(entry.get("book_txn_id"))))
        except (TypeError, ValueError):
            return "Error: every pair needs integer bank_item_id and book_txn_id."
    if len(pairs) > agent_ops.MAX_BULK_PAIRS:
        return (
            f"Error: pair list exceeds the {agent_ops.MAX_BULK_PAIRS}-pair cap — split the "
            "proposal. Nothing was confirmed."
        )

    session = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.id == session_id,
            ReconciliationSession.realm_id == realm_id,
        )
        .first()
    )
    if session is None:
        return f"Error: reconciliation session {session_id} not found for this company."
    if not is_open_session_status(session.status):
        return (
            f"Error: reconciliation session {session.id} is "
            f"{effective_session_status(session.status)}, not OPEN — all "
            f"{len(pairs)} pair(s) skipped ({agent_ops.SKIP_SESSION_CLOSED}); nothing was confirmed."
        )

    liveness = agent_ops.check_bulk_pairs_liveness(
        db, realm_id=realm_id, session=session, pairs=pairs
    )
    skip_parts: list = []
    if liveness.skipped_stale:
        skip_parts.append(
            f"{len(liveness.skipped_stale)} {agent_ops.SKIP_STALE} (bank/book row no longer exists)"
        )
    if liveness.skipped_cleared_elsewhere:
        skip_parts.append(
            f"{len(liveness.skipped_cleared_elsewhere)} {agent_ops.SKIP_CLEARED_ELSEWHERE} "
            "(already cleared or the bank line is already matched)"
        )
    if not liveness.valid:
        return (
            f"Error: none of the {len(pairs)} drafted pair(s) were still confirmable — "
            + "; ".join(skip_parts)
            + ". Nothing was confirmed."
        )
    confirmed = 0
    for bank_id, book_id in liveness.valid:  # SA-OK: bounded-loop ≤40 pairs; confirm_match is the frozen human-gated per-pair bridge
        try:
            match_service.confirm_match(
                db,
                realm_id=realm_id,
                session=session,
                bank_item_id=bank_id,
                book_txn_id=book_id,
            )
        except match_service.ConfirmationSessionNotOpenError:
            db.rollback()
            return (
                f"Error: reconciliation session {session.id} is no longer OPEN — all "
                f"{len(pairs)} pair(s) skipped ({agent_ops.SKIP_SESSION_CLOSED}); "
                "nothing was confirmed."
            )
        except (
            match_service.ConfirmationSessionNotFoundError,
            match_service.StatementLineNotInSessionError,
            match_service.BookTransactionNotInSessionError,
            match_service.MatchOutsideSessionWindowError,
            match_service.CleanStartNotConfiguredError,
        ):
            liveness.skipped_stale.append((bank_id, book_id))
            continue
        except (
            match_service.BookTransactionClearedError,
            match_service.MatchClaimConflictError,
        ):
            liveness.skipped_cleared_elsewhere.append((bank_id, book_id))
            continue
        confirmed += 1
    skip_parts = []
    if liveness.skipped_stale:
        skip_parts.append(
            f"{len(liveness.skipped_stale)} {agent_ops.SKIP_STALE} "
            "(outside this session's exact account/window relation)"
        )
    if liveness.skipped_cleared_elsewhere:
        skip_parts.append(
            f"{len(liveness.skipped_cleared_elsewhere)} "
            f"{agent_ops.SKIP_CLEARED_ELSEWHERE} "
            "(already cleared or one side is already matched)"
        )
    if confirmed == 0:
        db.rollback()
        return (
            f"Error: none of the {len(pairs)} drafted pair(s) were still confirmable — "
            + "; ".join(skip_parts)
            + ". Nothing was confirmed."
        )
    db.commit()
    message = (
        f"✅ Confirmed {confirmed} of {len(pairs)} pair(s) in reconciliation "
        f"session {session.id}."
    )
    if skip_parts:
        message += " Skipped: " + "; ".join(skip_parts) + "."
    return message


# ─────────────────────────────────────────────────────────────────────────────
# PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md). WF-12 agent-authored checkpointed plans (decision 3,
# E1 — the two-name split, the Wave-2.3 recon precedent): propose_agent_plan is the
# VISIBLE self-drafter (HANDLER_LANE_SELF_DRAFTER) — its handler runs at CHAT time,
# gates the model DAG through plan_validation.validate_plan (chunk A), persists DRAFT
# rows + the sealed steps_hash, and drafts ONE PENDING AGENT_PLAN ProposedAction with
# payload["tool_name"] = "agent_plan_activate_exec". agent_plan_activate_exec is the
# HIDDEN approval-time executor (in no bundle + MODEL_HIDDEN_TOOLS) resolved via
# AGENT_PLAN in ACTION_TYPE_TO_TOOL: it re-hashes the step rows (E3 tamper seal),
# creates the holder objective, claim-flips the plan ACTIVE, and enqueues the walk —
# it NEVER executes an approved action (decision 6). get_agent_plan_status is a READ.
# Paired safety_expectation AgentEvalFixtures propose_agent_plan_proposal /
# agent_plan_activate_exec_fixture (app/agent/evals.py, AP-T09 lockstep).
# ─────────────────────────────────────────────────────────────────────────────


def _handle_propose_agent_plan(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """WF-12 visible SELF-DRAFTER (decisions 3/5/10): runs at CHAT time — composes the
    DAG, gates it through plan_validation.validate_plan against a TRUSTED ANALYST /
    DRAFT_ONLY context (the walker's execution principal, so a step the walker could not
    run is refused NOW), persists DRAFT AgentPlan + steps + the sealed steps_hash on
    pass, and drafts ONE PENDING AGENT_PLAN ProposedAction whose payload["tool_name"] is
    the HIDDEN executor agent_plan_activate_exec. A doomed plan is refused with EVERY
    validation reason and ZERO rows written. safety_expectation AgentEvalFixture:
    propose_agent_plan_proposal."""
    from ..agent.runtime.plan_validation import validate_plan
    from ..agent.runtime.tool_registry import build_assistant_tool_registry, canonicalize_intent
    from ..agent.runtime.types import AgentIntent, ContextPacket, OutputMode, RiskLevel
    from ..models import AgentPlan, AgentPlanStep
    from ..services.tenant_companies import organization_for_company
    from .tools import ASSISTANT_TOOLS

    dry_run = _dry_run_requested(params)
    title = str(params.get("title") or "").strip()
    goal = str(params.get("goal") or "").strip()
    raw_steps = params.get("steps")
    if not title:
        return "Error: title is required (a short name for the plan)."
    if not goal:
        return "Error: goal is required (one sentence on what the plan should accomplish)."

    registry = build_assistant_tool_registry(ASSISTANT_TOOLS, TOOL_HANDLERS)

    # Covering intent derived from the steps' tools exactly as plan_walker.build_plan_context
    # does (general fallback → the per-step gate refuses loudly), so validation runs under the
    # same trusted ANALYST / DRAFT_ONLY context the walker will use.
    step_tools = []
    if isinstance(raw_steps, (list, tuple)):
        for s in raw_steps:
            if isinstance(s, dict):
                tool = registry.get(str(s.get("tool_name") or ""))  # SA-OK: in-memory registry dict lookup, not a DB query
                if tool is not None:
                    step_tools.append(tool)
    constrained = [t for t in step_tools if "general" not in t.allowed_intents]
    if not constrained:
        covering = AgentIntent.GENERAL.value
    else:
        common = set.intersection(*(set(t.allowed_intents) for t in constrained))
        covering = sorted(common)[0] if common else AgentIntent.GENERAL.value

    org = organization_for_company(db, realm_id)
    context = ContextPacket(
        run_id=None, conversation_id=None, user_id=None, user_email=None, tenant_id=None,
        company_id=realm_id, qbo_realm_id=realm_id, user_role="ANALYST",
        request_text=f"agent-plan draft: {title}", intent=canonicalize_intent(covering),
        risk_level=RiskLevel.DRAFT_ONLY, output_mode=OutputMode.JSON,
        organization_id=org.id if org else None,
    )

    result = validate_plan(raw_steps, registry=registry, context=context)
    if not result.ok:
        return (
            "Error: the plan did not pass validation, so nothing was drafted. Fix every "
            "issue and re-draft: " + "; ".join(result.refusals)
        )

    if dry_run:
        write_count = sum(1 for s in result.steps if s.is_write)
        return _dry_run_notice(
            f"an AGENT_PLAN proposal '{title}' with {len(result.steps)} step(s) "
            f"({write_count} write step(s)); the steps are re-hashed at approval and each "
            "write step drafts its own separate proposal for approval."
        )

    if org is None:
        return "Error: could not resolve the organization for this company — nothing was drafted."

    _pin_realm_write_scope(db, realm_id)
    plan = AgentPlan(
        realm_id=realm_id, organization_id=org.id, title=title, goal=goal,
        status="PENDING_APPROVAL", steps_hash=result.steps_hash,
    )
    db.add(plan)
    db.flush()  # assign plan.id for the step FK + the proposal payload
    canonical_steps: list[dict] = []
    for vs in result.steps:  # SA-OK: bounded-loop over ≤20 validated steps; db.add is a unit-of-work stage, not a per-row query
        db.add(AgentPlanStep(
            plan_id=plan.id, realm_id=realm_id, step_index=vs.step_index,
            tool_name=vs.tool_name, tool_input=dict(vs.tool_input),
            depends_on=list(vs.depends_on), is_write=vs.is_write, status="pending",
        ))
        canonical_steps.append({
            "step_index": vs.step_index, "tool_name": vs.tool_name,
            "tool_input": dict(vs.tool_input), "depends_on": list(vs.depends_on),
            "is_write": vs.is_write,
        })

    action = ProposedAction(
        company_id=realm_id,
        action_type="AGENT_PLAN",
        payload={
            "tool_name": "agent_plan_activate_exec",
            "summary": f"Activate agent plan '{title}' ({len(canonical_steps)} step(s))",
            "execution_mode": "internal_db",
            "approval_effect": (
                "Approving activates the plan and starts the walk — read steps run "
                "automatically, and each WRITE step drafts its OWN proposal for separate "
                "approval; rejecting discards the plan."
            ),
            "tool_input": {"plan_id": plan.id},
            "affected_entity": f"agent plan {plan.id}",
            "plan_id": plan.id,
            "goal": goal,
            "steps": canonical_steps,
            "steps_hash": result.steps_hash,
            "after_state": {"plan_id": plan.id, "status": "ACTIVE", "steps": canonical_steps},
            "source_data": {"source": "ai_assistant_chat", "tool_name": "propose_agent_plan"},
            "required_role": "EDITOR",
        },
        reasoning=(
            "Drafted by Peregrine assistant: a validated multi-step plan. The DAG passed the "
            "validation gate; approval re-hashes the steps and refuses on any tamper, then the "
            "walker drives it through the existing proposal funnel — every write step is still "
            "individually human-approved."
        ),
        confidence_score=0.8,
        status="PENDING",
        source="ai_assistant",
        category="proposal",
        priority="medium",
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    db.refresh(plan)
    return (
        DraftedToolResult((f"✅ Agent plan '{title}' drafted (Plan #{plan.id}, Action #{action.id}) with "
        f"{len(canonical_steps)} step(s). Nothing runs from chat — approve the AGENT_PLAN "
        "proposal in the Actions page to activate it; each write step then drafts its own "
        "proposal for separate approval."), proposal_id=action.id)
    )


def _handle_agent_plan_activate_exec(db: Session, realm_id: str, params: dict) -> str:
    """WF-12 HIDDEN executor (decisions 3/6/7, E3): runs ONLY at approval time (AGENT_PLAN
    → this handler via ACTION_TYPE_TO_TOOL); never offered to the model. Re-hashes the
    persisted step rows against AgentPlan.steps_hash and REFUSES on any mismatch (the E3
    tamper seal), find-or-creates the schedule-DISABLED holder AgentObjective (decision 7),
    claim-flips the plan DRAFT/PENDING_APPROVAL→ACTIVE (E9 conditional UPDATE), and enqueues
    the first walk. The walker NEVER executes an approved action (decision 6).
    safety_expectation AgentEvalFixture: agent_plan_activate_exec_fixture."""
    from sqlalchemy import update as _sql_update

    from ..agent.runtime.plan_validation import steps_hash_matches
    from ..models import AgentObjective, AgentPlan, AgentPlanStep
    from ..services.tenant_companies import organization_company_for_realm, organization_for_company

    try:
        plan_id = int(params.get("plan_id"))
    except (TypeError, ValueError):
        return "Error: plan_id is required."

    plan = (
        db.query(AgentPlan)
        .filter(AgentPlan.id == plan_id, AgentPlan.realm_id == realm_id)
        .first()
    )
    if plan is None:
        return f"Error: agent plan {plan_id} not found for this company."
    if plan.status == "ACTIVE":
        # Idempotent: a redelivered approval of an already-active plan is not a failure.
        return f"✅ Agent plan {plan_id} is already ACTIVE — the walk is underway."
    if plan.status not in ("DRAFT", "PENDING_APPROVAL"):
        return f"Error: agent plan {plan_id} is {plan.status} and can no longer be activated."

    steps = (
        db.query(AgentPlanStep)
        .filter(AgentPlanStep.plan_id == plan.id, AgentPlanStep.realm_id == realm_id)
        .order_by(AgentPlanStep.step_index.asc())
        .all()
    )
    if not steps:
        return f"Error: agent plan {plan_id} has no steps — nothing to activate."
    if not steps_hash_matches(steps, plan.steps_hash):
        return (
            f"Error: agent plan {plan_id} failed its tamper seal — the persisted steps no "
            "longer match the approved plan hash. The plan was NOT activated."
        )

    # Claim-flip FIRST (E9 conditional UPDATE — no read-then-write). A concurrent activation
    # loses the claim (rowcount 0) and refuses without side effects.
    won = db.execute(
        _sql_update(AgentPlan)
        .where(AgentPlan.id == plan.id)
        .where(AgentPlan.status.in_(("DRAFT", "PENDING_APPROVAL")))
        .values(status="ACTIVE")
    ).rowcount == 1
    if not won:
        return (
            f"Error: agent plan {plan_id} could not be activated — another decision moved it "
            "out of a draft state first."
        )

    # Decision 7: find-or-create the realm's ONE schedule-DISABLED agent-plan holder
    # objective (tenancy/scope + AgentRun parentage; NEVER scheduler-visible — enabled=False,
    # next_run_at=None, the run_close_objective_now holder precedent).
    org = organization_for_company(db, realm_id)
    org_company = (
        organization_company_for_realm(db, organization_id=org.id, realm_id=realm_id)
        if org else None
    )
    holder = (
        db.query(AgentObjective)
        .filter(
            AgentObjective.company_id == realm_id,
            AgentObjective.objective_type == "agent_plan",
        )
        .order_by(AgentObjective.id.asc())
        .first()
    )
    if holder is None:
        db.add(AgentObjective(
            organization_id=org.id if org else None,
            organization_company_id=org_company.id if org_company else None,
            company_id=realm_id,
            name="Agent plans (walker holder)",
            objective_type="agent_plan",
            enabled=False,
            next_run_at=None,
            permission_scope={"writeback": "proposal_only"},
        ))

    # Commit the ACTIVE flip + holder BEFORE .delay — the walk task re-loads the plan by PK
    # from a FRESH session and skips a non-ACTIVE plan (the close_run_start WS-3 lesson).
    db.commit()

    from ..tasks.agent_plans import run_agent_plan_walk
    run_agent_plan_walk.delay(plan.id, realm_id)
    return (
        f"✅ Agent plan {plan_id} activated — the walk has started. Read steps run "
        "automatically; each write step drafts its own proposal for separate approval."
    )


def _handle_get_agent_plan_status(db: Session, realm_id: str, params: dict) -> str:
    """WF-12 READ (decision 10): one agent plan's status + per-step progress, realm-scoped.
    Read-only (VIEWER); no write signal, no safety fixture (the refresh-READ precedent)."""
    from ..models import AgentPlan, AgentPlanStep

    try:
        plan_id = int(params.get("plan_id"))
    except (TypeError, ValueError):
        raise ToolFailure("invalid_request", ("Error: plan_id is required."))
    plan = (
        db.query(AgentPlan)
        .filter(AgentPlan.id == plan_id, AgentPlan.realm_id == realm_id)
        .first()
    )
    if plan is None:
        raise ToolFailure("invalid_request", (f"Error: agent plan {plan_id} not found for this company."))
    steps = (
        db.query(AgentPlanStep)
        .filter(AgentPlanStep.plan_id == plan.id, AgentPlanStep.realm_id == realm_id)
        .order_by(AgentPlanStep.step_index.asc())
        .all()
    )
    lines = [f"Agent plan #{plan.id} '{plan.title}' — status {plan.status}. Goal: {plan.goal}"]
    for s in steps:  # SA-OK: bounded-loop over ≤20 already-loaded step rows, no query inside
        detail = f"  step {s.step_index}: {s.tool_name} [{s.status}]"
        if s.is_write:
            detail += (
                f" (write, proposal #{s.proposed_action_id})"
                if s.proposed_action_id
                else " (write, no proposal yet)"
            )
        if s.evidence:
            detail += f" — {s.evidence}"
        lines.append(detail)
    return "\n".join(lines)


def _handle_list_unmatched_items(db: Session, realm_id: str, params: dict) -> str:
    """Wave-2.3 READ (decision 6 / E9): stable id-cursor page over a session's unmatched
    bucket (side = bank | book; bank items carry why_not_matched). Cursor = last id —
    stable across match recomputes (rows leave the pool but never reorder/duplicate)."""
    from ..models import ReconciliationSession
    from ..services.bank_recon import agent_ops
    from ..services.bank_recon import match as match_service

    session_id = params.get("session_id")
    if not session_id:
        raise ToolFailure("invalid_request", ("Error: session_id is required."))
    session = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.id == session_id,
            ReconciliationSession.realm_id == realm_id,
        )
        .first()
    )
    if session is None:
        raise ToolFailure("invalid_request", (f"Error: reconciliation session {session_id} not found for this company."))
    side = str(params.get("side") or "bank").strip().lower()
    if side not in {"bank", "book"}:
        raise ToolFailure("invalid_request", ("Error: side must be 'bank' (statement lines) or 'book' (book transactions)."))
    try:
        cursor = int(params.get("cursor") or 0)
        limit = int(params.get("limit") or agent_ops.DEFAULT_UNMATCHED_PAGE)
    except (TypeError, ValueError):
        raise ToolFailure("invalid_request", ("Error: cursor and limit must be integers."))
    try:
        result = match_service.find_matches(db, realm_id=realm_id, session=session)
    except match_service.CleanStartNotConfiguredError:
        raise ToolFailure("invalid_request", ("Error: this account has no clean-start anchor configured — set the clean start "
            "before reconciling."))
    page = agent_ops.serialize_unmatched(result, side=side, cursor=cursor, limit=limit)
    page["session_id"] = session.id
    page["session_status"] = effective_session_status(session.status)
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C4 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 5: typed result; serialize_result
    # is the byte-compat pin (json.dumps(model_dump(mode="json"), default=str)).
    from ..agent.runtime.tool_results import UnmatchedItemsPage, serialize_result

    return serialize_result(UnmatchedItemsPage.model_validate(page))


def _handle_get_recon_tieout(db: Session, realm_id: str, params: dict) -> str:
    """Wave-2.3 READ (decision 6 / E9): the session tie-out (opening + cleared −
    statement ending) PLUS the AR/AP subledger-to-GL tie-out, sharing ONE BalanceSheet
    fetch across both sides (the naive path fetched it twice). Optional ``sides`` limits
    the subledger legs. Honest degradation: a side whose inputs cannot be loaded returns
    available=False — never a fabricated clean tie-out. The agent presents this; a HUMAN
    completes the reconciliation (decision 2 — no agent completion authority)."""
    from ..analysis.subledger_recon import RECON_TOLERANCE, _BS_LABELS, _subledger_total
    from ..models import ReconciliationSession
    from ..services.bank_recon import session as session_service
    from ..services.coa_formula import _get_bs_total

    session_id = params.get("session_id")
    if not session_id:
        raise ToolFailure("invalid_request", ("Error: session_id is required."))
    session = (
        db.query(ReconciliationSession)
        .filter(
            ReconciliationSession.id == session_id,
            ReconciliationSession.realm_id == realm_id,
        )
        .first()
    )
    if session is None:
        raise ToolFailure("invalid_request", (f"Error: reconciliation session {session_id} not found for this company."))
    raw_sides = params.get("sides")
    if raw_sides is None:
        sides = ["ar", "ap"]
    elif isinstance(raw_sides, list) and all(str(s).lower() in {"ar", "ap"} for s in raw_sides):
        sides = [str(s).lower() for s in raw_sides]
    else:
        raise ToolFailure("invalid_request", ("Error: sides must be a list containing only 'ar' and/or 'ap'."))

    tieout = session_service.compute_tieout(db, realm_id=realm_id, session=session)
    payload = {
        "session": {
            "session_id": session.id,
            "account_id": session.account_id,
            "account_name": session.account_name,
            "status": effective_session_status(session.status),
            "period_start": session.period_start.isoformat(),
            "period_end": session.period_end.isoformat(),
            "opening": str(tieout.opening),
            "anchor_configured": tieout.anchor_configured,
            "cleared_sum": str(tieout.cleared_sum),
            "cleared_balance": str(tieout.cleared_balance),
            "ending_balance": str(tieout.ending_balance),
            "difference": str(tieout.difference),
            "tied_out": tieout.difference == 0,
        },
        "subledger": {},
        "note": (
            "Completion is a human act: a session completes only when the difference is zero "
            "or fully itemized as outstanding — the agent presents this tie-out, it cannot "
            "complete the reconciliation."
        ),
    }
    if not tieout.anchor_configured:
        payload["session"]["anchor_note"] = (
            "No clean-start anchor is configured — the opening 0 is a placeholder, not a real "
            "baseline; this tie-out is not authoritative."
        )

    if sides:
        client = None
        client_reason = ""
        try:
            client = get_qbo_client_from_db(db, realm_id)
        except Exception as exc:
            client_reason = "QuickBooks could not be reached"
            logger.warning("[recon_tieout] QBO client unavailable: %s", type(exc).__name__)
        bs = None
        if client is not None:
            # ONE BalanceSheet fetch shared across BOTH subledger sides (E9 — the naive
            # path fetched it once per side).
            try:
                bs = client.get_report(
                    "BalanceSheet",
                    {
                        "start_date": f"{session.period_end.year}-01-01",
                        "end_date": session.period_end.isoformat(),
                        "accounting_method": "Accrual",
                    },
                )
            except Exception as exc:
                logger.warning("[recon_tieout] BalanceSheet unavailable: %s", type(exc).__name__)
        for side in sides:  # SA-OK: bounded-loop over ≤2 sides; the reads are QBO report calls, not DB queries
            side_key = side.upper()
            subledger = _subledger_total(client, side_key, session.period_end) if client is not None else None
            gl_control = _get_bs_total(bs, _BS_LABELS[side_key]) if bs is not None else None
            if subledger is None or gl_control is None:
                if client is None:
                    reason = client_reason
                elif bs is None:
                    reason = f"{side_key} GL control balance could not be loaded (Balance Sheet unavailable)"
                elif gl_control is None:
                    reason = f"{side_key} GL control line is absent from the Balance Sheet"
                else:
                    reason = f"{side_key} subledger total could not be loaded"
                payload["subledger"][side] = {
                    "available": False,
                    "reason": f"{reason} — cannot tie out",
                }
                continue
            break_amount = round(abs(subledger - gl_control), 2)
            payload["subledger"][side] = {
                "available": True,
                "as_of": session.period_end.isoformat(),
                "subledger_total": round(subledger, 2),
                "gl_control_balance": round(gl_control, 2),
                "break_amount": break_amount,
                "tied_out": break_amount <= RECON_TOLERANCE,
            }
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C4 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 5: typed result; serialize_result
    # is the byte-compat pin (json.dumps(model_dump(mode="json"), default=str)).
    from ..agent.runtime.tool_results import ReconTieoutResult, serialize_result

    result = serialize_result(ReconTieoutResult.model_validate(payload))
    if not tieout.anchor_configured:
        raise ToolFailure("recon_anchor_missing", result)
    if any(not side["available"] for side in payload["subledger"].values()):
        raise ToolFailure("recon_subledger_unavailable", result, retryable=True)
    return result


def _handle_generate_close_tasks(db: Session, realm_id: str, params: dict) -> str:
    """Row 7 (WRITE, proposal-gated at approval): generate the month-end close task list for a
    period. Delegates to the frozen close_task_generation service (idempotent per period). The
    realm + period come from trusted scope + the drafted payload."""
    from ..services.close_task_generation import (
        TemplateNotFoundError,
        generate_close_tasks_service,
    )

    period = str(params.get("period") or "").strip()
    if not period:
        return "Error: period (YYYY-MM) is required."
    replace_existing = bool(params.get("replace_existing"))
    template_id = params.get("template_id")
    try:
        result = generate_close_tasks_service(
            db,
            realm_id,
            period,
            replace_existing=replace_existing,
            template_id=template_id if isinstance(template_id, int) else None,
        )
    except TemplateNotFoundError as exc:
        return f"Error: close template not found ({redact_to_string(str(exc), max_length=120)})."
    if result.get("status") == "skipped":
        return f"⏭️ {result.get('message')}"
    return (
        f"✅ Generated {result.get('tasks_created', 0)} close tasks for {period}"
        + (f" (template: {result['template_used']})" if result.get("template_used") else "")
        + "."
    )


def _handle_create_reforecast(db: Session, realm_id: str, params: dict) -> str:
    """Row 8 (WRITE, proposal-gated at approval): clone the base budget into a reforecast scenario
    and overwrite locked months with QBO actuals. Delegates to the frozen budget_reforecast
    service (clone-only — base untouched). The service raises ValueError when no months are
    locked."""
    from ..services.budget_reforecast import create_reforecast_service

    budget_year = params.get("budget_year") or datetime.utcnow().year
    try:
        result = create_reforecast_service(db, company_id=realm_id, budget_year=int(budget_year))
    except ValueError as exc:
        return f"Error: {redact_to_string(str(exc), max_length=200)}"
    status = result.get("status")
    if status == "actuals_unavailable":
        return "⚠️ Reforecast cloned, but QuickBooks actuals were unavailable — no months were overwritten."
    if status == "no_changes":
        return "ℹ️ Reforecast is already in sync — no months changed."
    return f"✅ Reforecast created for {budget_year}: {result['lines_updated']} lines synced from actuals."


def _handle_update_budget_driver(db: Session, realm_id: str, params: dict) -> str:
    """Row 9 (WRITE, proposal-gated at approval): update ONE budget driver's method/params.
    Resolves the BudgetDriver by id UNDER the realm scope (a driver from another tenant is not
    found), then delegates to the frozen budget_drivers service (which recomputes + serializes)."""
    from ..services.budget_drivers import update_budget_driver_service

    driver_id = params.get("driver_id")
    if not driver_id:
        return "Error: driver_id is required."
    driver = (
        db.query(BudgetDriver)
        .filter(BudgetDriver.id == driver_id, BudgetDriver.company_id == realm_id)
        .first()
    )
    if driver is None:
        return f"Error: budget driver {driver_id} not found for this company."
    updates = {
        field: params[field]
        for field in ("driver_method", "driver_params")
        if params.get(field) is not None
    }
    if not updates:
        return "Error: provide driver_method and/or driver_params to update."
    update_budget_driver_service(db, driver, updates)
    return f"✅ Budget driver {driver_id} updated ({', '.join(sorted(updates))})."


# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
def _handle_create_lease(db: Session, realm_id: str, params: dict) -> str:
    """LEASE_CREATE (WRITE, proposal-gated at approval): create a DRAFT ASC-842 lease.

    A SIMPLE single-name internal-DB write — the generic proposer drafts the PENDING
    proposal from these params at chat time; THIS handler runs only at approval. The
    trusted realm_id (server-pinned ContextPacket scope; the model's scope keys were
    stripped upstream by Control-5) is STAMPED onto the create payload — a tool-supplied
    realm_id is ignored (server scope wins). LeaseCreate re-validates types/defaults;
    create_lease_service enforces the ASC-842 term/classification rules, so a percent-form
    discount_rate (5 / 6.0) is REJECTED by validate_terms, never normalized. The engine
    owns all PV/measurement at activate — create stores DRAFT terms only.
    """
    from pydantic import ValidationError

    from ..routers.leases import LeaseCreate
    from ..services.lease_service import LeaseValidationError, create_lease_service

    data = {k: v for k, v in (params or {}).items() if k != "_dry_run"}
    data["realm_id"] = realm_id  # trusted scope wins; overwrite any model-supplied realm_id
    try:
        body = LeaseCreate(**data)
    except ValidationError as exc:
        return f"Error: {redact_to_string(str(exc), max_length=300)}"
    try:
        lease = create_lease_service(
            db,
            realm_id,
            body.dict(),
            created_by=str(params.get("_current_user_email") or "AI Assistant"),
        )
    except LeaseValidationError as exc:
        return f"Error: {redact_to_string(str(getattr(exc, 'detail', exc)), max_length=300)}"
    return f"✅ Lease {lease.id} created ({lease.status} — terms editable; activate to recognize)."


# PLAN-EXEMPT: chunk 5 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
def _handle_create_revenue_contract(db: Session, realm_id: str, params: dict) -> str:
    """REVENUE_CONTRACT_CREATE (WRITE, proposal-gated at approval): create a DRAFT ASC 606
    revenue contract + its performance obligations.

    A SIMPLE single-name internal-DB write — the generic proposer drafts the PENDING
    proposal from these params at chat time; THIS handler runs only at approval. The
    trusted realm_id (server-pinned ContextPacket scope; the model's scope keys were
    stripped upstream by Control-5) is STAMPED onto the create payload — a tool-supplied
    realm_id is ignored (server scope wins). RevenueContractCreate re-validates types;
    create_revenue_contract_service server-validates the ASC 606 inputs, so a fabricated /
    None / non-positive SSP (or a missing over-time criteria set) is REJECTED, never
    defaulted (the engine consumes human judgment, never invents it). Nothing is
    recognized at create — the engine allocates + recognizes at activation.
    """
    from pydantic import ValidationError

    from ..routers.revenue_contracts import RevenueContractCreate
    from ..services.revenue_contracts import (
        RevenueContractError,
        create_revenue_contract_service,
    )

    data = {k: v for k, v in (params or {}).items() if k != "_dry_run"}
    data["realm_id"] = realm_id  # trusted scope wins; overwrite any model-supplied realm_id
    try:
        body = RevenueContractCreate(**data)
    except ValidationError as exc:
        return f"Error: {redact_to_string(str(exc), max_length=300)}"
    try:
        contract = create_revenue_contract_service(
            db,
            realm_id,
            body.model_dump(),
            created_by=str(params.get("_current_user_email") or "AI Assistant"),
        )
    except RevenueContractError as exc:
        return f"Error: {redact_to_string(str(getattr(exc, 'detail', exc)), max_length=300)}"
    return (
        f"✅ Revenue contract {contract.id} created ({contract.status} — terms editable; "
        f"activate to allocate + recognize)."
    )


# update_lease field whitelist, by lease status. A DRAFT lease accepts exactly the human
# PUT /leases/{id} schema (LeaseUpdate): terms, accounts and automation policy, because
# nothing is recognized yet. Once a lease leaves DRAFT, its measurement basis, posting
# account ids and automation policy drive recognized balances and the monthly lease-JE
# beat, so chat may only relabel account names. An editor changes posting setup on the
# Leases page, and term/payment/rate changes go through remeasure_lease. status, realm_id
# and the measured outputs (initial_liability, initial_rou, ...) are never chat-editable.
_LEASE_ACCOUNT_NAME_FIELDS = frozenset({
    "rou_asset_account_name",
    "accum_rou_amort_account_name",
    "lease_liability_account_name",
    "interest_expense_account_name",
    "lease_expense_account_name",
    "rou_amort_expense_account_name",
})


def _lease_update_allowed_fields(status: str) -> frozenset[str]:
    from ..routers.leases import LeaseUpdate

    if status == "DRAFT":
        return frozenset(LeaseUpdate.model_fields)
    return _LEASE_ACCOUNT_NAME_FIELDS


def validate_lease_update_request(db: Session, realm_id: str, params: dict) -> tuple[LeaseSchedule, dict]:
    """Resolve the lease under the realm scope and return its validated update payload.

    Raises ``ToolFailure("invalid_request")`` for a missing lease, an empty payload, or
    any field the lease's CURRENT status does not allow (see the whitelist above).
    Runs at draft time (the chat proposer refuses before drafting), again before an
    approved proposal executes (internal_action_execution.gate_refusal_reason) and inside
    the handler, so a stale or tampered proposal cannot write a locked field.
    """
    from pydantic import ValidationError

    from ..routers.leases import LeaseUpdate

    lease_id = params.get("lease_id")
    if not lease_id:
        raise ToolFailure("invalid_request", "lease_id is required.")
    data = params.get("data")
    if not isinstance(data, dict) or not data:
        raise ToolFailure("invalid_request", "data must be a non-empty object of lease fields to change.")
    lease = (
        db.query(LeaseSchedule)
        .filter(LeaseSchedule.id == lease_id, LeaseSchedule.realm_id == realm_id)
        .first()
    )
    if lease is None:
        raise ToolFailure("invalid_request", f"Lease {lease_id} was not found for this company.")
    allowed = _lease_update_allowed_fields(lease.status)
    refused = sorted(str(field) for field in data if field not in allowed)
    if refused and lease.status == "DRAFT":
        raise ToolFailure("invalid_request", (
            f"Lease {lease.id} is DRAFT; {', '.join(refused)} cannot be set with update_lease. "
            f"Editable fields: {', '.join(sorted(allowed))}."
        ))
    if refused:
        raise ToolFailure("invalid_request", (
            f"Lease {lease.id} is {lease.status}; chat can only relabel its account names, so "
            f"{', '.join(refused)} cannot be changed here. An editor can change posting accounts or the "
            "automation policy on the Leases page; use remeasure_lease for term, payment or rate changes."
        ))
    try:
        body = LeaseUpdate(**data)
    except ValidationError as exc:
        raise ToolFailure(
            "invalid_request", f"Invalid lease fields: {redact_to_string(str(exc), max_length=300)}"
        ) from exc
    return lease, body.model_dump(exclude_unset=True)


# Proposal-only writes whose request is re-validated against CURRENT state: the
# ToolExecutor proposer runs these before drafting, and the internal execution core runs
# them again before an approved proposal executes. Each raises ToolFailure on a refusal.
PROPOSAL_REQUEST_VALIDATORS = {
    "update_lease": validate_lease_update_request,
}


def _handle_update_lease(db: Session, realm_id: str, params: dict) -> str:
    """Row 11 (WRITE, LEASE_UPDATE — material, runs only after an editor approves it).

    Re-validates the request against the per-status field whitelist
    (``validate_lease_update_request``) and then delegates to lease_service, which
    additionally rejects term fields on a non-DRAFT lease and validates DRAFT terms.
    """
    from ..services.lease_service import LeaseValidationError, update_lease_service

    lease, data = validate_lease_update_request(db, realm_id, params)
    try:
        updated = update_lease_service(db, realm_id, lease, data)
    except LeaseValidationError as exc:
        return f"Error: {redact_to_string(str(getattr(exc, 'detail', exc)), max_length=300)}"
    return f"✅ Lease {updated.id} updated ({updated.status})."


# PLAN-EXEMPT: chunk C3 of ratified plan 20260714-wave2-7b-lease-extraction (marker
# RATIFICATION-2026-07-14.md).
def _handle_get_lease_detail(db: Session, realm_id: str, params: dict) -> str:
    """READ tool (READ_ONLY_TOOLS lane — VIEWER, mutates_state=False, NO proposal, NO
    write): resolve one LeaseSchedule by id UNDER the realm scope and return its terms +
    amortization rows (the leases.py get_lease read shape). Tenant-scoped by the
    ``realm_id`` filter — ``realm_id`` is pinned server-side from the trusted ContextPacket
    (never model input), so a lease in another realm is invisible here (returns not-found).
    Closes the 2.7 finding-3 read gap: the agent can write a lease (create_lease) but had
    no way to read one to resolve a lease_id."""
    from ..models import LeaseScheduleRow
    from ..routers.leases import LeaseOut, LeaseRowOut

    lease_id = params.get("lease_id")
    if not lease_id:
        raise ToolFailure("invalid_request", ("Error: lease_id is required. Use get_schedule_data or the leases view to find the ID."))
    lease = (
        db.query(LeaseSchedule)
        .filter(LeaseSchedule.id == lease_id, LeaseSchedule.realm_id == realm_id)
        .first()
    )
    if lease is None:
        raise ToolFailure("invalid_request", (f"Error: lease {lease_id} not found for this company."))
    rows = (
        db.query(LeaseScheduleRow)
        .filter(
            LeaseScheduleRow.lease_id == lease.id,
            LeaseScheduleRow.realm_id == lease.realm_id,
        )
        .order_by(LeaseScheduleRow.period)
        .all()
    )
    # PLAN-EXEMPT: chunk C3 of ratified plan 20260714-wave2-7b-lease-extraction (marker
    # RATIFICATION-2026-07-14.md). The v1 LeaseOut/LeaseRowOut carry orm_mode Config
    # (legacy-in-HEAD); pass from_attributes=True explicitly so the v2 model_validate
    # bridges the ORM row without the deprecated .from_orm.
    payload = {
        "lease": LeaseOut.model_validate(lease, from_attributes=True).model_dump(),
        "rows": [
            LeaseRowOut.model_validate(r, from_attributes=True).model_dump() for r in rows
        ],
    }
    return json.dumps(payload, indent=1, default=str)


def _handle_apply_budget_drivers(db: Session, realm_id: str, params: dict) -> str:
    """Apply only the server-owned calculation the human reviewed."""
    from ..services.budget_reforecast import apply_reviewed_budget_driver_plan
    result = apply_reviewed_budget_driver_plan(db, company_id=realm_id, reviewed=params.get("_reviewed_budget_plan"))
    return (
        f"✅ Applied {result.get('applied', 0)} budget drivers "
        f"({result.get('parents_updated', 0)} parent lines re-aggregated)."
    )


def _handle_update_covenant(db: Session, realm_id: str, params: dict) -> str:
    """Row 14 (WRITE, proposal-gated at approval): edit a debt covenant's threshold / definition.
    Resolves the DebtCovenant by id UNDER the realm scope (a covenant from another tenant is not
    found), then delegates to the frozen covenant_config service (which snapshots before/after and
    persists the breach-detection-affecting fields). Called only at APPROVAL time. Its
    safety_expectation fixture (update_covenant_proposal) lives in app/agent/evals.py."""
    from ..services.covenant_config import CovenantConfigError, update_covenant_service

    covenant_id = params.get("covenant_id")
    if not covenant_id:
        return "Error: covenant_id is required."
    updates = params.get("updates")
    if not isinstance(updates, dict) or not updates:
        return "Error: updates must be a non-empty object of covenant fields to change."
    covenant = (
        db.query(DebtCovenant)
        .filter(DebtCovenant.id == covenant_id, DebtCovenant.company_id == realm_id)
        .first()
    )
    if covenant is None:
        return f"Error: covenant {covenant_id} not found for this company."
    try:
        result = update_covenant_service(db, realm_id, covenant, updates)
    except CovenantConfigError as exc:
        return f"Error: {redact_to_string(str(exc), max_length=200)}"
    changed = " (threshold changed)" if result.get("threshold_changed") else ""
    return f"✅ Covenant {covenant_id} updated{changed}."


def _handle_update_ebitda_config(db: Session, realm_id: str, params: dict) -> str:
    """Row 15 (WRITE, proposal-gated at approval): set/replace the company's single EBITDA add-back
    mapping (the input every EBITDA covenant computes from). Delegates to the frozen covenant_config
    service (add-backs persisted verbatim — explicit account mapping, never keyword inference).
    Called only at APPROVAL time. Its safety_expectation fixture (update_ebitda_config_proposal)
    lives in app/agent/evals.py."""
    from ..services.covenant_config import CovenantConfigError, update_ebitda_config_service

    addbacks = params.get("addbacks")
    if not isinstance(addbacks, list):
        return "Error: addbacks must be a list of add-back line objects (may be empty to clear)."
    try:
        result = update_ebitda_config_service(db, realm_id, addbacks)
    except CovenantConfigError as exc:
        return f"Error: {redact_to_string(str(exc), max_length=200)}"
    changed = "changed" if result.get("changed") else "unchanged"
    return f"✅ EBITDA add-back mapping updated ({len(addbacks)} line(s), {changed})."


def _handle_activate_lease(db: Session, realm_id: str, params: dict) -> str:
    """Row 16a (WRITE, proposal-gated at approval): activate a DRAFT ASC-842 lease — measure at the
    locked rate, persist the amortization rows, draft the commencement JE. Resolves the LeaseSchedule
    by id UNDER the realm scope, then delegates to the frozen lease_service (which ENFORCES DRAFT-only
    + required accounts, raising LeaseValidationError). Called only at APPROVAL time. Its
    safety_expectation fixture (activate_lease_proposal) lives in app/agent/evals.py."""
    from ..services.lease_service import LeaseValidationError, activate_lease_service

    lease_id = params.get("lease_id")
    if not lease_id:
        return "Error: lease_id is required."
    lease = (
        db.query(LeaseSchedule)
        .filter(LeaseSchedule.id == lease_id, LeaseSchedule.realm_id == realm_id)
        .first()
    )
    if lease is None:
        return f"Error: lease {lease_id} not found for this company."
    try:
        result = activate_lease_service(
            db,
            realm_id,
            lease,
            idc_source_account_id=params.get("idc_source_account_id"),
            idc_source_account_name=params.get("idc_source_account_name"),
            prepaid_source_account_id=params.get("prepaid_source_account_id"),
            prepaid_source_account_name=params.get("prepaid_source_account_name"),
            incentive_source_account_id=params.get("incentive_source_account_id"),
            incentive_source_account_name=params.get("incentive_source_account_name"),
            submitted_by=str(params.get("_current_user_email") or "AI Assistant"),
        )
    except LeaseValidationError as exc:
        return f"Error: {redact_to_string(str(getattr(exc, 'detail', exc)), max_length=300)}"
    return f"✅ Lease {result.get('lease_id', lease_id)} activated ({result.get('status')})."


def _handle_remeasure_lease(db: Session, realm_id: str, params: dict) -> str:
    """Row 16b (WRITE, proposal-gated at approval): remeasure an ACTIVE ASC-842 lease. Resolves the
    LeaseSchedule by id UNDER the realm scope, then delegates to the frozen lease_lifecycle service
    (which ENFORCES ACTIVE-only + the rate-change-carries-basis rule, raising LeaseValidationError).
    Called only at APPROVAL time. Its safety_expectation fixture (remeasure_lease_proposal) lives in
    app/agent/evals.py."""
    from ..services.lease_lifecycle import remeasure_lease_service
    from ..services.lease_service import LeaseValidationError

    lease_id = params.get("lease_id")
    if not lease_id:
        return "Error: lease_id is required."
    effective_period = str(params.get("effective_period") or "").strip()
    if not effective_period:
        return "Error: effective_period (YYYY-MM) is required."
    lease = (
        db.query(LeaseSchedule)
        .filter(LeaseSchedule.id == lease_id, LeaseSchedule.realm_id == realm_id)
        .first()
    )
    if lease is None:
        return f"Error: lease {lease_id} not found for this company."
    try:
        remeasure_lease_service(
            db,
            realm_id,
            lease,
            effective_period=effective_period,
            new_payment_amount=params.get("new_payment_amount"),
            new_escalation_steps=params.get("new_escalation_steps"),
            new_term_months=params.get("new_term_months"),
            new_discount_rate_annual=params.get("new_discount_rate_annual"),
            new_rate_basis=params.get("new_rate_basis"),
            rate_unchanged_reason=params.get("rate_unchanged_reason"),
            submitted_by=str(params.get("_current_user_email") or "AI Assistant"),
        )
    except LeaseValidationError as exc:
        return f"Error: {redact_to_string(str(getattr(exc, 'detail', exc)), max_length=300)}"
    return f"✅ Lease {lease_id} remeasured effective {effective_period}."


def _handle_terminate_lease(db: Session, realm_id: str, params: dict) -> str:
    """Row 16c (WRITE, proposal-gated at approval): terminate an ACTIVE ASC-842 lease. Resolves the
    LeaseSchedule by id UNDER the realm scope, then delegates to the frozen lease_lifecycle service
    (which ENFORCES ACTIVE-only + derecognition gain/loss, raising LeaseValidationError). Called only
    at APPROVAL time. Its safety_expectation fixture (terminate_lease_proposal) lives in
    app/agent/evals.py."""
    from datetime import date as _date

    from ..services.lease_lifecycle import terminate_lease_service
    from ..services.lease_service import LeaseValidationError

    lease_id = params.get("lease_id")
    if not lease_id:
        return "Error: lease_id is required."
    termination_date_raw = str(params.get("termination_date") or "").strip()
    if not termination_date_raw:
        return "Error: termination_date (YYYY-MM-DD) is required."
    try:
        termination_date = _date.fromisoformat(termination_date_raw)
    except ValueError:
        return "Error: termination_date must be an ISO date (YYYY-MM-DD)."
    lease = (
        db.query(LeaseSchedule)
        .filter(LeaseSchedule.id == lease_id, LeaseSchedule.realm_id == realm_id)
        .first()
    )
    if lease is None:
        return f"Error: lease {lease_id} not found for this company."
    try:
        terminate_lease_service(
            db,
            realm_id,
            lease,
            termination_date=termination_date,
            settlement_amount=params.get("settlement_amount") or 0,
            settlement_account_id=params.get("settlement_account_id"),
            settlement_account_name=params.get("settlement_account_name"),
            gain_loss_account_id=params.get("gain_loss_account_id"),
            gain_loss_account_name=params.get("gain_loss_account_name"),
            skip_draft_rows=bool(params.get("skip_draft_rows")),
            submitted_by=str(params.get("_current_user_email") or "AI Assistant"),
        )
    except LeaseValidationError as exc:
        return f"Error: {redact_to_string(str(getattr(exc, 'detail', exc)), max_length=300)}"
    return f"✅ Lease {lease_id} termination drafted effective {termination_date_raw}."


def _handle_dispose_schedule(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """Row 17 (WRITE, HANDLER lane): dispose a fixed-asset/intangible schedule. This handler runs at
    CHAT time (dispose_schedule is excluded from the internal-DB proposer) and self-drafts a PENDING
    JOURNAL_ENTRY ProposedAction via the frozen dispose_schedule_service — it mutates NO schedule
    state (the flip to DISPOSED happens only when the drafted JE proposal is approved and executes).
    The same handler-lane pattern the JE draft tool uses. Resolves the AccountingSchedule by id UNDER
    realm scope. Its safety_expectation fixture (dispose_schedule_proposal) lives in app/agent/evals.py."""
    from datetime import date as _date

    from ..services.schedule_disposal import ScheduleDisposalError, dispose_schedule_service

    schedule_id = params.get("schedule_id")
    if not schedule_id:
        return "Error: schedule_id is required."
    disposal_date_raw = str(params.get("disposal_date") or "").strip()
    if not disposal_date_raw:
        return "Error: disposal_date (YYYY-MM-DD) is required."
    try:
        disposal_date = _date.fromisoformat(disposal_date_raw)
    except ValueError:
        return "Error: disposal_date must be an ISO date (YYYY-MM-DD)."
    schedule = (
        db.query(AccountingSchedule)
        .filter(AccountingSchedule.id == schedule_id, AccountingSchedule.realm_id == realm_id)
        .first()
    )
    if schedule is None:
        return f"Error: schedule {schedule_id} not found for this company."
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit.
    # The draft itself happens inside the frozen dispose_schedule_service, so the
    # natural pre-draft point is before the service call: the preview validates
    # the schedule + date but does not run the service's own disposal checks
    # (those run at the real draft). safety_expectation AgentEvalFixture
    # dispose_schedule_proposal (app/agent/evals.py) unchanged.
    if _dry_run_requested(params):
        return _dry_run_notice(
            f"a JOURNAL_ENTRY disposal proposal for schedule #{schedule.id} "
            f"({schedule.name}) as of {disposal_date.isoformat()}; the schedule flips "
            "to DISPOSED only when that JE proposal is approved and executes."
        )
    try:
        result = dispose_schedule_service(
            db,
            schedule,
            disposal_date=disposal_date,
            proceeds=params.get("proceeds") or 0,
            proceeds_account_id=params.get("proceeds_account_id"),
            proceeds_account_name=params.get("proceeds_account_name"),
            asset_cost_account_id=params.get("asset_cost_account_id"),
            asset_cost_account_name=params.get("asset_cost_account_name"),
            gain_loss_account_id=params.get("gain_loss_account_id"),
            gain_loss_account_name=params.get("gain_loss_account_name"),
            skip_draft_entries=bool(params.get("skip_draft_entries")),
            submitted_by=str(params.get("_current_user_email") or "AI Assistant"),
        )
    except ScheduleDisposalError as exc:
        return f"Error: {redact_to_string(str(exc), max_length=300)}"
    proposal = result.proposal
    return (
        DraftedToolResult((f"Disposal drafted (ProposedAction #{proposal.id}). The disposal journal entry is PENDING — "
        f"the user must approve it in the Actions page before the schedule is disposed."), proposal_id=proposal.id)
    )


# ─────────────────────────────────────────────────────────────────────────────
# C3-DELETES (plan 20260706-cfo-grade, gap-register row 18): the 8 DESTRUCTIVE
# single-entity delete handlers. safety_expectation: each is proposal-gated — its
# AgentEvalFixture (app/agent/evals.py: delete_budget_line_proposal ...
# delete_knowledge_proposal, safety_expectation="proposal_only_for_financial_mutation")
# proves NOTHING deletes from chat. Each runs ONLY at approval (resolved from its
# <ENTITY>_DELETE ProposedAction via ACTION_TYPE_TO_TOOL) inside the realm-pinned
# FORCE-RLS scope. Every handler re-checks existence + tenant ownership of the ONE
# named entity BEFORE deleting (the entity may have changed/been removed between draft
# and approval): resolve under the realm scope, return "Error: ... not found" (skip)
# if gone or not owned. Single id only — NEVER bulk. Mirrors _handle_delete_schedule_row.
# ─────────────────────────────────────────────────────────────────────────────

def _handle_delete_budget_line(db: Session, realm_id: str, params: dict) -> str:
    """Delete one budget line, re-checking existence + ownership under the realm scope."""
    line_id = params.get("line_id")
    if not line_id:
        return "Error: 'line_id' is required. Use get_budget_data to find the ID."
    line = db.query(BudgetLine).filter(
        BudgetLine.id == line_id,
        BudgetLine.company_id == realm_id,
    ).first()
    if not line:
        return f"Error: Budget line with ID {line_id} not found for this company."
    db.delete(line)
    db.commit()
    return f"🗑️ Deleted budget line #{line_id}. This action cannot be undone."


def _handle_delete_budget_driver(db: Session, realm_id: str, params: dict) -> str:
    """Delete one budget driver, re-checking existence + ownership under the realm scope."""
    driver_id = params.get("driver_id")
    if not driver_id:
        return "Error: 'driver_id' is required. Use get_budget_data to find the ID."
    driver = db.query(BudgetDriver).filter(
        BudgetDriver.id == driver_id,
        BudgetDriver.company_id == realm_id,
    ).first()
    if not driver:
        return f"Error: Budget driver with ID {driver_id} not found for this company."
    db.delete(driver)
    db.commit()
    return f"🗑️ Deleted budget driver #{driver_id}. This action cannot be undone."


def _handle_delete_close_task(db: Session, realm_id: str, params: dict) -> str:
    """Delete one close task, re-checking existence + ownership under the realm scope."""
    task_id = params.get("task_id")
    if not task_id:
        return "Error: 'task_id' is required. Use get_close_status to find the ID."
    task = db.query(CloseTask).filter(
        CloseTask.id == task_id,
        CloseTask.realm_id == realm_id,
    ).first()
    if not task:
        return f"Error: Close task with ID {task_id} not found for this company."
    db.delete(task)
    db.commit()
    return f"🗑️ Deleted close task #{task_id}. This action cannot be undone."


def _handle_delete_covenant(db: Session, realm_id: str, params: dict) -> str:
    """Delete one covenant (and its snapshot children), re-checking ownership under the realm scope."""
    covenant_id = params.get("covenant_id")
    if not covenant_id:
        return "Error: 'covenant_id' is required. Use get_covenants_status to find the ID."
    cov = db.query(DebtCovenant).filter(
        DebtCovenant.id == covenant_id,
        DebtCovenant.company_id == realm_id,
    ).first()
    if not cov:
        return f"Error: Covenant with ID {covenant_id} not found for this company."
    # CovenantRecord.covenant_id FK has NO ON DELETE cascade — delete the realm-scoped
    # snapshot children FIRST or a bare db.delete(cov) fails with an FK violation (the
    # exact bug the router's delete_covenant guards against, covenants.py:1349-1359).
    from ..models import CovenantRecord
    db.query(CovenantRecord).filter(
        CovenantRecord.covenant_id == cov.id,
        CovenantRecord.company_id == cov.company_id,
    ).delete(synchronize_session=False)
    db.delete(cov)
    db.commit()
    return f"🗑️ Deleted covenant #{covenant_id}. This action cannot be undone."


def _handle_delete_custom_kpi(db: Session, realm_id: str, params: dict) -> str:
    """Delete one custom KPI, re-checking existence + ownership under the realm scope."""
    kpi_id = params.get("kpi_id")
    if not kpi_id:
        return "Error: 'kpi_id' is required."
    kpi = db.query(CustomKPI).filter(
        CustomKPI.id == kpi_id,
        CustomKPI.company_id == realm_id,
    ).first()
    if not kpi:
        return f"Error: Custom KPI with ID {kpi_id} not found for this company."
    db.delete(kpi)
    db.commit()
    return f"🗑️ Deleted custom KPI #{kpi_id}. This action cannot be undone."


def _handle_delete_cashflow_override(db: Session, realm_id: str, params: dict) -> str:
    """Delete one cash-flow override, re-checking existence + ownership under the realm scope."""
    from ..models import CashForecastOverride
    override_id = params.get("override_id")
    if not override_id:
        return "Error: 'override_id' is required."
    row = db.query(CashForecastOverride).filter(
        CashForecastOverride.id == override_id,
        CashForecastOverride.company_id == realm_id,
    ).first()
    if not row:
        return f"Error: Cash-flow override with ID {override_id} not found for this company."
    db.delete(row)
    db.commit()
    return f"🗑️ Deleted cash-flow override #{override_id}. This action cannot be undone."


def _handle_delete_document(db: Session, realm_id: str, params: dict) -> str:
    """Delete one document, re-checking existence + ownership under the realm scope."""
    document_id = params.get("document_id")
    if not document_id:
        return "Error: 'document_id' is required. Use search_documents to find the ID."
    row = db.query(Document).filter(
        Document.id == document_id,
        Document.company_id == realm_id,
    ).first()
    if not row:
        return f"Error: Document with ID {document_id} not found for this company."
    db.delete(row)
    db.commit()
    return f"🗑️ Deleted document #{document_id}. This action cannot be undone."


def _handle_delete_knowledge(db: Session, realm_id: str, params: dict) -> str:
    """Delete one taught-knowledge entry, re-checking existence + ownership under the realm scope."""
    from ..models import CompanyKnowledge
    knowledge_id = params.get("knowledge_id")
    if not knowledge_id:
        return "Error: 'knowledge_id' is required."
    obj = db.query(CompanyKnowledge).filter(
        CompanyKnowledge.id == knowledge_id,
        CompanyKnowledge.company_id == realm_id,
    ).first()
    if not obj:
        return f"Error: Knowledge entry with ID {knowledge_id} not found for this company."
    db.delete(obj)
    db.commit()
    return f"🗑️ Deleted knowledge entry #{knowledge_id}. This action cannot be undone."


# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C1 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
# ─────────────────────────────────────────────────────────────────────────────
# Wave-0 C1 (TG-2/TG-3): the two previously chat-unreachable QBO proposal lanes,
# exposed as HANDLER-lane self-drafters (the create_journal_entry pattern). Each
# drafts ONE PENDING ProposedAction via the SAME app/qbo_actions/proposer.py lane
# the human/admin routes use (no forked draft path); NOTHING writes to QBO from
# chat — execution happens only after human approval through the existing QBO
# executor. Safety fixtures: update_vendor_proposal / reverse_journal_entry_proposal
# (app/agent/evals.py).
# ─────────────────────────────────────────────────────────────────────────────

def _handle_update_vendor(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """TG-2: draft a PENDING UPDATE_VENDOR ProposedAction via propose_vendor_update —
    the SAME lane the admin POST /proposals route feeds (action_type UPDATE_VENDOR,
    payload = the QBO sparse Vendor body carrying "Id", exactly what the executor's
    UPDATE_VENDOR branch passes to qbo_actions.vendors.update_vendor at approval)."""
    from ..qbo_actions.proposer import propose_vendor_update

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # update_vendor_proposal unchanged).
    dry_run = _dry_run_requested(params)
    vendor_id = str(params.get("vendor_id") or "").strip()
    if not vendor_id:
        return (
            "Error: 'vendor_id' is required. Use search_finance_records with "
            "entity 'vendor' to find the QBO vendor Id."
        )
    updates = params.get("updates")
    if not isinstance(updates, dict) or not updates:
        return "Error: 'updates' must be a non-empty object of QBO Vendor fields to change."

    # The executor lane reads the target vendor from payload["Id"] (vendors.update_vendor).
    # Drop any model-supplied Id from the sparse updates so the explicit vendor_id wins.
    payload = {key: value for key, value in updates.items() if key not in {"Id", "id"}}
    if not payload:
        return "Error: 'updates' contained no updatable QBO Vendor fields."
    payload["Id"] = vendor_id

    reasoning = str(params.get("reason") or "Vendor update via AI assistant")
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit:
    # thread dry_run into the C2 proposer kwarg (builds, never persists).
    if dry_run:
        propose_vendor_update(db, realm_id, payload, reasoning=reasoning, dry_run=True)
        preview_fields = ", ".join(sorted(key for key in payload if key != "Id"))
        return _dry_run_notice(
            f"an UPDATE_VENDOR proposal for Vendor Id {vendor_id}; fields: {preview_fields}."
        )
    # Pin the realm GUC before the FORCE-RLS proposed_actions INSERT (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)
    action = propose_vendor_update(db, realm_id, payload, reasoning=reasoning)
    changed_fields = ", ".join(sorted(key for key in payload if key != "Id"))
    return (
        DraftedToolResult((f"Vendor update drafted (Action #{action.id}). Vendor Id {vendor_id}; "
        f"fields: {changed_fields}. This is a PENDING proposal — the user must approve it "
        f"in the Actions page before anything changes in QuickBooks."), proposal_id=action.id)
    )


def _handle_reverse_journal_entry(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """TG-3: draft a PENDING reversing-JE proposal via propose_je_reversal — the SAME
    lane the dashboard POST /actions/{id}/reverse route uses (eligibility via
    reversal_block_reason, debit/credit swap + original date via build_reversal_payload,
    two-way linkage). NEVER hand-mirrors a JE; the lane's block reason surfaces verbatim
    on ineligible targets and NO proposal is drafted."""
    from ..qbo_actions.proposer import propose_je_reversal, reversal_block_reason

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # reverse_journal_entry_proposal unchanged).
    dry_run = _dry_run_requested(params)
    try:
        action_id = int(params.get("action_id") or 0)
    except (TypeError, ValueError):
        return "Error: 'action_id' must be an integer ProposedAction id."
    if not action_id:
        return (
            "Error: 'action_id' is required — the id of the EXECUTED journal-entry "
            "proposal to reverse."
        )

    # Tenant guard: the original must belong to THIS realm (mirrors the router lane's
    # check_entity_tenant_access; realm_id here is trusted scope injected by the executor).
    original = (
        db.query(ProposedAction)
        .filter(ProposedAction.id == action_id, ProposedAction.company_id == realm_id)
        .first()
    )
    if original is None:
        return f"Error: Action #{action_id} not found for this company."

    block = reversal_block_reason(original)
    if block is not None:
        # Surface the lane's refusal text verbatim — no proposal, no hand-mirrored JE.
        return f"Error: {block[2]}"

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit:
    # thread dry_run into the C2 proposer kwarg (builds, never persists). The
    # block-reason gate above already ran, so the preview is truthful.
    if dry_run:
        propose_je_reversal(db, original, dry_run=True)
        return _dry_run_notice(
            f"a reversing journal-entry proposal for executed JE proposal #{original.id} "
            "(every line's debit/credit swapped, original date kept)."
        )
    # Pin the realm GUC before the FORCE-RLS proposed_actions INSERT (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)
    reversal = propose_je_reversal(db, original)
    return (
        DraftedToolResult((f"Reversal drafted (Action #{reversal.id}), reversing journal entry proposal "
        f"#{original.id}. Every line's debit/credit is swapped and the original date is "
        f"kept (the closed-period guard still applies). This is a PENDING proposal — the "
        f"user must approve it in the Actions page before it posts to QuickBooks."), proposal_id=reversal.id)
    )


# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections chunk C2 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
# ─────────────────────────────────────────────────────────────────────────────
# Wave-2.1 C2 (TG-1/WF-2): the four AR collections chat tools, HANDLER-lane
# self-drafters on the Wave-0 recipe. Each drafts ONE PENDING ProposedAction via
# the C1 proposers (app/qbo_actions/proposer.py) — NOTHING writes to QBO from
# chat; execution happens only after human approval through the AR executor lane
# (app/qbo_actions/ar_actions.py). Decision 2 (E9): no recipient exists anywhere
# on the reminder lane — QBO delivers to the invoice's own BillEmail. Decision 9:
# item_name is resolved to an ItemRef HERE at draft time; an unresolvable name is
# REFUSED with the candidate list — never guessed. Decision 11: the payment's
# CustomerRef comes from the invoice row, never from model input. Paired
# safety_expectation AgentEvalFixtures create_invoice_proposal /
# send_invoice_reminder_proposal / record_customer_payment_proposal /
# create_credit_memo_proposal land with chunk C3 in app/agent/evals.py.
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_item_lines(client, raw_lines: list) -> "tuple[list, str | None]":
    """Resolve each decision-9 line's item to a QBO ItemRef at DRAFT time.

    Returns ``(resolved_lines, refusal_text)``. A line carrying ``item_id`` passes
    through; a line carrying only ``item_name`` is matched EXACTLY
    (case-insensitive) against the QBO product/service list (Name or
    FullyQualifiedName). Zero or multiple exact matches → the whole draft is
    REFUSED with the candidate list (``refusal_text`` set, ``resolved_lines``
    empty) so the model corrects the name instead of Peregrine guessing an item.
    """
    items = None
    resolved = []
    for index, line in enumerate(raw_lines, start=1):  # SA-OK: bounded in-memory payload lines, not a DB query
        line = line if isinstance(line, dict) else {}
        try:
            amount = round(float(line.get("amount")), 2)
        except (TypeError, ValueError):
            return [], f"Error: line {index} 'amount' must be a number."
        item_id = str(line.get("item_id") or "").strip()
        item_name = str(line.get("item_name") or "").strip()
        if not item_id and not item_name:
            return [], f"Error: line {index} needs 'item_id' or 'item_name'."
        canonical_name = item_name
        if not item_id:
            if items is None:
                # One bulk pull for the whole draft (the _resolve_account_name pattern);
                # no user value is spliced into the QBO query string.
                raw = client.query("SELECT * FROM Item MAXRESULTS 1000")
                items = (raw or {}).get("QueryResponse", {}).get("Item", []) or []
            wanted = item_name.lower()
            exact = [
                item for item in items  # SA-OK: bounded in-memory item list from the single bulk pull above
                if str(item.get("Name") or "").strip().lower() == wanted
                or str(item.get("FullyQualifiedName") or "").strip().lower() == wanted
            ]
            if len(exact) != 1:
                partial = sorted(
                    {
                        str(item.get("Name") or "").strip()
                        for item in items  # SA-OK: bounded in-memory item list from the single bulk pull above
                        if wanted in str(item.get("Name") or "").lower()
                        or str(item.get("Name") or "").lower() in wanted
                    }
                    - {""}
                )
                candidates = partial or sorted(
                    {str(item.get("Name") or "").strip() for item in items} - {""}
                )
                shown = ", ".join(f"'{name}'" for name in candidates[:8]) or "(no items found)"
                problem = "matches multiple" if len(exact) > 1 else "does not match any"
                return [], (
                    f"Error: item_name '{item_name}' (line {index}) {problem} QuickBooks "
                    f"product/service exactly — refusing to guess. Candidates: {shown}. "
                    "Pass one of these names exactly, or the item_id."
                )
            item_id = str(exact[0].get("Id") or "").strip()
            canonical_name = str(exact[0].get("Name") or item_name).strip()
        entry: dict = {"item_id": item_id, "amount": amount}
        if canonical_name:
            entry["item_name"] = canonical_name
        if line.get("qty") is not None:
            entry["qty"] = float(line["qty"])
        if line.get("unit_price") is not None:
            entry["unit_price"] = float(line["unit_price"])
        if line.get("description"):
            entry["description"] = str(line["description"])
        resolved.append(entry)
    return resolved, None


def _load_invoice_entity(db: Session, realm_id: str, invoice_id: str) -> "tuple[dict, str | None]":
    """Read one invoice from QBO for a draft-time lookup. Returns (entity, error_text)."""
    try:
        client = get_qbo_client_from_db(db, realm_id)
        current = client.get_transaction("invoice", invoice_id)
    except Exception as e:
        return {}, _assistant_safe_error(
            "QuickBooks could not be reached to look up the invoice.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_ar_invoice_lookup_error",
        )
    entity = (current or {}).get("Invoice") or {}
    if not entity:
        return {}, f"Error: Invoice {invoice_id} was not found in QuickBooks for this company."
    return entity, None


def _handle_create_invoice(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """TG-1: draft a PENDING CREATE_INVOICE ProposedAction via propose_invoice_creation
    (decision 9 payload contract; item_name resolved to an ItemRef at draft time)."""
    from ..qbo_actions.proposer import propose_invoice_creation

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # create_invoice_proposal unchanged).
    dry_run = _dry_run_requested(params)
    customer_id = str(params.get("customer_id") or "").strip()
    if not customer_id:
        return (
            "Error: 'customer_id' is required. Use search_finance_records with "
            "entity 'customer' to find the QBO customer Id."
        )
    raw_lines = params.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        return "Error: 'lines' must be a non-empty array of invoice lines."

    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        return _assistant_safe_error(
            "QuickBooks could not be reached to resolve the invoice items.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_create_invoice_qbo_error",
        )
    resolved_lines, refusal = _resolve_item_lines(client, raw_lines)
    if refusal:
        return refusal

    txn_date = str(params.get("txn_date") or "").strip()[:10] or date.today().isoformat()
    payload: dict = {
        "CustomerRef": {"value": customer_id},
        "TxnDate": txn_date,
        "lines": resolved_lines,
    }
    if params.get("due_date"):
        payload["DueDate"] = str(params["due_date"])[:10]
    if params.get("memo"):
        payload["PrivateNote"] = str(params["memo"])
    reasoning = str(params.get("reason") or "Invoice drafted via AI assistant")

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit:
    # thread dry_run into the C2 proposer kwarg (builds, never persists). Item
    # resolution above already ran, so the preview is truthful.
    if dry_run:
        propose_invoice_creation(db, realm_id, payload, reasoning=reasoning, dry_run=True)
        preview_total = sum(float(line["amount"]) for line in resolved_lines)
        return _dry_run_notice(
            f"a CREATE_INVOICE proposal for customer {customer_id}: "
            f"{len(resolved_lines)} line(s), total ${preview_total:,.2f}, dated {txn_date}."
        )
    # Pin the realm GUC before the FORCE-RLS proposed_actions INSERT (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)
    action = propose_invoice_creation(db, realm_id, payload, reasoning=reasoning)
    total = sum(float(line["amount"]) for line in resolved_lines)
    return (
        DraftedToolResult((f"Invoice drafted (Action #{action.id}) for customer {customer_id}: "
        f"{len(resolved_lines)} line(s), total ${total:,.2f}, dated {txn_date}. This is a "
        f"PENDING proposal — the user must approve it in the Actions page before the "
        f"invoice is created in QuickBooks."), proposal_id=action.id)
    )


def _handle_send_invoice_reminder(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """WF-2: draft a PENDING SEND_INVOICE_REMINDER ProposedAction via
    propose_invoice_reminder (decisions 2/7: recipient locked — no recipient input
    exists; the executor arm throttles repeat sends per invoice)."""
    from ..qbo_actions.proposer import propose_invoice_reminder

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # send_invoice_reminder_proposal unchanged).
    dry_run = _dry_run_requested(params)
    invoice_id = str(params.get("invoice_id") or "").strip()
    if not invoice_id:
        return (
            "Error: 'invoice_id' is required. Find the open invoice via "
            "get_ar_ap_summary, run_qbo_report, or search_finance_records."
        )
    entity, error = _load_invoice_entity(db, realm_id, invoice_id)
    if error:
        return error
    balance = round(float(entity.get("Balance") or 0), 2)
    if balance <= 0:
        return (
            f"Error: Invoice {invoice_id} has no open balance — it appears fully paid, "
            "so no reminder draft was created."
        )
    customer_ref = entity.get("CustomerRef") or {}
    # Reviewer display info only — deliberately NO recipient/email key anywhere
    # (decision 2: QBO delivers to the invoice's own BillEmail).
    payload = {
        "invoice_id": invoice_id,
        "reminder_date": date.today().isoformat(),
        "CustomerRef": {
            "value": str(customer_ref.get("value") or ""),
            "name": str(customer_ref.get("name") or ""),
        },
        "DocNumber": str(entity.get("DocNumber") or ""),
        "TotalAmt": round(float(entity.get("TotalAmt") or 0), 2),
        "Balance": balance,
        "DueDate": str(entity.get("DueDate") or ""),
    }
    reasoning = str(params.get("reason") or "Invoice reminder drafted via AI assistant")

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit:
    # thread dry_run into the C2 proposer kwarg (builds, never persists). The
    # open-balance gate above already ran, so the preview is truthful.
    if dry_run:
        propose_invoice_reminder(db, realm_id, payload, reasoning=reasoning, dry_run=True)
        preview_label = payload["DocNumber"] or invoice_id
        return _dry_run_notice(
            f"a SEND_INVOICE_REMINDER proposal for invoice {preview_label} (open balance "
            f"${balance:,.2f}); after a real draft is approved, QuickBooks emails its "
            "standard reminder to the billing email already on the invoice."
        )
    # Pin the realm GUC before the FORCE-RLS proposed_actions INSERT (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)
    action = propose_invoice_reminder(db, realm_id, payload, reasoning=reasoning)
    label = payload["DocNumber"] or invoice_id
    return (
        DraftedToolResult((f"Reminder drafted (Action #{action.id}) for invoice {label} "
        f"(open balance ${balance:,.2f}). This is a PENDING proposal — nothing has been "
        f"sent. After approval in the Actions page, QuickBooks emails its standard "
        f"reminder to the billing email already on the invoice; the recipient cannot be "
        f"chosen or changed from chat."), proposal_id=action.id)
    )


def _handle_record_customer_payment(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """TG-1: draft a PENDING RECORD_CUSTOMER_PAYMENT ProposedAction via
    propose_customer_payment (decision 8; decision 11: CustomerRef comes from the
    invoice row, never from model input; the executor re-reads the open balance at
    execution time and refuses an overpayment)."""
    from ..qbo_actions.proposer import propose_customer_payment

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # record_customer_payment_proposal unchanged).
    dry_run = _dry_run_requested(params)
    invoice_id = str(params.get("invoice_id") or "").strip()
    if not invoice_id:
        return (
            "Error: 'invoice_id' is required. Find the open invoice via "
            "get_ar_ap_summary, run_qbo_report, or search_finance_records."
        )
    try:
        amount = round(float(params.get("amount")), 2)
    except (TypeError, ValueError):
        return "Error: 'amount' must be a positive number."
    if amount <= 0:
        return "Error: 'amount' must be a positive number."

    entity, error = _load_invoice_entity(db, realm_id, invoice_id)
    if error:
        return error
    customer_ref = entity.get("CustomerRef") or {}
    open_balance = round(float(entity.get("Balance") or 0), 2)

    payload: dict = {
        "invoice_id": invoice_id,
        # Decision 11: invoice-driven customer resolution — never model-supplied.
        "CustomerRef": {
            "value": str(customer_ref.get("value") or ""),
            "name": str(customer_ref.get("name") or ""),
        },
        "TotalAmt": amount,
        "TxnDate": str(params.get("payment_date") or "").strip()[:10] or date.today().isoformat(),
    }
    deposit_to = str(params.get("deposit_to_account_id") or "").strip()
    if deposit_to:
        payload["DepositToAccountRef"] = {"value": deposit_to}
    if params.get("memo"):
        payload["PrivateNote"] = str(params["memo"])
    reasoning = str(params.get("reason") or "Customer payment drafted via AI assistant")

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit:
    # thread dry_run into the C2 proposer kwarg (builds, never persists).
    if dry_run:
        propose_customer_payment(db, realm_id, payload, reasoning=reasoning, dry_run=True)
        return _dry_run_notice(
            f"a RECORD_CUSTOMER_PAYMENT proposal of ${amount:,.2f} against invoice "
            f"{entity.get('DocNumber') or invoice_id} (open balance ${open_balance:,.2f}; "
            "re-checked at approval, an overpayment is refused)."
        )
    # Pin the realm GUC before the FORCE-RLS proposed_actions INSERT (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)
    action = propose_customer_payment(db, realm_id, payload, reasoning=reasoning)
    return (
        DraftedToolResult((f"Payment drafted (Action #{action.id}): ${amount:,.2f} against invoice "
        f"{entity.get('DocNumber') or invoice_id} (open balance now ${open_balance:,.2f}). "
        f"This is a PENDING proposal — the user must approve it in the Actions page; the "
        f"open balance is re-checked at that moment and an overpayment is refused."), proposal_id=action.id)
    )


def _handle_create_credit_memo(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """TG-1: draft a PENDING CREATE_CREDIT_MEMO ProposedAction via propose_credit_memo
    (decision 9 shape, no DueDate; same draft-time item resolution as create_invoice)."""
    from ..qbo_actions.proposer import propose_credit_memo

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # create_credit_memo_proposal unchanged).
    dry_run = _dry_run_requested(params)
    customer_id = str(params.get("customer_id") or "").strip()
    if not customer_id:
        return (
            "Error: 'customer_id' is required. Use search_finance_records with "
            "entity 'customer' to find the QBO customer Id."
        )
    raw_lines = params.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        return "Error: 'lines' must be a non-empty array of credit memo lines."

    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        return _assistant_safe_error(
            "QuickBooks could not be reached to resolve the credit memo items.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_create_credit_memo_qbo_error",
        )
    resolved_lines, refusal = _resolve_item_lines(client, raw_lines)
    if refusal:
        return refusal

    txn_date = str(params.get("txn_date") or "").strip()[:10] or date.today().isoformat()
    payload: dict = {
        "CustomerRef": {"value": customer_id},
        "TxnDate": txn_date,
        "lines": resolved_lines,
    }
    if params.get("memo"):
        payload["PrivateNote"] = str(params["memo"])
    reasoning = str(params.get("reason") or "Credit memo drafted via AI assistant")

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4 dry exit:
    # thread dry_run into the C2 proposer kwarg (builds, never persists). Item
    # resolution above already ran, so the preview is truthful.
    if dry_run:
        propose_credit_memo(db, realm_id, payload, reasoning=reasoning, dry_run=True)
        preview_total = sum(float(line["amount"]) for line in resolved_lines)
        return _dry_run_notice(
            f"a CREATE_CREDIT_MEMO proposal for customer {customer_id}: "
            f"{len(resolved_lines)} line(s), total ${preview_total:,.2f}, dated {txn_date}."
        )
    # Pin the realm GUC before the FORCE-RLS proposed_actions INSERT (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)
    action = propose_credit_memo(db, realm_id, payload, reasoning=reasoning)
    total = sum(float(line["amount"]) for line in resolved_lines)
    return (
        DraftedToolResult((f"Credit memo drafted (Action #{action.id}) for customer {customer_id}: "
        f"{len(resolved_lines)} line(s), total ${total:,.2f}, dated {txn_date}. This is a "
        f"PENDING proposal — the user must approve it in the Actions page before the "
        f"credit memo is created in QuickBooks."), proposal_id=action.id)
    )


# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
# ── Wave-2.2 D (WF-3a/3b, decisions 9/10/11): the AP payment-batch chat tool, a
# HANDLER-lane self-drafter. Flow: validate → _pin_realm_write_scope → the EXISTING
# prioritization read (_prioritized_bills_payload, app/routers/ap_workflow.py) →
# propose_bill_payment_batch (app/qbo_actions/proposer.py) with the chat defaults
# (no agent_run_id, source="ai_assistant"). The proposer owns the money gates:
# Bank-account validation via the QBO client param, extant-execution exclusions
# with named reasons (the UNIQUE(company_id, bill_id) ledger's read-side), the
# AP_AGENT_BATCH_MAX_TOTAL draft cap, and check-only payment_method. NOTHING pays
# a bill from chat — execution is human-approval-gated through the BILL_PAY
# executor arm (app/qbo_actions/ap_actions.py). Decision 9: no recipient/vendor
# override exists anywhere on this lane. Paired safety_expectation
# AgentEvalFixture propose_payment_batch_proposal lives in app/agent/evals.py.
# ─────────────────────────────────────────────────────────────────────────────

_PAYMENT_BATCH_STRATEGIES = ("due_date", "discount", "cash_constrained")


def _handle_propose_payment_batch(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """WF-3a/3b: draft a PENDING BILL_PAY payment-batch ProposedAction via
    propose_bill_payment_batch, fed by the existing AP prioritization."""
    from ..qbo_actions.proposer import propose_bill_payment_batch
    from ..routers.ap_workflow import _prioritized_bills_payload

    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: pop the
    # injected _dry_run flag at entry (safety_expectation AgentEvalFixture
    # propose_payment_batch_proposal unchanged).
    dry_run = _dry_run_requested(params)
    payment_account_id = str(params.get("payment_account_id") or "").strip()
    if not payment_account_id:
        return (
            "Error: 'payment_account_id' is required — the QBO Bank account the checks "
            "draw on. Resolve it with search_chart_of_accounts."
        )
    txn_date = str(params.get("txn_date") or "").strip()[:10]
    if not txn_date:
        return "Error: 'txn_date' is required (YYYY-MM-DD) — the payment date for the batch."

    strategy = str(params.get("strategy") or "due_date").strip().lower()
    if strategy not in _PAYMENT_BATCH_STRATEGIES:
        return (
            f"Error: 'strategy' must be one of {', '.join(_PAYMENT_BATCH_STRATEGIES)}; "
            f"got '{strategy}'."
        )
    available_cash = params.get("available_cash")
    if available_cash is not None:
        try:
            available_cash = float(available_cash)
        except (TypeError, ValueError):
            return "Error: 'available_cash' must be a number."
    try:
        max_bills = int(params.get("max_bills") or 25)
    except (TypeError, ValueError):
        return "Error: 'max_bills' must be an integer."
    max_bills = max(1, min(max_bills, 50))

    # Pin the realm GUC before the FORCE-RLS proposed_actions INSERT (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)

    try:
        prioritized = _prioritized_bills_payload(
            db=db,
            realm_id=realm_id,
            strategy=strategy,
            available_cash=available_cash,
            max_bills=max_bills,
        )
    except Exception as e:
        return _assistant_safe_error(
            "QuickBooks could not be reached to load the open bills.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_propose_payment_batch_qbo_error",
        )
    bills = list(prioritized.get("bills") or [])
    if strategy == "cash_constrained" and available_cash:
        # The prioritization marks which bills fit the cash budget; only those enter the draft.
        bills = [bill for bill in bills if bill.get("included", True)]  # SA-OK: dict.get on the in-memory prioritized bill list, not an ORM query
    if not bills:
        return (
            "No open bills matched the prioritization — there is nothing to draft into a "
            "payment batch."
        )

    payload: dict = {
        "payment_account_id": payment_account_id,
        "TxnDate": txn_date,
        "payment_method": "check",
        "bills": bills,
    }
    if params.get("batch_name"):
        payload["batch_name"] = str(params["batch_name"])
    reasoning = str(params.get("reason") or "Payment batch drafted via AI assistant")

    try:
        qbo = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        return _assistant_safe_error(
            "QuickBooks could not be reached to validate the payment account.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_propose_payment_batch_qbo_error",
        )
    try:
        # Chat defaults: no agent_run_id, source="ai_assistant" (the proposer's defaults).
        # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: thread
        # dry_run into the C2 proposer kwarg. The proposer's money gates (bank-account
        # validation, extant-execution exclusions, the draft cap) run either way; a
        # dry call builds the batch without persisting, so the preview names the SAME
        # included/excluded bills a real draft would carry.
        if dry_run:
            action = propose_bill_payment_batch(db, qbo, realm_id, payload, reasoning=reasoning, dry_run=True)
        else:
            action = propose_bill_payment_batch(db, qbo, realm_id, payload, reasoning=reasoning)
    except ValueError as e:
        # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches D (marker
        # RATIFICATION-2026-07-13.md) — orchestrator wave-gate fix: route the proposer's
        # draft-time refusals (Bank-account validation, the draft cap, all-excluded/
        # zero-bill batches, bad dates) through the house safe-error wrapper. str(e) is
        # OUR proposer's crafted refusal text (user-safe by construction), passed as the
        # user_message so the honest named reason survives; the wrapper logs the cause
        # and stamps a trace id — satisfying the raw-exception-return static guard.
        return _assistant_safe_error(
            str(e),
            e,
            error_code="payment_batch_draft_refused",
            retryable=False,
            log_context="assistant_payment_batch_draft_refused",
        )

    drafted = action.payload or {}
    included = drafted.get("bills") or []
    excluded = drafted.get("excluded_bills") or []
    total = float(drafted.get("total_amount") or 0)
    # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 4: the dry
    # reply is the truthful would-draft summary with NO proposal id.
    if dry_run:
        text = _dry_run_notice(
            f"a BILL_PAY payment-batch proposal: {len(included)} bill(s) totaling "
            f"${total:,.2f}, paid by check from account {payment_account_id} on {txn_date}."
        )
    else:
        text = (
            f"Payment batch drafted (Action #{action.id}): {len(included)} bill(s) totaling "
            f"${total:,.2f}, paid by check from account {payment_account_id} on {txn_date}. "
            f"This is a PENDING proposal — no bill is paid until the user approves it in the "
            f"Actions page."
        )
    if excluded:
        details = "; ".join(
            f"bill {entry.get('bill_id')}: {entry.get('reason')}" for entry in excluded  # SA-OK: dict.get on the proposer's in-memory exclusion list, not an ORM query
        )
        text += (
            f" Excluded bill(s) ({len(excluded)}) — already covered by an extant payment "
            f"execution: {details}."
        )
    return text if dry_run else DraftedToolResult(text, proposal_id=action.id)


# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent).
# ─────────────────────────────────────────────────────────────────────────────
# Wave-2.5 B (RK-3c, decisions 5/6/7): the CECL allowance chat tool — ONE
# HANDLER-lane self-drafter whose injected `_dry_run` IS the estimate-only read.
# NO account params (explicit mapping only, PUT /cecl-config — never keyword
# inference) and NO amount params (the engine computes). The draft is EXACTLY
# what the engine computed: build_je_lines output, the full CeclEstimate stamped
# into the payload as `computation_evidence` PRE-insert (decision 6), and the
# memo linkage stamped POST-insert (decision 5/E4). Methodology is
# DRAFTED-FOR-CPA-REVIEW; every JE remains human-approval-gated. Paired
# safety_expectation AgentEvalFixture propose_allowance_je_proposal
# (app/agent/evals.py, AP-T09 lockstep).
# ─────────────────────────────────────────────────────────────────────────────

_CECL_SETUP_GUIDANCE = (
    "To enable the current-allowance comparison and drafting, an EDITOR must map the "
    "Allowance for Credit Losses account and the Bad Debt Expense account via "
    "PUT /cecl-config (GET /cecl-config suggests Classification-filtered candidates "
    "from the chart of accounts). The mapping is explicit — Peregrine never infers a "
    "contra-asset account from account names."
)


def _cecl_evidence_text(estimate: dict) -> str:
    """Render the full CeclEstimate evidence block as chat text (decision 4 shape).

    Δ / direction lines appear ONLY when the engine computed them — an unmapped
    company's estimate honestly shows the required allowance WITHOUT Δ.
    """
    lines = [
        f"Method: {estimate['method']} (drafted for CPA review).",
        f"As of {estimate['as_of']}; basis: {estimate['basis']}; "
        f"window {estimate['window_months']} months; maturity {estimate['maturity_days']} days; "
        f"min {estimate['min_bucket_invoices']} mature invoices per bucket.",
        "Aging buckets (balance x loss rate -> expected loss):",
    ]
    for row in estimate["bucket_rows"]:  # SA-OK: bounded-loop over the 5 in-memory aging buckets, no DB query
        rate = f"{row['loss_rate']:.2%}" if row["loss_rate"] is not None else "no estimable rate"
        expected = f"${row['expected_loss']:,.2f}" if row["expected_loss"] is not None else "not estimable"
        lines.append(
            f"  {row['bucket']}: ${row['balance']:,.2f} x {rate} "
            f"({row['invoice_count']} mature invoices) -> {expected}"
        )
    required_note = " (PARTIAL — see unestimated buckets)" if estimate["required_allowance_partial"] else ""
    lines.append(f"Total AR: ${estimate['total_ar']:,.2f}.")
    lines.append(f"Required allowance: ${estimate['required_allowance']:,.2f}{required_note}.")
    if estimate["current_allowance"] is not None:
        lines.append(
            f"Current allowance (account {estimate['allowance_account_id']}): "
            f"${estimate['current_allowance']:,.2f} "
            f"(QBO CurrentBalance {estimate['qbo_current_balance']:,.2f} — credit balance reads negative; sign pinned)."
        )
        if estimate["delta"] is not None:
            lines.append(
                f"Adjustment Δ: ${estimate['delta']:,.2f} ({estimate['direction']}); "
                f"de-minimis threshold ${estimate['de_minimis_delta']:,.2f}."
            )
    else:
        lines.append(
            "Current allowance: unavailable — no allowance account is mapped, so Δ is not computed."
        )
    if estimate["unestimated_buckets"]:
        lines.append("Unestimated buckets (insufficient history): " + ", ".join(estimate["unestimated_buckets"]) + ".")
    if estimate["anomalies"]:
        lines.append("Anomalies: " + " | ".join(estimate["anomalies"]))
    lines.append("Disclosed method limits:")
    for limit in estimate["disclosed_limits"]:  # SA-OK: bounded-loop over the fixed in-memory disclosure tuple, no DB query
        lines.append(f"  - {limit}")
    return "\n".join(lines)


def _handle_propose_allowance_je(db: Session, realm_id: str, params: dict) -> str | DraftedToolResult:
    """RK-3c: estimate the CECL allowance and draft the adjustment JE via the
    EXTENDED propose_journal_entry (evidence stamped pre-insert, memo linkage
    post-insert). `_dry_run` is the estimate-only read; refusals are honest
    (unmapped-with-setup-instructions / D2 partial-evidence naming buckets /
    de-minimis / a failed allowance read REFUSES — never a fabricated $0)."""
    from ..analysis.ar_aging import ar_aging
    from ..analysis.cecl_allowance import (
        CeclReadError,
        build_je_lines,
        compute_cecl_allowance,
        read_allowance_balance,
    )
    from ..analysis.payment_behavior import derive_collection_haircuts
    from ..qbo_actions.proposer import propose_journal_entry
    from ..services.cecl_config import get_cecl_config_service

    # Pop the injected _dry_run at ENTRY (the decision-4 census contract).
    dry_run = _dry_run_requested(params)

    as_of_raw = str(params.get("as_of") or "").strip()
    as_of = None
    if as_of_raw:
        try:
            as_of = date.fromisoformat(as_of_raw[:10])
        except ValueError:
            return "Error: 'as_of' must be a date in YYYY-MM-DD format."

    # Pin the realm GUC first: the CompanyCeclConfig read AND the (non-dry)
    # proposed_actions INSERT are realm-keyed under FORCE RLS (JE-handler pattern).
    _pin_realm_write_scope(db, realm_id)
    mapping = get_cecl_config_service(db, realm_id)

    try:
        client = get_qbo_client_from_db(db, realm_id)
    except Exception as e:
        return _assistant_safe_error(
            "QuickBooks could not be reached for the CECL allowance estimate.",
            e,
            error_code="assistant_qbo_connection_unavailable",
            log_context="assistant_cecl_qbo_error",
        )

    try:
        haircuts = derive_collection_haircuts(client)
        aging = ar_aging(client, as_of_date=as_of)
    except Exception as e:
        return _assistant_safe_error(
            "The collection history / AR aging needed for the CECL estimate could not be "
            "loaded from QuickBooks.",
            e,
            error_code="assistant_cecl_inputs_unavailable",
            log_context="assistant_cecl_inputs_error",
        )

    allowance = None
    if mapping["configured"]:
        try:
            allowance = read_allowance_balance(client, str(mapping["allowance_account_id"]))
        except CeclReadError as e:
            # Refusal, never a fabricated $0 (a $0 default would draft a
            # full-required-allowance JE). str(e) is the engine's crafted
            # user-safe refusal text (the payment-batch refusal precedent).
            logger.info("[CECL] draft refusal realm=%s reasons=allowance_read_failed", realm_id)
            return _assistant_safe_error(
                str(e),
                e,
                error_code="cecl_allowance_read_failed",
                log_context="assistant_cecl_allowance_read_refused",
            )

    estimate = compute_cecl_allowance(haircuts=haircuts, aging=aging, allowance=allowance)
    evidence_text = _cecl_evidence_text(estimate)
    unmapped = not mapping["configured"]

    if dry_run:
        # The estimate-only read (decision 5): UNMAPPED returns the estimate
        # WITHOUT Δ plus setup guidance — never a flat refusal; mapped returns
        # the full evidence picture whether or not a draft would be refused.
        if estimate["can_draft"]:
            return (
                _dry_run_notice(
                    f"a POST_JE CECL allowance {estimate['direction']} of "
                    f"${abs(estimate['delta']):,.2f} (required "
                    f"${estimate['required_allowance']:,.2f} vs current "
                    f"${estimate['current_allowance']:,.2f})."
                )
                + "\n\n" + evidence_text
            )
        reasons = "; ".join(estimate["draft_refusals"]) or "nothing to adjust"
        text = (
            "DRY RUN — no proposal was drafted and nothing was saved. Estimate only — "
            f"drafting is unavailable: {reasons}."
        )
        if unmapped:
            text += f" {_CECL_SETUP_GUIDANCE}"
        return text + "\n\n" + evidence_text

    if not estimate["can_draft"]:
        # The refusal-frequency log line (decision 5 — the D2-revisit needs data).
        logger.info(
            "[CECL] draft refusal realm=%s count=%d reasons=%s",
            realm_id, len(estimate["draft_refusals"]), "; ".join(estimate["draft_refusals"]),
        )
        reasons = "; ".join(estimate["draft_refusals"]) or "nothing to adjust"
        text = f"No CECL adjustment was drafted — {reasons}."
        if unmapped:
            text += f" {_CECL_SETUP_GUIDANCE}"
        return text + "\n\n" + evidence_text

    lines = build_je_lines(
        estimate,
        allowance_account_id=str(mapping["allowance_account_id"]),
        bad_debt_account_id=str(mapping["bad_debt_account_id"]),
    )
    payload = {
        "date": estimate["as_of"] or date.today().isoformat(),
        "memo": "CECL allowance adjustment — aging-bucket historical loss-rate estimate (drafted for CPA review)",
        "lines": lines,
    }
    user_reason = str(params.get("reason") or "").strip()
    reasoning = (
        f"CECL allowance {estimate['direction']}: required ${estimate['required_allowance']:,.2f} "
        f"vs current ${estimate['current_allowance']:,.2f} -> Δ ${estimate['delta']:,.2f}. "
        f"Method: aging-bucket historical loss rates from the company's own collection history "
        f"(basis: {estimate['basis']}); methodology drafted for CPA review."
    )
    if user_reason:
        reasoning = f"{reasoning} Requested because: {user_reason}"

    # The EXTENDED proposer (decisions 5/6): the full deterministic evidence block
    # is stamped into the payload PRE-insert (it folds into the dedup fingerprint
    # harmlessly), and the memo linkage lands POST-insert (memo maps to QBO
    # PrivateNote on the simplified shape — the reconcile probe's match key).
    action = propose_journal_entry(
        db,
        realm_id,
        payload,
        reasoning=reasoning,
        confidence_score=0.85,
        computation_evidence=dict(estimate),
        stamp_memo_linkage=True,
    )
    amount = abs(estimate["delta"])
    return (
        DraftedToolResult((f"CECL allowance {estimate['direction']} drafted (Action #{action.id}): "
        f"${amount:,.2f} — {'debit Bad Debt Expense / credit the Allowance' if estimate['direction'] == 'increase' else 'debit the Allowance / credit Bad Debt Expense'} "
        f"dated {payload['date']}. This is a PENDING proposal — the user must approve it in "
        f"the Actions page before it posts to QuickBooks. The full computation evidence rides "
        f"on the proposal (drafted for CPA review).\n\n{evidence_text}"), proposal_id=action.id)
    )


TOOL_HANDLERS = {
    "create_journal_entry": _handle_create_journal_entry,
    "reclassify_transaction": _handle_reclassify_transaction,
    "update_budget_line": _handle_update_budget_line,
    "create_close_task": _handle_create_close_task,
    "add_covenant": _handle_add_covenant,
    "search_chart_of_accounts": _handle_search_coa,
    "search_finance_records": _handle_search_finance_records,
    "get_financial_data": _handle_get_financial_data,
    "get_budget_data": _handle_get_budget_data,
    "compare_periods": _handle_compare_periods,
    "run_qbo_report": _handle_run_qbo_report,
    "search_documents": _handle_search_documents,
    "search_agreements": _handle_search_agreements,
    "get_agreement_context": _handle_get_agreement_context,
    "get_module_context": _handle_get_module_context,
    "search_asc_guidance": _handle_search_asc_guidance,
    "search_financial_expertise": _handle_search_financial_expertise,
    "get_schedule_data": _handle_get_schedule_data,
    # PLAN-EXEMPT: chunk C3 of ratified plan 20260714-wave2-7b-lease-extraction (marker
    # RATIFICATION-2026-07-14.md) — READ-only lease detail lookup (READ_ONLY_TOOLS lane).
    "get_lease_detail": _handle_get_lease_detail,
    "add_schedule_row": _handle_add_schedule_row,
    "update_schedule_row": _handle_update_schedule_row,
    "update_schedule_month_values": _handle_update_schedule_month_values,
    "bulk_set_budget_drivers": _handle_bulk_set_budget_drivers,
    "set_budget_months": _handle_set_budget_months,
    "analyze_spending_trends": _handle_analyze_spending_trends,
    "analyze_profitability": _handle_analyze_profitability,
    "investigate_account": _handle_investigate_account,
    "generate_report": _handle_generate_report,
    "get_close_status": _handle_get_close_status,
    "complete_close_task": _handle_complete_close_task,
    "get_ar_ap_summary": _handle_get_ar_ap_summary,
    "get_executive_briefing": _handle_get_executive_briefing,
    "delete_schedule_row": _handle_delete_schedule_row,
    "get_cashflow_snapshot": _handle_get_cashflow_snapshot,
    "update_cashflow_cells": _handle_update_cashflow_cells,
    "layer_cashflow_schedule": _handle_layer_cashflow_schedule,
    "spread_cashflow_balance": _handle_spread_cashflow_balance,
    "analyze_cashflow_position": _handle_analyze_cashflow_position,
    "get_cashflow_actuals_review": _handle_get_cashflow_actuals_review,
    "categorize_cashflow_actuals": _handle_categorize_cashflow_actuals,
    "teach_peregrine": _handle_teach_peregrine,
    "create_macro_scenario": _handle_create_macro_scenario,
    "analyze_lbo_balance": _handle_analyze_lbo_balance,
    "get_lbo_sensitivity": _handle_get_lbo_sensitivity,             # READ (clean) — LBO sensitivity grid
    "propose_lbo_assumptions": _handle_propose_lbo_assumptions,     # WRITE (proposal-gated at approval) — LBO_ASSUMPTIONS_UPDATE
    # Phase-A read-parity tools (C2-a)
    "get_covenants_status": _handle_get_covenants_status,
    "get_weekly_review": _handle_get_weekly_review,
    "get_budget_vs_actuals": _handle_get_budget_vs_actuals,
    "get_intelligence_snapshot": _handle_get_intelligence_snapshot,
    # C3-TOOLS wave 3.1 (rows 1-13, LOWER-RISK batch): 3 recompute-only READS + 7 proposal-gated writes.
    "refresh_ar_ap_snapshot": _handle_refresh_ar_ap_snapshot,       # row 1 READ
    "refresh_cashflow_forecast": _handle_refresh_cashflow_forecast,  # row 2 READ
    "refresh_covenant_status": _handle_refresh_covenant_status,      # row 3 READ
    "create_budget_line": _handle_create_budget_line,                # row 4 WRITE
    "create_custom_kpi": _handle_create_custom_kpi,                  # row 5a WRITE
    "update_custom_kpi": _handle_update_custom_kpi,                  # row 5b WRITE
    "update_close_task_state": _handle_update_close_task_state,      # row 6 WRITE
    "dismiss_alert": _handle_dismiss_alert,                          # row 10a WRITE
    "acknowledge_watchdog": _handle_acknowledge_watchdog,            # row 10b WRITE
    "recon_confirm_match": _handle_recon_confirm_match,              # row 12a WRITE
    "recon_unconfirm": _handle_recon_unconfirm,                      # row 12b WRITE
    # C3-TOOLS wave 3.1c (rows 7,8,9,11,13, EXTRACTED-SERVICE batch): 5 proposal-gated writes whose
    # approval-time handler calls the frozen service (rows 9/11 resolve the ORM entity by id first).
    "generate_close_tasks": _handle_generate_close_tasks,           # row 7  WRITE
    "create_reforecast": _handle_create_reforecast,                 # row 8  WRITE
    "update_budget_driver": _handle_update_budget_driver,           # row 9  WRITE
    "update_lease": _handle_update_lease,                           # row 11 WRITE
    # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker RATIFICATION-2026-07-14.md)
    "create_lease": _handle_create_lease,                           # LEASE_CREATE WRITE (TG-5 create half)
    # PLAN-EXEMPT: chunk 5 of ratified plan 20260714-wave3-2-asc606-engine (marker RATIFICATION-2026-07-14.md)
    "create_revenue_contract": _handle_create_revenue_contract,     # REVENUE_CONTRACT_CREATE WRITE (ASC 606 create)
    "apply_budget_drivers": _handle_apply_budget_drivers,           # row 13 WRITE
    # C3-TOOLS wave 3.2b (rows 14-17, HIGH-risk batch: breach-detection / ASC-842 statement impact).
    # Rows 14-16 are internal-DB proposal writes whose handler runs at APPROVAL time (resolves the
    # entity by id under realm scope + calls the frozen service). Row 17 dispose_schedule is a
    # HANDLER-lane write whose handler runs at CHAT time and self-drafts a PENDING JE proposal.
    "update_covenant": _handle_update_covenant,                     # row 14  WRITE
    "update_ebitda_config": _handle_update_ebitda_config,           # row 15  WRITE
    "activate_lease": _handle_activate_lease,                       # row 16a WRITE
    "remeasure_lease": _handle_remeasure_lease,                     # row 16b WRITE
    "terminate_lease": _handle_terminate_lease,                     # row 16c WRITE
    "dispose_schedule": _handle_dispose_schedule,                   # row 17  WRITE (handler lane)
    # C3-DELETES wave 3.3b (row 18): the 8 DESTRUCTIVE single-entity delete handlers. Each runs
    # ONLY at approval (resolved via <ENTITY>_DELETE in ACTION_TYPE_TO_TOOL) and re-checks
    # existence + tenant ownership under the pinned realm before deleting. safety_expectation:
    # proposal_only_for_financial_mutation (app/agent/evals.py delete_*_proposal fixtures).
    "delete_budget_line": _handle_delete_budget_line,               # → BUDGET_LINE_DELETE
    "delete_budget_driver": _handle_delete_budget_driver,           # → BUDGET_DRIVER_DELETE
    "delete_close_task": _handle_delete_close_task,                 # → CLOSE_TASK_DELETE
    "delete_covenant": _handle_delete_covenant,                     # → COVENANT_DELETE
    "delete_custom_kpi": _handle_delete_custom_kpi,                 # → KPI_DELETE
    "delete_cashflow_override": _handle_delete_cashflow_override,   # → CASHFLOW_OVERRIDE_DELETE
    "delete_document": _handle_delete_document,                     # → DOCUMENT_DELETE
    "delete_knowledge": _handle_delete_knowledge,                   # → KNOWLEDGE_DELETE
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C1 (marker on disk;
    # active-plan.json sidecar absent in this worktree). Wave-0 C1 (TG-2/TG-3): HANDLER-lane
    # self-drafters onto the existing QBO proposal lanes (app/qbo_actions/proposer.py) —
    # chat-time drafting of a PENDING ProposedAction only, executed by the QBO executor at
    # approval time. Paired safety_expectation AgentEvalFixtures update_vendor_proposal /
    # reverse_journal_entry_proposal (app/agent/evals.py, safety_expectation
    # "proposal_only_for_qbo_write") prove the proposal-only routing behaviorally.
    "update_vendor": _handle_update_vendor,                         # TG-2 WRITE (handler lane → UPDATE_VENDOR)
    "reverse_journal_entry": _handle_reverse_journal_entry,         # TG-3 WRITE (handler lane → POST_JE reversal)
    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C2 (marker on disk;
    # active-plan.json sidecar absent in this dispatched session). Wave-2.1 C2 (TG-1/WF-2):
    # HANDLER-lane self-drafters onto the C1 AR proposal lanes (app/qbo_actions/proposer.py) —
    # chat-time drafting of a PENDING ProposedAction only, executed by the AR executor lane
    # (app/qbo_actions/ar_actions.py) at approval time. Paired safety_expectation
    # AgentEvalFixtures create_invoice_proposal / send_invoice_reminder_proposal /
    # record_customer_payment_proposal / create_credit_memo_proposal land with chunk C3
    # (app/agent/evals.py, safety_expectation "proposal_only_for_qbo_write").
    "create_invoice": _handle_create_invoice,                       # TG-1 WRITE (handler lane → CREATE_INVOICE)
    "send_invoice_reminder": _handle_send_invoice_reminder,         # WF-2 WRITE (handler lane → SEND_INVOICE_REMINDER)
    "record_customer_payment": _handle_record_customer_payment,     # TG-1 WRITE (handler lane → RECORD_CUSTOMER_PAYMENT)
    "create_credit_memo": _handle_create_credit_memo,               # TG-1 WRITE (handler lane → CREATE_CREDIT_MEMO)
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker on disk;
    # active-plan.json sidecar absent in this dispatched session). Wave-2.2 D
    # (WF-3a/3b): HANDLER-lane self-drafter onto the chunk-A/C BILL_PAY lane
    # (app/qbo_actions/proposer.py → app/qbo_actions/ap_actions.py at approval time).
    # Paired safety_expectation AgentEvalFixture propose_payment_batch_proposal
    # (app/agent/evals.py, safety_expectation "proposal_only_for_qbo_write").
    "propose_payment_batch": _handle_propose_payment_batch,         # WF-3a/3b WRITE (handler lane → BILL_PAY)
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.3 (TG-4/WF-4, decision 3
    # TWO-NAME architecture): the two VISIBLE tools are HANDLER-lane self-drafters
    # whose handlers run at CHAT time (guards / suggest engine, then draft ONE
    # PENDING ProposedAction with payload["tool_name"] = the exec name); the two
    # *_exec tools are HIDDEN approval-time executors (in no bundle +
    # MODEL_HIDDEN_TOOLS) resolved via RECON_SESSION_OPEN / RECON_BULK_CONFIRM in
    # ACTION_TYPE_TO_TOOL. Paired safety_expectation AgentEvalFixtures
    # open_recon_session_proposal / propose_bulk_matches_proposal /
    # recon_session_open_exec_fixture / recon_bulk_confirm_exec_fixture
    # (app/agent/evals.py, AP-T09 lockstep).
    "open_recon_session": _handle_open_recon_session,               # TG-4 WRITE (handler lane → RECON_SESSION_OPEN)
    "propose_bulk_matches": _handle_propose_bulk_matches,           # WF-4 WRITE (handler lane → RECON_BULK_CONFIRM)
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.5 B (RK-3c, decision 5):
    # HANDLER-lane self-drafter onto the EXTENDED propose_journal_entry lane
    # (app/qbo_actions/proposer.py) — chat-time drafting of ONE PENDING POST_JE
    # only, executed by the standard JE executor arm at approval time. Paired
    # safety_expectation AgentEvalFixture propose_allowance_je_proposal
    # (app/agent/evals.py, safety_expectation "proposal_only_for_qbo_write").
    "propose_allowance_je": _handle_propose_allowance_je,           # RK-3c WRITE (handler lane → POST_JE, CECL engine)
    "recon_session_open_exec": _handle_recon_session_open_exec,     # TG-4 EXEC-ONLY (approval time)
    "recon_bulk_confirm_exec": _handle_recon_bulk_confirm_exec,     # WF-4 EXEC-ONLY (approval time, E5 steal-guard)
    "list_unmatched_items": _handle_list_unmatched_items,           # TG-4 READ (stable id-cursor)
    "get_recon_tieout": _handle_get_recon_tieout,                   # WF-4 READ (one-BS-fetch tie-out)
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 3, E1 — the two-name split): the visible
    # self-drafter runs at CHAT time (validate → persist DRAFT → draft the AGENT_PLAN
    # proposal with payload["tool_name"] = the exec name); the *_exec tool is the HIDDEN
    # approval-time executor (in no bundle + MODEL_HIDDEN_TOOLS) resolved via AGENT_PLAN in
    # ACTION_TYPE_TO_TOOL; get_agent_plan_status is a READ. Paired safety_expectation
    # AgentEvalFixtures propose_agent_plan_proposal / agent_plan_activate_exec_fixture
    # (app/agent/evals.py, AP-T09 lockstep).
    "propose_agent_plan": _handle_propose_agent_plan,               # WF-12 WRITE (handler lane → AGENT_PLAN)
    "agent_plan_activate_exec": _handle_agent_plan_activate_exec,   # WF-12 EXEC-ONLY (approval time, E3 tamper seal)
    "get_agent_plan_status": _handle_get_agent_plan_status,         # WF-12 READ
}

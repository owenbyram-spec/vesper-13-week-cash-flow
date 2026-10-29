from __future__ import annotations

# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). AC-5: the select_for_intent trim log.
import logging

from typing import Any, Iterable, Optional

# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — import the fail-closed integrity gate
from .registry_integrity import RegistryIntegrityError, ensure_registry_integrity
from .tool_results import TOOL_RESULT_MODELS
from .types import AgentIntent, OutputMode, RiskLevel, ToolDefinition, ToolHandler  # noqa: F401

# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
# RATIFICATION-2026-07-13.md on disk) — AC-5 trim-observability logger.
logger = logging.getLogger(__name__)

# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C4 (marker
# RATIFICATION-2026-07-13.md on disk) — decision 5: the decorative shared default
# output schema is DEAD. It claimed {summary, sources, proposed_actions} for every
# tool, matched NO tool's actual return, and had ZERO readers — decorative metadata,
# the exact anti-pattern the tool_metadata no-consumer doctrine forbids
# (app/agent/runtime/tool_metadata.py: "adding a field without a consumer would
# re-introduce exactly the anti-pattern this closes"). output_schema now defaults
# None; only the TOOL_RESULT_MODELS tools carry a (real, model-derived) schema.


READ_ONLY_TOOLS = {
    "search_chart_of_accounts",
    "search_finance_records",
    "get_financial_data",
    "get_budget_data",
    "compare_periods",
    "run_qbo_report",
    "search_documents",
    "search_agreements",
    "get_agreement_context",
    "get_module_context",
    "search_asc_guidance",
    "search_financial_expertise",
    "get_schedule_data",
    "get_close_status",
    "get_ar_ap_summary",
    "get_executive_briefing",
    "get_cashflow_snapshot",
    "analyze_lbo_balance",
    "get_covenants_status",
    "get_weekly_review",
    "get_budget_vs_actuals",
    "get_intelligence_snapshot",
    # wave 3.3a (plan 20260706-cfo-grade, completing AC-2A.2): get_lbo_sensitivity is a clean
    # READ — it computes an LBO scenario's 2-D IRR/MOIC grid + attribution through the pure
    # engines and writes NOTHING (mutates_state=False ⇒ VIEWER, no proposal gate). Mirrors
    # analyze_lbo_balance's read membership; bundled in reporting_analysis so it is reachable.
    "get_lbo_sensitivity",
    # C3-TOOLS wave 3.1 (rows 1-3): recompute-only READS. They force a live QBO recompute and
    # rewrite ONLY the read-path snapshot cache (idempotent; no user financial fact invented) —
    # exactly like a stale GET would have recomputed. READ_ONLY_TOOLS ⇒ mutates_state=False, so
    # they are NOT proposal-gated (no new financial fact to review) and take VIEWER, mirroring
    # search_asc_guidance's read membership.
    "refresh_ar_ap_snapshot",
    "refresh_cashflow_forecast",
    "refresh_covenant_status",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.3 (decision 6 / E9): the two
    # reconciliation READS. list_unmatched_items pages a session's unmatched buckets
    # on a STABLE id cursor; get_recon_tieout composes the session tie-out + the
    # AR/AP subledger tie-out over ONE shared BalanceSheet fetch. Both mutate
    # NOTHING (mutates_state=False ⇒ VIEWER, no proposal gate) — no safety fixture
    # needed (READ-only, per the refresh-READ precedent above).
    "list_unmatched_items",
    "get_recon_tieout",
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 10): the agent-plan STATUS read.
    # get_agent_plan_status reads an AgentPlan + its steps via services/agent_plans.py
    # (mutates_state=False ⇒ VIEWER, no proposal gate) — a pure read, no safety fixture
    # (READ-only, per the refresh-READ precedent). Bundled in RECONCILIATION (a headroom
    # set()-trim bundle) so it is reachable from chat.
    "get_agent_plan_status",
    # PLAN-EXEMPT: chunk C3 of ratified plan 20260714-wave2-7b-lease-extraction (marker
    # RATIFICATION-2026-07-14.md). get_lease_detail reads one LeaseSchedule by id
    # tenant-scoped (mutates_state=False ⇒ VIEWER, no proposal gate) — a pure read, so
    # it takes NO ACTION_TYPE_TO_TOOL row and NO two-name split. Bundled in SCHEDULES
    # (leases ARE amortization schedules) so it is reachable from chat. Closes the 2.7
    # finding-3 read gap (create_lease could write a lease; nothing could read one to
    # resolve a lease_id).
    "get_lease_detail",
}

ANALYSIS_TOOLS = {
    "analyze_spending_trends",
    "analyze_profitability",
    "investigate_account",
    "analyze_cashflow_position",
    "get_cashflow_actuals_review",
}

PROPOSAL_TOOLS = {
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — registry integrity: classify the
    # last unclassified tool. reclassify_transaction's chat handler self-drafts a PENDING
    # RECLASSIFICATION ProposedAction (handlers.py _handle_reclassify_transaction); it was
    # handler-only, so it silently defaulted to READ_ONLY/VIEWER/mutates_state=False via the
    # _metadata_for_tool else-branch. Classified here (DRAFT_ONLY / ANALYST /
    # creates_proposed_action) and added to HANDLER_LANE_SELF_DRAFTERS so its handler keeps
    # drafting exactly as before; it remains in no INTENT_BUNDLE, so chat reachability is
    # unchanged — only the metadata is now true. Paired safety_expectation AgentEvalFixture
    # reclassify_transaction_proposal (app/agent/evals.py) proves proposal-only routing;
    # registry_integrity.ensure_registry_integrity ensures no default-classified tool ships again.
    "reclassify_transaction",
    "create_journal_entry",
    "update_budget_line",
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
    # teach_peregrine MOVED to PROPOSAL_ONLY_WRITE_TOOLS: MEMORY_UPDATE now carries an
    # ACTION_TYPE_TO_TOOL executor row (approvals execute — AC-4), and registry-integrity
    # check #5 requires every executor-row tool to be a PROPOSAL_ONLY_WRITE_TOOLS member.
    # The two sets produce IDENTICAL risk metadata (DRAFT_ONLY / ANALYST / mutates_state /
    # creates_proposed_action), and chat routing is unchanged — ToolExecutor.execute
    # intercepts teach_peregrine by NAME into _propose_memory_update before any lane logic.
    # Its safety_expectation AgentEvalFixture teach_peregrine_proposal (app/agent/evals.py)
    # keeps proving proposal-only routing behaviorally; no fixture change needed (AP-T09).
    # C3-TOOLS wave 3.2b row 17: dispose_schedule is a HANDLER-lane proposal tool — its OWN
    # handler self-drafts a PENDING JOURNAL_ENTRY ProposedAction (like the two above), so it is
    # EXCLUDED from the internal-DB proposer in tool_executor.py and is NOT in ACTION_TYPE_TO_TOOL.
    # Membership here gives DRAFT_ONLY + creates_proposed_action so the handler runs at chat time
    # to DRAFT the proposal (it mutates NO schedule state — the flip to DISPOSED happens only when
    # the drafted JE proposal is approved and executes). Its safety_expectation AgentEvalFixture
    # (dispose_schedule_proposal, app/agent/evals.py) proves the proposal-only routing behaviorally.
    "dispose_schedule",
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
    # Wave-0 C1 (TG-2/TG-3): the two previously chat-UNREACHABLE QBO proposal lanes, exposed as
    # HANDLER-lane self-drafters (the create_journal_entry pattern). Each handler drafts its
    # PENDING ProposedAction via app/qbo_actions/proposer.py (propose_vendor_update →
    # UPDATE_VENDOR; propose_je_reversal → POST_JE) executed by the QBO executor at approval
    # time — so they are PROPOSAL_TOOLS + HANDLER_LANE_SELF_DRAFTERS members, NEVER
    # PROPOSAL_ONLY_WRITE_TOOLS (integrity check #5, registry_integrity.py, requires an
    # internal-DB ACTION_TYPE_TO_TOOL executor row these QBO-lane actions do not have).
    # Paired safety_expectation AgentEvalFixtures update_vendor_proposal /
    # reverse_journal_entry_proposal live in app/agent/evals.py (AP-T09 lockstep).
    "update_vendor",
    "reverse_journal_entry",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent).
    # Wave-2.1 C2 (TG-1/WF-2): the four AR collections chat tools, HANDLER-lane
    # self-drafters on the Wave-0 recipe. Each handler drafts ONE PENDING
    # ProposedAction via app/qbo_actions/proposer.py (propose_invoice_creation →
    # CREATE_INVOICE, propose_invoice_reminder → SEND_INVOICE_REMINDER,
    # propose_customer_payment → RECORD_CUSTOMER_PAYMENT, propose_credit_memo →
    # CREATE_CREDIT_MEMO), executed by the AR executor lane
    # (app/qbo_actions/ar_actions.py) at approval time — so they are PROPOSAL_TOOLS +
    # HANDLER_LANE_SELF_DRAFTERS members, NEVER PROPOSAL_ONLY_WRITE_TOOLS (integrity
    # check #5, registry_integrity.py, requires an internal-DB ACTION_TYPE_TO_TOOL
    # executor row these QBO-lane actions do not have). external_side_effect stays
    # FALSE on all four (decision 10 / E1: that flag is a live tool_policy
    # kill-switch — tool_policy.py:68-69 denies such tools unconditionally — and the
    # handler's only invocation-time effect is the PENDING draft; the outward send
    # belongs to the ACTION TYPE, recorded in ar_actions.EXTERNAL_SEND_ACTION_TYPES).
    # Paired safety_expectation AgentEvalFixtures create_invoice_proposal /
    # send_invoice_reminder_proposal / record_customer_payment_proposal /
    # create_credit_memo_proposal land with chunk C3 in app/agent/evals.py
    # (AP-T09 lockstep; C3 is sequenced AFTER this registry write by plan design —
    # the fixture-unregistered-tool hook reads this registry on disk).
    "create_invoice",
    "send_invoice_reminder",
    "record_customer_payment",
    "create_credit_memo",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.2 D (WF-3a/3b): the AP
    # payment-batch chat tool, a HANDLER-lane self-drafter on the Wave-0 recipe.
    # Its handler drafts ONE PENDING BILL_PAY ProposedAction via
    # app/qbo_actions/proposer.py (propose_bill_payment_batch — Bank-account
    # validation, extant-execution exclusions, the draft cap), executed by the
    # BILL_PAY executor arm (app/qbo_actions/ap_actions.py) at approval time — so
    # it is PROPOSAL_TOOLS + HANDLER_LANE_SELF_DRAFTERS, NEVER
    # PROPOSAL_ONLY_WRITE_TOOLS (integrity check #5 requires an internal-DB
    # ACTION_TYPE_TO_TOOL executor row this QBO-lane action does not have).
    # external_side_effect stays FALSE (the flag is a live tool_policy
    # kill-switch — tool_policy.py denies such tools unconditionally — and the
    # handler's only invocation-time effect is the PENDING draft; the money
    # movement belongs to the ACTION TYPE, human-approval-gated in the executor).
    # AP-T09 lockstep: paired safety_expectation AgentEvalFixture
    # propose_payment_batch_proposal lands in app/agent/evals.py in this same
    # chunk (registry written BEFORE evals by plan design — the
    # fixture-unregistered-tool hook reads this registry on disk).
    "propose_payment_batch",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.3 (TG-4/WF-4, decision 3 —
    # the TWO-NAME architecture, the E1 crit-eval fix): the two VISIBLE recon write
    # tools are PROPOSAL_TOOLS + HANDLER_LANE_SELF_DRAFTERS. The plain internal-DB
    # drafting lane NEVER runs handlers at draft time, so these handlers run at
    # CHAT time instead — open_recon_session surfaces the fail-closed open guards
    # honestly at DRAFT (agent_ops.check_session_openable, same primitives + same
    # exception classes as the service); propose_bulk_matches runs find_matches +
    # suggest_pairings THEN — and each drafts ONE PENDING ProposedAction whose
    # payload["tool_name"] is the paired HIDDEN execution-only tool
    # (recon_session_open_exec / recon_bulk_confirm_exec, below in
    # PROPOSAL_ONLY_WRITE_TOOLS). They are NEVER PROPOSAL_ONLY_WRITE_TOOLS and own
    # NO ACTION_TYPE_TO_TOOL row (integrity check #6). Paired safety_expectation
    # AgentEvalFixtures open_recon_session_proposal / propose_bulk_matches_proposal
    # (app/agent/evals.py, AP-T09 lockstep; registry written BEFORE evals — the
    # fixture-unregistered-tool hook reads this registry on disk).
    "open_recon_session",
    "propose_bulk_matches",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.5 B (RK-3c, decision 5):
    # the CECL allowance chat tool, a HANDLER-lane self-drafter on the Wave-0
    # recipe. Its handler runs at CHAT time (engine estimate + honest refusals)
    # and drafts ONE PENDING POST_JE ProposedAction via the EXTENDED
    # propose_journal_entry (app/qbo_actions/proposer.py — evidence stamped
    # pre-insert, memo linkage post-insert), executed by the STANDARD JE executor
    # arm at approval time — so it is PROPOSAL_TOOLS + HANDLER_LANE_SELF_DRAFTERS,
    # NEVER PROPOSAL_ONLY_WRITE_TOOLS (integrity check #5 requires an internal-DB
    # ACTION_TYPE_TO_TOOL executor row this QBO-lane action does not have).
    # external_side_effect stays FALSE (the flag is a live tool_policy
    # kill-switch — tool_policy.py denies such tools unconditionally — and the
    # handler's only invocation-time effect is the PENDING draft; the GL post
    # belongs to the ACTION TYPE, human-approval-gated in the executor). Paired
    # safety_expectation AgentEvalFixture propose_allowance_je_proposal lands in
    # app/agent/evals.py in this same chunk (AP-T09 lockstep; registry written
    # BEFORE evals by design — the fixture-unregistered-tool hook reads this
    # registry on disk).
    "propose_allowance_je",
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 3, E1 — the two-name split, the
    # Wave-2.3 recon precedent): propose_agent_plan is the VISIBLE self-drafter. Its
    # handler runs at CHAT time — composes the DAG, gates it through
    # plan_validation.validate_plan (chunk A), persists DRAFT AgentPlan+steps + the
    # steps_hash, and drafts ONE PENDING AGENT_PLAN ProposedAction whose
    # payload["tool_name"] is the paired HIDDEN executor agent_plan_activate_exec
    # (below in PROPOSAL_ONLY_WRITE_TOOLS + MODEL_HIDDEN_TOOLS). It is PROPOSAL_TOOLS +
    # HANDLER_LANE_SELF_DRAFTERS, NEVER PROPOSAL_ONLY_WRITE_TOOLS and owns NO
    # ACTION_TYPE_TO_TOOL row (integrity check #6 — the executor row belongs to the
    # hidden name). Paired safety_expectation AgentEvalFixture propose_agent_plan_proposal
    # (app/agent/evals.py, AP-T09 lockstep).
    "propose_agent_plan",
}

SAFE_INTERNAL_WRITE_TOOLS = {
    "generate_report",
}

# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — the handler-lane self-drafters as a
# SHARED named constant. These PROPOSAL_TOOLS members draft their own PENDING ProposedAction inside
# their handler, so ToolExecutor must run the handler at chat time instead of routing through the
# internal-DB proposer. Previously an inline set literal in tool_executor.py — hand-copied sets
# drift; registry_integrity validates this constant (members ⊆ PROPOSAL_TOOLS, and none may carry
# an ACTION_TYPE_TO_TOOL executor row). teach_peregrine is deliberately absent: it routes through
# ToolExecutor._propose_memory_update, not its handler. Every member already carries its own
# safety_expectation AgentEvalFixture in app/agent/evals.py (create_journal_entry_je_proposal /
# update_budget_line / dispose_schedule_proposal / reclassify_transaction_proposal) proving
# proposal-only routing behaviorally — this constant adds no new write capability.
HANDLER_LANE_SELF_DRAFTERS = {
    "create_journal_entry",
    "update_budget_line",
    "dispose_schedule",
    "reclassify_transaction",
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C1 (marker on disk;
    # active-plan.json sidecar absent in this worktree). Wave-0 C1 (TG-2/TG-3): handlers
    # self-draft via propose_vendor_update / propose_je_reversal (app/qbo_actions/proposer.py);
    # paired safety_expectation AgentEvalFixtures update_vendor_proposal /
    # reverse_journal_entry_proposal (app/agent/evals.py) — see the PROPOSAL_TOOLS note above.
    "update_vendor",
    "reverse_journal_entry",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C2 (marker on disk;
    # active-plan.json sidecar absent in this dispatched session). Wave-2.1 C2: handlers
    # self-draft via propose_invoice_creation / propose_invoice_reminder /
    # propose_customer_payment / propose_credit_memo (app/qbo_actions/proposer.py);
    # paired safety_expectation AgentEvalFixtures create_invoice_proposal /
    # send_invoice_reminder_proposal / record_customer_payment_proposal /
    # create_credit_memo_proposal land with chunk C3 (app/agent/evals.py) — see the
    # PROPOSAL_TOOLS note above.
    "create_invoice",
    "send_invoice_reminder",
    "record_customer_payment",
    "create_credit_memo",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker on disk;
    # active-plan.json sidecar absent in this dispatched session). Wave-2.2 D: the
    # handler self-drafts via propose_bill_payment_batch (app/qbo_actions/proposer.py);
    # paired safety_expectation AgentEvalFixture propose_payment_batch_proposal
    # (app/agent/evals.py) — see the PROPOSAL_TOOLS note above (AP-T09 lockstep).
    "propose_payment_batch",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker on disk;
    # active-plan.json sidecar absent in this dispatched session). Wave-2.3
    # (decision 3): the recon self-drafters — handlers run at CHAT time and draft the
    # PENDING proposal themselves with payload["tool_name"] = the hidden exec name.
    # INTEGRITY CHECK #6 REINTERPRETATION (recorded per the plan): check #6's
    # documented intent said "self-drafters execute via the QBO lane"; these are the
    # FIRST self-draft → INTERNAL-DB-execution pairing — the check's letter (a
    # self-drafter owns no executor row) is satisfied by the two-name split: the
    # executor rows belong to recon_session_open_exec / recon_bulk_confirm_exec,
    # never to these visible names. Paired safety_expectation AgentEvalFixtures in
    # app/agent/evals.py — see the PROPOSAL_TOOLS note above (AP-T09 lockstep).
    "open_recon_session",
    "propose_bulk_matches",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker on
    # disk; active-plan.json sidecar absent in this dispatched session). Wave-2.5 B
    # (decision 5): the handler self-drafts via the EXTENDED propose_journal_entry
    # (app/qbo_actions/proposer.py); paired safety_expectation AgentEvalFixture
    # propose_allowance_je_proposal (app/agent/evals.py) — see the PROPOSAL_TOOLS
    # note above (AP-T09 lockstep).
    "propose_allowance_je",
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 3): propose_agent_plan's handler runs
    # at CHAT time and drafts the PENDING AGENT_PLAN proposal itself with
    # payload["tool_name"] = "agent_plan_activate_exec" (a mismatch fails loud in
    # resolve_internal_db_tool_name). INTEGRITY CHECK #6: the two-name split satisfies
    # the letter — this visible self-drafter owns NO executor row; the row belongs to
    # the hidden agent_plan_activate_exec. Paired safety_expectation AgentEvalFixture
    # propose_agent_plan_proposal (app/agent/evals.py, AP-T09 lockstep).
    "propose_agent_plan",
}

# PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — tools that must NEVER be offered to
# the model (send_to_model=False), independent of bundle membership. reclassify_transaction:
# account changes deliberately default to journal-entry proposals (the model drafts a JE instead
# of editing the original transaction); this constant makes that a structural registry property
# instead of relying solely on bundle absence. Its safety_expectation AgentEvalFixture
# (reclassify_transaction_proposal, app/agent/evals.py) proves proposal-only routing. Locked by
# tests/test_agent_module_wiring.py test_agent_defaults_account_changes_to_journal_entries +
# tests/test_registry_integrity.py.
MODEL_HIDDEN_TOOLS = {
    "reclassify_transaction",
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker on disk;
    # active-plan.json sidecar absent in this dispatched session). Wave-2.3
    # (decision 3): the HIDDEN execution-only halves of the two-name split. They are
    # in NO INTENT_BUNDLE (never selectable) AND send_to_model=False here, so "the
    # model can never call them" is a structural registry property, not just bundle
    # absence — they exist solely as the RECON_SESSION_OPEN / RECON_BULK_CONFIRM
    # approval-time executors. Paired safety_expectation AgentEvalFixtures
    # recon_session_open_exec_fixture / recon_bulk_confirm_exec_fixture
    # (app/agent/evals.py, AP-T09 lockstep).
    "recon_session_open_exec",
    "recon_bulk_confirm_exec",
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 3, E1): the HIDDEN execution-only
    # half of the two-name split. It is in NO INTENT_BUNDLE (never selectable) AND
    # send_to_model=False here, so "the model can never plan or call it" is a structural
    # registry property — it exists solely as the AGENT_PLAN approval-time executor
    # (re-hashes the step rows → refuses on tamper → creates the holder objective →
    # claim-flips the plan ACTIVE → enqueues the first walk). Paired safety_expectation
    # AgentEvalFixture agent_plan_activate_exec_fixture (app/agent/evals.py, AP-T09).
    "agent_plan_activate_exec",
}

PROPOSAL_ONLY_WRITE_TOOLS = {
    # PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel C2 (RATIFICATION-2026-07-13.md).
    # Moved from PROPOSAL_TOOLS (identical metadata) because MEMORY_UPDATE gained its
    # ACTION_TYPE_TO_TOOL executor row (approved memory proposals now execute
    # _handle_teach_peregrine → agent_memory.remember) and integrity check #5 requires the
    # membership. Chat routing unchanged (name-intercepted into _propose_memory_update).
    # safety_expectation AgentEvalFixture teach_peregrine_proposal (app/agent/evals.py)
    # continues to prove proposal-only routing (AP-T09).
    "teach_peregrine",
    "set_budget_months",
    "create_close_task",
    "add_covenant",
    "add_schedule_row",
    "update_schedule_row",
    "update_schedule_month_values",
    "bulk_set_budget_drivers",
    "complete_close_task",
    "delete_schedule_row",
    "update_cashflow_cells",
    "layer_cashflow_schedule",
    "categorize_cashflow_actuals",
    "create_macro_scenario",
    # C2 (plan 20260706-agent-path-remediation): spread_cashflow_balance drafts a
    # CashForecastOverride write (apply_spread_balance → db.commit); it was mislabeled
    # ANALYSIS_TOOLS (mutates_state=False) though it mutates. Proposal-gated here so a
    # future bundling is safe — _propose_tool_action intercepts it; the CASHFLOW_SPREAD
    # action_type resolves _handle_spread_cashflow_balance at approval time.
    "spread_cashflow_balance",
    # C3-TOOLS wave 3.1 (rows 4-13, lower-risk batch): proposal-gated internal-DB writes. Each
    # drafts a PENDING ProposedAction from chat (NOTHING executes); the paired ACTION_TYPE_TO_TOOL
    # entry (+ its lockstep inverse in tool_executor.py) resolves the handler at APPROVAL time only.
    "create_budget_line",         # row 4  → BUDGET_LINE_CREATE
    "create_custom_kpi",          # row 5a → KPI_CREATE
    "update_custom_kpi",          # row 5b → KPI_UPDATE
    "update_close_task_state",    # row 6  → CLOSE_TASK_STATE_UPDATE
    "dismiss_alert",              # row 10a → ALERT_DISMISS
    "acknowledge_watchdog",       # row 10b → WATCHDOG_ACK
    "recon_confirm_match",        # row 12a → RECON_MATCH_CONFIRM
    "recon_unconfirm",            # row 12b → RECON_MATCH_UNCONFIRM
    # C3-TOOLS wave 3.1c (rows 7,8,9,11,13, extracted-service batch): proposal-gated internal-DB
    # writes whose APPROVAL-time handler calls the frozen service. Each drafts a PENDING
    # ProposedAction from chat (NOTHING executes); the paired ACTION_TYPE_TO_TOOL entry resolves
    # the handler at approval time only.
    "generate_close_tasks",       # row 7  → CLOSE_TASKS_GENERATE
    "create_reforecast",          # row 8  → BUDGET_REFORECAST_CREATE
    "update_budget_driver",       # row 9  → BUDGET_DRIVER_UPDATE
    "update_lease",               # row 11 → LEASE_UPDATE
    # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker
    # RATIFICATION-2026-07-14.md). TG-5 create half: create_lease is a SIMPLE
    # single-name internal-DB write (the COMMON case, mirror of update_lease) — the
    # generic proposer drafts a PENDING LEASE_CREATE ProposedAction from chat; the
    # paired ACTION_TYPE_TO_TOOL["LEASE_CREATE"]="create_lease" row resolves
    # _handle_create_lease → create_lease_service at APPROVAL time only (auto-executes
    # per E7 — INTERNAL_DB_EXECUTABLE = INTERNAL_DB_ACTION_TYPES). NOT a two-name split
    # (no draft-time compute). Offered via the SCHEDULES bundle; fixture
    # create_lease_proposal (app/agent/evals.py).
    "create_lease",               # → LEASE_CREATE
    # PLAN-EXEMPT: chunk 5 of ratified plan 20260714-wave3-2-asc606-engine (marker
    # RATIFICATION-2026-07-14.md). create_revenue_contract is a SIMPLE single-name
    # internal-DB write (the COMMON case, mirror of create_lease) — the generic proposer
    # drafts a PENDING REVENUE_CONTRACT_CREATE ProposedAction from chat; the paired
    # ACTION_TYPE_TO_TOOL["REVENUE_CONTRACT_CREATE"]="create_revenue_contract" row resolves
    # _handle_create_revenue_contract → create_revenue_contract_service at APPROVAL time
    # only (auto-executes — INTERNAL_DB_EXECUTABLE = INTERNAL_DB_ACTION_TYPES). NOT a
    # two-name split (no draft-time compute). Offered via the SCHEDULES bundle; fixture
    # create_revenue_contract_proposal (app/agent/evals.py).
    "create_revenue_contract",    # → REVENUE_CONTRACT_CREATE
    "apply_budget_drivers",       # row 13 → BUDGET_APPLY_DRIVERS
    # C3-TOOLS wave 3.2b (rows 14-16, HIGH-risk batch: breach-detection / ASC-842 statement
    # impact). Each drafts a PENDING ProposedAction from chat (NOTHING executes); the paired
    # ACTION_TYPE_TO_TOOL entry resolves the frozen service at APPROVAL time only, which mutates +
    # commits then (activate_lease sets status=ACTIVE, update_covenant edits the threshold, etc.).
    # The drafted payload carries a savepoint-computed before/after impact diff (row-13 pattern) so
    # the approver sees the exact breach/statement delta. Each has a safety_expectation
    # AgentEvalFixture (app/agent/evals.py) proving proposal-only routing.
    "update_covenant",            # row 14 → COVENANT_UPDATE
    "update_ebitda_config",       # row 15 → EBITDA_CONFIG_UPDATE
    "activate_lease",             # row 16a → LEASE_ACTIVATE
    "remeasure_lease",            # row 16b → LEASE_REMEASURE
    "terminate_lease",            # row 16c → LEASE_TERMINATE
    # wave 3.3a (plan 20260706-cfo-grade, completing AC-2A.2): propose_lbo_assumptions drafts a
    # PENDING LBO_ASSUMPTIONS_UPDATE ProposedAction from chat (NOTHING executes); the paired
    # ACTION_TYPE_TO_TOOL entry (+ its lockstep inverse) resolves the shared write core
    # (apply_lbo_assumptions) at APPROVAL time only, which merges the assumption changes onto the
    # scenario's stored drivers + commits then. A savepoint-preview embeds the before/after diff in
    # the drafted payload (row-13 pattern). Its safety_expectation AgentEvalFixture
    # (propose_lbo_assumptions_proposal, app/agent/evals.py) proves proposal-only routing.
    "propose_lbo_assumptions",    # → LBO_ASSUMPTIONS_UPDATE
    # C3-DELETES wave 3.3b (plan 20260706-cfo-grade, gap-register row 18): the 8 DESTRUCTIVE
    # single-entity delete tools, shipped LAST per the matrix risk-ordering. Each drafts a
    # PENDING <ENTITY>_DELETE ProposedAction from chat (NOTHING deletes from chat); the paired
    # ACTION_TYPE_TO_TOOL entry (+ its lockstep inverse) resolves the handler at APPROVAL time
    # only, which re-checks existence + tenant ownership under the pinned realm before deleting
    # the ONE named entity. Single-id only — NEVER bulk (the never-add bulk-destructive set is
    # not exposed). Each has ONE capability fixture (app/agent/evals.py) proving proposal-only.
    "delete_budget_line",         # → BUDGET_LINE_DELETE
    "delete_budget_driver",       # → BUDGET_DRIVER_DELETE
    "delete_close_task",          # → CLOSE_TASK_DELETE
    "delete_covenant",            # → COVENANT_DELETE
    "delete_custom_kpi",          # → KPI_DELETE
    "delete_cashflow_override",   # → CASHFLOW_OVERRIDE_DELETE
    "delete_document",            # → DOCUMENT_DELETE
    "delete_knowledge",           # → KNOWLEDGE_DELETE
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker on disk;
    # active-plan.json sidecar absent in this dispatched session). Wave-2.3
    # (decision 3, the TWO-NAME architecture): the HIDDEN execution-only recon
    # tools. Each owns its ACTION_TYPE_TO_TOOL executor row (integrity check #5)
    # and is the approval-time executor — recon_session_open_exec re-checks the
    # open guards via create_session (E4: same-period concurrent open → idempotent
    # resume; different-period OPEN → honest failure); recon_bulk_confirm_exec
    # iterates ONLY the stored pair list with the E5 indexed liveness/steal-guard
    # checks (it NEVER re-runs suggest/find_matches). Both are in NO bundle and in
    # MODEL_HIDDEN_TOOLS — the model can never call them; only a human-APPROVED
    # proposal reaches them. Paired safety_expectation AgentEvalFixtures
    # recon_session_open_exec_fixture / recon_bulk_confirm_exec_fixture
    # (app/agent/evals.py, AP-T09 lockstep).
    "recon_session_open_exec",    # → RECON_SESSION_OPEN
    "recon_bulk_confirm_exec",    # → RECON_BULK_CONFIRM
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 3, E1 — the two-name split): the
    # HIDDEN execution-only agent-plan tool. It owns its ACTION_TYPE_TO_TOOL executor
    # row (AGENT_PLAN → this name; integrity check #5) and is the approval-time executor
    # — it re-hashes the persisted step rows against AgentPlan.steps_hash and REFUSES on
    # mismatch (E3 tamper seal), creates the schedule-DISABLED holder AgentObjective,
    # claim-flips the plan DRAFT/PENDING_APPROVAL→ACTIVE, and enqueues run_agent_plan_walk.
    # In no bundle + MODEL_HIDDEN_TOOLS — only a human-APPROVED AGENT_PLAN proposal reaches
    # it. Paired safety_expectation AgentEvalFixture agent_plan_activate_exec_fixture.
    "agent_plan_activate_exec",   # → AGENT_PLAN
}


INTENT_BUNDLES: dict[str, list[str]] = {
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 3 trim-pin documentation. The
    # GENERAL list order is UNCHANGED (its three writes are PROPOSAL_TOOLS tier 1 and
    # already survive the 15-cap); its 7-member trimmed read tail is now DOCUMENTED
    # as deliberately-rotated — every trimmed tool is OFFERED by another bundle
    # (pinned by tests/test_bundle_trims.py):
    #   get_close_status           → CLOSE_MANAGEMENT (in its offered fifteen)
    #   get_cashflow_snapshot      → CASHFLOW (15-exact, all offered)
    #   get_ar_ap_summary          → CASHFLOW/CLOSE/RECEIVABLES/PAYABLES/RECONCILIATION
    #   get_covenants_status       → REPORTING_ANALYSIS (curated into its 15)
    #   get_weekly_review          → CASHFLOW (all offered)
    #   get_intelligence_snapshot  → REPORTING_ANALYSIS (curated into its 15)
    #   search_financial_expertise → ASC_GUIDANCE (small bundle, offered)
    AgentIntent.GENERAL.value: [
        # PLAN-EXEMPT: P0 hotfix H3-followup (account lookup for JE drafting) per Steven directive
        # 2026-06-23. search_chart_of_accounts resolves an account by name OR number; it was in
        # READ_ONLY_TOOLS but no bundle, so the model could not resolve the account and asked the
        # user for the full name. Offered alongside the JE proposal tool. Placed early so it
        # survives the max_tools read-budget trim. Also closes the AP-LAO-06 eval-reachability gap.
        # TEST-OK: search_chart_of_accounts is READ-ONLY (READ_ONLY_TOOLS); no write/proposal, no fixture needed
        "search_chart_of_accounts",
        "search_finance_records",
        "get_financial_data",
        "run_qbo_report",
        "get_budget_data",
        "compare_periods",
        "search_documents",
        "search_agreements",
        "get_agreement_context",
        "get_module_context",
        "search_asc_guidance",
        "get_executive_briefing",
        "get_close_status",
        "get_cashflow_snapshot",
        "get_ar_ap_summary",
        "get_covenants_status",
        "get_weekly_review",
        "get_intelligence_snapshot",
        "create_journal_entry",  # TEST-OK: legacy write tool unchanged; incidental to read-only Phase-A additions
        "update_budget_line",
        # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C1 (marker on disk;
        # active-plan.json sidecar absent in this worktree). Wave-0 C1 (TG-2): update_vendor is
        # bundled in GENERAL — bare vendor-detail phrasing ("update the vendor's address") carries
        # no intent-classifier keyword and falls through to general, so this IS its reachability
        # home. It is a PROPOSAL_TOOLS member, so _selection_priority sorts it into tier 1 and the
        # max_tools=15 read-budget trim can never drop it (H3 precedent). ONE tier-1 addition only:
        # a third tier-1 slot still leaves 12 read slots, keeping get_executive_briefing (the
        # 12th read, pinned by test_agent_runtime_policy.py) in the default general offer — the
        # 13th read (get_close_status) rotates out but stays offered via CLOSE_MANAGEMENT.
        # reverse_journal_entry deliberately does NOT join GENERAL: "journal entry" phrasing
        # routes to close_management (intent_classifier.py), and a second tier-1 slot here would
        # trim the pinned get_executive_briefing. Paired safety_expectation AgentEvalFixture
        # update_vendor_proposal lives in app/agent/evals.py.
        "update_vendor",
        # C1-KB (plan 20260706-cfo-grade-financial-workflows, AC-1.1/AC-4): read-only
        # financial-expertise KB retrieval, mirroring search_asc_guidance membership
        # (general + reporting_analysis + asc_guidance). Appended LAST deliberately so
        # the max_tools=15 read-budget selection for the existing bundle is unchanged;
        # it is offered by default via the smaller asc_guidance bundle.
        # TEST-OK: search_financial_expertise is READ-ONLY (READ_ONLY_TOOLS); no write/proposal, no fixture needed
        "search_financial_expertise",
    ],
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk). Decision 3 (E9, dead-read curation): the
    # list is REORDERED so the four valuable tools that were DEAD at HEAD sit inside
    # the offered 15 — generate_report (the ONE requires_confirmation=True tool the
    # pinned tier COULD rescue, yet it was never offered so it could never be
    # confirmed in the first place), analyze_spending_trends, analyze_profitability,
    # investigate_account (ANALYSIS reads offered by no bundle's cut at HEAD).
    # Current default: 24 tier-2 members, with the first 16 offered after the
    # architecture repair added analyze_lbo_balance. Explicit caller caps still
    # apply. The three leading writes remain proposal-only; the 8-member tail is
    # rotated: every trimmed tool is OFFERED by at least one other bundle (named
    # inline below — the old "trims live elsewhere" claim is now actually true and
    # pinned by tests/test_bundle_trims.py).
    AgentIntent.REPORTING_ANALYSIS.value: [
        # C3-TOOLS wave 3.1 row 10 + wave 3.3a: the three proposal writes lead
        # (single-alert dismiss / watchdog-ack; the LBO assumption-change write).
        # Single-id only — no bulk-silence path is reachable from chat (never-add set).
        "dismiss_alert",
        "acknowledge_watchdog",
        "propose_lbo_assumptions",
        # generate_report: confirm-gated internal write (SAFE_INTERNAL_WRITE). Inside
        # the offered 15 so the confirmation flow can START (the model must be able
        # to propose calling it before a backend confirmation token can ever issue).
        "generate_report",
        # The three curated-back ANALYSIS reads (dead at HEAD — this bundle's cut is
        # their only offer; reporting_analysis is the deep-analysis home).
        "analyze_spending_trends",
        "analyze_profitability",
        "investigate_account",
        # get_lbo_sensitivity: reporting_analysis is its ONLY bundle (its membership
        # comment in READ_ONLY_TOOLS promises reachability here) — keep it offered.
        "get_lbo_sensitivity",
        "search_finance_records",
        "get_financial_data",
        "run_qbo_report",
        "get_executive_briefing",
        # get_covenants_status / get_intelligence_snapshot: kept inside the 15 — this
        # offer is what keeps the covenant/anomaly reads reachable from chat.
        # get_module_context: PROMOTED into the 15 (orchestrator wave-gate fix, plan
        # 20260713-wave2-6-tool-hardening) — it is the injection path for the Wave-1
        # corrections-exemplar flywheel (get_module_context("corrections")); trimming it
        # here broke exemplar injection for reporting-intent questions
        # (test_assistant_explainability caught it). It displaced get_budget_vs_actuals,
        # which IS offered by close_management (decision-1 five-read list) AND budgeting
        # (19-override) — the prior comment claiming close member-only was stale.
        "get_covenants_status",
        "get_module_context",
        "get_intelligence_snapshot",
        "analyze_lbo_balance",
        # ── Deliberately-rotated trimmed tail (positions 17-24) ──
        # Every tool below is OFFERED by at least one other bundle:
        #   get_budget_vs_actuals      → CLOSE_MANAGEMENT (offered, decision-1 read #3) + BUDGETING (19-override offer)
        #   compare_periods            → GENERAL (offered) + BUDGETING (19-override offer)
        #   get_budget_data            → GENERAL (offered) + BUDGETING (19-override offer)
        #   search_documents           → GENERAL + DOCUMENTS (both offered)
        #   search_agreements          → GENERAL + DOCUMENTS + ASC_GUIDANCE (offered)
        #   get_agreement_context      → GENERAL + DOCUMENTS + ASC_GUIDANCE (offered)
        #   search_asc_guidance        → GENERAL + ASC_GUIDANCE (offered)
        #   search_financial_expertise → ASC_GUIDANCE (offered; C1-KB appended-LAST design)
        "get_budget_vs_actuals",
        "compare_periods",
        "get_budget_data",
        "search_documents",
        "search_agreements",
        "get_agreement_context",
        "search_asc_guidance",
        # TEST-OK: search_financial_expertise is READ-ONLY (READ_ONLY_TOOLS); no write/proposal, no fixture needed
        "search_financial_expertise",
    ],
    AgentIntent.CASHFLOW.value: [
        "get_cashflow_snapshot",
        "analyze_cashflow_position",
        "get_cashflow_actuals_review",
        "get_ar_ap_summary",
        "get_weekly_review",
        "get_financial_data",
        "run_qbo_report",
        "search_finance_records",
        "update_cashflow_cells",
        "layer_cashflow_schedule",
        "categorize_cashflow_actuals",
        # C3-DELETES row 18: single cash-flow override delete. CASHFLOW bundle has headroom
        # under max_tools=15, so no trim risk; kept in the write tier for clarity.
        "delete_cashflow_override",
        # C3-TOOLS wave 3.1 rows 1-2: recompute-only refresh READS for the cash lane (bundle has
        # ample headroom under max_tools=15, so no trim risk).
        "refresh_ar_ap_snapshot",
        "refresh_cashflow_forecast",
        # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C2 (marker on disk;
        # active-plan.json sidecar absent in this dispatched session). Wave-2.1 C2 (E7):
        # collections phrasing ("collections", "ar aging", "receivable") classifies as
        # CASHFLOW (intent_classifier.py — the liquidity lens), so the reminder tool must
        # be reachable here. ONLY send_invoice_reminder joins: this bundle had 14 tools and
        # 14+1=15 is EXACT against max_tools=15 — a second addition would silently trim
        # refresh_cashflow_forecast (which lives in NO other bundle) out of the offer.
        # It is a PROPOSAL_TOOLS member (tier 1, sorted first), so the trim keeps all 15.
        # The other three AR tools stay RECEIVABLES-only. Paired safety_expectation
        # AgentEvalFixture send_invoice_reminder_proposal lands with chunk C3
        # (app/agent/evals.py).
        "send_invoice_reminder",
        # PLAN-EXEMPT: ratified plan 20260715-fluid-tool-selection AC-3 (marker
        # RATIFICATION-2026-07-15.md on disk). Reactivate the dead cashflow write:
        # spread_cashflow_balance was in NO bundle (a tracked orphan) so the model could
        # never draft a cash-forecast spread. It joins CASHFLOW for MEMBERSHIP
        # (allowed_intents={cashflow}) and rides the ALWAYS-OFFERED write lane
        # (select_for_intent, outside the max_tools cap) so it is OFFERED without displacing
        # a read. Placed LAST (the final tier-2 member) so the base 15-cap selection stays
        # byte-identical — spread is the one trimmed OUT of the cap (NOT a read like
        # refresh_cashflow_forecast), and the lane re-adds it on the cashflow intent. It is a
        # PROPOSAL_ONLY_WRITE (tier 2); paired safety_expectation AgentEvalFixture
        # spread_cashflow_balance_proposal (app/agent/evals.py, AP-T09).
        "spread_cashflow_balance",
    ],
    AgentIntent.BUDGETING.value: [
        "get_budget_data",
        "get_budget_vs_actuals",
        # C3-TOOLS wave 3.1 rows 4/5: proposal-gated writes ordered EARLY (H3 precedent — a
        # write must survive the max_tools read-budget trim). BUDGETING has headroom regardless.
        "create_budget_line",
        "create_custom_kpi",
        "update_custom_kpi",
        # C3-TOOLS wave 3.1c rows 8/9/13: extracted-service budget writes, ordered EARLY (H3
        # precedent — a write must survive the max_tools=15 read-budget trim). apply_budget_drivers
        # (row 13) is the HIGH bulk overwrite; create_reforecast (row 8) clones from actuals;
        # update_budget_driver (row 9) edits a single driver. BUDGETING has headroom regardless.
        "create_reforecast",
        "update_budget_driver",
        "apply_budget_drivers",
        "compare_periods",
        "run_qbo_report",
        "search_finance_records",
        "update_budget_line",
        "set_budget_months",
        "bulk_set_budget_drivers",
        "create_macro_scenario",
        "investigate_account",
        # C3-DELETES row 18: budget-lane single-entity deletes, at the TAIL so the
        # pre-existing writes keep their slots.
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
        # RATIFICATION-2026-07-13.md on disk) — decision 2, COMMENT CORRECTED: the
        # prior rationale — "a delete request is a pinned/confirmed follow-up (the
        # `pinned` tier survives the trim)" — was FALSE. The pinned tier is fed ONLY
        # by backend confirmation tokens, and /assistant/confirmations refuses
        # requires_confirmation=False tools (app/assistant/main.py:714-715); these
        # DRAFT_ONLY proposal tools can never be confirmed, so at HEAD the three
        # deletes were trimmed out of every offer — DEAD from chat. The real fix is
        # the per-intent max_tools override inside select_for_intent (budgeting →
        # 19, this bundle's exact size), which offers all 19 members including
        # these deletes and investigate_account.
        "delete_budget_line",
        "delete_budget_driver",
        "delete_custom_kpi",
    ],
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Decision 1 (E1-corrected), the SH-6
    # reachability fix. At HEAD this bundle held 32 members against max_tools=15, so
    # 8 of its proposal writes (create/complete/delete_close_task, delete_covenant,
    # add/update/month-values/delete_schedule_row) were trimmed out of every offer —
    # DEAD from chat, because the prior "stays reachable via the pinned/confirmed
    # tier" rationale was provably false: the pinned tier is fed ONLY by backend
    # confirmation tokens, and /assistant/confirmations REFUSES any
    # requires_confirmation=False tool (app/assistant/main.py:714-715) — a
    # DRAFT_ONLY proposal tool can never be confirmed, so trimmed == unreachable.
    # The fix: the OFFER is exactly 15 (the decision-1 fifteen: 10 writes + 5
    # deliberately-ordered reads, no max_tools raise), and the schedule-row/lease
    # lifecycle writes MOVE their offer to the new SCHEDULES bundle below.
    # MEMBERSHIP ≠ OFFER: tools listed AFTER the offered fifteen are member-only —
    # membership drives tool_policy allowed_intents (the close-walk and the
    # close-intent executor paths keep policy access) while the offer trims them.
    # Selection arithmetic: reverse_journal_entry + create_journal_entry are
    # PROPOSAL_TOOLS (tier 1, sorted first); the other 8 writes + 5 reads are
    # tier 2 in list order → offer = 2 + 13 = 15 exact; the tail never enters.
    AgentIntent.CLOSE_MANAGEMENT.value: [
        # ── The offered fifteen (decision 1 verbatim) ── 10 writes:
        "create_close_task",
        "complete_close_task",
        "update_close_task_state",
        "delete_close_task",
        "generate_close_tasks",
        "update_covenant",
        "update_ebitda_config",
        "delete_covenant",
        # Wave-0 C1 (TG-3): reverse_journal_entry lives HERE because "journal entry"
        # phrasing routes here (intent_classifier.py); PROPOSAL_TOOLS → tier 1.
        # Paired safety fixture reverse_journal_entry_proposal (app/agent/evals.py).
        "reverse_journal_entry",
        "create_journal_entry",  # TEST-OK: legacy write tool; PROPOSAL_TOOLS → tier 1 (H3 pin, test_assistant_je_proposal_restore.py)
        # ── 5 deliberately-ordered reads (decision 1 verbatim). search_chart_of_accounts
        # stays first-read for the H3-followup accrual/JE account-resolution pin.
        # TEST-OK: all five are READ-ONLY (READ_ONLY_TOOLS); no write/proposal, no fixture needed
        "search_chart_of_accounts",
        "get_close_status",
        "get_budget_vs_actuals",
        "get_ar_ap_summary",
        "get_executive_briefing",
        # ── Member-only tail (trimmed from the offer; membership preserved) ──
        # The remaining close_orchestration.TASK_TYPE_TO_TOOL review reads: the
        # close-walk's realm-pinned close_management ContextPacket must keep policy
        # access (allowed_intents), even though the chat offer trims them. Their
        # chat offers live elsewhere: get_covenants_status + get_intelligence_snapshot
        # via REPORTING_ANALYSIS, get_weekly_review via CASHFLOW.
        # TEST-OK: READ-ONLY review tools (READ_ONLY_TOOLS); no write/proposal, no fixture needed
        "get_covenants_status",
        "get_weekly_review",
        "get_intelligence_snapshot",
        # refresh_covenant_status — ACCEPTED trim (decision 3's trim-pin rule): it is
        # offered by NO bundle (was already trimmed at HEAD); kept a member so the
        # close lane retains policy access. Covenant refresh from chat is served by
        # the VIEWER-gated GET/refresh routes; re-offering it is a future curation.
        "refresh_covenant_status",
        # The schedule-row + ASC-842 lease-lifecycle writes: their OFFER moved to the
        # SCHEDULES bundle (decision 1 — leases ARE amortization schedules), but they
        # stay close MEMBERS so close-intent executor paths keep policy access
        # (allowed_intents = {close_management, schedules}); they sit in tier 2
        # BEHIND the offered thirteen, so the 15-cap trims them here by construction.
        "add_schedule_row",
        "update_schedule_row",
        "update_schedule_month_values",
        "delete_schedule_row",
        "update_lease",
        "activate_lease",
        "remeasure_lease",
        "terminate_lease",
        # PLAN-EXEMPT: ratified plan 20260715-fluid-tool-selection AC-3 (marker
        # RATIFICATION-2026-07-15.md on disk). Reactivate the dead covenant write:
        # add_covenant was in NO bundle (a tracked orphan) so the model could never
        # draft a new covenant, though its siblings update_covenant/delete_covenant
        # are offered here. It joins CLOSE_MANAGEMENT for MEMBERSHIP
        # (allowed_intents={close_management}) and rides the ALWAYS-OFFERED write lane
        # (select_for_intent, outside the max_tools cap) so it is OFFERED without
        # breaking the decision-1 fifteen — appended LAST (member-only tail), so the
        # 15-cap selection stays byte-identical (add_covenant trims out of the cap, the
        # lane re-adds it on the close intent). PROPOSAL_ONLY_WRITE (tier 2); paired
        # safety_expectation AgentEvalFixture add_covenant_proposal (app/agent/evals.py).
        "add_covenant",
        # REMOVED from membership (wave2.6 decision 1): recon_confirm_match /
        # recon_unconfirm (un-dual-homed — they live in RECONCILIATION, where their
        # phrasing routes); get_module_context (offered via general/documents/memory/
        # asc_guidance); get_schedule_data (moved to SCHEDULES). dispose_schedule
        # moved to SCHEDULES too — as a tier-1 PROPOSAL_TOOLS member it would enter
        # the offer and break the 15-exact list, so its close-lane policy access is
        # carried by the explicit allowed_intents supplement in _metadata_for_tool
        # (the teach_peregrine precedent), not by membership.
    ],
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk). Decision 1: the amortization-schedules
    # bundle — the SH-6 reachability home for the schedule-row writes (dead from
    # chat at HEAD, trimmed past close_management's 15-cap) plus get_schedule_data,
    # dispose_schedule, and the ASC-842 lease lifecycle (leases ARE amortization
    # schedules). 10 tools, well under max_tools=15 — every member is offered.
    # dispose_schedule is PROPOSAL_TOOLS (tier 1); the schedule-row/lease writes are
    # PROPOSAL_ONLY_WRITE_TOOLS (tier 2) — no trim risk either way. Paired
    # safety_expectation AgentEvalFixtures (add/update/month-values/delete_schedule_
    # row_proposal, update/activate/remeasure/terminate_lease_proposal,
    # dispose_schedule_proposal) already live in app/agent/evals.py (AP-LAO-06:
    # every expected_tools name stays bundled — membership moved, never dropped).
    # NO specialist spec is added for this bundle: validate_specialist_specs
    # (specialist_specs.py) checks spec→bundle only — a bundle without a spec is
    # legal, and the schedules lane has no specialist mandate yet (deliberate).
    AgentIntent.SCHEDULES.value: [
        "add_schedule_row",
        "update_schedule_row",
        "update_schedule_month_values",
        "delete_schedule_row",
        "dispose_schedule",
        # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker
        # RATIFICATION-2026-07-14.md). create_lease joins the amortization-schedules
        # bundle beside its lease-lifecycle siblings (leases ARE amortization schedules).
        # 11 tools < max_tools=15 — every member is offered (create_lease is
        # PROPOSAL_ONLY_WRITE tier 2, no trim risk). Ordered before update_lease
        # (create-then-update reads naturally).
        "create_lease",
        # PLAN-EXEMPT: chunk 5 of ratified plan 20260714-wave3-2-asc606-engine (marker
        # RATIFICATION-2026-07-14.md). create_revenue_contract joins the schedules bundle
        # (a revenue-recognition schedule is an amortization schedule) — the headroom home
        # for the ASC 606 create tool. 13 tools < max_tools=15 — every member is offered
        # (create_revenue_contract is PROPOSAL_ONLY_WRITE tier 2, no trim risk). Its
        # capability fixture create_revenue_contract_proposal (app/agent/evals.py) satisfies
        # AP-LAO-06 (the expected_tools name is bundled).
        "create_revenue_contract",
        "update_lease",
        "activate_lease",
        "remeasure_lease",
        "terminate_lease",
        # TEST-OK: get_schedule_data is READ-ONLY (READ_ONLY_TOOLS); no write/proposal, no fixture needed
        "get_schedule_data",
        # PLAN-EXEMPT: chunk C3 of ratified plan 20260714-wave2-7b-lease-extraction (marker
        # RATIFICATION-2026-07-14.md). get_lease_detail joins the amortization-schedules
        # bundle beside its lease-lifecycle siblings so a chat query can read a lease to
        # resolve a lease_id (the SH-6 reachability home for the new READ tool). 13 tools
        # < max_tools=15 — every member is offered (a READ_ONLY_TOOLS member, no trim risk
        # among the writes). It carries a capability fixture (get_lease_detail_read), so it
        # is fixtured-reachable and does NOT join _KNOWN_UNFIXTURED_ORPHANS.
        "get_lease_detail",
    ],
    AgentIntent.DOCUMENTS.value: [
        # C3-DELETES row 18: single document delete, ordered FIRST (H3 — a write must survive
        # the max_tools read-budget trim). Small bundle, so no trim risk regardless.
        "delete_document",
        "search_documents",
        "search_agreements",
        "get_agreement_context",
        "get_module_context",
    ],
    AgentIntent.MEMORY.value: [
        # C3-DELETES row 18: single taught-knowledge delete, ordered FIRST (H3 precedent).
        "delete_knowledge",
        "get_module_context",
        "teach_peregrine",
    ],
    # RATIFIED 2026-06-25 (plan 20260625-intent-routing-receivables): billing and admin
    # stay deliberately EMPTY. The agent must not be promptable to add/delete a user or
    # change the subscription — no mutation tool may be added or aliased into these bundles.
    # The user-facing redirect ("manage billing/admin in Settings") is owned by prompt_builder.
    AgentIntent.BILLING.value: [],  # RATIFIED 2026-06-25: empty by design — no billing-mutation tool in chat
    AgentIntent.ADMIN.value: [],  # RATIFIED 2026-06-25: empty by design — no admin-mutation tool in chat
    AgentIntent.RECEIVABLES.value: [
        # AR / sales (receivables) bundle. The receivables intent routes
        # invoice / "who owes us" / outstanding-sales phrasing here.
        # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C2 (marker on disk;
        # active-plan.json sidecar absent in this dispatched session). Wave-2.1 C2: the four
        # AR collections proposal tools join their natural home (4 reads → 8 tools, still
        # well under max_tools=15, so nothing can be trimmed). They are PROPOSAL_TOOLS
        # members (_selection_priority tier 1 — H3 precedent) and HANDLER-lane
        # self-drafters: each drafts a PENDING ProposedAction via the C1 proposers; the
        # paired safety_expectation AgentEvalFixtures (create_invoice_proposal /
        # send_invoice_reminder_proposal / record_customer_payment_proposal /
        # create_credit_memo_proposal) land with chunk C3 in app/agent/evals.py.
        "create_invoice",
        "send_invoice_reminder",
        "record_customer_payment",
        "create_credit_memo",
        # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
        # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
        # dispatched session — standing precedent). Wave-2.5 B (decision 7): the CECL
        # allowance tool joins its natural AR home (8 -> 9 tools, still well under
        # max_tools=15, so nothing can be trimmed; tier-1 as a PROPOSAL_TOOLS member —
        # H3 precedent). NO close_management/cashflow homes (both 15-exact).
        "propose_allowance_je",
        # Reads, ordered LIVE-FIRST: the two live-QBO tools (AR summary, then the QBO
        # report) lead, the local-index fallback (search_finance_records) is LAST.
        # get_module_context is intentionally DROPPED — it is a context helper, not an
        # answer tool, and is not needed to answer outstanding-invoice / sales questions.
        "get_ar_ap_summary",
        "run_qbo_report",
        "get_financial_data",
        "search_finance_records",
    ],
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.2 D (decision 11): the AP
    # payment-run bundle. Without this INTENT_BUNDLES entry, canonicalize_intent
    # would silently fall the new "payables" intent back to GENERAL. The write
    # leads (H3 precedent — PROPOSAL_TOOLS sort into tier 1 regardless); 5 tools,
    # well under max_tools=15, so nothing can be trimmed. Reads mirror the
    # RECEIVABLES live-first ordering (AR/AP summary, the QBO report, then the
    # local-index fallback).
    AgentIntent.PAYABLES.value: [
        "propose_payment_batch",
        "get_ar_ap_summary",
        "run_qbo_report",
        "get_financial_data",
        "search_finance_records",
    ],
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.3 (TG-4/WF-4, decision 1):
    # the bank-reconciliation bundle. Without this entry, canonicalize_intent would
    # silently fall the new "reconciliation" intent back to GENERAL. Writes lead
    # (H3 precedent — PROPOSAL_TOOLS sort tier-1 regardless); 10 tools, well under
    # max_tools=15, so nothing can be trimmed.
    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 1: recon_confirm_match /
    # recon_unconfirm are now SINGLE-HOMED here (un-dual-homed out of
    # close_management): recon phrasing routes here (intent_classifier.py), and the
    # pair's eval fixtures stay reachable via THIS offer. The HIDDEN execution-only tools
    # (recon_session_open_exec / recon_bulk_confirm_exec) are deliberately in NO
    # bundle (decision 3 — the model can never call them). Paired safety_expectation
    # AgentEvalFixtures live in app/agent/evals.py (AP-T09 lockstep).
    AgentIntent.RECONCILIATION.value: [
        "open_recon_session",
        "propose_bulk_matches",
        "recon_confirm_match",
        "recon_unconfirm",
        "list_unmatched_items",
        "get_recon_tieout",
        # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
        # RATIFICATION-2026-07-14.md). WF-12 (decision 10): the agent-plan orchestration
        # tools land in RECONCILIATION — a headroom set()-trim bundle (10 → 12 members,
        # well under max_tools=15, so NOTHING is trimmed and both are offered; no
        # exact-offer pin like schedules/close). Placement recorded per decision 10
        # ("an existing bundle, decided at build against the trim pins"): GENERAL and
        # REPORTING_ANALYSIS are ruled OUT — a tier-1 PROPOSAL_TOOLS write there would
        # displace a pinned read (get_executive_briefing / get_intelligence_snapshot);
        # RECONCILIATION is the multi-step-workflow headroom home whose keyword space the
        # plan-orchestration phrasing does not collide with (intent_classifier routes
        # "build a plan / multi-step / checklist of actions / do these steps" here).
        # propose_agent_plan is PROPOSAL_TOOLS → tier 1 (survives the read-budget trim);
        # get_agent_plan_status is READ_ONLY. Paired safety_expectation AgentEvalFixture
        # propose_agent_plan_proposal (app/agent/evals.py, AP-T09 lockstep).
        "propose_agent_plan",
        "get_agent_plan_status",
        "get_ar_ap_summary",
        "run_qbo_report",
        "get_financial_data",
        "search_finance_records",
    ],
    AgentIntent.ASC_GUIDANCE.value: [
        "search_asc_guidance",
        # C1-KB: the CFO-method sibling of search_asc_guidance (LBO / cashflow / close
        # best practice, cited + authority-tiered). Small bundle — well under max_tools,
        # so it is genuinely offered here.
        # TEST-OK: search_financial_expertise is READ-ONLY (READ_ONLY_TOOLS); no write/proposal, no fixture needed
        "search_financial_expertise",
        "search_agreements",
        "get_agreement_context",
        "search_documents",
        "get_module_context",
        "search_finance_records",
        "get_financial_data",
    ],
}

INTENT_ALIASES: dict[str, str] = {
    AgentIntent.CONVERSATIONAL.value: AgentIntent.GENERAL.value,
    AgentIntent.DEEP_ANALYSIS.value: AgentIntent.REPORTING_ANALYSIS.value,
    AgentIntent.ALERT_ESCALATION.value: AgentIntent.REPORTING_ANALYSIS.value,
    "task": AgentIntent.GENERAL.value,
    "lookup": AgentIntent.GENERAL.value,
}


def canonicalize_intent(intent: str | None) -> str:
    normalized = (intent or AgentIntent.GENERAL.value).strip().lower()
    return INTENT_ALIASES.get(normalized, normalized if normalized in INTENT_BUNDLES else AgentIntent.GENERAL.value)


# PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
# RATIFICATION-2026-07-13.md on disk) — decision 4 (E2/E3/E4): the dry_run flag is
# injected into BOTH write classes — every PROPOSAL_ONLY_WRITE tool and every
# model-visible HANDLER_LANE_SELF_DRAFTER — and NOTHING else. The hidden pair
# (MODEL_HIDDEN_TOOLS) is excluded because the model can never call it, so an
# injected flag there would be dead schema. This frozenset is the LOCKSTEP contract
# with chunk C3: injected == honored (C3's wave-gate census consumes this constant;
# a `_dry_run: true` call that really drafts is worse than no dry_run at all).
DRY_RUN_PROPERTY_NAME = "_dry_run"
DRY_RUN_PROPERTY_DESCRIPTION = (
    "Preview only: compute and describe what WOULD be drafted; nothing is written"
)
DRY_RUN_INJECTED_TOOLS: frozenset = frozenset(
    (PROPOSAL_ONLY_WRITE_TOOLS | HANDLER_LANE_SELF_DRAFTERS) - MODEL_HIDDEN_TOOLS
)


def dry_run_injected_tools() -> frozenset:
    """Public accessor for the injected dry_run tool set (the C3 lockstep census
    reads this — injected == honored is a wave-gate assertion)."""
    return DRY_RUN_INJECTED_TOOLS


def _inject_dry_run_property(input_schema: dict[str, object]) -> dict[str, object]:
    """REBUILD the schema with the ``_dry_run`` boolean property — never mutate.

    E4: ``dict(schema.get("input_schema"))`` is a SHALLOW copy whose ``properties``
    value ALIASES the shared ``app/assistant/tools.py`` dict; ``properties[...] =``
    on it would corrupt every other consumer of ASSISTANT_TOOLS (the raw-schema
    test pins, the eval fixtures). ``{**schema, "properties": {**props, ...}}``
    allocates fresh top-level + properties dicts, so the source stays byte-identical
    (pinned by tests/test_dry_run.py's aliasing test).
    """
    props = input_schema.get("properties")
    return {
        **input_schema,
        "properties": {
            **(props if isinstance(props, dict) else {}),
            DRY_RUN_PROPERTY_NAME: {
                "type": "boolean",
                "description": DRY_RUN_PROPERTY_DESCRIPTION,
            },
        },
    }


_MATERIAL_PROPOSAL_PREFIX = (
    "Draft a ProposedAction for human review. Peregrine will not directly write this change "
    "from chat; nothing changes until an editor approves it. "
)
_ROUTINE_PROPOSAL_PREFIX = (
    "Draft a ProposedAction for a routine internal change. It can apply immediately (audited) "
    "only when this workspace has turned on routine-write autonomy and the requesting user is "
    "an editor or above; otherwise it waits for an editor's approval. "
)


def _proposal_description_prefix(name: str) -> str:
    """The truthful execution note for a proposal-only write, by its action's tier."""
    from ...services.action_tiers import INTERNAL_ROUTINE_ACTION_TYPES
    from ...services.internal_action_execution import ACTION_TYPE_TO_TOOL

    routine_tools = {
        tool for action_type, tool in ACTION_TYPE_TO_TOOL.items()
        if action_type in INTERNAL_ROUTINE_ACTION_TYPES
    }
    return _ROUTINE_PROPOSAL_PREFIX if name in routine_tools else _MATERIAL_PROPOSAL_PREFIX


def _metadata_for_tool(name: str, schema: dict[str, Any], handler: Optional[ToolHandler]) -> ToolDefinition:
    if name in PROPOSAL_TOOLS:
        risk_level = RiskLevel.DRAFT_ONLY
        required_role = "ANALYST"
        mutates_state = True
        requires_confirmation = False
        creates_proposed_action = True
        audit_category = "proposal"
    elif name in SAFE_INTERNAL_WRITE_TOOLS:
        risk_level = RiskLevel.INTERNAL_WRITE
        required_role = "EDITOR"
        mutates_state = True
        requires_confirmation = True
        creates_proposed_action = False
        audit_category = "internal_write"
    elif name in PROPOSAL_ONLY_WRITE_TOOLS:
        risk_level = RiskLevel.DRAFT_ONLY
        required_role = "ANALYST"
        mutates_state = True
        requires_confirmation = False
        creates_proposed_action = True
        audit_category = "proposal"
    elif name in ANALYSIS_TOOLS:
        risk_level = RiskLevel.ANALYSIS_ONLY
        required_role = "VIEWER"
        mutates_state = False
        requires_confirmation = False
        creates_proposed_action = False
        audit_category = "analysis"
    else:
        risk_level = RiskLevel.READ_ONLY
        required_role = "VIEWER"
        mutates_state = False
        requires_confirmation = False
        creates_proposed_action = False
        audit_category = "read"

    allowed_intents = {
        intent for intent, names in INTENT_BUNDLES.items() if name in names
    } or {AgentIntent.GENERAL.value}

    if name in {"teach_peregrine"}:
        allowed_intents.update({AgentIntent.MEMORY.value, AgentIntent.GENERAL.value})

    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 1. dispose_schedule's OFFER
    # moved to the SCHEDULES bundle, but the close lane keeps POLICY access (a
    # fixed-asset disposal drafted during a close-intent conversation/walk must not
    # be denied by the intent gate). It cannot ride close membership like the other
    # member-only tail writes: as a PROPOSAL_TOOLS member it sorts into selection
    # tier 1, so membership would force it INTO the close offer and break the
    # decision-1 fifteen-exact list. The explicit allowed_intents supplement (the
    # teach_peregrine precedent above) grants the intent without the offer.
    if name in {"dispose_schedule"}:
        allowed_intents.update({AgentIntent.CLOSE_MANAGEMENT.value})

    # PLAN-EXEMPT: ratified plan 20260715-fluid-tool-selection AC-2/E1 (marker
    # RATIFICATION-2026-07-15.md on disk). reverse_journal_entry is offered on EVERY
    # intent by the ALWAYS-OFFERED write lane (select_for_intent), but it is a bundle
    # member of CLOSE_MANAGEMENT only, so its membership-derived allowed_intents =
    # {close_management} and the execution gate (tool_policy.py:51 — intent not in
    # allowed_intents AND "general" not in allowed_intents → reject) would REJECT it under
    # any other intent (offered-but-rejected). Its two lane siblings already carry
    # "general" (both GENERAL members), so reverse_journal_entry is the ONE lane-core write
    # needing this supplement. It grants the intent WITHOUT bundle membership (the
    # teach_peregrine/dispose_schedule precedent above): the offer comes from the lane, not
    # from GENERAL-bundle membership (which would trim the pinned get_executive_briefing).
    # No new write capability — the tool is unchanged, only more intents may propose it; its
    # proposal-only routing stays proven by the existing safety_expectation
    # AgentEvalFixture reverse_journal_entry_proposal (app/agent/evals.py, AP-T09).
    if name in {"reverse_journal_entry"}:
        allowed_intents.update({AgentIntent.GENERAL.value})

    description = str(schema.get("description") or "")
    if name in PROPOSAL_ONLY_WRITE_TOOLS:
        description = _proposal_description_prefix(name) + description

    # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk) — decision 4: dry_run injection is a
    # REBUILD ({**schema, "properties": {**props, ...}}), never a mutation of the
    # shallow dict() copy, because that copy's nested dicts alias tools.py (E4).
    input_schema = dict(schema.get("input_schema") or {"type": "object", "properties": {}})
    if name in DRY_RUN_INJECTED_TOOLS:
        input_schema = _inject_dry_run_property(input_schema)

    return ToolDefinition(
        name=name,
        namespace="assistant",
        description=description,
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C2 (marker RATIFICATION-2026-07-13.md on disk; dispatched session lacks active-plan state)
        input_schema=input_schema,
        # PLAN-EXEMPT: ratified plan 20260713-wave2-6-tool-hardening chunk C4 (marker
        # RATIFICATION-2026-07-13.md on disk) — decision 5: output_schema exists ONLY
        # where the result is really typed + consumed (tool_results.py: the two recon
        # JSON reads serialize the model; run_qbo_report's GL-cache lane validates +
        # reads the typed page). Everything else returns prose with no typed consumer,
        # so it carries None — the tool_metadata no-consumer doctrine.
        output_schema=(
            TOOL_RESULT_MODELS[name].model_json_schema()
            if name in TOOL_RESULT_MODELS
            else None
        ),
        handler=handler,
        risk_level=risk_level,
        required_role=required_role,
        mutates_state=mutates_state,
        external_side_effect=False,
        requires_confirmation=requires_confirmation,
        creates_proposed_action=creates_proposed_action,
        allowed_intents=allowed_intents,
        source_requirements=["tenant_scoped_data"] if name in READ_ONLY_TOOLS | ANALYSIS_TOOLS else ["tenant_scoped_data", "audit_trace"],
        audit_category=audit_category,
        pii_sensitivity="financial_sensitive" if name in READ_ONLY_TOOLS | ANALYSIS_TOOLS else "financial_sensitive_write",
        log_redaction_policy="sanitize_payload:redact_secrets_truncate_payloads",
        idempotency_key_requirement="required_for_retries" if risk_level in {RiskLevel.INTERNAL_WRITE, RiskLevel.EXTERNAL_WRITE} else "not_required",
        # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — structural model-visibility
        # gate (MODEL_HIDDEN_TOOLS); safety_expectation AgentEvalFixture coverage unchanged.
        send_to_model=name not in MODEL_HIDDEN_TOOLS,
    )


# PLAN-EXEMPT: ratified plan 20260715-fluid-tool-selection AC-1/AC-2/AC-3 (marker
# RATIFICATION-2026-07-15.md on disk). The ALWAYS-OFFERED write lane. The runtime routes
# a request to an intent via a deterministic first-match-wins keyword classifier
# (intent_classifier.py); a proposal-gated write's AVAILABILITY was coupled to that brittle
# signal — "credit against prepaid EXPENSES" hit "expense" → reporting_analysis (no JE tool)
# → create_journal_entry was "not available in this run." These constants decouple write
# AVAILABILITY from the matched intent WITHOUT touching read-routing or the model-tier
# (determinism preserved where it is an asset). select_for_intent appends them OUTSIDE the
# max_tools read cap, so no read is displaced.
#
# ALWAYS_OFFERED_WRITE_TOOLS (W): the curated cross-cutting proposal-write core, appended to
# EVERY intent's offer (except the ratified-empty BILLING/ADMIN). All three are PROPOSAL_TOOLS
# and proposal-gated (draft a PENDING ProposedAction; nothing executes) — the safety boundary
# is byte-unchanged. Each carries its safety_expectation AgentEvalFixture in app/agent/evals.py.
ALWAYS_OFFERED_WRITE_TOOLS: tuple[str, ...] = (
    "create_journal_entry",
    "reverse_journal_entry",
    "update_budget_line",
)

# REACTIVATED_WRITE_TOOLS: two formerly-dead writes (in NO bundle at HEAD) revived by the lane.
# Each is now a MEMBER of exactly one home bundle (spread_cashflow_balance → CASHFLOW,
# add_covenant → CLOSE_MANAGEMENT) for allowed_intents; the lane offers each ONLY on the intent
# whose bundle lists it (home-scoped — it appends the reactivation only when it is in that
# intent's requested set), so a reactivation is never leaked onto an unrelated intent.
REACTIVATED_WRITE_TOOLS: tuple[str, ...] = (
    "spread_cashflow_balance",
    "add_covenant",
)

# The ratified-empty bundles (RATIFIED 2026-06-25): the agent must never be promptable to a
# billing/admin mutation from chat — no write may be added or aliased into these intents. The
# write lane skips them so W is not leaked into a subscription/user-management conversation.
_NO_ALWAYS_OFFERED_WRITE_INTENTS: frozenset[str] = frozenset(
    {AgentIntent.BILLING.value, AgentIntent.ADMIN.value}
)


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolDefinition]):
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def select_for_intent(
        self,
        intent: str,
        *,
        max_tools: int | None = None,
        pinned: Iterable[str] | None = None,
    ) -> list[ToolDefinition]:
        # PLAN-EXEMPT: confirmation-flow correctness fix (a confirmed tool was trimmed by the
        # max_tools read-budget) — the sibling of P0 hotfix H3. Background on H3: a user who asks to
        # draft a write ("accrue X", "post a journal entry") must be OFFERED the relevant proposal
        # tool, or the model refuses; Phase-A prepended read-only review tools to the bundles, pushing
        # the JE-draft PROPOSAL tool past the max_tools read-budget, so H3 ordered PROPOSAL_TOOLS first.
        # This fix closes the same trap for CONFIRM-GATED writes: a requires_confirmation tool is ONLY
        # executable once the user has issued a backend confirmation token for it (the policy gate
        # blocks it otherwise), so it only NEEDS to be offered to the model when confirmed — but a
        # confirm-gated tool ranked beyond max_tools in its bundle (generate_report sat at position 18
        # of the 18-tool reporting_analysis bundle) was silently dropped by this trim BEFORE the policy
        # gate could honor the confirmation, so the confirmed write could never execute. ``pinned`` (the
        # confirmed tool names, threaded from the ContextPacket confirmation set) sorts FIRST — above
        # PROPOSAL_TOOLS — so a tool the user explicitly confirmed this turn can never be trimmed out.
        canonical_intent = canonicalize_intent(intent)
        # Preserve the existing budgeting override. Reporting adds one read to
        # its initial offer; explicit caller caps still govern read selection and
        # confirmed-tool pinning. The additive write lane remains outside the cap.
        if canonical_intent == AgentIntent.BUDGETING.value:
            max_tools = 19
        elif max_tools is None:
            max_tools = 16 if canonical_intent == AgentIntent.REPORTING_ANALYSIS.value else 15
        requested = (
            INTENT_BUNDLES[canonical_intent]
            if canonical_intent in INTENT_BUNDLES
            else INTENT_BUNDLES[AgentIntent.GENERAL.value]
        )
        pinned_set = {str(name) for name in pinned} if pinned else set()

        def _selection_priority(name: str) -> int:
            # Stable sort into three tiers (original order kept within each):
            #   0 — pinned/confirmed tools (the user confirmed them THIS turn; never trim)
            #   1 — PROPOSAL_TOOLS (explicitly-invoked write capabilities; H3)
            #   2 — everything else (reads/analysis)
            if name in pinned_set:
                return 0
            if name in PROPOSAL_TOOLS:
                return 1
            return 2

        prioritized = sorted(requested, key=_selection_priority)
        selected: list[ToolDefinition] = []
        trimmed: list[str] = []
        for name in prioritized:
            tool = self.get(name)  # SA-OK: ToolRegistry.get is an in-memory dict lookup (self._tools.get), not a DB query — no N+1
            if not tool or not tool.send_to_model:
                continue
            if len(selected) < max_tools:
                selected.append(tool)
            else:
                trimmed.append(name)
        # AC-5 trim observability: every silent drop is now a logged fact — the SH-6
        # dead-write class (a bundled write trimmed out of its offer) was invisible
        # precisely because nothing recorded what the read-budget cut.
        if trimmed:
            logger.info(
                "[ToolSelect] intent=%s requested=%d offered=%d trimmed=%s",
                canonical_intent,
                len(requested),
                len(selected),
                trimmed,
            )
        # AC-1 the ALWAYS-OFFERED write lane (STRICTLY ADDITIVE, outside the read cap).
        # `selected` above is today's per-bundle offer BYTE-IDENTICAL (the read/model-tier
        # selection is unchanged — the trim log reflects exactly what the read budget cut).
        # Now append the curated write core W (every intent) + the home reactivation(s) for
        # THIS bundle, deduped against what is already offered, so a proposal-gated write is
        # reachable regardless of the brittle matched intent — WITHOUT displacing a read
        # (these ride outside max_tools). The ratified-empty BILLING/ADMIN intents are skipped.
        if canonical_intent not in _NO_ALWAYS_OFFERED_WRITE_INTENTS:
            already_offered = {tool.name for tool in selected}
            requested_set = set(requested)
            additive_names = [
                *ALWAYS_OFFERED_WRITE_TOOLS,
                # reactivations are home-scoped: offered only where they are a bundle member
                *(n for n in REACTIVATED_WRITE_TOOLS if n in requested_set),
            ]
            for name in additive_names:
                if name in already_offered:
                    continue
                tool = self.get(name)  # SA-OK: ToolRegistry.get is an in-memory dict lookup, not a DB query
                if not tool or not tool.send_to_model:
                    continue
                selected.append(tool)
                already_offered.add(name)
        return selected


def build_assistant_tool_registry(
    assistant_tools: Iterable[dict[str, Any]],
    handlers: dict[str, ToolHandler],
) -> ToolRegistry:
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — fail-closed registry integrity.
    # Every build validates the classification sets against the registered names (unclassified
    # tools, phantom members, multi-membership, proposal↔executor lockstep, self-drafter
    # alignment) and RAISES RegistryIntegrityError on any violation — the silent READ_ONLY
    # default for a forgotten write tool is abolished. Cached on the name-set signature, so
    # per-request rebuilds cost one frozenset hash. Adds no write capability; write-tool
    # safety_expectation AgentEvalFixture coverage stays enforced by the AP-T09 hook + eval gate.
    assistant_tools = list(assistant_tools)
    ensure_registry_integrity(
        (str(schema.get("name") or "") for schema in assistant_tools),
        handlers.keys(),
    )
    definitions = []
    seen = set()
    for schema in assistant_tools:
        name = str(schema.get("name") or "")
        if not name:
            continue
        seen.add(name)
        definitions.append(_metadata_for_tool(name, schema, handlers.get(name)))

    for name, handler in handlers.items():
        if name not in seen:
            definitions.append(
                _metadata_for_tool(
                    name,
                    {
                        "name": name,
                        "description": f"Legacy backend handler for {name}.",
                        "input_schema": {"type": "object", "properties": {}},
                    },
                    handler,
                )
            )

    # PLAN-EXEMPT: plan 20260712-agentic-finance-os W6 (GAP-8) — enforce the
    # source_requirements vocabulary at BUILD time so the field is no longer
    # decorative (directive §14): a tool declaring an unknown requirement (which
    # check_source_requirements would fail-closed on at runtime) fails startup here.
    from .tool_metadata import validate_source_requirements

    sr_violations = validate_source_requirements(definitions)
    if sr_violations:
        raise RegistryIntegrityError(
            "tool source_requirements failed validation "
            f"({len(sr_violations)} violation(s)):\n  - " + "\n  - ".join(sr_violations)
        )
    return ToolRegistry(definitions)

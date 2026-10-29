# app/assistant/tools.py
# Tool definitions for the Peregrine AI Assistant.
# Contains the ASSISTANT_TOOLS list (98 tool schemas) passed to Claude.
# PLAN-EXEMPT: chunk 5 of ratified plan 20260714-wave3-2-asc606-engine (marker
# RATIFICATION-2026-07-14.md). Header count 97 -> 98: create_revenue_contract (a SIMPLE
# single-name internal-DB write — the ASC 606 agent create tool, mirror of create_lease).
# The test literal bump (97 -> 98 in tests/test_tools_vendor_je_reversal.py) rides this chunk.
# PLAN-EXEMPT: chunk C3 of ratified plan 20260714-wave2-7b-lease-extraction (marker
# RATIFICATION-2026-07-14.md). Header count 96 -> 97: get_lease_detail (a READ tool —
# READ_ONLY_TOOLS lane, no executor row, no two-name split; closes the 2.7 finding-3
# read gap where the agent can write a lease but not read one to resolve a lease_id).
# The test literal bump (96 -> 97 in tests/test_tools_vendor_je_reversal.py) rides this chunk.
# PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker
# RATIFICATION-2026-07-14.md). Header count 95 -> 96: create_lease (a SIMPLE
# single-name internal-DB write — the create half of TG-5). The test literal bump
# (95 -> 96 in tests/test_tools_vendor_je_reversal.py) rides this same chunk.
# PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
# RATIFICATION-2026-07-14.md). Header count 92 -> 95: the two-name agent-plan lane —
# propose_agent_plan (visible self-drafter) + get_agent_plan_status (visible read) +
# agent_plan_activate_exec (hidden approval-time executor). The test literal bump
# (92 -> 95 in tests/test_tools_vendor_je_reversal.py) rides this same chunk.
# PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Header count 91 -> 92
# (propose_allowance_je below); the SH-7 test literal bump (91 -> 92 in
# tests/test_tools_vendor_je_reversal.py) belongs to the ORCHESTRATOR post-wave.
# PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Header count 84 -> 85
# (propose_payment_batch below).
# PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections C2 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — standing precedent). Header count 80 -> 84 (the four AR
# collections tools below).
# PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 C1/SH-7 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
# NOTE: the schema count in the header line is pinned by tests/test_tools_vendor_je_reversal.py
# (the SH-7 drift test) — update it in the same edit that adds/removes a tool.

ASSISTANT_TOOLS = [
    {
        "name": "create_journal_entry",
        "description": (
            "Draft a journal entry for human review. The JE will be created as a PENDING "
            "proposal that the user must approve before it posts to QuickBooks. "
            "Always include at least 2 lines that balance (total debits == total credits). "
            "Use account names from the chart of accounts. Use this tool for any account "
            "reclass, recode, or correction request instead of editing the original transaction directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "JE date in YYYY-MM-DD format"},
                "memo": {"type": "string", "description": "Description / memo for the journal entry"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_name": {"type": "string", "description": "QBO account name (e.g. 'Accounts Payable')"},
                            "account_id": {"type": "string", "description": "QBO account ID if known, otherwise leave empty"},
                            "debit": {"type": "number", "description": "Debit amount (0 if credit line)"},
                            "credit": {"type": "number", "description": "Credit amount (0 if debit line)"},
                            "description": {"type": "string", "description": "Line description"},
                        },
                        "required": ["account_name", "debit", "credit"],
                    },
                    "description": "Journal entry lines — debits and credits must balance",
                },
            },
            "required": ["date", "memo", "lines"],
        },
    },
    {
        "name": "update_budget_line",
        "description": (
            "Record a review-only budget suggestion (BUDGET_UPDATE finding). "
            "Approval does not change budget values. For executable changes, first resolve "
            "the exact budget, line and year, then use set_budget_months."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_name": {"type": "string", "description": "Budget account name (e.g. 'Marketing')"},
                # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 7 (SH-5) month-key contract.
                "month": {"type": "string", "description": "Three-letter month key (jan, feb, mar, …) — canonical. YYYY-MM (e.g. 2026-03) is tolerated and normalized to jan..dec."},
                "amount": {"type": "number", "description": "New dollar amount for that month"},
                "notes": {"type": "string", "description": "Reason for the change"},
            },
            "required": ["account_name", "month", "amount"],
        },
    },
    {
        "name": "set_budget_months",
        # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 7 (SH-5):
        # the description taught YYYY-MM keys while the property demanded jan..dec (a
        # live contradiction), and the "direct internal write ... after backend
        # confirmation" sentence was FALSE — this tool drafts a PENDING proposal.
        "description": (
            "BULK UPDATE: Set multiple months on a budget line in one call. "
            "Pass monthly_values as a dict of three-letter month keys (jan..dec) to dollar "
            "amounts; YYYY-MM keys are tolerated and normalized to jan..dec. "
            "Creates a PENDING proposal the user must approve before the budget changes. "
            "Use this for schedule-linked budgets (depreciation, interest, amortization) "
            "or whenever you need to set many months at once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_name": {
                    "type": "string",
                    "description": "Budget account name (e.g. 'Depreciation Expense', 'Interest Expense')",
                },
                "monthly_values": {
                    "type": "object",
                    "description": "Dict of three-letter month keys to dollar amounts. Keys MUST be lowercase: jan, feb, mar, apr, may, jun, jul, aug, sep, oct, nov, dec. Example: {'jan': 738.04, 'feb': 738.04, 'mar': 738.04, ...}",
                },
                "budget_year": {
                    "type": "integer",
                    "description": "Budget year (defaults to current year)",
                },
                "notes": {
                    "type": "string",
                    "description": "Reason/source for values (e.g. 'Per depreciation schedule', 'Per loan agreement at 6.5%')",
                },
            },
            "required": ["account_name", "monthly_values"],
        },
    },
    {
        "name": "create_close_task",
        "description": "Add a task to the month-end close checklist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "Period in YYYY-MM format"},
                "title": {"type": "string", "description": "Task title"},
                "description": {"type": "string", "description": "Task description"},
            },
            "required": ["period", "title"],
        },
    },
    {
        "name": "add_covenant",
        "description": "Track a new debt covenant for monitoring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "loan_name": {"type": "string", "description": "Name of the loan or credit facility"},
                "name": {"type": "string", "description": "Covenant name (e.g. 'Minimum DSCR')"},
                "metric_type": {"type": "string", "enum": ["DSCR", "LEVERAGE", "MIN_CASH", "CUSTOM"], "description": "Type of metric"},
                "threshold": {"type": "number", "description": "Threshold value"},
                "breach_direction": {"type": "string", "enum": ["BELOW", "ABOVE"], "description": "Breach if value goes BELOW or ABOVE threshold"},
            },
            "required": ["loan_name", "name", "metric_type", "threshold"],
        },
    },
    {
        "name": "search_chart_of_accounts",
        "description": (
            "Search the QuickBooks Chart of Accounts to find account IDs and names. "
            "Use this before creating a journal entry if you need the correct account ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search_term": {"type": "string", "description": "Partial account name to search for"},
            },
            "required": ["search_term"],
        },
    },
    {
        "name": "search_finance_records",
        "description": (
            "Search normalized QuickBooks records deterministically without scanning raw JSON. "
            "Use this for transaction/entity lookups such as exact QBO IDs, invoice numbers, "
            "customer/vendor names, memo text, open invoices, vendor bills, deposits, payments, "
            "journal entries, accounts, classes, and locations. Natural-language queries are parsed "
            "into structured filters; returned records come only from the database search index."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query, e.g. 'open invoices for NTOS last month'.",
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["customer", "vendor", "invoice", "bill", "payment", "deposit", "journal_entry", "account", "class", "location"],
                    "description": "Optional entity type filter.",
                },
                "start_date": {"type": "string", "description": "Optional transaction start date in YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "Optional transaction end date in YYYY-MM-DD."},
                "min_amount": {"type": "number", "description": "Optional minimum amount."},
                "max_amount": {"type": "number", "description": "Optional maximum amount."},
                "status": {"type": "string", "description": "Optional status filter such as open, paid, posted, active, inactive."},
                "customer_name": {"type": "string", "description": "Optional customer-name filter; fuzzy matching is supported."},
                "vendor_name": {"type": "string", "description": "Optional vendor-name filter; fuzzy matching is supported."},
                "account": {"type": "string", "description": "Optional account-name filter."},
                "class_name": {"type": "string", "description": "Optional class-name filter."},
                "location": {"type": "string", "description": "Optional location/department filter."},
                "limit": {"type": "integer", "description": "Maximum records to return, default 10."},
            },
            "required": ["query"],
        },
    },
    # PLAN-EXEMPT: Steven work order — route vendor/actuals questions to get_financial_data,
    # not get_budget_data; description names vendor_spend monthly as the actuals AP source.
    {
        "name": "get_financial_data",
        "description": (
            "Fetch pre-computed ACTUAL financial analysis from QuickBooks (what really "
            "happened — not budget/plan). Available: vendor_spend, customer_revenue, "
            "expense_trends, profit_margins, cogs_anomalies, ar_aging, ap_aging, "
            "transaction_anomalies. This tool has no cash forecast: for cash position or "
            "cash-flow forecast questions use get_cashflow_snapshot (the 13-week forecast the "
            "user sees on the forecast page). "
            "vendor_spend (especially with period='monthly') returns per-vendor ACTUAL spend "
            "by month from Bills + vendor-tagged Purchases — it is the correct tool for "
            "'which vendors did we pay / not pay in {month}', 'are we missing any vendors', "
            "'new or dropped vendors', and vendor spend trends; the monthly result includes a "
            "missing_vendors list (recurring vendors absent in the target month). "
            "Set period to 'monthly' for month-by-month breakdowns. "
            "NOTE: For account-level or transaction-level detail, use run_qbo_report instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_type": {
                    "type": "string",
                    "enum": ["vendor_spend", "customer_revenue", "expense_trends",
                             "profit_margins", "cogs_anomalies",
                             "ar_aging", "ap_aging", "transaction_anomalies"],
                    "description": "Which financial report to pull from QBO",
                },
                "period": {
                    "type": "string",
                    "enum": ["ttm", "monthly"],
                    "description": "'ttm' for trailing 12 month totals (default), 'monthly' for month-by-month breakdown with growth trends",
                },
                "month": {
                    "type": "string",
                    "description": "Optional target month 'YYYY-MM' for vendor_spend monthly — the month evaluated for missing/new vendors. Defaults to the latest complete month.",
                },
            },
            "required": ["report_type"],
        },
    },
    # PLAN-EXEMPT: Steven work order — narrow get_budget_data to budget-vs-actual ONLY so
    # the bare word 'spending' no longer pulls it in for actuals questions.
    {
        "name": "get_budget_data",
        "description": (
            "Fetch BUDGET vs ACTUAL variance data for a company. Returns account-level "
            "budgeted amounts alongside actuals for variance analysis. Use this ONLY when the "
            "question is explicitly about the budget, the plan, or budgeted-vs-actual variance "
            "(e.g. 'how are we tracking vs budget for {month}'). Do NOT use it for questions "
            "about what actually happened — what we spent, who we paid, or missing/new vendors; "
            "for those use get_financial_data (vendor_spend / expense_trends) or run_qbo_report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "budget_year": {
                    "type": "integer",
                    "description": "Budget year (e.g. 2026). Defaults to current year.",
                },
                "account_filter": {
                    "type": "string",
                    "description": "Optional: filter to accounts containing this text",
                },
            },
        },
    },
    {
        "name": "compare_periods",
        "description": (
            "Compare financial metrics across periods. Returns current vs prior values with "
            "delta and percentage change. Use for MoM, QoQ, or YoY analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["revenue", "expenses", "net_income", "gross_profit", "ebitda"],
                    "description": "Which metric to compare",
                },
                "comparison": {
                    "type": "string",
                    "enum": ["mom", "qoq", "yoy"],
                    "description": "Month-over-month, quarter-over-quarter, or year-over-year",
                },
            },
            "required": ["metric", "comparison"],
        },
    },
    {
        "name": "run_qbo_report",
        "description": (
            "Run a QuickBooks report to get DETAILED, ACCOUNT-LEVEL financial data. "
            "This is the most powerful analysis tool — use it for deep-dive questions. "
            "ProfitAndLoss: every account line with amounts. "
            "BalanceSheet: every asset/liability/equity account. "
            "GeneralLedger: individual TRANSACTIONS by account with dates, vendors, memos, and amounts — "
            "use this for 'show me what we spent on X' or 'what transactions hit this account' questions. "
            "CashFlow: cash flow statement detail. "
            "TrialBalance: all accounts with debit/credit balances."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_type": {
                    "type": "string",
                    "enum": ["ProfitAndLoss", "BalanceSheet", "CashFlow", "GeneralLedger", "TrialBalance"],
                    "description": "Type of QBO report to run",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format (defaults to first of current month)",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format (defaults to today)",
                },
                "account": {
                    "type": "string",
                    "description": "For GeneralLedger only: account name or number to filter to (e.g. '7430 Software', 'Payroll'). Resolves to QBO account ID automatically.",
                },
                # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 6
                # (SH-1): keyset paging over the account-filtered cached GL read.
                # The live report-parse path (no account filter / cache miss)
                # IGNORES both parameters.
                "limit": {
                    "type": "integer",
                    "description": "For the account-filtered GeneralLedger cache read only: transactions per page (default 80, max 200). Ignored by the live report-parse path.",
                },
                "cursor": {
                    "type": "string",
                    "description": "For the account-filtered GeneralLedger cache read only: opaque continuation token from a prior page's 'next_cursor'. Omit to start from the beginning. Ignored by the live report-parse path.",
                },
            },
            "required": ["report_type"],
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Search uploaded company documents (leases, contracts, agreements, etc.) by keyword. "
            "Use this when the user asks about specific terms, dates, or details from their documents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term or keywords to find in documents"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_agreements",
        "description": (
            "Search uploaded material agreements/contracts, including operating agreements, LP agreements, "
            "leases, loan agreements, vendor contracts, and legacy company agreement uploads. "
            "Searches summaries, extracted economics, cap table data, and raw extracted text snippets. "
            "Use this before answering questions about contract terms, obligations, fees, rights, "
            "ownership, renewal dates, covenants, or other agreement-specific context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for in agreements/contracts"},
                "include_raw_excerpt": {
                    "type": "boolean",
                    "description": "Whether to include short raw text excerpts around matches. Defaults to true.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_agreement_context",
        "description": (
            "Get a structured summary of uploaded material agreements/contracts for this company. "
            "Returns agreement type, summary, economics, cap table extraction counts, and linked document metadata. "
            "Use this when the user asks what agreements are on file or wants agreement-derived context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agreement_id": {
                    "type": "integer",
                    "description": "Optional specific company_agreements ID. If omitted, returns all recent agreements.",
                },
            },
        },
    },
    {
        "name": "get_module_context",
        "description": (
            "Inspect a Peregrine module's current context in assistant-readable form. "
            "Supported modules include agreements, documents, budget, cashflow, close, covenants, "
            "kpis, lbo, accounting_schedules, cap_table, alerts, knowledge, and corrections. "
            "The 'corrections' module surfaces past human corrections for THIS company as trusted "
            "few-shot exemplars — fetch it before categorizing / proposing so you don't repeat a "
            "mistake the user already corrected. "
            "Use this to orient before answering module-specific questions or deciding which deeper tool to call next."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "description": "Module name, e.g. agreements, documents, budget, cashflow, covenants, cap_table, lbo, close.",
                },
            },
            "required": ["module"],
        },
    },
    {
        "name": "search_asc_guidance",
        "description": (
            "Search the ASC (Accounting Standards Codification) knowledge map for U.S. GAAP "
            "issue-spotting and routing. Covers ~150 small-business topics across revenue (ASC 606), "
            "leases (ASC 842), inventory (330), fixed assets/impairment (360), intangibles/software/"
            "goodwill (350), credit losses (326), debt/interest (470/835), contingencies (450/460), "
            "income taxes (740), business combinations (805), consolidation/related parties (810/850), "
            "fair value/derivatives/FX (815/820/830), subsequent events (855), and more. "
            "Use this to identify the likely ASC topic, the high-level treatment, and whether the "
            "matter needs CPA/specialist review BEFORE answering any accounting-standards or GAAP "
            "question. This is a triage layer, not authoritative GAAP — always confirm conclusions "
            "against authoritative ASC text, and first establish the reporting basis (GAAP vs "
            "tax/cash/modified-cash). Optionally filter by a routing cluster."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The accounting question, transaction, or ASC code to look up, e.g. "
                        "'how do we recognize SaaS subscription revenue', 'ASC 842 tenant improvement "
                        "allowance', or 'goodwill impairment'."
                    ),
                },
                "route_tag": {
                    "type": "string",
                    "description": (
                        "Optional routing cluster to restrict the search to, e.g. revenue, leases, "
                        "inventory_cogs, fixed_assets, intangibles_software_goodwill, "
                        "receivables_credit_losses, debt_interest, contingencies_commitments, "
                        "income_taxes, business_combinations, consolidation_related_parties, "
                        "fair_value_derivatives_fx, compensation_benefits, basis_and_presentation, "
                        "subsequent_events, industry_specific."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_financial_expertise",
        "description": (
            "Search the financial-expertise knowledge base for CFO-grade METHOD guidance across "
            "three domains: lbo (sources & uses, debt tranches, cash sweep, PIK, covenants, "
            "MOIC/IRR returns, rollover, EBITDA add-backs, sensitivity, exit), cashflow (13-week "
            "direct forecast, direct-vs-indirect, DSO/DPO/DIO/CCC working-capital days, runway, "
            "scenarios, variance and MAPE/bias accuracy discipline), and close (close calendar, "
            "bank and balance-sheet reconciliations, accruals and cut-off, flux thresholds, "
            "roll-forwards, sub-ledger tie-outs, controls, close-quality metrics). Every record "
            "carries a source citation, dates, and an authority tier (binding | "
            "proposed-not-binding | practitioner-methodology) — CITE all three when you use one, "
            "and never present a practitioner-methodology record as a regulatory requirement. "
            "This is distilled method reference, not authoritative GAAP (use search_asc_guidance "
            "for ASC topic routing) and not tenant data; values it does not carry are named gaps "
            "to surface, never to guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The method question or task, e.g. 'how should the cash sweep work', "
                        "'DSO formula for the forecast', 'flux analysis thresholds', "
                        "'is exit equity net of cash'."
                    ),
                },
                "domain": {
                    "type": "string",
                    "enum": ["lbo", "cashflow", "close"],
                    "description": "Optional domain to restrict the search to.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_schedule_data",
        "description": (
            "Get the current accounting schedule grid data. Returns all rows with monthly expense values. "
            "IMPORTANT schedule type mapping:\n"
            "- FIXED_ASSET = depreciation schedule (use for depreciation expense budgets)\n"
            "- PREPAID = prepaid expense amortization schedule\n"
            "- ACCRUAL = accrued expense schedule\n"
            "- INTANGIBLE = intangible asset amortization schedule\n"
            "Each row has monthly values — SUM ALL rows to get total monthly depreciation/amortization for budgeting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_type": {
                    "type": "string",
                    "enum": ["PREPAID", "FIXED_ASSET", "ACCRUAL", "INTANGIBLE"],
                    "description": "FIXED_ASSET for depreciation, PREPAID for prepaids, ACCRUAL for accruals, INTANGIBLE for intangible amortization",
                },
            },
        },
    },
    # PLAN-EXEMPT: chunk C3 of ratified plan 20260714-wave2-7b-lease-extraction (marker
    # RATIFICATION-2026-07-14.md). READ-only lease detail lookup — closes the 2.7
    # finding-3 gap (the agent can write a lease via create_lease but had no way to
    # read one to resolve a lease_id). No proposal, no write; mirrors the VIEWER-gated
    # leases.py get_lease read surface.
    {
        "name": "get_lease_detail",
        "description": (
            "Read one ASC 842 lease schedule by its numeric lease_id and return its terms "
            "(classification, commencement date, term, payment, discount rate, ROU/liability "
            "accounts, status) plus its amortization rows (period-by-period payment, interest, "
            "liability reduction, ending liability, ROU amortization, ending ROU, expense). "
            "Use this to look up a specific lease before discussing or updating it. Read-only: "
            "this never writes or drafts a change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lease_id": {
                    "type": "integer",
                    "description": "The numeric id of the lease schedule to read.",
                },
            },
            "required": ["lease_id"],
        },
    },
    {
        "name": "add_schedule_row",
        "description": (
            "Add a new row to an accounting schedule. This is a direct internal write and is "
            "only available after backend confirmation; it does not post directly to QBO. "
            "Provide the expense account, vendor/name, amount, and amortization period. "
            "IMPORTANT: If no start_date is given, it defaults to today. NEVER assume a prior year — "
            "always use the CURRENT year from today's date unless the user explicitly states otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_type": {"type": "string", "enum": ["PREPAID", "FIXED_ASSET", "ACCRUAL", "INTANGIBLE"], "description": "Schedule type"},
                "expense_account": {"type": "string", "description": "Expense account name (e.g. '6210: Trade Shows')"},
                "vendor_name": {"type": "string", "description": "Vendor/payee name"},
                "amount": {"type": "number", "description": "Total dollar amount"},
                "months": {"type": "integer", "description": "Number of months to amortize over"},
                "note": {"type": "string", "description": "Optional note/description"},
                "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format. Use the current year unless the    movement explicitly says otherwise."},
            },
            "required": ["expense_account", "vendor_name", "amount", "months"],
        },
    },
    {
        "name": "update_schedule_row",
        "description": (
            "Update fields on an existing schedule row. Provide the schedule_id and any fields to change. "
            "Use get_schedule_data first to find the correct schedule_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {"type": "integer", "description": "ID of the schedule row to update"},
                "expense_account": {"type": "string", "description": "New expense account name"},
                "vendor_name": {"type": "string", "description": "New vendor name"},
                "amount": {"type": "number", "description": "New total amount"},
                "months": {"type": "integer", "description": "New amortization period in months"},
                "note": {"type": "string", "description": "New note/description"},
            },
            "required": ["schedule_id"],
        },
    },
    {
        "name": "update_schedule_month_values",
        "description": (
            "Set or clear specific monthly expense values on a schedule row. "
            "Provide month_values as a dict of period keys (YYYY-MM) to dollar amounts. "
            "Set clear_all=true to zero out all months first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {"type": "integer", "description": "ID of the schedule row"},
                "month_values": {
                    "type": "object",
                    "description": "Dict of period (YYYY-MM) to amount, e.g. {'2025-04': 500, '2025-05': 500}",
                },
                "clear_all": {"type": "boolean", "description": "If true, zero out all existing month values before applying new ones"},
            },
            "required": ["schedule_id"],
        },
    },
    {
        "name": "bulk_set_budget_drivers",
        "description": (
            "Bulk-update budget drivers for a company. By default, creates drivers for all vendor "
            "sub-lines that don't have one yet and sets them to the specified method (default: TTM Average). "
            "Can also update existing drivers that have no method set. "
            "Use this when the user asks to set all drivers to TTM average, bulk-assign driver methods, "
            "or apply a growth rate to revenue. For growth rates, use method='yoy_growth' with "
            "growth_rate=3 for 3% growth, combined with driver_category='revenue'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["ttm_avg", "pct_revenue", "fixed", "yoy_growth", "volume"],
                    "description": "Driver method to apply. ttm_avg=TTM Average, pct_revenue=% of Revenue, fixed=Fixed Amount, yoy_growth=YoY Growth (use with growth_rate), volume=Volume × Price",
                },
                "growth_rate": {
                    "type": "number",
                    "description": "Growth rate percentage to apply when method is 'yoy_growth'. E.g. 3 for 3% growth, 10 for 10% growth.",
                },
                "budget_year": {
                    "type": "integer",
                    "description": "Budget year (defaults to current year)",
                },
                "driver_category": {
                    "type": "string",
                    "enum": ["revenue", "expense"],
                    "description": "Filter to only revenue or expense drivers (default: all)",
                },
                "only_unset": {
                    "type": "boolean",
                    "description": "If true (default), only update drivers without a method. If false, override all.",
                },
            },
        },
    },
    {
        "name": "analyze_spending_trends",
        "description": (
            "COMPOSITE ANALYSIS TOOL — runs a multi-source spending analysis in one call. "
            "Fetches vendor spend data AND expense trends from QBO, then runs AI analysis "
            "to identify spending patterns, anomalies, and recommendations. "
            "Use this for questions like 'analyze our spending', 'where is our money going', "
            "'vendor spending trends', 'cost analysis'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["ttm", "monthly"],
                    "description": "'ttm' for trailing 12 month totals, 'monthly' for month-by-month trends",
                },
                "focus": {
                    "type": "string",
                    "description": "Optional focus area (e.g. 'software', 'payroll', 'marketing')",
                },
            },
        },
    },
    {
        "name": "analyze_profitability",
        "description": (
            "COMPOSITE ANALYSIS TOOL — runs a full profitability analysis in one call. "
            "Fetches the P&L report AND profit margin data from QBO, then runs AI analysis "
            "to evaluate revenue, margins, cost structure, and make recommendations. "
            "Use this for questions like 'how profitable are we', 'margin analysis', "
            "'revenue vs expenses', 'how did we do this month'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD (defaults to first of current month)",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD (defaults to today)",
                },
            },
        },
    },
    {
        "name": "investigate_account",
        "description": (
            "COMPOSITE ANALYSIS TOOL — deep-dives into a specific account in one call. "
            "Fetches the General Ledger for that account AND budget data, then runs AI analysis "
            "to show transactions, trends, budget variance, and anomalies. "
            "Use this for questions like 'what's happening with software expense', "
            "'deep dive into payroll', 'investigate rent account', 'what hit marketing this month'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_name": {
                    "type": "string",
                    "description": "Account name or partial match (e.g. 'Software', 'Payroll', 'Rent')",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD (defaults to 90 days ago)",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD (defaults to today)",
                },
            },
            "required": ["account_name"],
        },
    },
    # ── Phase 5: Report & Close Tools ──────────────────────────────────────
    {
        "name": "generate_report",
        "description": (
            "Generate a financial report (PDF board pack) for a company and period. "
            "The report includes P&L, Balance Sheet, Cash Flow, KPIs, BVA variances, "
            "AR/AP aging, and executive narrative. Returns a confirmation with the report ID. "
            "Use this when the user says 'generate the March report' or 'create the board pack'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Report period in YYYY-MM format (e.g. '2026-02' for February 2026)",
                },
            },
            "required": ["period"],
        },
    },
    {
        "name": "get_close_status",
        "description": (
            "Check the month-end close progress for a period. Returns task counts by status "
            "(open, completed, skipped), completion percentage, overdue tasks, and a list "
            "of remaining open tasks. Use this when the user asks 'how is the close going' "
            "or 'what's left on the March close'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "Close period in YYYY-MM format (e.g. '2026-03')",
                },
            },
            "required": ["period"],
        },
    },
    {
        "name": "complete_close_task",
        "description": (
            "Mark a close checklist task as complete. Use get_close_status first to see "
            "available tasks and their IDs. Returns confirmation of the completed task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "ID of the close task to mark complete",
                },
                "completed_by": {
                    "type": "string",
                    "description": "Name of person completing the task (optional, defaults to 'AI Assistant')",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_ar_ap_summary",
        "description": (
            "Get a combined Accounts Receivable and Accounts Payable snapshot. "
            "Returns total AR, total AP, aging buckets (0-30, 31-60, 61-90, 90+ days), "
            "top overdue customers, top outstanding vendors, and net working capital position. "
            "Use this for questions like 'what does our AR/AP look like', "
            "'who owes us money', 'what bills are outstanding'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_executive_briefing",
        "description": (
            "Retrieve the latest AI-generated executive briefing for the company. "
            "This is a pre-computed summary of the company's financial position including "
            "key metrics, trends, and areas of concern. Use this when the user asks for "
            "'the executive summary', 'morning briefing', or 'how are we doing overall'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "delete_schedule_row",
        "description": (
            "Delete a row from an accounting schedule (prepaid, fixed asset, accrual, or intangible). "
            "This removes the row and all its monthly entries. Use get_schedule_data first to find "
            "the correct schedule_id. This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "integer",
                    "description": "ID of the schedule row to delete",
                },
            },
            "required": ["schedule_id"],
        },
    },
    # ── Phase 6: Cash Flow Forecast AI Planning Tools ──────────────────────
    {
        "name": "get_cashflow_snapshot",
        "description": (
            "Get the current 13-week cash flow forecast grid for this company. "
            "Returns: week headers, beginning/ending balances, all customer rows, "
            "all vendor rows, totals, and bank balance. "
            "Use this FIRST before making any cash flow changes — you need to see the current state. "
            "This is the same data the user sees on their forecast page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "update_cashflow_cells",
        "description": (
            "Batch-update cells in the 13-week cash flow forecast. Each update creates or overwrites "
            "a cell override for a specific entity + week. Use this to model payment plans, delay payments, "
            "adjust collection timing, or run what-if scenarios. This edits an existing row directly. "
            "This is only available after backend confirmation. "
            "IMPORTANT: You must specify the week_ending as a Friday date (YYYY-MM-DD) matching one of "
            "the week_headers from get_cashflow_snapshot. "
            "The entity_name must exactly match a customer or vendor name from the forecast grid. "
            "All changes are saved and persist — the user can see and undo them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_name": {"type": "string", "description": "Customer or vendor name (exact match from forecast)"},
                            "entity_type": {"type": "string", "enum": ["customer", "vendor"], "description": "Whether this is a customer (cash in) or vendor (cash out)"},
                            "week_ending": {"type": "string", "description": "Friday date YYYY-MM-DD for the target week"},
                            "amount": {"type": "number", "description": "New dollar amount for this cell (replaces existing value)"},
                            "note": {"type": "string", "description": "Reason for the change (e.g. 'Payment plan - $5k/week')"},
                        },
                        "required": ["entity_name", "entity_type", "week_ending", "amount"],
                    },
                    "description": "List of cell updates to apply",
                },
            },
            "required": ["updates"],
        },
    },
    {
        "name": "layer_cashflow_schedule",
        "description": (
            "Create or update an additive manual cash flow row in the forecast instead of overwriting existing logic. "
            "Use this for accruals, payment plans, deferred balances, or known future inflows/outflows that should be "
            "expressed across the forecast horizon as their own line item. The assistant can set the typical day, "
            "average amount, and optionally weekly amounts for explicit Friday buckets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Name for the additive forecast row, e.g. 'Insurance Accrual'"},
                "entity_type": {"type": "string", "enum": ["customer", "vendor"], "description": "customer for cash in, vendor for cash out"},
                "day_of_month": {"type": "integer", "description": "Typical calendar day-of-month this row lands within the Friday-ending week"},
                "avg_amount": {"type": "number", "description": "Typical monthly amount represented by this row"},
                "weekly_amounts": {
                    "type": "array",
                    "description": "Optional explicit weekly schedule for this row",
                    "items": {
                        "type": "object",
                        "properties": {
                            "week_ending": {"type": "string", "description": "Friday date YYYY-MM-DD"},
                            "amount": {"type": "number", "description": "Cash amount for that specific week"},
                        },
                        "required": ["week_ending", "amount"],
                    },
                },
                "note": {"type": "string", "description": "Optional reason for the additive schedule"},
            },
            "required": ["entity_name", "entity_type"],
        },
    },
    {
        "name": "spread_cashflow_balance",
        "description": (
            "Take a known balance and spread it across the cash flow forecast as a new additive row. "
            "Use this when the user says things like 'spread this accrual over 6 weeks' or "
            "'lay this prepaid balance into the next 3 months'. The assistant should prefer this tool "
            "for accrued balances or deferred inflows/outflows rather than overwriting an existing customer/vendor row."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_name": {"type": "string", "description": "Name for the additive forecast row, e.g. 'Insurance Accrual'"},
                "entity_type": {"type": "string", "enum": ["customer", "vendor"], "description": "customer for cash in, vendor for cash out"},
                "total_amount": {"type": "number", "description": "Total balance to spread across the forecast"},
                "periods": {"type": "integer", "description": "Number of weeks or months to spread across"},
                "cadence": {"type": "string", "enum": ["weekly", "monthly"], "description": "Whether to spread weekly or monthly"},
                "start_week_ending": {"type": "string", "description": "Optional Friday YYYY-MM-DD to start from. Defaults to the first forecast week."},
                "day_of_month": {"type": "integer", "description": "For monthly spreads, the day-of-month that should land within the Friday-ending week"},
                "note": {"type": "string", "description": "Optional reason for the spread"},
            },
            "required": ["entity_name", "entity_type", "total_amount", "periods", "cadence"],
        },
    },
    {
        "name": "analyze_cashflow_position",
        "description": (
            "Analyze the current cash flow forecast and provide strategic recommendations. "
            "Fetches the live forecast data, examines weekly cash in/out patterns, major swings, "
            "layered schedules, and ending balance trajectory. Returns a detailed analysis with "
            "actionable suggestions. Use this when the user asks 'how does our cash look', "
            "'any cash flow concerns', 'what should I prioritize this week', or 'cash position analysis'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "Optional focus area: 'inflows', 'outflows', 'liquidity', or 'weekly_trends'",
                },
            },
        },
    },
    {
        "name": "get_cashflow_actuals_review",
        "description": (
            "Get the pulled bank transactions for a weekly cash forecast actuals review. "
            "Returns the current week's or specified week's transactions, grouped by category status "
            "(categorized, uncategorized, excluded), so the assistant can help the user review and classify them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "week_ending": {
                    "type": "string",
                    "description": "Optional Friday date YYYY-MM-DD. Defaults to the latest pulled week.",
                },
                "category": {
                    "type": "string",
                    "enum": ["all", "uncategorized", "categorized", "excluded", "decomposed", "pending"],
                    "description": "Optional category/status filter. Use 'uncategorized' or 'pending' for transactions pending categorization.",
                },
            },
        },
    },
    {
        "name": "categorize_cashflow_actuals",
        "description": (
            "Categorize one or more pulled bank transactions for the weekly cash forecast actuals review. "
            "Use this after reviewing uncategorized items with the user. You can either pass explicit transaction "
            "items by id, or bulk-update matching transactions by week_ending plus match_category. For example, "
            "to categorize all pending transactions for a week as Other Cash In, pass week_ending, "
            "match_category='uncategorized', entity_name='Other Cash In', entity_type='other_cash_in', "
            "and category='categorized'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "CashForecastActual id"},
                            "entity_name": {"type": "string", "description": "Chosen customer, vendor, or special bucket name"},
                            "entity_type": {
                                "type": "string",
                                "enum": ["customer", "vendor", "other_cash_in", "non_routine"],
                                "description": "Entity direction/type. Use other_cash_in for the Other Cash In bucket.",
                            },
                            "category": {"type": "string", "enum": ["categorized", "excluded"], "description": "Final classification state"},
                        },
                        "required": ["id"],
                    },
                    "description": "Transactions to categorize",
                },
                "week_ending": {
                    "type": "string",
                    "description": "Friday date YYYY-MM-DD for a bulk update when items are not provided.",
                },
                "match_category": {
                    "type": "string",
                    "enum": ["uncategorized", "pending", "categorized", "excluded", "all"],
                    "description": "Which existing transactions to update in a bulk operation. Defaults to uncategorized.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["in", "out"],
                    "description": "Optional direction filter for a bulk update.",
                },
                "entity_name": {
                    "type": "string",
                    "description": "Default entity/bucket name for all updated transactions, e.g. Other Cash In.",
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["customer", "vendor", "other_cash_in", "non_routine"],
                    "description": "Default entity type for all updated transactions.",
                },
                "category": {
                    "type": "string",
                    "enum": ["categorized", "excluded"],
                    "description": "Final classification state. Defaults to categorized.",
                },
            },
        },
    },
    {
        "name": "teach_peregrine",
        "description": (
            "Teach the AI a new persistent fact or rule about this company. "
            "This creates a PENDING memory proposal for review instead of silently writing long-term memory. "
            "Use this when the user gives you a standing instruction, correction, or fact "
            "(e.g., 'Moving forward, rent is $5k', 'Always classify XYZ as COGS', "
            "'We don't use this vendor anymore')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category for this fact, like 'Vendor Info', 'Accounting Rule', 'Deal Context', or 'General'",
                },
                "fact": {
                    "type": "string",
                    "description": "The clear, factual statement to remember",
                },
            },
            "required": ["category", "fact"],
        },
    },
    {
        "name": "create_macro_scenario",
        "description": (
            "Create a new budget scenario by cloning the current active budget and applying a macro multiplier. "
            "Use this when the user asks to simulate a scenario, like 'What if COGS inflates by 5%' or 'Run a downside scenario with 10% lower revenue'. "
            "It will clone all budget lines into a new scenario name, apply the multiplier to the specified account type or name, and save it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario_name": {
                    "type": "string",
                    "description": "Name for the new scenario (e.g., 'Upside 5% Inflate', 'Downside Revenue')",
                },
                "target_type": {
                    "type": "string",
                    "description": "Which accounts to target: 'cogs', 'opex', 'revenue', or 'all'",
                },
                "multiplier": {
                    "type": "number",
                    "description": "The math multiplier to apply. E.g., for 5% inflation use 1.05. For 10% drop, use 0.90.",
                },
                "budget_year": {
                    "type": "integer",
                    "description": "The year to clone. Defaults to current year if omitted.",
                },
            },
            "required": ["scenario_name", "target_type", "multiplier"],
        },
    },
    {
        "name": "analyze_lbo_balance",
        "description": (
            "Analyze the LBO model grid to debug balance check issues. "
            "Returns the projected values for key balance sheet and cash flow items "
            "(Total Assets, Liabilities, Equity, Cash, Retained Earnings, Net Income, etc.) "
            "across all projected years to help identify where the structure is failing to balance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario_name": {
                    "type": "string",
                    "description": "Name of the scenario to analyze (e.g. 'Base', 'Upside'). If omitted, uses the active scenario.",
                },
            },
        },
    },
    {
        "name": "get_lbo_sensitivity",
        "description": (
            "Get an LBO scenario's 2-D sensitivity grid of returns (IRR / MOIC) so you can "
            "critique how the deal's returns move with its key drivers. Choose the axes pair: "
            "'entry_exit' (entry multiple × exit multiple) or 'leverage_growth' (opening "
            "leverage × EBITDA growth). Also returns the base-case returns-attribution bridge "
            "(how much of the return comes from EBITDA growth vs multiple expansion vs debt "
            "paydown). Use this for 'how sensitive are our returns', 'critique this LBO', "
            "'what drives the IRR', 'stress the entry/exit multiple'. Read-only — it computes, "
            "it never changes the model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario_id": {
                    "type": "integer",
                    "description": "The LBO scenario id to analyze.",
                },
                "axes": {
                    "type": "string",
                    "enum": ["entry_exit", "leverage_growth"],
                    "description": "Which 2-D grid to return: 'entry_exit' (default) or 'leverage_growth'.",
                },
            },
            "required": ["scenario_id"],
        },
    },
    {
        "name": "propose_lbo_assumptions",
        "description": (
            "Propose changes to an LBO scenario's assumptions/drivers (e.g. revenue growth, "
            "margins, entry/exit multiple, hold period, DSO/DPO, sweep %, tax rate) for human "
            "review. Peregrine will NOT change the model from chat — this drafts a proposal the "
            "user must approve; only on approval do the assumptions actually update and the model "
            "re-solve. Pass 'scenario_id' and an 'updates' object of ONLY the assumption keys you "
            "want to change (they merge onto the scenario's current assumptions). Use this when the "
            "user asks to change / adjust / try a different assumption on an LBO scenario."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario_id": {
                    "type": "integer",
                    "description": "The LBO scenario id whose assumptions to change.",
                },
                "updates": {
                    "type": "object",
                    "description": (
                        "The assumption keys to change, mapped to their new values (e.g. "
                        "{\"entry_ebitda_multiple\": 9.0, \"exit_year\": 5}). Only the keys you "
                        "pass change; every other assumption is preserved."
                    ),
                },
            },
            "required": ["scenario_id", "updates"],
        },
    },
    # ── Phase-A read-parity tools (C2-a): live review READs ────────────────
    {
        "name": "get_covenants_status",
        "description": (
            "Get the LIVE pass/warn/breach status for every active debt covenant, computed "
            "from current QuickBooks data — the same compute the Covenants page shows. "
            "Returns each covenant's name, loan, threshold, current value, and status "
            "(pass/warn/breach) plus the overall status. "
            "Use this when the user asks 'are we in compliance', 'any covenant breaches', "
            "'how is our DSCR / leverage', or 'covenant status'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_weekly_review",
        "description": (
            "Get the weekly cash review summary — current week's inflow/outflow/net vs the "
            "prior 4-week average, with week-over-week change and flagged anomalous transactions. "
            "This mirrors the Weekly Review page. "
            "Use this for 'how was this week', 'weekly cash review', 'any unusual transactions this week'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_budget_vs_actuals",
        "description": (
            "Get LIVE Budget vs Actuals variance for a single month — actuals pulled fresh from "
            "the QuickBooks P&L (not just stored budget rows), with variance $ and variance % per "
            "line and flagged lines where |variance%| exceeds threshold. Mirrors the Budget vs "
            "Actuals page. "
            "Use this for 'how are we tracking to budget', 'budget variance for March', "
            "'where are we over/under budget'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "budget_year": {
                    "type": "integer",
                    "description": "Budget year (e.g. 2026). Defaults to the current year.",
                },
                # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 7 (SH-5) month-key contract.
                "month": {
                    "type": "string",
                    "description": "Three-letter month key (jan, feb, mar, …) — canonical. YYYY-MM (e.g. 2026-03) and full names ('March') are also accepted. Defaults to the current month.",
                },
            },
        },
    },
    {
        "name": "get_intelligence_snapshot",
        "description": (
            "Get the consolidated LIVE intelligence snapshot — a 5-dimension view (revenue trend + "
            "gross margin, top customer concentration, top vendor spend, AR aging, and transaction "
            "anomalies) computed fresh from QuickBooks. Mirrors the Intelligence page. "
            "Use this for 'give me the overview', 'business intelligence snapshot', "
            "'what should I be watching', or a broad health check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # ── C3-TOOLS wave 3.1 (plan 20260706-cfo-grade-financial-workflows, gap-register rows 1-13) ──
    # Rows 1-3: recompute-only READS (force a live QBO recompute + rewrite the read-path snapshot;
    # they invent no data and mutate no source record). Rows 4-13: proposal-gated WRITES — each is
    # DRAFTED as a ProposedAction for human approval; nothing executes from chat.
    {
        "name": "refresh_ar_ap_snapshot",
        "description": (
            "Force a fresh recompute of the AR/AP aging snapshot from QuickBooks and rewrite the "
            "read cache. Use this when the AR/AP summary looks stale or the user asks to refresh "
            "receivables/payables. Recompute-only — it invents no data and changes no financial record."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_cashflow_forecast",
        "description": (
            "Force a full rebuild of the 13-week cash-flow forecast from QuickBooks and re-snapshot "
            "it. Use this when the forecast grid looks stale or the user asks to refresh the cash "
            "forecast. Recompute-only — persists only the forecast cache, no user financial fact."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_covenant_status",
        "description": (
            "Force a live recompute of every active debt covenant's pass/warn/breach status from "
            "QuickBooks and rewrite each snapshot. Use this when covenant status looks stale or the "
            "user asks to refresh covenants. Snapshot-append only — no user data is changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of": {"type": "string", "description": "Optional closed month YYYY-MM to evaluate as of; defaults to the last closed month."},
            },
        },
    },
    {
        "name": "create_budget_line",
        "description": (
            "Draft a new budget line (account) for review. Creates a PENDING proposal — it is not "
            "written to the budget until an editor approves. Use set_budget_months "
            "to change an EXISTING line; use this only to add a brand-new account line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_name": {"type": "string", "description": "New budget account name (e.g. 'Marketing')"},
                "account_type": {"type": "string", "enum": ["revenue", "cogs", "opex"], "description": "Account category"},
                "budget_year": {"type": "integer", "description": "Budget year (defaults to current year)"},
                "scenario": {"type": "string", "description": "Scenario name (defaults to 'base')"},
                # PLAN-EXEMPT: plan 20260713-wave2-6-tool-hardening C3 — decision 7 (SH-5) month-key contract.
                "monthly_amounts": {"type": "object", "description": "Optional dict of three-letter month keys (jan..dec) to dollar amounts; YYYY-MM keys are tolerated and normalized to jan..dec before storage"},
                "notes": {"type": "string", "description": "Optional note/reason"},
            },
            "required": ["account_name", "account_type"],
        },
    },
    {
        "name": "create_custom_kpi",
        "description": (
            "Draft a new custom KPI for review. Creates a PENDING proposal. The formula is a "
            "structured formula object (formula_json), never free-form code. Use this when the user "
            "asks to track a new metric like 'revenue per employee'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "KPI name (e.g. 'Revenue per Employee')"},
                "description": {"type": "string", "description": "Optional description"},
                "unit": {"type": "string", "description": "Unit: USD, %, ratio, or custom (defaults USD)"},
                "formula_json": {"type": "object", "description": "Structured formula definition (account references + operators); never a code string"},
            },
            "required": ["name", "formula_json"],
        },
    },
    {
        "name": "update_custom_kpi",
        "description": (
            "Draft an update to an existing custom KPI for review. Creates a PENDING proposal. "
            "Provide kpi_id and any fields to change (name, description, unit, formula_json)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kpi_id": {"type": "integer", "description": "ID of the custom KPI to update"},
                "name": {"type": "string", "description": "New name"},
                "description": {"type": "string", "description": "New description"},
                "unit": {"type": "string", "description": "New unit (USD, %, ratio, custom)"},
                "formula_json": {"type": "object", "description": "New structured formula definition"},
            },
            "required": ["kpi_id"],
        },
    },
    {
        "name": "update_close_task_state",
        "description": (
            "Draft a change to a month-end close task's state — skip, reopen, or assign it — for "
            "review. Creates a PENDING proposal. Use get_close_status first to find task IDs. "
            "Use complete_close_task to mark a task complete; use this for skip/reopen/assign."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "ID of the close task"},
                "action": {"type": "string", "enum": ["skip", "reopen", "assign"], "description": "State transition to apply"},
                "reason": {"type": "string", "description": "For skip: the skip reason"},
                "assigned_to": {"type": "string", "description": "For assign: the assignee"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "description": "For assign: optional priority"},
            },
            "required": ["task_id", "action"],
        },
    },
    {
        "name": "dismiss_alert",
        "description": (
            "Draft dismissal (or restoration) of ONE anomaly alert by its id, for review. Creates a "
            "PENDING proposal. One alert at a time — bulk-silencing is not available from chat. Use "
            "get_module_context('alerts') to find alert ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_id": {"type": "integer", "description": "ID of the single alert to dismiss"},
                "dismissed": {"type": "boolean", "description": "true to dismiss (default), false to restore"},
            },
            "required": ["alert_id"],
        },
    },
    {
        "name": "acknowledge_watchdog",
        "description": (
            "Draft acknowledgement of ONE watchdog alert by its id, for review. Creates a PENDING "
            "proposal. One alert at a time — bulk-acknowledge is not available from chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_id": {"type": "integer", "description": "ID of the single watchdog alert to acknowledge"},
                "acknowledged_by": {"type": "string", "description": "Optional name of the acknowledger"},
            },
            "required": ["alert_id"],
        },
    },
    {
        "name": "recon_confirm_match",
        "description": (
            "Draft confirmation of one proposed bank-reconciliation pairing (a bank statement line "
            "matched to a book transaction), for review. Creates a PENDING proposal. Provide the "
            "session_id, bank_item_id, and book_txn_id from the reconciliation session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer", "description": "Reconciliation session id"},
                "bank_item_id": {"type": "integer", "description": "Bank statement line id"},
                "book_txn_id": {"type": "string", "description": "Book transaction id"},
            },
            "required": ["session_id", "bank_item_id", "book_txn_id"],
        },
    },
    {
        "name": "recon_unconfirm",
        "description": (
            "Draft reversal of a previously-confirmed bank-reconciliation pairing, for review. "
            "Creates a PENDING proposal. Provide session_id, bank_item_id, and book_txn_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer", "description": "Reconciliation session id"},
                "bank_item_id": {"type": "integer", "description": "Bank statement line id"},
                "book_txn_id": {"type": "string", "description": "Book transaction id"},
            },
            "required": ["session_id", "bank_item_id", "book_txn_id"],
        },
    },
    # C3-TOOLS wave 3.1c (rows 7,8,9,11,13) — the extracted-service batch. Each drafts a PENDING
    # proposal from chat; the paired ACTION_TYPE_TO_TOOL entry resolves the frozen service at
    # APPROVAL time only. Nothing executes from chat.
    {
        "name": "generate_close_tasks",
        "description": (
            "Draft generation of the month-end close task list for a period, for review. Creates a "
            "PENDING proposal. Idempotent per period — if tasks already exist for the period it "
            "skips unless replace_existing is set. Use get_close_status to see existing tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "The YYYY-MM period to generate close tasks for"},
                "replace_existing": {"type": "boolean", "description": "Delete the period's existing tasks first (default false)"},
                "template_id": {"type": "integer", "description": "Optional saved close-template id to generate from"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "create_reforecast",
        "description": (
            "Draft a budget reforecast for review — clone the base budget into a reforecast scenario "
            "and overwrite locked (closed) months with QuickBooks actuals. Creates a PENDING proposal. "
            "The base budget is never changed (clone-only). Lock the months to sync first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "budget_year": {"type": "integer", "description": "Budget year to reforecast (defaults to current year)"},
            },
        },
    },
    {
        "name": "update_budget_driver",
        "description": (
            "Draft an update to ONE budget driver's method/params, for review. Creates a PENDING "
            "proposal. Provide driver_id and the fields to change (driver_method and/or driver_params). "
            "Use get_budget_data to find driver ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "driver_id": {"type": "integer", "description": "ID of the budget driver to update"},
                "driver_method": {"type": "string", "description": "New driver method (ttm_avg | pct_revenue | fixed | yoy_growth | volume)"},
                "driver_params": {"type": "object", "description": "New structured driver params (e.g. {\"pct\": 10})"},
            },
            "required": ["driver_id"],
        },
    },
    # PLAN-EXEMPT: chunk A of ratified plan 20260714-wave2-7-lease-flux (marker
    # RATIFICATION-2026-07-14.md). create_lease — a SIMPLE single-name internal-DB write:
    # the generic proposer drafts a PENDING LEASE_CREATE proposal from these params;
    # _handle_create_lease → create_lease_service runs at approval. realm_id is NOT a
    # parameter (server-injected trusted scope). discount_rate_annual is a DECIMAL rate
    # in (0,1) — a percent-form value (5 / 6.0) is REJECTED by validate_terms, never
    # normalized. Accounts are optional at create (set them at activate).
    {
        "name": "create_lease",
        "description": (
            "Draft a NEW ASC 842 lease schedule (DRAFT status), for review. Creates a PENDING "
            "proposal. Nothing is recognized at create — the lease terms are editable until it is "
            "activated (activation measures the ROU asset / lease liability). discount_rate_annual "
            "MUST be a decimal rate in (0,1), e.g. 0.06 for 6% — a whole number like 6 is rejected. "
            "Provide the core terms; QBO account ids are set later at activation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Lease name / description of the leased asset"},
                "description": {"type": "string", "description": "Optional longer description"},
                "classification": {"type": "string", "enum": ["operating", "finance"], "description": "ASC 842 classification (any true finance criterion forces 'finance')"},
                "classification_criteria": {"type": "object", "description": "The five ASC 842-10-25-2 finance criteria as booleans (transfers_ownership, purchase_option_reasonably_certain, term_major_part_of_life, pv_substantially_all_fair_value, specialized_asset)"},
                "commencement_date": {"type": "string", "description": "Lease commencement date, YYYY-MM-DD"},
                "term_months": {"type": "integer", "description": "Lease term in months (>= 1)"},
                "payment_amount": {"type": "number", "description": "Fixed periodic (monthly) payment amount"},
                "payment_timing": {"type": "string", "enum": ["advance", "arrears"], "description": "Payment at period start (advance) or end (arrears); default arrears"},
                "escalation_steps": {"type": "array", "description": "Optional step-up schedule: [{start_month, payment}] — payment applies from that 1-based month forward", "items": {"type": "object"}},
                "discount_rate_annual": {"type": "number", "description": "Annual discount rate as a DECIMAL in (0,1), e.g. 0.06 for 6%. A percent-form value (5, 6.0) is rejected."},
                "rate_basis": {"type": "string", "enum": ["ibr", "risk_free"], "description": "Rate basis: incremental borrowing rate or risk-free; default ibr"},
                "initial_direct_costs": {"type": "number", "description": "Initial direct costs (>= 0); default 0"},
                "incentives": {"type": "number", "description": "Lease incentives received (>= 0); default 0"},
                "prepaid_amount": {"type": "number", "description": "Prepaid lease amount (>= 0); default 0"},
                "short_term_exempt": {"type": "boolean", "description": "Short-term lease election (term <= 12 months) — expensed as incurred, no ROU/liability; default false"},
                "automation_policy": {"type": "string", "enum": ["manual", "auto_propose", "auto_post_deterministic"], "description": "Automation policy for the monthly rows; default auto_propose"},
            },
            "required": ["name", "classification", "commencement_date", "term_months", "payment_amount", "discount_rate_annual"],
        },
    },
    # PLAN-EXEMPT: chunk 5 of ratified plan 20260714-wave3-2-asc606-engine (marker
    # RATIFICATION-2026-07-14.md). create_revenue_contract — a SIMPLE single-name
    # internal-DB write (the ASC 606 create tool, mirror of create_lease): the generic
    # proposer drafts a PENDING REVENUE_CONTRACT_CREATE proposal from these params;
    # _handle_create_revenue_contract → create_revenue_contract_service runs at approval.
    # realm_id is NOT a parameter (server-injected trusted scope, Control-5). The ENGINE
    # owns every recognized-revenue number at activation; the HUMAN supplies the SSP,
    # method, and ASC 606-10-25-27 criteria answers here — a fabricated/None SSP is
    # REJECTED server-side by create_revenue_contract_service, never defaulted. Nothing is
    # recognized at create (measure-at-activate); accounts are optional at create.
    {
        "name": "create_revenue_contract",
        "description": (
            "Draft a NEW ASC 606 revenue contract (DRAFT status), for review. Creates a PENDING "
            "proposal. Nothing is recognized at create — the transaction price is allocated across "
            "the performance obligations by relative standalone selling price (SSP) only when the "
            "contract is activated. The SSP, the recognition method, and (for over-time obligations) "
            "the ASC 606-10-25-27 criteria answers are REQUIRED human judgments — the engine consumes "
            "them but NEVER invents them; a missing or invalid SSP is rejected, not defaulted. Provide "
            "the contract header and at least one performance obligation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Contract name (e.g. 'Acme SaaS + implementation 2026')"},
                "description": {"type": "string", "description": "Optional longer description"},
                "customer_name": {"type": "string", "description": "Optional QBO customer name"},
                "contract_date": {"type": "string", "description": "Contract inception / agreement date, YYYY-MM-DD"},
                "transaction_price": {"type": "number", "description": "The confirmed FIXED total transaction price the engine allocates (Σ allocated ≡ this). Variable consideration is out of scope this wave."},
                "performance_obligations": {
                    "type": "array",
                    "description": (
                        "One or more distinct performance obligations. Each: obligation_index (int, "
                        "unique, ordered), name (string), ssp (number > 0 — the REQUIRED standalone "
                        "selling price the human supplies), recognition_method ('over_time' | "
                        "'point_in_time'). For an over_time obligation also supply over_time_measure "
                        "('cost_to_cost' | 'output'), recognition_criteria (the ASC 606-10-25-27 "
                        "boolean answers — customer_simultaneously_receives_and_consumes / "
                        "performance_creates_or_enhances_asset_customer_controls / "
                        "no_alternative_use_and_enforceable_right_to_payment; at least one must be "
                        "true), and for cost_to_cost also total_estimated_cost (number > 0). Optional "
                        "per obligation: deferred_revenue_account_id / _name, contract_asset_account_id "
                        "/ _name, revenue_account_id / _name."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["name", "contract_date", "transaction_price", "performance_obligations"],
        },
    },
    {
        "name": "update_lease",
        "description": (
            "Draft an update to a lease, for review. Creates a PENDING proposal that an editor must "
            "approve. A DRAFT lease accepts its terms, account ids/names and automation_policy. Once "
            "a lease is no longer DRAFT, only the *_account_name labels can be changed here — posting "
            "account ids, automation_policy, status and measurement terms are refused (use "
            "remeasure_lease for term/payment/rate changes). Provide lease_id and the fields."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lease_id": {"type": "integer", "description": "ID of the lease to update"},
                "data": {"type": "object", "description": "The lease fields to change. DRAFT: term_months, payment_amount, discount_rate_annual, classification, account ids/names, automation_policy, etc. Non-DRAFT: *_account_name fields only."},
            },
            "required": ["lease_id", "data"],
        },
    },
    {
        "name": "apply_budget_drivers",
        "description": (
            "Draft applying ALL budget drivers' computed values to their budget lines (a bulk "
            "overwrite), for review. Creates a PENDING proposal that carries a before/after diff so "
            "the approver sees the full impact before anything is written. Use for a scenario apply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "budget_year": {"type": "integer", "description": "Budget year to apply drivers for (defaults to current year)"},
                "scenario": {"type": "string", "description": "Scenario name (defaults to 'base')"},
                "force": {"type": "boolean", "description": "Apply even non-dirty drivers, overwriting manual edits (default false)"},
            },
        },
    },
    # ── C3-TOOLS wave 3.2b (plan 20260706-cfo-grade-financial-workflows, gap-register rows 14-17,
    # the HIGH-risk batch: breach-detection / ASC-842 statement impact) ──────────────────────────
    # Each tool below is proposal-gated and carries a safety_expectation AgentEvalFixture in
    # app/agent/evals.py (update_covenant_proposal / update_ebitda_config_proposal /
    # activate_lease_proposal / remeasure_lease_proposal / terminate_lease_proposal /
    # dispose_schedule_proposal) proving proposal-only routing behaviorally.
    # Rows 14/15/16 → PROPOSAL_ONLY_WRITE lane (apply-at-approval): chat-time drafts a PENDING
    # ProposedAction; the frozen service runs ONLY at approval time, resolving the entity by id.
    # The drafted payload carries a savepoint-computed before/after impact diff (the row-13 pattern)
    # so the approver sees the exact statement/breach delta. Row 17 dispose_schedule → HANDLER lane
    # (its handler self-drafts a PENDING JE proposal, like create_journal_entry — it mutates NO
    # schedule state at chat). NOTHING mutates from chat.
    {
        "name": "update_covenant",
        "description": (
            "Draft an edit to a debt covenant's threshold / definition (threshold, breach_direction, "
            "custom_formula, EBITDA add-backs, debt-service accounts), for review. Creates a PENDING "
            "proposal carrying the before/after covenant snapshot so the approver sees exactly how "
            "breach detection changes. Peregrine does not change the covenant from chat. Provide "
            "covenant_id and the fields to change; use get_covenants_status to find covenant ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "covenant_id": {"type": "integer", "description": "ID of the DebtCovenant to edit"},
                "updates": {
                    "type": "object",
                    "description": (
                        "The patch object of covenant fields to change: threshold (number), "
                        "breach_direction ('BELOW'|'ABOVE'), custom_formula (string), "
                        "ebitda_addbacks (list of add-back lines), debt_service_accounts (list), "
                        "warn_cushion (number), active (boolean). Add-backs are an explicit account "
                        "mapping — never inferred from a name."
                    ),
                },
            },
            "required": ["covenant_id", "updates"],
        },
    },
    {
        "name": "update_ebitda_config",
        "description": (
            "Draft a change to the company's single EBITDA add-back definition (the mapping every "
            "EBITDA covenant — DSCR and Debt/EBITDA — computes from), for review. Creates a PENDING "
            "proposal carrying the before/after add-back mapping so the approver sees how the "
            "covenant EBITDA inputs change. Add-backs are an explicit account mapping supplied "
            "verbatim, never keyword-inferred. Peregrine does not change EBITDA from chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "addbacks": {
                    "type": "array",
                    "description": (
                        "The full replacement list of add-back line objects (each: label, kind, "
                        "amount, account_ref, sign, category). This REPLACES the existing mapping."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["addbacks"],
        },
    },
    {
        "name": "activate_lease",
        "description": (
            "Draft activation of a DRAFT ASC-842 lease, for review. Creates a PENDING proposal "
            "carrying the before/after recognition (initial liability + ROU) so the approver sees "
            "the statement impact. Activation measures the lease at the locked discount rate, "
            "persists the amortization schedule, and drafts the commencement JE — ALL at approval "
            "time only. Peregrine does not activate the lease from chat. Provide lease_id and the "
            "source accounts the commencement JE moves amounts out of."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lease_id": {"type": "integer", "description": "ID of the DRAFT lease to activate"},
                "idc_source_account_id": {"type": "string", "description": "Account initial direct costs currently sit in (required if IDC > 0)"},
                "idc_source_account_name": {"type": "string", "description": "Name of the IDC source account"},
                "prepaid_source_account_id": {"type": "string", "description": "Account prepaid rent currently sits in (required if prepaid > 0)"},
                "prepaid_source_account_name": {"type": "string", "description": "Name of the prepaid source account"},
                "incentive_source_account_id": {"type": "string", "description": "Account lease incentives currently sit in (required if incentives > 0)"},
                "incentive_source_account_name": {"type": "string", "description": "Name of the incentive source account"},
            },
            "required": ["lease_id"],
        },
    },
    {
        "name": "remeasure_lease",
        "description": (
            "Draft a remeasurement of an ACTIVE ASC-842 lease (a modification: changed payment, "
            "term, escalation, or an explicit rate change), for review. Creates a PENDING proposal "
            "carrying the before/after remeasured liability + ROU so the approver sees the statement "
            "impact. The schedule rows are rebuilt only when the proposal EXECUTES. Peregrine does "
            "not remeasure from chat. A payment or term change is remeasured at an UPDATED rate: "
            "pass new_discount_rate_annual with new_rate_basis. Only a payment-only reassessment "
            "named by rate_unchanged_reason keeps the original locked rate; without either, approval "
            "refuses the remeasurement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lease_id": {"type": "integer", "description": "ID of the ACTIVE lease to remeasure"},
                "effective_period": {"type": "string", "description": "The YYYY-MM period the remeasurement takes effect"},
                "new_payment_amount": {"type": "number", "description": "New fixed monthly payment (optional)"},
                "new_escalation_steps": {
                    "type": "array",
                    "description": "New escalation schedule: [{start_month, payment}, ...] (optional)",
                    "items": {"type": "object"},
                },
                "new_term_months": {"type": "integer", "description": "New remaining term in months (optional)"},
                "new_discount_rate_annual": {"type": "number", "description": "New annual discount rate — must be paired with new_rate_basis (optional)"},
                "new_rate_basis": {"type": "string", "enum": ["ibr", "risk_free"], "description": "The basis for a rate change — required when new_discount_rate_annual is given"},
                "rate_unchanged_reason": {
                    "type": "string",
                    "enum": ["residual_value_guarantee", "contingency_resolution"],
                    "description": "Payment-only reassessment that keeps the original rate (cannot be combined with a new rate or a term change)",
                },
            },
            "required": ["lease_id", "effective_period"],
        },
    },
    {
        "name": "terminate_lease",
        "description": (
            "Draft termination of an ACTIVE ASC-842 lease, for review. Creates a PENDING proposal "
            "carrying the before/after and the derecognition gain/loss so the approver sees the "
            "statement impact. gain/loss = remaining liability − ROU net book value − settlement. "
            "The lease flips TERMINATED only when the proposal EXECUTES. Peregrine does not "
            "terminate from chat. Provide lease_id, the termination_date, and any settlement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lease_id": {"type": "integer", "description": "ID of the ACTIVE lease to terminate"},
                "termination_date": {"type": "string", "description": "Termination date YYYY-MM-DD"},
                "settlement_amount": {"type": "number", "description": "Cash settlement paid/received at termination (default 0)"},
                "settlement_account_id": {"type": "string", "description": "Account the settlement cash moves through (required if settlement > 0)"},
                "settlement_account_name": {"type": "string", "description": "Name of the settlement account"},
                "gain_loss_account_id": {"type": "string", "description": "P&L account for the derecognition gain/loss (optional; resolved by default)"},
                "gain_loss_account_name": {"type": "string", "description": "Name of the gain/loss account"},
                "skip_draft_rows": {"type": "boolean", "description": "Skip unposted DRAFT schedule rows at/after termination (default false)"},
            },
            "required": ["lease_id", "termination_date"],
        },
    },
    {
        "name": "dispose_schedule",
        "description": (
            "Draft disposal of a fixed-asset or intangible accounting schedule, for review. This "
            "DRAFTS a PENDING journal-entry proposal (NBV, gain/loss on disposal) — it changes NO "
            "schedule state from chat; the schedule flips DISPOSED only when the JE proposal is "
            "approved and executes. gain/loss = proceeds − net book value. No depreciation is taken "
            "in the disposal month. Provide schedule_id, disposal_date, any proceeds, and the "
            "asset cost + proceeds + gain/loss accounts. Use get_schedule_data to find schedule ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {"type": "integer", "description": "ID of the FIXED_ASSET or INTANGIBLE schedule to dispose"},
                "disposal_date": {"type": "string", "description": "Disposal date YYYY-MM-DD"},
                "proceeds": {"type": "number", "description": "Sale proceeds (default 0)"},
                "proceeds_account_id": {"type": "string", "description": "Account the proceeds were/will be deposited to (required if proceeds > 0)"},
                "proceeds_account_name": {"type": "string", "description": "Name of the proceeds account"},
                "asset_cost_account_id": {"type": "string", "description": "The QBO fixed-asset COST account the purchase was capitalized to (required)"},
                "asset_cost_account_name": {"type": "string", "description": "Name of the asset cost account"},
                "gain_loss_account_id": {"type": "string", "description": "P&L account for the gain/loss on disposal (optional; resolved by default)"},
                "gain_loss_account_name": {"type": "string", "description": "Name of the gain/loss account"},
                "skip_draft_entries": {"type": "boolean", "description": "Skip unposted DRAFT schedule entries at/after disposal (default false)"},
            },
            "required": ["schedule_id", "disposal_date"],
        },
    },
    # ── C3-DELETES (plan 20260706-cfo-grade, gap-register row 18): the 8 DESTRUCTIVE
    # single-entity delete tools. Each drafts a PENDING <ENTITY>_DELETE ProposedAction from
    # chat (NOTHING deletes from chat); the paired ACTION_TYPE_TO_TOOL entry resolves the
    # handler at APPROVAL time only, which re-checks existence + tenant ownership under the
    # pinned realm before deleting the single named entity. Single-id only — NEVER bulk.
    {
        "name": "delete_budget_line",
        "description": (
            "Delete a single budget line by its id. This removes the line and its vendor/customer "
            "sub-lines. Use get_budget_data first to find the line_id. This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "integer", "description": "ID of the budget line to delete"},
            },
            "required": ["line_id"],
        },
    },
    {
        "name": "delete_budget_driver",
        "description": (
            "Delete a single budget driver by its id. Use get_budget_data first to find the "
            "driver_id. This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "driver_id": {"type": "integer", "description": "ID of the budget driver to delete"},
            },
            "required": ["driver_id"],
        },
    },
    {
        "name": "delete_close_task",
        "description": (
            "Delete a single month-end close task by its id. Use get_close_status first to find the "
            "task_id. This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "ID of the close task to delete"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_covenant",
        "description": (
            "Delete a single debt covenant by its id. This removes the covenant and its stored "
            "period snapshot records. Use get_covenants_status first to find the covenant_id. "
            "This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "covenant_id": {"type": "integer", "description": "ID of the covenant to delete"},
            },
            "required": ["covenant_id"],
        },
    },
    {
        "name": "delete_custom_kpi",
        "description": (
            "Delete a single custom KPI definition by its id. Use get_intelligence_snapshot first to "
            "find the kpi_id. This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kpi_id": {"type": "integer", "description": "ID of the custom KPI to delete"},
            },
            "required": ["kpi_id"],
        },
    },
    {
        "name": "delete_cashflow_override",
        "description": (
            "Delete a single cash-flow forecast cell override by its id. This removes ONE override "
            "row (one entity + week). Use get_cashflow_snapshot to inspect the forecast first. "
            "This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "override_id": {"type": "integer", "description": "ID of the cash-flow override to delete"},
            },
            "required": ["override_id"],
        },
    },
    {
        "name": "delete_document",
        "description": (
            "Delete a single stored document by its id. Use search_documents first to find the "
            "document_id. This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "ID of the document to delete"},
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "delete_knowledge",
        "description": (
            "Delete a single piece of taught company knowledge by its id (for removing an incorrect "
            "fact). This action cannot be undone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "knowledge_id": {"type": "integer", "description": "ID of the knowledge entry to delete"},
            },
            "required": ["knowledge_id"],
        },
    },
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening A1 — registry integrity requires every
    # handler to carry a real input schema (the synthetic "Legacy backend handler" definition
    # validated nothing). reclassify_transaction was the last handler-only tool: its chat handler
    # self-drafts a PENDING RECLASSIFICATION ProposedAction for human approval (proposal-only —
    # safety_expectation AgentEvalFixture reclassify_transaction_proposal in app/agent/evals.py).
    # It stays in NO intent bundle, so it is not offered to the model — this entry documents the
    # true contract without changing reachability.
    # QBO-OK: balanced-upstream idempotent-upstream — this is a schema-only tool description (no
    # QBO write here); the actual write runs at approval time through execute_proposed_action,
    # which owns the balance assert and the SHA-256 execution idempotency ledger.
    {
        "name": "reclassify_transaction",
        "description": (
            "Draft a ProposedAction to reclassify a QuickBooks transaction to a different account. "
            "Peregrine will not directly modify the transaction from chat; the reclassification "
            "executes only after human approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "QBO entity type (e.g. Purchase, JournalEntry, Deposit)",
                },
                "txn_id": {"type": "string", "description": "QBO transaction id to reclassify"},
                "to_account_id": {"type": "string", "description": "Target account id"},
                "to_account_name": {"type": "string", "description": "Target account name"},
                "reason": {"type": "string", "description": "Why the transaction should be reclassified"},
                "updates": {
                    "type": "object",
                    "description": "Sparse QBO update fields applied at approval time",
                },
            },
            "required": ["entity", "txn_id"],
        },
    },
    # PLAN-EXEMPT: ratified plan 20260713-wave0-ai-integration-p0 chunk C1 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this worktree).
    # ── Wave-0 C1 (TG-2/TG-3): the two fully-built but previously chat-UNREACHABLE proposal
    # lanes, exposed as HANDLER-lane tools (the create_journal_entry pattern). Each handler
    # self-drafts a PENDING ProposedAction via app/qbo_actions/proposer.py
    # (propose_vendor_update / propose_je_reversal) — NOTHING writes to QBO from chat;
    # execution happens only after human approval through the existing QBO executor
    # (executor.py UPDATE_VENDOR / POST_JE). Safety fixtures: update_vendor_proposal /
    # reverse_journal_entry_proposal in app/agent/evals.py.
    # QBO-OK: balanced-upstream idempotent-upstream — schema-only tool descriptions (no QBO
    # write here); the actual writes run at approval time through execute_proposed_action,
    # which owns the JE balance assert and the SHA-256 execution idempotency ledger.
    {
        "name": "update_vendor",
        "description": (
            "Draft an update to a QuickBooks vendor's details (display name, contact info, "
            "active flag, etc.) for human review. Creates a PENDING proposal through the same "
            "vendor-update lane the admin proposals endpoint uses; Peregrine will not change "
            "the vendor from chat — the update executes only after approval. Provide the QBO "
            "vendor_id (use search_finance_records with entity 'vendor' to find it) and a "
            "sparse 'updates' object containing only the QBO Vendor fields being changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vendor_id": {"type": "string", "description": "QBO Vendor Id to update"},
                "updates": {
                    "type": "object",
                    "description": (
                        "Sparse QBO Vendor fields to change, e.g. DisplayName, CompanyName, "
                        "PrintOnCheckName, Active, PrimaryEmailAddr, PrimaryPhone, BillAddr. "
                        "Include ONLY the fields being changed."
                    ),
                },
                "reason": {"type": "string", "description": "Why the vendor is being updated"},
            },
            "required": ["vendor_id", "updates"],
        },
    },
    {
        "name": "reverse_journal_entry",
        "description": (
            "Draft a reversing journal entry for an EXECUTED journal-entry proposal, for human "
            "review. Creates a PENDING proposal through the same reversal lane as the dashboard "
            "Reverse button (every line's debit/credit swapped, original date kept so the "
            "closed-period guard still applies, linked to the original in both directions). "
            "ALWAYS use this instead of hand-building a mirror entry with create_journal_entry. "
            "Only an executed journal entry that is neither itself a reversal nor already "
            "reversed can be reversed; ineligible targets return the reason instead of a draft. "
            "Provide the action_id of the executed journal-entry proposal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "integer",
                    "description": "Id of the EXECUTED journal-entry ProposedAction to reverse",
                },
                "reason": {"type": "string", "description": "Why the entry is being reversed"},
            },
            "required": ["action_id"],
        },
    },
    # PLAN-EXEMPT: ratified plan 20260713-wave2-1-ar-collections chunk C2 (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent).
    # ── Wave-2.1 C2 (TG-1/WF-2): the four AR collections chat tools on the Wave-0
    # recipe. Each handler self-drafts ONE PENDING ProposedAction via
    # app/qbo_actions/proposer.py (propose_invoice_creation / propose_invoice_reminder /
    # propose_customer_payment / propose_credit_memo) — NOTHING writes to QBO from chat;
    # execution happens only after human approval through the AR executor lane
    # (app/qbo_actions/ar_actions.py: closed-period date-map, send throttle,
    # recipient lock, reconcile probes). Decision 2 (E9): the send tool exposes NO
    # recipient field anywhere — QBO delivers to the invoice's own BillEmail; a
    # model-suppliable recipient would be a prompt-injection exfiltration lane.
    # Paired safety_expectation AgentEvalFixtures create_invoice_proposal /
    # send_invoice_reminder_proposal / record_customer_payment_proposal /
    # create_credit_memo_proposal land with chunk C3 in app/agent/evals.py.
    # QBO-OK: balanced-upstream idempotent-upstream — schema-only tool descriptions (no
    # QBO write here); the actual writes run at approval time through
    # execute_proposed_action, which owns the SHA-256 execution idempotency ledger.
    {
        "name": "create_invoice",
        "description": (
            "Draft a new QuickBooks customer invoice for human review. Creates a PENDING "
            "proposal — Peregrine will not create the invoice from chat; it is created in "
            "QuickBooks only after the user approves it in the Actions page. Resolve the "
            "customer first with search_finance_records entity 'customer' to get the QBO "
            "customer id. Every line needs a product/service item: pass item_id if known, "
            "or item_name — the draft resolves item_name against the QBO product/service "
            "list and REFUSES with the candidate list when the name does not match exactly "
            "(it never guesses an item). Tax follows the QuickBooks company defaults."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "QBO Customer Id (resolve via search_finance_records entity 'customer')",
                },
                "txn_date": {
                    "type": "string",
                    "description": "Invoice date YYYY-MM-DD (defaults to today when omitted)",
                },
                "due_date": {"type": "string", "description": "Optional due date YYYY-MM-DD"},
                "memo": {"type": "string", "description": "Optional private memo for the invoice"},
                "lines": {
                    "type": "array",
                    "description": (
                        "Invoice lines — at least one. Each line needs item_id OR item_name "
                        "(unresolvable names are refused with candidates, never guessed)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "string", "description": "QBO Item Id if known"},
                            "item_name": {
                                "type": "string",
                                "description": "QBO product/service name (resolved to an ItemRef at draft time)",
                            },
                            "amount": {"type": "number", "description": "Line total amount"},
                            "qty": {"type": "number", "description": "Optional quantity"},
                            "unit_price": {"type": "number", "description": "Optional unit price"},
                            "description": {"type": "string", "description": "Optional line description"},
                        },
                        "required": ["amount"],
                    },
                },
                "reason": {"type": "string", "description": "Why the invoice is being created"},
            },
            "required": ["customer_id", "lines"],
        },
    },
    {
        "name": "send_invoice_reminder",
        "description": (
            "Draft a payment-reminder email for an existing unpaid QuickBooks invoice, for "
            "human review. Creates a PENDING proposal — nothing is sent from chat; after the "
            "user approves it in the Actions page, QuickBooks itself emails its standard "
            "reminder (with the invoice PDF) to the billing email already on the invoice. "
            "The destination cannot be chosen or changed here — there is no recipient input. "
            "Find the invoice id via the AR reads (get_ar_ap_summary, run_qbo_report) or "
            "search_finance_records. Repeat reminders for the same invoice are throttled "
            "server-side at execution time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "string",
                    "description": "QBO Invoice Id to remind on (find via the AR reads)",
                },
                "reason": {"type": "string", "description": "Why the reminder is being sent"},
            },
            "required": ["invoice_id"],
        },
    },
    {
        "name": "record_customer_payment",
        "description": (
            "Draft a customer payment received against an open QuickBooks invoice, for human "
            "review. Creates a PENDING proposal — nothing is recorded from chat; the payment "
            "posts only after the user approves it, and the invoice's open balance is "
            "re-checked at that moment (an overpayment is refused, never clipped). The "
            "customer is taken from the invoice itself. Partial payments are allowed up to "
            "the open balance. The deposit account defaults to Undeposited Funds unless "
            "deposit_to_account_id is provided. Find the invoice id via the AR reads "
            "(get_ar_ap_summary, run_qbo_report) or search_finance_records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "string",
                    "description": "QBO Invoice Id the payment applies to",
                },
                "amount": {"type": "number", "description": "Payment amount received"},
                "payment_date": {
                    "type": "string",
                    "description": "Payment date YYYY-MM-DD (defaults to today when omitted)",
                },
                "deposit_to_account_id": {
                    "type": "string",
                    "description": "Optional QBO deposit account Id (omit for Undeposited Funds)",
                },
                "memo": {"type": "string", "description": "Optional private memo for the payment"},
                "reason": {"type": "string", "description": "Why the payment is being recorded"},
            },
            "required": ["invoice_id", "amount"],
        },
    },
    {
        "name": "create_credit_memo",
        "description": (
            "Draft a QuickBooks credit memo for a customer, for human review. Creates a "
            "PENDING proposal — Peregrine will not create the credit memo from chat; it is "
            "created in QuickBooks only after the user approves it in the Actions page. "
            "Resolve the customer first with search_finance_records entity 'customer'. Lines "
            "follow the same contract as create_invoice: item_id if known, or item_name "
            "resolved at draft time (unresolvable names are refused with the candidate list, "
            "never guessed). Tax follows the QuickBooks company defaults."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "QBO Customer Id (resolve via search_finance_records entity 'customer')",
                },
                "txn_date": {
                    "type": "string",
                    "description": "Credit memo date YYYY-MM-DD (defaults to today when omitted)",
                },
                "memo": {"type": "string", "description": "Optional private memo for the credit memo"},
                "lines": {
                    "type": "array",
                    "description": (
                        "Credit memo lines — at least one. Each line needs item_id OR item_name "
                        "(unresolvable names are refused with candidates, never guessed)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "string", "description": "QBO Item Id if known"},
                            "item_name": {
                                "type": "string",
                                "description": "QBO product/service name (resolved to an ItemRef at draft time)",
                            },
                            "amount": {"type": "number", "description": "Line total amount"},
                            "qty": {"type": "number", "description": "Optional quantity"},
                            "unit_price": {"type": "number", "description": "Optional unit price"},
                            "description": {"type": "string", "description": "Optional line description"},
                        },
                        "required": ["amount"],
                    },
                },
                "reason": {"type": "string", "description": "Why the credit memo is being issued"},
            },
            "required": ["customer_id", "lines"],
        },
    },
    # PLAN-EXEMPT: ratified plan 20260713-wave2-2-ap-batches chunk D (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent).
    # ── Wave-2.2 D (WF-3a/3b, decisions 9/10/11): the AP payment-batch chat tool, a
    # HANDLER-lane self-drafter. The handler reads the EXISTING prioritization
    # (app/routers/ap_workflow.py _prioritized_bills_payload) and drafts ONE PENDING
    # BILL_PAY ProposedAction via app/qbo_actions/proposer.py
    # (propose_bill_payment_batch — Bank-account validation, extant-execution
    # exclusions with named reasons, the AP_AGENT_BATCH_MAX_TOTAL draft cap) —
    # NOTHING pays a bill from chat; execution is human-approval-gated through the
    # BILL_PAY executor arm (app/qbo_actions/ap_actions.py). Decision 9: NO
    # recipient/vendor-override fields exist anywhere on this lane — the bills come
    # from the QBO prioritization read, payment_method is check-only, and the
    # payee is each bill's own vendor. Paired safety_expectation AgentEvalFixture
    # propose_payment_batch_proposal lands in app/agent/evals.py (AP-T09 lockstep).
    # QBO-OK: balanced-upstream idempotent-upstream — schema-only tool description
    # (no QBO write here); the actual BillPayment posts run at approval time through
    # execute_proposed_action → the shared core's per-bill idempotency ledger.
    {
        "name": "propose_payment_batch",
        "description": (
            "Draft an accounts-payable payment batch from the company's open bills, for "
            "human review. Creates a PENDING proposal — Peregrine will not pay any bill "
            "from chat; the batch executes only after the user approves it in the Actions "
            "page, and every payment is check-only from the named bank account. Bills are "
            "selected by the existing AP prioritization (due_date, discount, or "
            "cash_constrained with available_cash); each bill is paid to its own vendor — "
            "the payee cannot be chosen or changed here. Bills already covered by another "
            "payment batch are excluded with a named reason, and batches over the agent "
            "draft cap are refused. Resolve the bank account id first with "
            "search_chart_of_accounts (it must be a QBO Bank account)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_account_id": {
                    "type": "string",
                    "description": (
                        "QBO Bank account Id the checks draw on (resolve via "
                        "search_chart_of_accounts; non-Bank accounts are refused)"
                    ),
                },
                "txn_date": {
                    "type": "string",
                    "description": "Payment date YYYY-MM-DD carried on every check in the batch",
                },
                "strategy": {
                    "type": "string",
                    "description": (
                        "Prioritization strategy: due_date (most overdue first, default), "
                        "discount (early-pay discounts first), or cash_constrained "
                        "(fit within available_cash)"
                    ),
                },
                "available_cash": {
                    "type": "number",
                    "description": "Cash budget for the cash_constrained strategy (ignored otherwise)",
                },
                "max_bills": {
                    "type": "integer",
                    "description": "Maximum open bills to consider (default 25, capped at 50)",
                },
                "batch_name": {"type": "string", "description": "Optional display name for the batch"},
                "reason": {"type": "string", "description": "Why the payment batch is being drafted"},
            },
            "required": ["payment_account_id", "txn_date"],
        },
    },
    # PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk A (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.3 (TG-4/WF-4, decision 3
    # TWO-NAME architecture): 4 VISIBLE tools (bundled in the reconciliation
    # intent) + 2 HIDDEN execution-only tools (registered here because integrity
    # check #4 requires schema↔handler pairing; they are in NO bundle and in
    # MODEL_HIDDEN_TOOLS, so the model can never call them — they exist as the
    # RECON_SESSION_OPEN / RECON_BULK_CONFIRM approval-time executors). Paired
    # safety_expectation AgentEvalFixtures open_recon_session_proposal /
    # propose_bulk_matches_proposal / recon_session_open_exec_fixture /
    # recon_bulk_confirm_exec_fixture (app/agent/evals.py, AP-T09 lockstep).
    # NOTE: the header tool-count pin (85) is bumped 85→91 by the ORCHESTRATOR
    # post-wave together with its test literal (plan §Decomposition, E2).
    {
        "name": "open_recon_session",
        "description": (
            "Open (or resume) a bank-reconciliation session for one bank/credit-card account "
            "and statement period. Creates a PENDING proposal for a routine internal change: it "
            "can apply immediately (audited) only when this workspace has turned on routine-write "
            "autonomy and the requesting user is an editor or above; otherwise the session opens "
            "only after an editor approves it in the Actions page. The draft is "
            "refused honestly when the account already has an OPEN session (resume that one "
            "instead), when the period is already reconciled through the clean-start anchor "
            "(reopen the prior reconciliation), or when the account has no clean-start anchor "
            "configured (set the clean start first). Pass the exact controlled, parsed "
            "statement_import_id shown by statement import history/progress; the tool never "
            "guesses an import from account/date fields. Resolve the account id with "
            "search_chart_of_accounts if needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "statement_import_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Exact controlled parsed statement import id to freeze into the session",
                },
                "account_id": {"type": "string", "description": "QBO bank/credit-card Account Id to reconcile"},
                "account_name": {"type": "string", "description": "Display name of the account (defaults to the id)"},
                "period_start": {"type": "string", "description": "Statement period start, YYYY-MM-DD"},
                "period_end": {"type": "string", "description": "Statement period end (the statement closing date), YYYY-MM-DD"},
                "ending_balance": {"type": "number", "description": "The statement CLOSING balance the cleared balance must tie out to"},
            },
            "required": [
                "statement_import_id",
                "account_id",
                "period_start",
                "period_end",
                "ending_balance",
            ],
        },
    },
    {
        "name": "propose_bulk_matches",
        "description": (
            "Run one bounded reconciliation suggestion page over a session's UNMATCHED "
            "items and draft ONE bulk-confirmation proposal (the suggested "
            "bank-line ↔ book-transaction pairs with a per-pair rationale), for human review. "
            "Creates a PENDING proposal — nothing is confirmed from chat; at approval every "
            "pair is re-validated against the live data and stale pairs are skipped, never "
            "force-confirmed. If no confident suggestions exist, nothing is drafted (use "
            "list_unmatched_items to review the leftovers). When a next-page or single-use "
            "retry cursor is returned, pass that exact opaque string in a new explicit call; "
            "the tool never follows cursors or spends on retries automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer", "description": "OPEN reconciliation session id"},
                "max_pairs": {"type": "integer", "description": "Maximum pairs to include (default 40, capped at 40)"},
                "cursor": {
                    "type": "string",
                    "description": (
                        "Optional opaque next-page or retry cursor returned by the prior "
                        "propose_bulk_matches call; omit for the first page"
                    ),
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "list_unmatched_items",
        "description": (
            "List a reconciliation session's UNMATCHED items — side='bank' (statement lines "
            "with no book counterpart, each with a why_not_matched reason) or side='book' "
            "(book transactions with no statement line). Read-only. Paginated on a stable id "
            "cursor: pass the returned next_cursor to get the next page (default 25 items, "
            "max 100)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer", "description": "Reconciliation session id"},
                "side": {"type": "string", "description": "'bank' (default) or 'book'"},
                "cursor": {"type": "integer", "description": "Id cursor from the prior page's next_cursor (omit for the first page)"},
                "limit": {"type": "integer", "description": "Items per page (default 25, max 100)"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_recon_tieout",
        "description": (
            "Read a reconciliation session's tie-out (opening anchor + cleared transactions "
            "vs the statement ending balance) plus the AR/AP subledger-to-GL tie-out, sharing "
            "one Balance Sheet fetch. Read-only; completion itself is a human act — this tool "
            "presents everything needed, it cannot complete the reconciliation. NOTE: the "
            "subledger legs make several uncached QuickBooks report calls and can take 5–15 "
            "seconds; pass sides=[] to skip them and return only the session tie-out."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer", "description": "Reconciliation session id"},
                "sides": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subledger sides to tie out: any of 'ar', 'ap' (default both; [] skips the QBO calls)",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "recon_session_open_exec",
        "description": (
            "EXECUTION-ONLY (not offered to the model): runs an approved RECON_SESSION_OPEN "
            "proposal — opens (or idempotently resumes) the reconciliation session via the "
            "guarded service, re-checking the exact controlled import and open guards at "
            "execution. Drafted by "
            "open_recon_session; executes only after human approval in the Actions page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "statement_import_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Exact controlled statement import id frozen by the approved proposal",
                },
                "account_id": {"type": "string", "description": "QBO Account Id"},
                "account_name": {"type": "string", "description": "Account display name"},
                "period_start": {"type": "string", "description": "YYYY-MM-DD"},
                "period_end": {"type": "string", "description": "YYYY-MM-DD"},
                "ending_balance": {"type": "string", "description": "Statement closing balance (decimal string)"},
            },
            "required": [
                "statement_import_id",
                "account_id",
                "period_start",
                "period_end",
                "ending_balance",
            ],
        },
    },
    {
        "name": "recon_bulk_confirm_exec",
        "description": (
            "EXECUTION-ONLY (not offered to the model): runs an approved RECON_BULK_CONFIRM "
            "proposal — iterates ONLY the stored pair list with per-pair liveness re-validation "
            "(session OPEN; book transaction uncleared; bank line present and unclaimed) and "
            "confirms the still-live subset; stale pairs are skipped with named reasons. Never "
            "re-runs the suggest engine. Drafted by propose_bulk_matches; executes only after "
            "human approval in the Actions page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer", "description": "Reconciliation session id"},
                "pairs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "bank_item_id": {"type": "integer", "description": "Bank statement line id"},
                            "book_txn_id": {"type": "integer", "description": "Book transaction id"},
                        },
                        "required": ["bank_item_id", "book_txn_id"],
                    },
                    "description": "The drafted pair list (max 40)",
                },
            },
            "required": ["session_id", "pairs"],
        },
    },
    # PLAN-EXEMPT: chunk D of ratified plan 20260714-wave3-1-agent-plans (marker
    # RATIFICATION-2026-07-14.md). WF-12 (decision 3, E1 — the two-name split): the
    # VISIBLE self-drafter propose_agent_plan (its schema IS the DAG the model composes),
    # the VISIBLE read get_agent_plan_status, and the HIDDEN approval-time executor
    # agent_plan_activate_exec (registered here because integrity check #4 requires
    # schema↔handler pairing; it is in NO bundle + MODEL_HIDDEN_TOOLS, so the model can
    # never call it). Paired safety_expectation AgentEvalFixtures propose_agent_plan_proposal
    # / agent_plan_activate_exec_fixture (app/agent/evals.py, AP-T09 lockstep).
    {
        "name": "propose_agent_plan",
        "description": (
            "Compose a novel MULTI-STEP plan (a checklist of actions / 'do these steps' / a "
            "multi-step workflow) as a small dependency DAG over Peregrine's OWN tools, and "
            "draft it for human review. Creates ONE PENDING proposal — NOTHING runs from "
            "chat: the whole plan is human-approved before step 1 runs, and every WRITE step "
            "still drafts its own separate proposal that needs its own approval. Steps are a "
            "list of {step_index, tool_name, tool_input, depends_on}; depends_on lists the "
            "step_index values a step waits on (a DAG, no cycles). Bounds: ≤20 steps, ≤5 write "
            "steps. Write steps must be internal-DB proposal tools; read tools run automatically "
            "once approved. Never put a tenant/company/realm id in a step input — scope is "
            "server-side. A plan that fails validation is refused with EVERY reason listed and "
            "nothing is drafted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short human title for the plan"},
                "goal": {"type": "string", "description": "What the plan should accomplish, in one sentence"},
                "steps": {
                    "type": "array",
                    "description": "The plan DAG (≤20 steps, ≤5 write steps)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_index": {"type": "integer", "description": "Unique integer id for this step; depends_on references it"},
                            "tool_name": {"type": "string", "description": "The Peregrine tool this step calls (must exist and be model-visible)"},
                            "tool_input": {"type": "object", "description": "The tool's arguments (NO scope keys — scope is injected server-side)"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "step_index values this step waits on (empty = ready first)",
                            },
                        },
                        "required": ["step_index", "tool_name"],
                    },
                },
            },
            "required": ["title", "goal", "steps"],
        },
    },
    {
        "name": "get_agent_plan_status",
        "description": (
            "Read one agent plan's status and per-step progress (status, attempts, park "
            "reasons, and the linked proposal for each write step). Read-only. Use the plan_id "
            "returned when the plan was drafted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "integer", "description": "The agent plan id"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "agent_plan_activate_exec",
        "description": (
            "EXECUTION-ONLY (not offered to the model): runs an approved AGENT_PLAN proposal — "
            "re-hashes the persisted plan steps against the sealed steps_hash and refuses on any "
            "tamper, creates the schedule-disabled holder objective, flips the plan ACTIVE, and "
            "enqueues the first walk. Drafted by propose_agent_plan; executes only after human "
            "approval in the Actions page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "integer", "description": "The DRAFT agent plan id to activate"},
            },
            "required": ["plan_id"],
        },
    },
    # PLAN-EXEMPT: ratified plan 20260713-wave2-5-cecl-allowance chunk B (marker
    # RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
    # dispatched session — standing precedent). Wave-2.5 B (RK-3c, decision 5):
    # ONE self-drafting CECL tool. Deliberately NO account params (the explicit
    # mapping set via PUT /cecl-config is the only account source — never keyword
    # inference; a model-suppliable account would bypass the mapping discipline)
    # and NO amount params (the engine computes; a model-suppliable amount would
    # bypass the deterministic estimate). The registry-injected _dry_run IS the
    # estimate-only read (no separate read tool — decision 5). Paired
    # safety_expectation AgentEvalFixture propose_allowance_je_proposal
    # (app/agent/evals.py, AP-T09 lockstep).
    # QBO-OK: balanced-upstream idempotent-upstream — schema-only tool description
    # (no QBO write here); the JE posts at approval time through
    # execute_proposed_action, which owns the balance assert and the SHA-256
    # execution idempotency ledger.
    {
        "name": "propose_allowance_je",
        "description": (
            "Estimate the allowance for credit losses (CECL / ASC 326, bad-debt allowance / "
            "allowance for doubtful accounts) from the company's OWN collection history and "
            "current AR aging, and draft the adjustment journal entry for human review. "
            "Creates a PENDING proposal — Peregrine will not post the entry from chat; it "
            "posts to QuickBooks only after the user approves it in the Actions page. The "
            "accounts come EXCLUSIVELY from the company's saved CECL mapping (set via the "
            "CECL settings page / PUT /cecl-config) and the amount is computed by the "
            "engine — neither can be supplied here. When the mapping is not configured, or "
            "any nonzero aging bucket lacks enough payment history, or the adjustment is "
            "de-minimis, the tool honestly refuses to draft and explains what is missing. "
            "Pass _dry_run: true for the estimate ONLY (full computation evidence, nothing "
            "drafted). The methodology is drafted for CPA review; every estimate carries "
            "its disclosed limits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "string",
                    "description": "AR aging as-of date YYYY-MM-DD (defaults to today)",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the allowance adjustment is being drafted",
                },
            },
            "required": [],
        },
    },
]

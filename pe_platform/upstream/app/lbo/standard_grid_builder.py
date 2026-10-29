"""
Standardized Grid Builder — builds a deterministic 3-statement PE model
from mapped LboCoaMapping categories, guaranteeing identical structure across all deals.
"""

from typing import List, Dict, Any, Optional  # noqa: F401

# PLAN-EXEMPT: B5-2 P1 field bug (live-prod-corroborated 2026-06-24) — carry the QBO
# account name so per-account child rows show the real name, not "Account <id>".
from typing import NotRequired, TypedDict  # noqa: E402

# The grid's positional column width (plan 20260718-fycy-absolute-axis, AC-1): every
# built row's ``values`` array is sized to it (Y0 in slot 0, cols 1..N−1 projected by
# the engine), so widening the axis never leaves a hardcoded 6-slot literal behind. No
# circular import — grid_helpers imports only ..models.
from .grid_helpers import NUM_YEAR_COLS  # noqa: E402

# Child (per-account) breakout rows start at this row_index base so they never
# collide with the dense template indices (which start at 100, ~47 rows). Children
# are HISTORICAL-COMPOSITION rows: they carry the account's Y0 balance, are NEVER
# projected (driver_type="auto" => the engine stamp (grid_projection) skips them), and the
# parent category row keeps its exact summed value + driver. See LOCK-FB4-1/2/3.
_CHILD_ROW_INDEX_BASE = 1000
# PLAN-EXEMPT: LBO-FIX-3 (live-prod-corroborated 2026-06-24) — stride the template row indices so
# each parent line item has room for its per-account child rows to sort DIRECTLY beneath it
# (child row_index = parent_index + offset), instead of all children landing in a block at the
# bottom. Stride 100 → up to 99 child accounts per parent (the largest category here is ~31).
_TEMPLATE_ROW_STRIDE = 100
# A non-input, read-only data row_type. The FE (SpreadsheetGrid) renders any
# non-("input"/"header"/"blank") row read-only with its indent; the recalc formula
# writeback (lbo.py) + evaluate_grid only touch ("formula","subtotal","pct"), so a
# "data" child's stored Y0 survives every recalc untouched. cf_mapper already treats
# "data" as a real (non-input) data row — this reuses that existing convention.
_CHILD_ROW_TYPE = "data"
# PLAN-EXEMPT: LBO field-bug wave-2 (Owen 2026-06-29) — line_codes that get NO per-account child
# breakout. ``cash`` is the balance-sheet PLUG (the engine stamp (grid_projection) sets it to ending cash from the
# cash-flow cascade), so it is never driven — its children cannot roll up to the plugged parent
# and would sit frozen at Y0 while the parent grows. Keep cash a single plugged line.
_NO_CHILD_BREAKOUT_CODES = frozenset({"cash"})


class _TbItem(TypedDict):
    """One trial-balance line as build_standard_grid receives it."""
    id: str
    balance: float
    # PLAN-EXEMPT: B5-2 — optional QBO account name; absent on older callers (falls back
    # to "Account <id>"), present once lbo.py seeds it through.
    name: NotRequired[str]


class _ChildRow(TypedDict):
    """A per-account child grid row (the F-B4d granularity rows, made driver-able by
    F-B4e). Mirrors the grid-row dict shape build_standard_grid emits for the template
    parents, but typed. By default a child carries the ``parent_share`` driver
    (``driver_params={"share": child_Y0/parent_Y0}``) + a ``_parent_line_code`` tag, so
    ``the engine stamp (grid_projection)`` projects it (``share × parent's own-driver temp``)
    and rolls the parent up to ``Σ children``. The all-default shares sum to 1.0, so the
    aggregate is unchanged until a user differentiates a child. Degenerate
    ``parent_Y0 == 0`` (WS-6): the child stays the F-B4d historical-only row
    (``driver_type="auto"``, NO ``_parent_line_code``) so the parent keeps its own-driver
    projection and the child is never projected."""
    row: int
    line_code: None
    section: str
    label: str
    row_type: str
    display_fmt: str
    indent: int
    formula: None
    driver_type: str
    driver_params: dict[str, float]
    qbo_account_id: str
    values: list[float | None]
    _parent_line_code: str | None


# Standard PE Line Items
GRID_TEMPLATE = [
    # --- INCOME STATEMENT ---
    {"section": "is", "line_code": "revenue", "label": "Gross Revenue", "type": "input", "driver": "yoy"},
    {"section": "is", "line_code": "cogs", "label": "Cost of Goods Sold (COGS)", "type": "input", "driver": "pct_revenue"},
    {"section": "is", "line_code": "gross_profit", "label": "Gross Profit", "type": "formula", "formula": "=R{revenue}-R{cogs}"},
    {"section": "is", "line_code": "sga", "label": "SG&A", "type": "input", "driver": "pct_revenue"},
    {"section": "is", "line_code": "total_opex", "label": "Total Operating Expenses", "type": "formula", "formula": "=R{sga}"},
    # PLAN-EXEMPT: LBO field-bug wave-4 (Owen 2026-06-29). Net Operating Income IS EBITDA: there is
    # NO EBIT line. EBITDA = Gross Profit − operating OpEx (SG&A, EXCLUDING D&A). Depreciation &
    # Amortization is computed and presented BELOW EBITDA (a vintage schedule in the engine stamp (grid_projection)),
    # alongside the non-operating Other Income/Expense, interest and taxes — all below the line.
    {"section": "is", "line_code": "ebitda", "label": "EBITDA", "type": "formula", "formula": "=R{gross_profit}-R{total_opex}"},
    {"section": "is", "line_code": "depreciation", "label": "Depreciation & Amortization", "type": "input", "driver": "depr"},
    {"section": "is", "line_code": "other_income", "label": "Other Income (non-operating)", "type": "input", "driver": "pct_revenue"},
    {"section": "is", "line_code": "other_expense", "label": "Other Expense (non-operating)", "type": "input", "driver": "pct_revenue"},
    {"section": "is", "line_code": "interest", "label": "Interest Expense", "type": "input", "driver": "auto"},
    {"section": "is", "line_code": "taxes", "label": "Tax Expense", "type": "input", "driver": "pct_ebit"}, # tax on operating income (EBITDA − D&A)
    {"section": "is", "line_code": "net_income", "label": "Net Income", "type": "formula", "formula": "=R{ebitda}-R{depreciation}+R{other_income}-R{other_expense}-R{interest}-R{taxes}"},

    # --- BALANCE SHEET ASSETS ---
    {"section": "bs_asset", "line_code": "cash", "label": "Cash & Equivalents", "type": "input", "driver": "auto"}, # Plugged from CF
    {"section": "bs_asset", "line_code": "ar", "label": "Accounts Receivable", "type": "input", "driver": "dso"},
    {"section": "bs_asset", "line_code": "inventory", "label": "Inventory", "type": "input", "driver": "dio"},
    {"section": "bs_asset", "line_code": "other_ca", "label": "Other Current Assets", "type": "input", "driver": "auto"},
    {"section": "bs_asset", "line_code": "total_ca", "label": "Total Current Assets", "type": "formula", "formula": "=R{cash}+R{ar}+R{inventory}+R{other_ca}"},
    {"section": "bs_asset", "line_code": "ppe", "label": "Net Property, Plant & Equipment", "type": "input", "driver": "depr"},
    {"section": "bs_asset", "line_code": "intangibles", "label": "Intangibles & Goodwill", "type": "input", "driver": "auto"},
    {"section": "bs_asset", "line_code": "total_assets", "label": "Total Assets", "type": "formula", "formula": "=R{total_ca}+R{ppe}+R{intangibles}"},

    # --- BALANCE SHEET LIABILITIES ---
    {"section": "bs_liability", "line_code": "ap", "label": "Accounts Payable", "type": "input", "driver": "dpo"},
    {"section": "bs_liability", "line_code": "accrued_liab", "label": "Accrued Liabilities", "type": "input", "driver": "auto"},
    {"section": "bs_liability", "line_code": "other_cl", "label": "Other Current Liabilities", "type": "input", "driver": "auto"},
    {"section": "bs_liability", "line_code": "total_cl", "label": "Total Current Liabilities", "type": "formula", "formula": "=R{ap}+R{accrued_liab}+R{other_cl}"},
    {"section": "bs_liability", "line_code": "debt_std", "label": "Short-Term Debt", "type": "input", "driver": "debt_amort"},
    {"section": "bs_liability", "line_code": "debt_ltd", "label": "Long-Term Debt", "type": "input", "driver": "debt_amort"},
    {"section": "bs_liability", "line_code": "total_debt", "label": "Total Debt", "type": "formula", "formula": "=R{debt_std}+R{debt_ltd}"},
    {"section": "bs_liability", "line_code": "total_liab", "label": "Total Liabilities", "type": "formula", "formula": "=R{total_cl}+R{total_debt}"},

    # --- BALANCE SHEET EQUITY ---
    {"section": "equity", "line_code": "common_equity", "label": "Common Equity & Paid-In Capital", "type": "input", "driver": "auto"},
    {"section": "equity", "line_code": "retained_earnings", "label": "Retained Earnings", "type": "input", "driver": "auto"}, # Special case rollback
    {"section": "equity", "line_code": "total_equity", "label": "Total Equity", "type": "formula", "formula": "=R{common_equity}+R{retained_earnings}"},
    
    # --- BALANCE CHECK ---
    {"section": "bs", "line_code": "total_liab_equity", "label": "Total Liabilities & Equity", "type": "formula", "formula": "=R{total_liab}+R{total_equity}"},
    {"section": "bs", "line_code": "balance_check", "label": "Balance Check", "type": "formula", "formula": "=R{total_assets}-R{total_liab_equity}"},

    # --- CASH FLOW STATEMENT (Derived deterministically later) ---
    {"section": "cf", "line_code": "cf_ni", "label": "Net Income", "type": "formula", "formula": "=R{net_income}"},
    {"section": "cf", "line_code": "cf_depr", "label": "Depreciation & Amortization (add-back)", "type": "formula", "formula": "=R{depreciation}"},
    {"section": "cf", "line_code": "cf_ar", "label": "Change in Accounts Receivable", "type": "formula", "formula": "=DELTA(R{ar}, -1)"}, # Needs special string eval handler later
    {"section": "cf", "line_code": "cf_inv", "label": "Change in Inventory", "type": "formula", "formula": "=DELTA(R{inventory}, -1)"},
    {"section": "cf", "line_code": "cf_oca", "label": "Change in Other Current Assets", "type": "formula", "formula": "=DELTA(R{other_ca}, -1)"},
    {"section": "cf", "line_code": "cf_ap", "label": "Change in Accounts Payable", "type": "formula", "formula": "=DELTA(R{ap}, 1)"},
    {"section": "cf", "line_code": "cf_accrued", "label": "Change in Accrued Liabilities", "type": "formula", "formula": "=DELTA(R{accrued_liab}, 1)"},
    {"section": "cf", "line_code": "cf_ocl", "label": "Change in Other Current Liabilities", "type": "formula", "formula": "=DELTA(R{other_cl}, 1)"},
    {"section": "cf", "line_code": "cf_cfo", "label": "Net Cash from Operations", "type": "formula", "formula": "=R{cf_ni}+R{cf_depr}+R{cf_ar}+R{cf_inv}+R{cf_oca}+R{cf_ap}+R{cf_accrued}+R{cf_ocl}"},
    
    {"section": "cf", "line_code": "cf_capex", "label": "Capital Expenditures", "type": "formula", "formula": "=0"}, # Handled by capex driver subtraction
    {"section": "cf", "line_code": "cf_cfi", "label": "Net Cash from Investing", "type": "formula", "formula": "=R{cf_capex}"},
    # PLAN-EXEMPT: AC-1 LBO v2-path remediation (Owen work order 2026-06-29). FCF = CFO + CFI
    # (cf_cfi is already negative for capex). The live debt-schedule + returns chain reads this
    # by line_code (_load_grid_summary). NOT referenced by any other formula.
    {"section": "cf", "line_code": "fcf", "label": "Free Cash Flow", "type": "formula", "formula": "=R{cf_cfo}+R{cf_cfi}"},


    {"section": "cf", "line_code": "cf_debt_std", "label": "Change in Short-Term Debt", "type": "formula", "formula": "=DELTA(R{debt_std}, 1)"},
    {"section": "cf", "line_code": "cf_debt_ltd", "label": "Change in Long-Term Debt", "type": "formula", "formula": "=DELTA(R{debt_ltd}, 1)"},
    # AC-FRR-9 (amendment 2026-07-20): the clean-surplus equity flow — actual columns compute it
    # from ΔTE − NI at the band fetch (inline-gated); projected columns get the engine's per-year
    # distributions stamped (negated). Absent-not-zero when no prior equity level exists.
    {"section": "cf", "line_code": "cf_equity", "label": "Distributions / Contributions (net)", "type": "formula", "formula": "=0"},
    {"section": "cf", "line_code": "cf_cff", "label": "Net Cash from Financing", "type": "formula", "formula": "=R{cf_debt_std}+R{cf_debt_ltd}+R{cf_equity}"},
    
    {"section": "cf", "line_code": "cf_net_change", "label": "Net Change in Cash", "type": "formula", "formula": "=R{cf_cfo}+R{cf_cfi}+R{cf_cff}"},
    {"section": "cf", "line_code": "cf_beg_cash", "label": "Beginning Cash", "type": "input", "driver": "auto"},
    {"section": "cf", "line_code": "cf_end_cash", "label": "Ending Cash", "type": "formula", "formula": "=R{cf_beg_cash}+R{cf_net_change}"},
]

def build_standard_grid(
    trial_balance: List[Dict[str, Any]], 
    mappings: Dict[str, str]
) -> List[Dict[str, Any]]:
    """
    Build the standard 25-row 3-statement grid.
    `trial_balance` is a simple list of {"id": "123", "balance": 4500.0}
    `mappings` is a dict of QBO Account ID -> `line_code`
    """
    
    # 1. Aggregate Y0 (LTM) balances by line_code (the ONE shared per-period
    # aggregation — C2-a G4: the historical fiscal-year columns reuse it).
    y0_balances = aggregate_tb_by_line_code(trial_balance, mappings)

    # 2. Build grid definitions
    rows = []
    line_code_to_row_idx = {}
    
    row_idx = 100 # start at 100 to allow space, or just 1

    # PLAN-EXEMPT: LBO-FIX-3 — assign STRIDED row indices so each parent has room for its child
    # rows to sort right after it. Formulas are generated from this map below, so the wider
    # indices stay internally consistent.
    for tdef in GRID_TEMPLATE:
        line_code_to_row_idx[tdef["line_code"]] = row_idx
        row_idx += _TEMPLATE_ROW_STRIDE
        
    for tdef in GRID_TEMPLATE:
        code = tdef["line_code"]
        r_idx = line_code_to_row_idx[code]
        
        # Format formula with actual row targets
        formula = tdef.get("formula")
        if formula:
            for k, v in line_code_to_row_idx.items():
                formula = formula.replace(f"{{R{{{k}}}}}", f"R{v}")
                formula = formula.replace(f"{{{k}}}", str(v))
        
        val = y0_balances.get(code, 0.0)
        
        row_dict = {
            "row": r_idx,
            "line_code": code,
            "section": tdef["section"],
            "label": tdef["label"],
            "row_type": tdef["type"] if not formula else "formula",
            "display_fmt": "currency" if tdef["type"] != "header" else "none",
            "indent": 1 if tdef["type"] == "input" else 0,
            "formula": formula,
            "driver_type": tdef.get("driver", "auto"),
            "driver_params": {},
            "qbo_account_id": None,
            "values": [val] + [None] * (NUM_YEAR_COLS - 1)
        }
        rows.append(row_dict)

    # PLAN-EXEMPT: AC-3 LBO v2-path remediation (Owen work order 2026-06-29). Compute the Y0
    # (col 0) values for every IS/BS/CF formula+subtotal row at BUILD time. recalc-v2 runs only
    # the engine stamp (grid_projection), which projects cols 1..5 and never touches col 0; it then writes col 0 back
    # UNCHANGED. So a formula row's Y0 must be correct here — else the whole Y0 statement (and
    # ltm_ebitda the S&U reads) is the build-default 0.0. Mirrors the engine stamp (grid_projection)'s
    # algebra for a single column; the two MUST stay in sync (see the engine stamp (grid_projection) sections 2/5/6/8).
    _compute_y0_subtotals(rows)

    # 3. Per-account child rows (F-B4d granularity, F-B4e driver-able — LOCK-FB4-1/2/3).
    # For each category PARENT that has >=2 NON-ZERO accounts, emit one indented child
    # row per non-zero account. F-B4e makes each child DRIVER-ABLE and the parent the
    # SUM of its children in every projected column. By default a child carries the
    # `parent_share` driver (share = child_Y0 / parent_Y0) + a `_parent_line_code` tag:
    #   - line_code=None         -> not a recognized slot; the rollup keys off the tag
    #   - driver_type="parent_share" -> the engine stamp (grid_projection) resolves it to share × the parent's
    #                                   OWN-driver projection for the column; since the
    #                                   shares sum to 1.0, Σ children == the parent's own
    #                                   projection (the all-default model == today)
    #   - row_type="data"        -> FE renders read-only + indented; recalc's formula
    #                               writeback (only formula/subtotal/pct) never overwrites
    # The parent row above is UNCHANGED at build (same summed Y0, same driver, same
    # line_code); the engine overwrites the parent's FORWARD columns with Σ children.
    # Zero-balance accounts -> NO child. A category with <=1 non-zero account -> NO
    # children. Degenerate parent_Y0 == 0 (WS-6) -> the child stays the F-B4d
    # historical-only row (driver_type="auto", NO tag) so the parent keeps its
    # own-driver projection and the child is never projected.
    # PLAN-EXEMPT: LBO-FIX-3 — pass the parent index map so children nest under their parent.
    rows.extend(_build_account_child_rows(trial_balance, mappings, line_code_to_row_idx))

    return rows


def aggregate_tb_by_line_code(
    trial_balance: list[_TbItem],
    mappings: dict[str, str],
) -> dict[str, float]:
    """Aggregate parsed report/trial-balance leaves into per-``line_code`` totals.

    C2-a (plan 20260717-three-statement-spine, G4): the ONE aggregation both the
    Y0/LTM build above and the per-fiscal-year historical columns use — extracted
    from ``build_standard_grid`` step 1 unchanged, so every period column sums
    accounts into template slots by the identical rule. Unmapped/unknown
    categories are dropped exactly as before (no fabricated slot).
    """
    totals = {item["line_code"]: 0.0 for item in GRID_TEMPLATE if item.get("line_code")}
    for tb_item in trial_balance:
        acct_id = str(tb_item.get("id"))  # SA-OK: dict.get on an in-memory trial_balance item, NOT an ORM query
        balance = float(tb_item.get("balance", 0.0))  # SA-OK: dict.get on an in-memory trial_balance item, NOT an ORM query
        line_code = mappings.get(acct_id)  # SA-OK: dict.get on the in-memory mappings dict, NOT an ORM query
        if line_code and line_code in totals:
            totals[line_code] += balance
    return totals


def _build_account_child_rows(
    trial_balance: list[_TbItem],
    mappings: dict[str, str],
    parent_row_idx: dict[str, int],  # PLAN-EXEMPT: LBO-FIX-3 — parent line_code -> its row index
) -> list[_ChildRow]:
    """Build the indented per-account child rows for categories with >=2 non-zero
    accounts. Pure + deterministic: groups the trial balance by mapped line_code,
    preserves source order, drops zero-balance accounts, and emits a child row for
    each remaining account ONLY when its category has >=2 of them.

    F-B4e: each child is DRIVER-ABLE. By default it carries `driver_type="parent_share"`
    with `driver_params={"share": child_Y0 / parent_Y0}` and a `_parent_line_code` tag,
    so `the engine stamp (grid_projection)` projects it (`share × the parent's own-driver temp`)
    and sets `parent = Σ children`. The shares for a category sum to 1.0 by construction
    (`Σ child_Y0 == parent_Y0`), so an all-default model projects byte-for-byte as the
    pre-F-B4e own-driver model (the regression-safety gate). Degenerate (WS-6):
    `parent_Y0 == 0` (e.g. sign-mixed accounts netting to zero) ⇒ `share` is undefined,
    so the child falls back to the F-B4d historical-only shape (`driver_type="auto"`, NO
    `_parent_line_code`) — the parent keeps its own-driver projection and the child is
    never projected (never a crash, never a fabricated split). The child's Y0 column
    always carries the account balance.
    """
    template_by_code = {
        t["line_code"]: t for t in GRID_TEMPLATE if t.get("line_code")
    }

    # Group non-zero accounts under each category, preserving trial_balance order
    # so child row order is deterministic for the golden.
    accounts_by_code: dict[str, list[_TbItem]] = {}
    for tb_item in trial_balance:
        acct_id = str(tb_item.get("id"))  # SA-OK: dict.get on an in-memory trial_balance item, NOT an ORM query (this module imports only typing, no Session)
        balance = float(tb_item.get("balance", 0.0))
        if balance == 0.0:
            continue  # zero-balance account -> no child (LOCK-FB4-2)
        line_code = mappings.get(acct_id)
        # Only break out accounts whose category is an actual template slot (a parent
        # row exists to sum into). An unmapped/unknown category has no parent, so a
        # child would be an orphan — skip it.
        if not line_code or line_code not in template_by_code:
            continue
        # PLAN-EXEMPT: B5-2 — carry the account name through so the child row labels with it.
        accounts_by_code.setdefault(line_code, []).append(
            {"id": acct_id, "balance": balance, "name": str(tb_item.get("name") or "")}
        )

    child_rows: list[_ChildRow] = []
    # PLAN-EXEMPT: LBO-FIX-3 — children sort under their parent (parent_row_idx + offset),
    # not a global bottom-block counter; get_grid orders by row_index → the FE renders nested.
    for tdef in GRID_TEMPLATE:
        code = tdef.get("line_code")
        if code in _NO_CHILD_BREAKOUT_CODES:  # PLAN-EXEMPT: wave-2 — cash is a plug, no breakout
            continue
        accounts = accounts_by_code.get(code, [])
        if len(accounts) < 2:
            continue  # <=1 non-zero account -> no breakout (parent already shows it)
        parent_indent = 1 if tdef.get("type") == "input" else 0  # SA-OK: dict.get on the in-memory template, NOT an ORM query (module has no Session)
        # PLAN-EXEMPT: LBO-FIX-3 — children sort right after their parent's strided index.
        base_idx = parent_row_idx.get(code, _CHILD_ROW_INDEX_BASE)  # SA-OK: dict.get on the index map, NOT an ORM query
        child_offset = 1
        # parent_Y0 == Σ of the category's accounts. Zero-balance accounts were
        # already dropped above and contribute 0, so the non-zero `accounts` sum IS
        # the parent's Y0 (the value build_standard_grid put on the parent row).
        parent_y0 = sum(a["balance"] for a in accounts)
        degenerate = parent_y0 == 0.0  # WS-6: share undefined -> historical-only children
        for acct in accounts:
            if degenerate:
                # WS-6: keep the F-B4d historical-only shape so the parent keeps its
                # own-driver projection and the child is never projected (no crash,
                # no fabricated split).
                driver_type = "auto"
                driver_params: dict[str, float] = {}
                parent_line_code: str | None = None
            else:
                # Default driver: the child's Y0 proportional share of the parent.
                # share = child_Y0 / parent_Y0; the shares for the category sum to
                # 1.0, so an all-default model == the pre-F-B4e own-driver model.
                driver_type = "parent_share"
                driver_params = {"share": acct["balance"] / parent_y0}
                parent_line_code = code
            child_rows.append({
                "row": base_idx + child_offset,  # PLAN-EXEMPT: LBO-FIX-3 — nest under the parent index
                "line_code": None,  # NOT a recognized code -> rollup keys off the tag
                "section": tdef["section"],
                "label": (acct.get("name") or "").strip() or f"Account {acct['id']}",  # PLAN-EXEMPT: B5-2 — real QBO name, id only as fallback
                "row_type": _CHILD_ROW_TYPE,  # read-only data row (not "input")
                "display_fmt": "currency",
                "indent": parent_indent + 1,  # indented under the parent
                "formula": None,
                "driver_type": driver_type,
                "driver_params": driver_params,
                "qbo_account_id": acct["id"],
                "values": [acct["balance"]] + [None] * (NUM_YEAR_COLS - 1),
                "_parent_line_code": parent_line_code,
            })
            child_offset += 1  # PLAN-EXEMPT: LBO-FIX-3 — next child under the same parent
    return child_rows


# The algebraic rows compute_statement_subtotals derives (write-back set for the
# row-based wrapper below). Order mirrors the computation order.
STATEMENT_SUBTOTAL_CODES: tuple[str, ...] = (
    "gross_profit", "total_opex", "ebitda", "net_income",
    "total_ca", "total_assets", "total_cl", "total_debt", "total_liab",
    "total_equity", "total_liab_equity", "balance_check",
    "cf_ni", "cf_depr", "cf_ar", "cf_inv", "cf_oca", "cf_ap", "cf_accrued",
    "cf_ocl", "cf_cfo", "cf_capex", "cf_cfi", "fcf", "cf_debt_std",
    "cf_debt_ltd", "cf_equity", "cf_cff", "cf_net_change", "cf_end_cash",
)


def compute_statement_subtotals(
    levels: dict[str, float | None],
    prior_levels: dict[str, float | None] | None = None,
) -> dict[str, float]:
    """The ONE single-column IS/BS/CF statement algebra, period-aware (C2-a G4).

    ``levels`` maps ``line_code`` → the column's input values; the return maps
    EVERY line_code (inputs + the computed ``STATEMENT_SUBTOTAL_CODES``) → float
    (absent/None inputs read as 0.0, matching the historical Y0 ``g()`` rule).

    ``prior_levels is None`` (the Y0/LTM build convention — no prior column):
    every balance-sheet DELTA is 0.0, ``cf_capex`` is 0.0, and ``cf_beg_cash``
    stays whatever the column supplied (default 0.0) — byte-identical to the
    pre-period-axis ``_compute_y0_subtotals`` behavior.

    ``prior_levels`` given (a historical fiscal-year column with a real prior
    column): the CF deltas are real (``cf_ar = −Δar`` … ``cf_ap = +Δap``),
    ``cf_capex = −(ΔNetPP&E + depr_ppe) − intangible purchases`` via
    ``derive_capex`` with the AC-FRR-9b intangibles split (0.0 when the PP&E
    endpoints are absent — never fabricated), and ``cf_beg_cash`` is the prior
    column's cash.

    AC-FRR-9a (amendment 2026-07-20): ``cf_equity = ΔTotalEquity − NetIncome``
    (company-side sign: contribution inflow +, distribution outflow −) — the SAME
    clean-surplus identity ``realized_returns.clean_surplus_flows`` derives
    sponsor-side (``flow = −(ΔTE − NI)``); the cross-parity golden pins the two
    against each other so the derivation can never fork. GATED on the prior
    actually carrying an equity LEVEL (a ``total_equity`` key, not None): with no
    prior equity level ``cf_equity`` is ABSENT (never 0.0, never a ΔTE measured
    against a fabricated $0 basis — the refuter's degraded-entry first-column
    fabrication). ``cf_cff`` folds ``cf_equity`` in (absent reads 0), so with the
    equity flow + the intangibles split the derived ``cf_end_cash`` articulates
    to the column's BS ``cash`` whenever both columns tie (the refuter-proved
    identity); any residual is DISCLOSED by the band's articulation check
    (AC-FRR-10), never plugged.
    Pure dict math; no DB, no QBO client (golden-testable conftest-free).
    """
    out: dict[str, float] = {}
    for code, v in levels.items():
        out[code] = float(v) if v is not None else 0.0

    def g(code: str) -> float:
        return out.get(code, 0.0)

    def gp(code: str) -> float:
        if prior_levels is None:
            return 0.0
        v = prior_levels.get(code)  # SA-OK: dict.get on the in-memory prior-column levels, NOT an ORM query
        return float(v) if v is not None else 0.0

    def delta(code: str, sign: int) -> float:
        if prior_levels is None:
            return 0.0  # the no-prior (Y0/oldest-column) convention
        return sign * (g(code) - gp(code))

    # --- Income statement ---
    # PLAN-EXEMPT: wave-4 — EBITDA = NOI (no EBIT). Operating OpEx excludes D&A; D&A + the
    # non-operating Other Income/Expense + interest + taxes are all BELOW EBITDA, in net income.
    out["gross_profit"] = g("revenue") - g("cogs")
    out["total_opex"] = g("sga")                           # operating opex EXCLUDES D&A
    out["ebitda"] = g("gross_profit") - g("total_opex")    # EBITDA directly = NOI (D&A is BELOW the line, not in total_opex)
    out["net_income"] = (
        g("ebitda") - g("depreciation") + g("other_income") - g("other_expense")
        - g("interest") - g("taxes")
    )

    # --- Balance sheet ---
    out["total_ca"] = g("cash") + g("ar") + g("inventory") + g("other_ca")
    out["total_assets"] = g("total_ca") + g("ppe") + g("intangibles")
    out["total_cl"] = g("ap") + g("accrued_liab") + g("other_cl")
    out["total_debt"] = g("debt_std") + g("debt_ltd")
    out["total_liab"] = g("total_cl") + g("total_debt")
    out["total_equity"] = g("common_equity") + g("retained_earnings")
    out["total_liab_equity"] = g("total_liab") + g("total_equity")
    out["balance_check"] = g("total_assets") - g("total_liab_equity")

    # --- Cash flow (deltas 0 with no prior column; real deltas otherwise) ---
    out["cf_ni"] = g("net_income")
    out["cf_depr"] = g("depreciation")
    out["cf_ar"] = delta("ar", -1)
    out["cf_inv"] = delta("inventory", -1)
    out["cf_oca"] = delta("other_ca", -1)
    out["cf_ap"] = delta("ap", 1)
    out["cf_accrued"] = delta("accrued_liab", 1)
    out["cf_ocl"] = delta("other_cl", 1)
    if prior_levels is None:
        out["cf_capex"] = 0.0
    else:
        # Lazy import keeps this module's import graph flat (seed_derivation is stdlib-pure).
        # AC-FRR-9b: THE CF-articulation call site — the ONLY derive_capex caller that passes
        # the intangibles endpoints (the D&A split). QBO's D&A line includes intangibles
        # amortization; without the split it lands in cf_capex as phantom PP&E spend (the live
        # 2020 defect: capex overstated by the full annual amortization). Absent intangibles
        # read 0.0 via g/gp, making the split a no-op (byte-identical legacy behavior).
        from .seed_derivation import derive_capex
        capex = derive_capex(
            g("ppe"), gp("ppe"), g("depreciation"),
            intangibles_now=g("intangibles"), intangibles_prior=gp("intangibles"),
        )
        out["cf_capex"] = -capex if capex is not None else 0.0
    out["cf_cfi"] = g("cf_capex")
    out["cf_cfo"] = (
        g("cf_ni") + g("cf_depr") + g("cf_ar") + g("cf_inv") + g("cf_oca")
        + g("cf_ap") + g("cf_accrued") + g("cf_ocl")
    )
    out["cf_debt_std"] = delta("debt_std", 1)
    out["cf_debt_ltd"] = delta("debt_ltd", 1)
    # AC-FRR-9a: the clean-surplus equity flow, company-side sign — cf_equity = ΔTE − NI.
    # Same identity as realized_returns.clean_surplus_flows (sponsor-side −(ΔTE − NI));
    # the parity golden pins band cf_equity ≡ −realized flow. Gated on the prior carrying a
    # REAL equity level: key absent / None → cf_equity ABSENT (never 0.0, never a flow
    # fabricated against a $0 basis). The band fetch's inline NI/TE reconciliation gates
    # (post_close_band + band_articulation) additionally suppress it per column.
    _prior_te = prior_levels.get("total_equity") if prior_levels is not None else None  # SA-OK: dict.get on the in-memory prior-column levels, NOT an ORM query
    if _prior_te is not None:
        out["cf_equity"] = round((g("total_equity") - float(_prior_te)) - g("net_income"), 2)
    else:
        # ABSENT-not-zero: the template-seeded aggregation (aggregate_tb_by_line_code)
        # initializes EVERY line_code — cf_equity included — to 0.0, so a passthrough 0.0
        # here would read as a REAL "no equity transaction" on a column whose flow is simply
        # UNKNOWABLE (no prior equity level). Pop it so the honest state is an absent cell.
        out.pop("cf_equity", None)
    out["cf_cff"] = g("cf_debt_std") + g("cf_debt_ltd") + g("cf_equity")
    out["fcf"] = g("cf_cfo") + g("cf_cfi")                 # AC-1: FCF = CFO + CFI
    out["cf_net_change"] = g("cf_cfo") + g("cf_cfi") + g("cf_cff")
    if prior_levels is not None:
        out["cf_beg_cash"] = gp("cash")                    # beginning cash = prior column's cash
    out["cf_end_cash"] = g("cf_beg_cash") + g("cf_net_change")
    return out


# PLAN-EXEMPT: AC-3 LBO v2-path remediation (Owen work order 2026-06-29) — build-time Y0 algebra.
def _compute_y0_subtotals(rows: list[dict[str, Any]]) -> None:  # CRAFT-OK: a grid row is a free-form in-memory dict (no TypedDict for template parents; matches the module + the engine stamp (grid_projection) HEAD shape)
    """Fill values[0] (Y0/LTM) for every IS/BS/CF formula+subtotal row IN PLACE.

    the engine stamp (grid_projection) only projects cols 1..5 (Y0 is the QBO-actuals seed it never recomputes), so
    the build is the ONLY place Y0 subtotals get computed. C2-a (G4): the algebra itself now
    lives in the period-aware ``compute_statement_subtotals`` (prior_levels=None — the no-prior
    Y0 convention: all CF balance-sheet deltas 0), shared with the historical fiscal-year
    columns; this wrapper only maps row values[0] ⇄ the levels dict. Behavior is byte-identical
    to the pre-period-axis inline algebra. Pure dict math; no DB, no QBO client.
    """
    by_code: dict[str, dict[str, Any]] = {  # CRAFT-OK: a grid row is a free-form in-memory dict (matches the module + the engine stamp (grid_projection) HEAD shape)
        r["line_code"]: r for r in rows if r.get("line_code")
    }
    levels: dict[str, float | None] = {}
    for code, row in by_code.items():
        vals = row.get("values")  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
        levels[code] = vals[0] if vals else None
    computed = compute_statement_subtotals(levels, prior_levels=None)
    for code in STATEMENT_SUBTOTAL_CODES:
        # AC-FRR-9a: cf_equity is CONDITIONALLY absent (no prior column at Y0 → no equity
        # level to delta from), so the writeback guards on presence — the row keeps its
        # build value rather than KeyError-ing on the honestly-absent code.
        if code in by_code and code in computed:
            by_code[code]["values"][0] = computed[code]

def resolve_formula_references(formula: str, line_code_to_row: Dict[str, int]) -> str:
    """Helper to convert {line_code} in formulas to R[row_index]"""
    if not formula:
        return formula
    ret = formula
    for code, r_idx in line_code_to_row.items():
        # Replace occurrences of {code} with r_idx
        ret = ret.replace(f"{{{code}}}", str(r_idx))
    return ret

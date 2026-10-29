from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

# PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-A1) — parse_qbo_fault carries the specific
# Intuit fault (status/code/intuit_tid) into trend-failure logs instead of a generic string.
from ..qbo_client import QBOClient, parse_qbo_fault
from ..services.redaction import redact_to_string

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Period resolution
# ---------------------------------------------------------
def resolve_period(
    year: Optional[int],
    month: Optional[int],
    *,
    fiscal_year_start_month: Optional[int] = None,
    db: Optional[Session] = None,
    realm_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    Decide which month to analyze and compute:

      - start_date / end_date: that specific calendar month
      - ttm_start / ttm_end: 12‑month window ending at that month
      - fy_start / fy_end: fiscal-year-to-date window ending at that month
      - fy_start_month / fy_start_source: the fiscal-year anchor month (1..12)
        and WHERE it came from ("caller" | "qbo_company_info" |
        "default_january") — the January fallback is always visible here
      - label: human-friendly label, e.g. "Dec 2025"

    If year/month are not provided, defaults to last completed calendar month.

    Fiscal-year resolution (financial-modeling-correctness §8 — fiscal year is
    company DATA, not a constant): a caller-supplied ``fiscal_year_start_month``
    wins; otherwise ``db`` + ``realm_id`` resolve it from QBOCompanyInfo; with
    neither, January is used and the payload/log say so. All three parameters are
    optional keywords so existing callers are unchanged.
    """
    today = date.today()

    if year is None or month is None:
        # Default: last completed calendar month
        first_of_this_month = date(today.year, today.month, 1)
        end = first_of_this_month - timedelta(days=1)  # last day of previous month
        start = date(end.year, end.month, 1)
    else:
        # User provided specific year/month
        y: int = year
        m: int = month
        start = date(y, m, 1)
        if m == 12:
            end = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(y, m + 1, 1) - timedelta(days=1)

    # TTM window: 12-month period ending at `end`
    # ... (rest of logic)

    # ... in compute_balance_sheet_kpis ...
    cash: float = 0.0  # noqa: F841
    ar: float = 0.0  # noqa: F841
    total_assets: float = 0.0  # noqa: F841
    current_assets: float = 0.0  # noqa: F841
    current_liab: float = 0.0  # noqa: F841
    long_term_liab: float = 0.0  # noqa: F841
    total_liab: float = 0.0  # noqa: F841
    total_equity: float = 0.0  # noqa: F841

    # TTM window: 12-month period ending at `end`
    if end.month == 12:
        ttm_start = date(end.year, 1, 1)
    else:
        ttm_start_year = end.year - 1
        ttm_start_month = end.month + 1
        ttm_start = date(ttm_start_year, ttm_start_month, 1)
    ttm_end = end

    # Fiscal-year start: the company's REAL fiscal calendar, never an assumed
    # January (financial-modeling-correctness §8). Caller value → QBOCompanyInfo
    # → January default, provenance surfaced in the payload (fy_start_source).
    if fiscal_year_start_month is not None and 1 <= int(fiscal_year_start_month) <= 12:
        fy_month, fy_source = int(fiscal_year_start_month), "caller"
    else:
        if fiscal_year_start_month is not None:
            logger.warning(
                "resolve_period: caller-supplied fiscal_year_start_month %r out of range 1..12 — resolving from company data instead",
                fiscal_year_start_month,
            )
        fy_month, fy_source = _fiscal_year_start_month(db, realm_id)

    # The fiscal year containing `end` starts this calendar year if `end` is on
    # or after the anchor month, else it started last calendar year.
    if end.month >= fy_month:
        fy_start = date(end.year, fy_month, 1)
    else:
        fy_start = date(end.year - 1, fy_month, 1)
    fy_end = end

    label = end.strftime("%b %Y")  # e.g. "Dec 2025"

    return {
        "label": label,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "ttm_start": ttm_start.isoformat(),
        "ttm_end": ttm_end.isoformat(),
        "fy_start": fy_start.isoformat(),
        "fy_end": fy_end.isoformat(),
        "fy_start_month": str(fy_month),
        "fy_start_source": fy_source,
    }


def safe_float(x) -> float:
    # Kept (0.0-on-failure) ONLY for external callers where 0.0-on-missing is
    # semantically correct (e.g. app/reporting/data_gatherer.py budget-month
    # lookups, where an absent month genuinely means $0 budgeted). QBO report
    # row/summary parsing inside THIS file uses _parse_amount_or_none instead —
    # a failed parse there is an honest gap, never a fabricated 0.0.
    try:
        if isinstance(x, str):
            x = x.replace(",", "").replace("$", "").replace("(", "-").replace(")", "")
        return float(x)
    except Exception:
        # logger.debug(f"DEBUG: safe_float failed for input: {x}")
        return 0.0


def _parse_amount_or_none(x) -> Optional[float]:
    """Parse a QBO report amount (commas, $, paren-negatives) or None on failure.

    Mirrors app/budget/qbo_reports._parse_amount's accepted forms, but a failed
    parse yields None — an honest gap the caller must handle — never a fabricated
    0.0 (financial-modeling-correctness: refuse over fabricate).
    """
    if x is None:
        return None
    try:
        s = str(x).replace(",", "").replace("$", "").strip()
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        return float(s)
    except (ValueError, TypeError):
        return None


def _is_blank(raw) -> bool:
    """True when a report cell carries no value at all (None / empty / spaces)."""
    return raw is None or not str(raw).strip()


# Account-name markers identifying D&A expense lines on a QBO P&L — the SAME
# identification app/fpa/driver_mapping.py uses for its remediated EBITDA
# add-back (standard-line identification, not covenant add-back inference).
_DA_NAME_MARKERS = ("depreciation", "amortization", "amortisation")


def _sum_da_single_column(report: Dict) -> Tuple[float, bool]:
    """(D&A total, was-any-D&A-line-identified) for a single-column P&L report.

    Follows app/fpa/driver_mapping._sum_da_accounts: identify D&A by account-NAME
    matching on the rows this report itself carries, summing |amount| via the
    CANONICAL QBO report parser (app/budget/qbo_reports — the one-shared-walker
    rule) rather than re-walking Rows/Row/ColData here.
    """
    from ..budget.qbo_reports import _parse_pl_single_month_entries

    entries = _parse_pl_single_month_entries(report)
    matched = [
        entry
        for entry in entries
        if any(marker in (entry.get("account_name") or "").lower() for marker in _DA_NAME_MARKERS)
    ]
    total = sum(abs(float(entry.get("amount") or 0.0)) for entry in matched)
    return total, bool(matched)


def _da_values_per_column(report: Dict) -> Tuple[List[float], bool]:
    """Per-value-column D&A totals for a multi-column (Month-summarized) P&L.

    The canonical single-month parser cannot address per-month columns, so this
    walker applies its identification rules to the multi-column shape: a D&A line
    is any Data row — or account-id-bearing Section header (a parent account's
    own line) — whose account NAME carries a D&A marker. Section Summary rows are
    never read (they would double-count child rows). Values align index-for-index
    with ColData[1:] (the same alignment compute_trends' get_section_totals
    uses), so vals[-1] is the report's Total column when one exists.

    Returns (per-column |amount| sums, whether any D&A line was identified).
    """
    vals: List[float] = []
    found = False

    def _accumulate(col_data: List[Dict]) -> None:
        nonlocal found
        found = True
        cells = col_data[1:]
        if len(vals) < len(cells):
            vals.extend([0.0] * (len(cells) - len(vals)))
        for i, cell in enumerate(cells):
            raw = cell.get("value")
            amt = _parse_amount_or_none(raw)
            if amt is None:
                # Blank month cell = no D&A booked that month (genuine absence);
                # non-blank garbage is a LOGGED data-quality gap, never silent.
                if not _is_blank(raw):
                    logger.warning(
                        "data_quality: D&A row cell unparseable (%r) — omitted from the EBITDA add-back (honest gap, not 0.0)",
                        raw,
                    )
                continue
            vals[i] += abs(amt)

    def _walk(rows: List[Dict]) -> None:
        for row in rows or []:
            rtype = (row.get("type") or "").lower()
            if rtype == "section":
                header_cols = row.get("Header", {}).get("ColData", []) or []
                name = (header_cols[0].get("value") or "").lower() if header_cols else ""
                if (
                    header_cols
                    and header_cols[0].get("id")
                    and any(m in name for m in _DA_NAME_MARKERS)
                ):
                    _accumulate(header_cols)
                _walk(row.get("Rows", {}).get("Row", []) or [])
            elif rtype == "data":
                col_data = row.get("ColData", []) or []
                name = (col_data[0].get("value") or "").lower() if col_data else ""
                if any(m in name for m in _DA_NAME_MARKERS):
                    _accumulate(col_data)

    _walk(report.get("Rows", {}).get("Row", []) or [])
    return vals, found


def _ebitda_basis(da_identified: bool) -> str:
    """Honest basis label for an EBITDA figure: was a D&A add-back possible?"""
    return "da_added_back" if da_identified else "no_da_identified"


def _fiscal_year_start_month(db: Optional[Session], realm_id: Optional[str]) -> Tuple[int, str]:
    """Resolve the company's fiscal-year start month (1..12) plus its provenance.

    Reads QBOCompanyInfo.fiscal_year_start_month via the Wave-A canonical parser
    (app/services/company_profile — handles month NAMES and numbers; the one home
    for that parse). Falls back to January (1) with a LOGGED reason whenever the
    row is missing/unparseable or no db/realm context was supplied; the fallback
    is also surfaced to callers via the returned provenance string.
    """
    if db is None or not realm_id:
        logger.info(
            "resolve_period: no db/realm context supplied — fiscal-year start defaulting to January"
        )
        return 1, "default_january"
    try:
        # Local import: company_profile pulls in models/review_checks; keep this
        # module import-light (same pattern as get_db_report's models import).
        from ..services.company_profile import _qbo_company_fiscal_month

        month = _qbo_company_fiscal_month(db, str(realm_id))
    except Exception as e:
        logger.warning(
            "resolve_period: QBOCompanyInfo fiscal-year lookup failed for realm %s (%s) — defaulting to January",
            realm_id,
            redact_to_string(str(e), max_length=200),
        )
        return 1, "default_january"
    if month is None:
        logger.info(
            "resolve_period: QBOCompanyInfo has no parseable FiscalYearStartMonth for realm %s — defaulting to January",
            realm_id,
        )
        return 1, "default_january"
    return month, "qbo_company_info"


# ---------------------------------------------------------
# Balance Sheet KPIs from Accounts
# ---------------------------------------------------------
def compute_balance_sheet_kpis(client: QBOClient, as_of_date: str, accounting_method: str = "Accrual") -> Dict[str, float]:
    """
    Fetches the Balance Sheet report as of `as_of_date` and extracts key totals.
    This ensures historical accuracy (vs querying Account.CurrentBalance which is always today).
    """
    logger.debug(f"DEBUG: Computing BS KPIs as of {as_of_date}")

    data_map = {
        "Total Assets": 0.0,
        "Total Liabilities": 0.0,
        "Total Equity": 0.0,
        "Total Current Assets": 0.0,
        "Total Current Liabilities": 0.0,
        "Total Long-Term Liabilities": 0.0,
        "Accounts Receivable": 0.0,
        "Accounts Payable": 0.0,
        "Checking": 0.0, # Will capture Bank/Cash
    }
    try:
        # Fetch Balance Sheet Report
        # QBO API: To get a specific date, just pass end_date. Do not send date_macro="Specific" (invalid).
        # We also want to summarize by Total to get a single column (plus previous period if we wanted comparison, but we just want the snapshot).
        # Calculate start of year for context (though BS is point-in-time, defining the period is safer)
        # as_of_date is YYYY-MM-DD
        try:
            y = int(as_of_date[:4])
            start_date = f"{y}-01-01"
        except (ValueError, TypeError):
            start_date = as_of_date # Fallback
            
        r = client.get_report("BalanceSheet", {
            "start_date": start_date,
            "end_date": as_of_date,
            "summarize_column_by": "Total",
            "accounting_method": accounting_method
        })
        
        header = r.get("Header", {})
        logger.debug(f"DEBUG: QBO Report Header: Time={header.get('Time')}, ReportBasis={header.get('ReportBasis')}, StartPeriod={header.get('StartPeriod')}, EndPeriod={header.get('EndPeriod')}, Currency={header.get('Currency')}")

        data_map = {
            "Total Assets": 0.0,
            "Total Liabilities": 0.0,
            "Total Equity": 0.0,
            "Total Current Assets": 0.0,
            "Total Current Liabilities": 0.0,
            "Total Long-Term Liabilities": 0.0,
            "Accounts Receivable": 0.0,
            "Accounts Payable": 0.0,
            "Checking": 0.0, # Will capture Bank/Cash
        }
        
        # -------------------------------------------------------------
        # Flatten the report into a Key-Value map for "Index Match" style lookup
        # -------------------------------------------------------------
        flat_map: Dict[str, float] = {}
        
        def flatten_report(rows_list, context=""):
            for row in rows_list:
                r_type = row.get("type", "")
                
                # 1. Handle Summary/Totals (Sections)
                if r_type == "Section":
                    # Get Section Header (e.g. "Bank Accounts")
                    header = row.get("Header", {}).get("ColData", [])
                    label = (header[0].get("value") or "").strip() if header else ""
                    
                    # Store context for debugging or disambiguation if needed
                    new_context = f"{context} > {label}" if context else label
                    
                    # Get Summary Value if present (e.g. "Total Bank Accounts")
                    summary = row.get("Summary", {})
                    if summary:
                        col_data = summary.get("ColData", [])
                        if col_data:
                            sum_label = (col_data[0].get("value") or "").strip()
                            raw = col_data[-1].get("value")
                            val = _parse_amount_or_none(raw)
                            if val is None:
                                # Honest gap: an unparseable/blank summary is OMITTED
                                # from the map (get_val's alias + derive-from-counterpart
                                # fallbacks then handle it), never stored as a
                                # fabricated 0.0. Garbage (non-blank) is logged.
                                if not _is_blank(raw):
                                    logger.warning(
                                        "data_quality: BS summary row %r amount unparseable (%r) — omitted from KPI map (honest gap, not 0.0)",
                                        sum_label,
                                        raw,
                                    )
                            else:
                                # Store in map (normalize usage)
                                # We store both exact label and lower-case
                                flat_map[sum_label] = val
                                flat_map[sum_label.lower()] = val

                            # Also store generic "Total X" if label is "Total Bank Accounts"
                            # This helps if we look for "Total Assets" and it's there.

                    # Recurse
                    flatten_report(row.get("Rows", {}).get("Row", []), new_context)
                    
                # 2. Handle Data Rows (Individual Accounts)
                elif r_type == "Data":
                    col_data = row.get("ColData", [])
                    if col_data:
                        label = (col_data[0].get("value") or "").strip()
                        raw = col_data[-1].get("value")
                        val = _parse_amount_or_none(raw)
                        if val is None:
                            # Honest gap: omit (blank = no balance shown); garbage
                            # is a logged data-quality gap, never a silent 0.0.
                            if not _is_blank(raw):
                                logger.warning(
                                    "data_quality: BS data row %r amount unparseable (%r) — omitted from KPI map (honest gap, not 0.0)",
                                    label,
                                    raw,
                                )
                            continue

                        flat_map[label] = val
                        flat_map[label.lower()] = val

                        # Debug logic for Bank Accounts: Check context OR name
                        is_bank_context = any(x in context.lower() for x in ["bank accounts", "cash and cash equivalents"])
                        is_bank_name = any(x in label.lower() for x in ["bank", "checking", "savings", "cash", "undeposited"])
                        
                        if is_bank_context or is_bank_name:
                             logger.debug(f"DEBUG: Bank Data Row: {label} = {val} (Context: {context})")

        # Flatten it!
        root_rows = r.get("Rows", {}).get("Row", [])
        flatten_report(root_rows)
        
        # logger.debug(f"DEBUG: Flattened Map Keys: {list(flat_map.keys())}")
        
        # -------------------------------------------------------------
        # Extract using Direct Lookups ("Index Match")
        # -------------------------------------------------------------
        
        # Helper to find first valid key from a list
        def get_val(keys_to_try: List[str]) -> float:
            for k in keys_to_try:
                if k in flat_map:
                    # logger.debug(f"DEBUG: Found {k} = {flat_map[k]}")
                    return flat_map[k]
                if k.lower() in flat_map:
                    return flat_map[k.lower()]
            # Absent key = the row simply isn't on this BS (a genuine zero, e.g. no
            # long-term liabilities) — 0.0 is semantically correct here. Parse
            # FAILURES never land on this path silently: they were omitted from
            # flat_map WITH a data_quality log above, and the total-level fallback
            # derivations below reconstruct the value from its counterparts.
            return 0.0

        # Cash: Try "Total Bank Accounts", "Total Cash", "Total Checking/Savings"
        # If "Total Bank Accounts" is negative but user expects positive, 
        # maybe "Cash and cash equivalents" is the parent?
        # User log showed "Total Bank Accounts" was -2560.8.
        # If user says that's wrong, we might need to look for "Undeposited Funds" separately and add it?
        # Or maybe "Total Current Assets" - "Total Accounts Receivable" - "Total Other Current Assets"?
        # For now, we trust QBO's specific totals if available.
        
        cash = get_val(["Total Bank Accounts", "Total Cash", "Total Cash and cash equivalents", "Total Checking/Savings"])
        
        # If Cash is surprisingly low/negative, see if "Undeposited Funds" exists and add it 
        # (Only if it's NOT already in Total Bank Accounts. Usually Undeposited Funds is "Other Current Asset" in QBO).
        undeposited = get_val(["Undeposited Funds", "Total Undeposited Funds"])
        if undeposited != 0 and "Undeposited Funds" not in data_map: # Avoid double counting logic if we could check hierarchy
             # QBO taxonomy: Bank Accounts are Type "Bank". Undeposited Funds is Type "Other Current Asset".
             # So "Total Bank Accounts" likely EXCLUDES Undeposited Funds.
             # We should probably ADD Undeposited Funds to our "Cash" metric for the dashboard.
             logger.debug(f"DEBUG: Adding Undeposited Funds ({undeposited}) to Cash ({cash})")
             cash += undeposited

        ar = get_val(["Total Accounts Receivable", "Accounts Receivable (A/R)"])
        
        total_assets = get_val(["Total Assets"])
        total_current_assets = get_val(["Total Current Assets"])
        
        total_liab = get_val(["Total Liabilities"])
        total_current_liab = get_val(["Total Current Liabilities"])
        total_long_term_liab = get_val(["Total Long-Term Liabilities", "Total Long Term Liabilities"])
        
        total_equity = get_val(["Total Equity"])
        
        # Fallback calculations if totals missing
        if total_assets == 0 and total_liab != 0 and total_equity != 0:
            total_assets = total_liab + total_equity
            
        if total_liab == 0:
            if total_current_liab != 0 or total_long_term_liab != 0:
                total_liab = total_current_liab + total_long_term_liab
            elif total_assets != 0 and total_equity != 0:
                total_liab = total_assets - total_equity

        data_map["Checking"] = cash
        data_map["Accounts Receivable"] = ar
        data_map["Total Assets"] = total_assets
        data_map["Total Current Assets"] = total_current_assets
        data_map["Total Current Liabilities"] = total_current_liab
        data_map["Total Long-Term Liabilities"] = total_long_term_liab
        data_map["Total Liabilities"] = total_liab
        data_map["Total Equity"] = total_equity
        
        logger.debug(f"DEBUG: Extracted Cash={cash}, AR={ar}, Assets={total_assets}")

    except Exception as e:
        logger.warning(
            "Balance Sheet KPI extraction failed: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("Balance Sheet KPI data could not be loaded from QuickBooks.") from e

    # Map to internal keys
    return {
        "cash": data_map["Checking"], 
        "ar": data_map["Accounts Receivable"],
        "total_assets": data_map["Total Assets"],
        "current_assets": data_map["Total Current Assets"],
        "current_liabilities": data_map["Total Current Liabilities"],
        "long_term_liabilities": data_map["Total Liabilities"] - data_map["Total Current Liabilities"], # Approx
        "total_liabilities": data_map["Total Liabilities"],
        "total_equity": data_map["Total Equity"]
    }

# ---------------------------------------------------------
# Profit & Loss helpers
# ---------------------------------------------------------
def sum_pl_section(report_json: Dict, section_label: str) -> float:
    """
    Sum a top-level section like 'Income', 'Cost of Goods Sold', 'Expenses',
    'Other Income', 'Other Expense' from a ProfitAndLoss report.
    Uses the 'Summary' row of the section to get the accurate total, handling nested groups.
    """
    logger.debug(f"DEBUG: sum_pl_section Report Keys: {list(report_json.keys())}")
    rows_data = report_json.get("Rows", {})
    if not isinstance(rows_data, dict):
        logger.debug(f"DEBUG: 'Rows' is not a dict: {type(rows_data)}")
        rows = []
    else:
        rows = rows_data.get("Row", [])
    
    if not rows:
        logger.debug("DEBUG: Root 'Row' list is EMPTY or None.")
    else:
        logger.debug(f"DEBUG: sum_pl_section found {len(rows)} root rows.")
    
    rows = rows or []

    SECTION_ALIASES = {
        "income": ["income", "revenue", "total income", "total revenue", "sales", "total sales"],
        "cost of goods sold": ["cost of goods sold", "cost of sales", "total cost of goods sold", "cogs"],
        "gross profit": ["gross profit", "total gross profit", "grossprofit"],
        "expenses": ["expenses", "total expenses"],
        # QBO emits these sections as "Other Income" / group "OtherIncome" and
        # "Other Expenses" / group "OtherExpenses" (plural); match every spelling.
        "other income": ["other income", "total other income", "otherincome"],
        "other expense": [
            "other expense", "total other expense",
            "other expenses", "total other expenses", "otherexpenses", "otherexpense",
        ],
        "operating activities": [
            "operating activities",
            "net cash provided by operating activities",
            "net cash used for operating activities",
            "cash flows from operating activities"
        ],
        "investing activities": [
            "investing activities",
            "net cash provided by investing activities",
            "net cash used for investing activities",
            "cash flows from investing activities"
        ],
        "financing activities": [
            "financing activities",
            "net cash provided by financing activities",
            "net cash used for financing activities",
            "cash flows from financing activities"
        ]
    }

    target = section_label.lower()
    aliases = SECTION_ALIASES.get(target, [target])
    
    # Recursive search function. Each level is scanned for a match BEFORE descending,
    # and an account parent (a section whose header carries a QBO account id) is never
    # taken for a report group: an "Other Expenses" ACCOUNT inside Expenses must not be
    # read as the "Other Expenses" SECTION, which would count its amount twice.
    def _section_matches(row) -> bool:
        if (row.get("type") or "").lower() != "section":
            return False
        header = row.get("Header", {}).get("ColData", []) or []
        if header and header[0].get("id"):
            return False
        header_val = (header[0].get("value", "") if header else "").lower()
        group_val = (row.get("group", "") or "").lower()
        return any(alias == header_val or alias == group_val for alias in aliases)

    def find_section_value(current_rows, depth=0):
        for row in current_rows:
            if _section_matches(row):
                header = row.get("Header", {}).get("ColData", []) or []
                header_val = (header[0].get("value", "") if header else "").lower()
                logger.debug(f"DEBUG: Found section '{header_val}' matching target '{target}'")

                # Validation: Try to get explicit Summary total first
                summary = row.get("Summary", {})
                col_data = summary.get("ColData", [])

                if col_data:
                    raw = col_data[-1].get("value")
                    val = _parse_amount_or_none(raw)
                    if val is not None:
                        logger.debug(f"DEBUG: Extracted value {val} from '{header_val}' Summary")
                        return val
                    # Honest gap: a summary that EXISTS but will not parse is never
                    # returned as a fabricated 0.0 — log the data-quality gap and
                    # degrade to the manual child sum below (the existing fallback).
                    if not _is_blank(raw):
                        logger.warning(
                            "data_quality: P&L section %r summary amount unparseable (%r) — falling back to child-row sum (honest gap, not 0.0)",
                            header_val,
                            raw,
                        )

                # Fallback: If no (usable) Summary row, sum the children manually
                logger.debug(f"DEBUG: No usable Summary for '{header_val}'. Summing children manually...")
                return sum_section_recursively(row)

        for row in current_rows:
            if (row.get("type") or "").lower() != "section":
                continue
            # Recurse into children (to find the target section deeper in tree)
            child_rows = row.get("Rows", {}).get("Row", [])
            if child_rows:
                val = find_section_value(child_rows, depth + 1)
                if val is not None:
                    return val
        return None

    def sum_section_recursively(section_row):
        total = 0.0
        rows = section_row.get("Rows", {}).get("Row", [])
        for r in rows:
            rtype = (r.get("type") or "").lower()
            if rtype == "data":
                # Add data row value (last column usually holds the amount)
                cdata = r.get("ColData", [])
                if cdata:
                    raw = cdata[-1].get("value")
                    val = _parse_amount_or_none(raw)
                    if val is None:
                        # Blank cell = nothing booked (genuine absence contributes
                        # nothing); non-blank garbage is a LOGGED data-quality gap,
                        # never a silent fabricated 0.0 inside the section sum.
                        if not _is_blank(raw):
                            logger.warning(
                                "data_quality: P&L data row amount unparseable (%r) — omitted from section sum (honest gap, not 0.0)",
                                raw,
                            )
                        continue
                    total += val
            elif rtype == "section":
                # Recurse into subsection
                total += sum_section_recursively(r)
        return total

    logger.debug(f"DEBUG: sum_pl_section scanning for '{target}' via aliases {aliases}")
    val = find_section_value(rows)
    if val is None:
        # Section absent from the report = that P&L bucket is genuinely zero for
        # the period (e.g. no Other Income section exists) — semantically-correct
        # 0.0, distinct from a parse failure (those are logged above).
        logger.debug(f"DEBUG: Section '{target}' NOT FOUND in report.")
        return 0.0
    return val


def compute_period_pl_kpis(client: QBOClient, period: Dict[str, str], accounting_method: str = "Accrual") -> Dict[str, object]:
    """
    P&L for the selected month:
      - revenue, gp, opex, ebitda (with the D&A add-back), da, ebitda_basis,
        non-op, net-income
    """
    rev: float = 0.0
    gp: float = 0.0
    opex: float = 0.0
    ebitda: float = 0.0
    non_op: float = 0.0
    ni: float = 0.0
    da: float = 0.0
    da_identified: bool = False

    logger.debug(f"DEBUG: compute_period_pl_kpis called with period: {period}")

    try:
        r = client.get_report(
            "ProfitAndLoss",
            {
                "start_date": period["start_date"],
                "end_date": period["end_date"],
                "accounting_method": accounting_method,
            },
        )

        income = sum_pl_section(r, "income")
        cogs = sum_pl_section(r, "cost of goods sold")
        gp_native = sum_pl_section(r, "gross profit")
        expenses = sum_pl_section(r, "expenses")
        other_income = sum_pl_section(r, "other income")
        other_expense = sum_pl_section(r, "other expense")

        rev = income
        # Use native QBO gross profit if available, otherwise fallback
        gp = gp_native if gp_native != 0 else (income - cogs)
        opex = expenses
        # EBITDA is an ADD-BACK, never GP − OpEx: on a QBO P&L, D&A typically sits
        # IN the expense section, so GP − OpEx is EBIT mislabeled
        # (financial-modeling-correctness §1; app/fpa/driver_mapping.py:77-83 is the
        # remediated pattern this follows). Back D&A out explicitly; when no D&A
        # line is identifiable the value equals EBIT and ebitda_basis says so.
        da, da_identified = _sum_da_single_column(r)
        ebitda = gp - opex + da
        non_op = other_expense  # treat "Other Expense" as non-operating
        ni = gp - opex + other_income - other_expense

    except Exception as e:
        logger.warning(
            "Period P&L KPI extraction failed: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("Period P&L KPI data could not be loaded from QuickBooks.") from e

    result = {
        "revenue": rev,
        "gross_profit": gp,
        "operating_expenses": opex,
        "ebitda": ebitda,
        "da": da,
        "ebitda_basis": _ebitda_basis(da_identified),
        "non_operating_expenses": non_op,
        "net_income": ni,
    }
    logger.debug(f"DEBUG: compute_period_pl_kpis result: {result}")
    return result





def compute_ttm_pl_kpis(client: QBOClient, period: Dict[str, str], accounting_method: str = "Accrual") -> Dict[str, object]:
    """
    Trailing-12-Months (TTM) KPIs: revenue, GP, EBITDA (with the D&A add-back),
    D&A, margins, and the honest EBITDA basis label.
    """
    ttm_revenue: float = 0.0
    ttm_cogs: float = 0.0
    ttm_gp: float = 0.0
    ttm_opex: float = 0.0
    ttm_ebitda: float = 0.0
    ttm_da: float = 0.0
    da_identified: bool = False

    try:
        ttm = client.get_report(
            "ProfitAndLoss",
            {
                "start_date": period["ttm_start"],
                "end_date": period["ttm_end"],
                "summarize_column_by": "Month",
                "accounting_method": accounting_method,
            },
        )

        # Use the robust sum_pl_section helper
        ttm_revenue = sum_pl_section(ttm, "income")
        ttm_cogs = sum_pl_section(ttm, "cost of goods sold")
        ttm_gp_native = sum_pl_section(ttm, "gross profit")

        # Use native QBO gross profit if available, otherwise fallback
        ttm_gp = ttm_gp_native if ttm_gp_native != 0 else (ttm_revenue - ttm_cogs)
        ttm_opex = sum_pl_section(ttm, "expenses")

        # EBITDA is an ADD-BACK, never GP − OpEx (financial-modeling-correctness §1;
        # driver_mapping.py pattern). This report is Month-summarized, so identify
        # D&A per column; vals align with ColData[1:] and [-1] is the SAME Total
        # column sum_pl_section reads for every other section above.
        da_vals, da_identified = _da_values_per_column(ttm)
        ttm_da = da_vals[-1] if da_vals else 0.0
        ttm_ebitda = ttm_gp - ttm_opex + ttm_da

    except Exception as e:
        logger.warning(
            "TTM P&L KPI extraction failed: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("TTM P&L KPI data could not be loaded from QuickBooks.") from e

    ttm_gm_pct = (ttm_gp / ttm_revenue) if ttm_revenue else 0.0
    ttm_ebitda_ratio = (ttm_ebitda / ttm_revenue) if ttm_revenue else 0.0

    return {
        "ttm_revenue": ttm_revenue,
        "ttm_cogs": ttm_cogs,
        "ttm_gross_profit": ttm_gp,
        "ttm_ebitda": ttm_ebitda,
        "ttm_da": ttm_da,
        "ttm_ebitda_basis": _ebitda_basis(da_identified),
        "ttm_gross_margin_pct": ttm_gm_pct,
        "ttm_ebitda_ratio": ttm_ebitda_ratio,
    }


def compute_ytd_pl_kpis(client: QBOClient, period: Dict[str, str], accounting_method: str = "Accrual") -> Dict[str, object]:
    """
    Fiscal-year-to-date KPIs: sales, GP, GP ratio, EBITDA (with the D&A
    add-back), EBITDA ratio, and the honest EBITDA basis label. The fiscal
    window itself comes from resolve_period's company-data fiscal anchor.
    """
    sales: float = 0.0
    gp: float = 0.0
    ebitda: float = 0.0
    da: float = 0.0
    da_identified: bool = False

    try:
        r = client.get_report(
            "ProfitAndLoss",
            {
                "start_date": period["fy_start"],
                "end_date": period["fy_end"],
                "accounting_method": accounting_method,
            },
        )

        income = sum_pl_section(r, "income")
        cogs = sum_pl_section(r, "cost of goods sold")
        gp_native = sum_pl_section(r, "gross profit")
        expenses = sum_pl_section(r, "expenses")

        sales = income
        # Use native QBO gross profit if available, otherwise fallback
        gp = gp_native if gp_native != 0 else (income - cogs)
        # EBITDA is an ADD-BACK, never GP − OpEx (financial-modeling-correctness
        # §1; driver_mapping.py pattern) — single-column report, canonical parser.
        da, da_identified = _sum_da_single_column(r)
        ebitda = gp - expenses + da

    except Exception as e:
        logger.warning(
            "YTD P&L KPI extraction failed: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("YTD P&L KPI data could not be loaded from QuickBooks.") from e

    gp_ratio = gp / sales if sales else 0.0
    ebitda_ratio = ebitda / sales if sales else 0.0

    # For now we don't compute full YTD cash flow in detail; placeholder
    net_cf = 0.0

    return {
        "sales": sales,
        "gross_profit": gp,
        "gross_profit_ratio": gp_ratio,
        "ebitda": ebitda,
        "da": da,
        "ebitda_basis": _ebitda_basis(da_identified),
        "ebitda_ratio": ebitda_ratio,
        "net_cash_flow": net_cf,
    }


# ---------------------------------------------------------
# Cash Flow report helpers
# ---------------------------------------------------------
def compute_cashflow_kpis(client: QBOClient, period: Dict[str, str], accounting_method: str = "Accrual") -> Dict[str, float]:
    """
    Cashflow for the selected month (Operating / Investing / Financing / Net CF).
    """
    cfo: float = 0.0
    cfi: float = 0.0
    cff: float = 0.0
    net_cf: float = 0.0

    try:
        r = client.get_report(
            "CashFlow",
            {
                "start_date": period["start_date"],
                "end_date": period["end_date"],
                "accounting_method": accounting_method,
            },
        )

        # Use sum_pl_section as it works for any section-based report with Summary rows
        cfo = sum_pl_section(r, "operating activities")
        cfi = sum_pl_section(r, "investing activities")
        cff = sum_pl_section(r, "financing activities")
        net_cf = cfo + cfi + cff

    except Exception as e:
        logger.warning(
            "Cash Flow KPI extraction failed: %s",
            redact_to_string(str(e), max_length=500),
        )
        raise RuntimeError("Cash Flow KPI data could not be loaded from QuickBooks.") from e

    return {"cfo": cfo, "cfi": cfi, "cff": cff, "net_cash_flow": net_cf}


# ---------------------------------------------------------
# Ratios Engine
# ---------------------------------------------------------
# Status vocabulary (docs/engineering/financial-data-failure-semantics.md) for a ratio
# that has no meaningful value: a zero denominator, or a negative one that would flip
# the reading (debt/equity on negative equity reads as "no leverage").
RATIO_DIVIDE_BY_ZERO = "divide_by_zero"
RATIO_NEGATIVE_DENOMINATOR = "negative_denominator"


def compute_ratios_with_quality(
    period_pl: Dict[str, float],
    bs: Dict[str, float],
) -> Tuple[Dict[str, Optional[float]], Dict[str, str]]:
    """
    Compute profitability, liquidity, and capital-structure ratios.

    A ratio whose denominator is zero or negative is None (render "n/a"), never 0.0:
    a company with no current liabilities is not insolvent. The second value maps each
    such ratio to its status code (RATIO_DIVIDE_BY_ZERO / RATIO_NEGATIVE_DENOMINATOR).
    """
    revenue = period_pl["revenue"]
    gp = period_pl["gross_profit"]
    opex = period_pl["operating_expenses"]
    ebitda = period_pl["ebitda"]
    ni = period_pl["net_income"]

    current_assets = bs["current_assets"]
    current_liab = bs["current_liabilities"]
    cash = bs["cash"]
    ar = bs["ar"]
    total_liab = bs["total_liabilities"]
    total_equity = bs["total_equity"]
    total_assets = bs["total_assets"]

    status: Dict[str, str] = {}

    def _ratio(name: str, numerator: float, denominator: float) -> Optional[float]:
        if not denominator:
            status[name] = RATIO_DIVIDE_BY_ZERO
            return None
        if denominator < 0:
            status[name] = RATIO_NEGATIVE_DENOMINATOR
            return None
        return numerator / denominator

    ratios = {
        # Profitability
        "gross_profit_ratio": _ratio("gross_profit_ratio", gp, revenue),
        "operating_cost_ratio": _ratio("operating_cost_ratio", opex, revenue),
        "ebitda_ratio": _ratio("ebitda_ratio", ebitda, revenue),
        "net_profit_ratio": _ratio("net_profit_ratio", ni, revenue),
        # Liquidity
        "current_ratio": _ratio("current_ratio", current_assets, current_liab),
        "quick_ratio": _ratio("quick_ratio", cash + ar, current_liab),
        # Capital structure; proprietary ratio = total assets / total equity
        "debt_to_equity_ratio": _ratio("debt_to_equity_ratio", total_liab, total_equity),
        "proprietary_ratio": _ratio("proprietary_ratio", total_assets, total_equity),
    }
    return ratios, status


def compute_ratios(
    period_pl: Dict[str, float],
    bs: Dict[str, float],
) -> Dict[str, Optional[float]]:
    """The ratio values of compute_ratios_with_quality (None where not meaningful)."""
    return compute_ratios_with_quality(period_pl, bs)[0]


# ---------------------------------------------------------
# Trends: Revenue & GM% (TTM)
# ---------------------------------------------------------
def compute_trends(
    client: QBOClient,
    period: Dict[str, str],
) -> Tuple[List[str], List[float], List[float], List[float]]:
    """
    Returns (months, revenue_values, ebitda_values, gp_values) for TTM through
    the selected month. ebitda_values carry the per-month D&A add-back
    (financial-modeling-correctness §1); when no D&A line is identifiable the
    series equals monthly EBIT and an INFO log records that basis (the 4-tuple
    shape is pinned by callers, so the label cannot ride the payload).
    """
    months: List[str] = []
    rev_vals: List[float] = []
    ebitda_vals: List[float] = []
    gp_vals: List[float] = []

    try:
        r = client.get_report(
            "ProfitAndLoss",
            {
                "start_date": period["ttm_start"],
                "end_date": period["ttm_end"],
                "summarize_column_by": "Month",
            },
        )

        header_cols = r.get("Columns", {}).get("Column", []) or []
        months = [c.get("ColTitle") for c in header_cols[1:]]

        # The last column is usually "Total", which we want to exclude for the trend line
        if months and (months[-1] or "").lower() == "total":
             months = months[:-1]
             # We will slice the values later after collection

        rows = r.get("Rows", {}).get("Row", []) or []
        income_rows = cogs_rows = expenses_rows = None

        rows = r.get("Rows", {}).get("Row", []) or []
        income_rows = cogs_rows = expenses_rows = None  # noqa: F841

        # Reuse section aliases from sum_pl_section (redefined here for scope or could be moved to module level)
        SECTION_ALIASES = {
            "income": ["income", "revenue", "total income", "total revenue", "sales", "total sales"],
            "cost of goods sold": ["cost of goods sold", "cost of sales", "total cost of goods sold", "cogs"],
            "gross profit": ["gross profit", "total gross profit", "grossprofit"],
            "expenses": ["expenses", "total expenses"]
        }

        # Helper to match alias
        def match_section(label, target):
            target_list = SECTION_ALIASES.get(target, [target])
            return label in target_list

        
        # Helper to recursively find a section row
        def find_section(rows_list, target):
            for row in rows_list:
                rtype = (row.get("type") or "").lower()
                if rtype == "section":
                    header = row.get("Header", {}).get("ColData", [])
                    label = (header[0].get("value", "") if header else "").lower().strip()
                    group = (row.get("group", "") or "").lower().strip()
                    
                    if match_section(label, target) or match_section(group, target):
                        return row
                    
                    # Recurse
                    sub = row.get("Rows", {}).get("Row", [])
                    found = find_section(sub, target)
                    if found: 
                        return found
            return None

        # Extract subsections
        income_row = find_section(rows, "income")
        cogs_row = find_section(rows, "cost of goods sold")
        expenses_row = find_section(rows, "expenses")
        gp_row = find_section(rows, "gross profit")

        def get_section_totals(sec_row: Optional[Dict], num_cols: int) -> List[float]:
            if not sec_row:
                return [0.0] * num_cols
            
            # Extract absolute calculated total from QBO's intrinsic Summary row
            summary = sec_row.get("Summary", {}).get("ColData", [])
            if summary and len(summary) > 1:
                vals = []
                for c in summary[1:]:
                    raw = c.get("value", "0")
                    val = _parse_amount_or_none(raw)
                    if val is None:
                        # Blank month cell = no activity (genuine zero). Non-blank
                        # garbage is a LOGGED data-quality gap — the trend series
                        # shape is pinned to floats by external consumers
                        # (sum() in dashboard.py / intelligence.py), so the honest
                        # surface for a bad cell here is the log, not None.
                        if not _is_blank(raw):
                            logger.warning(
                                "data_quality: trend summary cell unparseable (%r) — contributes 0.0 to the series (gap logged, series shape pinned by callers)",
                                raw,
                            )
                        vals.append(0.0)
                    else:
                        vals.append(val)
                if len(vals) < num_cols:
                    # Missing trailing columns = months genuinely absent from the
                    # section (no activity) — 0.0 is semantically correct.
                    vals += [0.0] * (num_cols - len(vals))
                return vals
            return [0.0] * num_cols

        n_cols = len(months)
        income_vals = get_section_totals(income_row, n_cols)
        cogs_vals = get_section_totals(cogs_row, n_cols)
        expenses_vals = get_section_totals(expenses_row, n_cols)
        gp_native_vals = get_section_totals(gp_row, n_cols)

        # logger.debug(f"DEBUG: compute_trends vals -> Income: {len(income_vals)}, COGS: {len(cogs_vals)}, Exp: {len(expenses_vals)}")

        # EBITDA = Income - COGS - Expenses + D&A (the add-back —
        # financial-modeling-correctness §1; driver_mapping.py pattern). Without
        # the add-back this series was monthly EBIT mislabeled. The 4-tuple return
        # shape is pinned by external consumers (dashboard_cache, intelligence's
        # sum() aggregation), so the no-D&A-identified case is surfaced via the
        # log here rather than a payload label.
        da_vals, da_identified = _da_values_per_column(r)
        if not da_identified:
            logger.info(
                "compute_trends: no D&A accounts identified on the TTM P&L — monthly EBITDA equals EBIT for this series"
            )

        # We need to handle list lengths safely
        length = max(len(income_vals), len(cogs_vals), len(expenses_vals))

        # Pad with 0s
        def pad(lst, n):
            return lst + [0.0] * (n - len(lst))

        income_vals = pad(income_vals, length)
        cogs_vals = pad(cogs_vals, length)
        expenses_vals = pad(expenses_vals, length)
        da_vals = pad(da_vals, length)

        rev_vals = income_vals
        for i in range(length):
            # EBITDA Calculation (with the per-month D&A add-back)
            ebitda = income_vals[i] - cogs_vals[i] - expenses_vals[i] + da_vals[i]
            ebitda_vals.append(ebitda)
            
            # GP Calculation
            native_gp = gp_native_vals[i] if len(gp_native_vals) > i else 0.0
            gp_val = native_gp if native_gp != 0 else (income_vals[i] - cogs_vals[i])
            gp_vals.append(gp_val)

        # Remove Total column if we detected it earlier (months length was reduced)
        if len(rev_vals) > len(months):
             rev_vals = rev_vals[:len(months)]
        if len(ebitda_vals) > len(months):
             ebitda_vals = ebitda_vals[:len(months)]
        if len(gp_vals) > len(months):
             gp_vals = gp_vals[:len(months)]

    except Exception as e:
        # PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-A1) — the generic message was the
        # only record of WHY the trend failed. Carry the log-safe Intuit fault fields
        # (status, fault codes, intuit_tid — never the body/Detail) into the log and the
        # raised exception so Sentry/logs pinpoint the cause; the chained ``from e`` keeps
        # the full traceback.
        fault = parse_qbo_fault(getattr(e, "response", None))
        logger.warning(
            "Trend KPI extraction failed: status=%s fault_codes=%s intuit_tid=%s error=%s",
            fault.get("status"),
            ",".join(fault.get("fault_codes") or []) or None,
            fault.get("intuit_tid"),
            redact_to_string(str(e), max_length=500),
        )
        fault_bits = [
            f"QBO status {fault['status']}" if fault.get("status") else "",
            f"fault {','.join(fault['fault_codes'])}" if fault.get("fault_codes") else "",
            f"intuit_tid {fault['intuit_tid']}" if fault.get("intuit_tid") else "",
        ]
        fault_context = "; ".join(bit for bit in fault_bits if bit)
        message = "Trend KPI data could not be loaded from QuickBooks."
        if fault_context:
            message = f"{message} ({fault_context})"
        raise RuntimeError(message) from e

    return months, rev_vals, ebitda_vals, gp_vals


def ebitda_margin_series(
    ebitda_values: "list[float]",
    revenue_values: "list[float]",
) -> "list[float | None]":
    """Monthly EBITDA margin % aligned index-for-index with the compute_trends arrays.

    PLAN-EXEMPT: 2026-07-09 honest-failure batch (AC-B1/B3) — the margin series is computed
    here, next to the trend math it derives from, so both arrays come from the ONE QBO
    ProfitAndLoss report and can never misalign.

    Honest-gap rule (refuse over fabricate): a month whose revenue is zero, missing, or
    negative has no computable margin and yields None — never a fabricated 0%. Negative
    EBITDA over positive revenue is a legitimate negative margin and is returned as such.
    """
    series: "list[float | None]" = []
    for i, ebitda in enumerate(ebitda_values):
        revenue = revenue_values[i] if i < len(revenue_values) else None
        if revenue is None or revenue <= 0:
            series.append(None)
        else:
            series.append(round(ebitda / revenue * 100.0, 1))
    return series



def get_db_report(db: Session, realm_id: str, report_name: str) -> Optional[Dict]:
    """
    Fetch the latest synced report from the DB.
    """
    from ..models import QBOReport  # local import to avoid circular dependency
    try:
        rec = (
            db.query(QBOReport)
            .filter_by(realm_id=str(realm_id), report_name=report_name)
            .order_by(QBOReport.created_at.desc())
            .first()
        )
        if rec and rec.data:
            return rec.data
    except Exception as e:
        logger.debug(f"Error in get_db_report: {e}")
        pass
    return None

def fetch_report_or_fallback(
    client: QBOClient, 
    db: Session, 
    report_name: str, 
    live_params: Dict
) -> Dict:
    """
    Try to look up the report in DB first (snapshot).
    If not found, fallback to live API call.
    """
    # 1. Try DB
    # Note: DB reports are typically full "ThisFiscalYearToDate" with "Month" cols.
    # If the user asks for a specific month, we iterate the columns in the DB report.
    # For TTM, we might strictly need live API if the DB report doesn't cover 12 months.
    # For MVP: Prefer DB for Speed/Stability if available.
    if db:
        data = get_db_report(db, client.realm_id, report_name)
        if data:
            return data
            
    # 2. Fallback Live
    return client.get_report(report_name, live_params)


# ---------------------------------------------------------
# Main entrypoint used by /dashboard/summary
# ---------------------------------------------------------
def compute_advanced_kpis(
    client: QBOClient,
    year: Optional[int],
    month: Optional[int],
    db: Session = None,  # Added db parameter
    accounting_method: str = "Accrual",
) -> Dict[str, object]:
    """
    The single entrypoint used by /dashboard/summary.

    Returns a dict matching the shape expected by the new dashboard.html.
    """
    # Resolve the period against the company's REAL fiscal calendar (QBOCompanyInfo
    # via db + the client's realm); without context this falls back to January and
    # the payload's fy_start_source says so (financial-modeling-correctness §8).
    period = resolve_period(
        year,
        month,
        db=db,
        realm_id=getattr(client, "realm_id", None),
    )
    logger.debug(f"DEBUG: compute_advanced_kpis period: {period}")
    
    # 1. Fetch & Compute independently (Robust approach)
    logger.debug(f"DEBUG: compute_advanced_kpis START. Period: {period}")
    
    logger.debug("DEBUG: calling compute_period_pl_kpis...")
    pl_period = compute_period_pl_kpis(client, period, accounting_method)
    logger.debug(f"DEBUG: pl_period: {pl_period}")

    logger.debug("DEBUG: calling compute_ytd_pl_kpis...")
    ytd = compute_ytd_pl_kpis(client, period, accounting_method)

    logger.debug("DEBUG: calling compute_ttm_pl_kpis...")
    ttm = compute_ttm_pl_kpis(client, period, accounting_method)

    logger.debug("DEBUG: calling compute_cashflow_kpis...")
    cf = compute_cashflow_kpis(client, period, accounting_method)
    
    # Balance sheet (Point in Time)
    # Use end_date to get historical snapshot
    bs_date = period["end_date"]
    logger.debug(f"DEBUG: calling compute_balance_sheet_kpis for {bs_date}...")
    bs = compute_balance_sheet_kpis(client, bs_date, accounting_method)
    logger.debug(f"DEBUG: bs keys: {list(bs.keys())}")

    # Ratios
    logger.debug("DEBUG: calling compute_ratios...")
    ratios, ratio_status = compute_ratios_with_quality(pl_period, bs)
    logger.debug("DEBUG: ratios computed.")

    return {
        "available": True,
        "errors": [],
        "data_quality": {
            "is_partial": False,
            "unavailable_sections": [],
            "ratio_status": ratio_status,
        },
        "selected_period_label": period["label"],
        "fy_start_month": period["fy_start_month"],
        "fy_start_source": period["fy_start_source"],

        "period_revenue": pl_period["revenue"],
        "period_gross_profit": pl_period["gross_profit"],
        "period_operating_expenses": pl_period["operating_expenses"],
        "period_ebitda": pl_period["ebitda"],
        "period_da": pl_period["da"],
        "period_ebitda_basis": pl_period["ebitda_basis"],
        "period_non_operating_expenses": pl_period["non_operating_expenses"],
        "period_net_income": pl_period["net_income"],

        "cash": bs["cash"],
        "ar": bs["ar"],
        "total_assets": bs["total_assets"],
        "current_liabilities": bs["current_liabilities"],
        "long_term_liabilities": bs["long_term_liabilities"],
        "total_equity": bs["total_equity"],

        "cashflow": cf,
        "ratios": ratios,

        "ytd": ytd,

        "ttm_revenue": ttm["ttm_revenue"],
        "ttm_gross_profit": ttm["ttm_gross_profit"],
        "ttm_gross_margin_pct": ttm["ttm_gross_margin_pct"],
        "ttm_ebitda": ttm["ttm_ebitda"],
        "ttm_da": ttm["ttm_da"],
        "ttm_ebitda_basis": ttm["ttm_ebitda_basis"],
        "ttm_ebitda_ratio": ttm["ttm_ebitda_ratio"],
    }

# Extraction Helpers
# Extraction Helpers
def extract_period_pl(report: Dict, period: Dict) -> Dict[str, object]:
    """Parse specific month column from P&L report using robust traversal."""
    
    target_label = period["label"] # e.g. "Dec 2025"
    logger.debug(f"DEBUG: P&L Extraction Target: {target_label}")
    
    col_idx = -1
    cols = report.get("Columns", {}).get("Column", []) or []
    
    # 1. Identify Column Index
    for i, c in enumerate(cols):
        title = c.get("ColTitle", "")
        # Robust match: check if label in title OR title in label. The title must
        # be non-empty: QBO's account column carries ColTitle "" and an empty
        # string is `in` every label, which silently locked col_idx onto the
        # account-NAME column (every amount then failed to parse).
        if title and (target_label in title or title in target_label):
            col_idx = i
            logger.debug(f"DEBUG: Found column '{title}' at index {i}")
            break
            
    if col_idx == -1:
        logger.debug(f"DEBUG: Column {target_label} NOT FOUND. Available columns: {[c.get('ColTitle') for c in cols]}")
        # Fallback: Use the last valid data column (before Total) if strictly 1 month requested?
        # For now, if we can't find the month, we return 0s to avoid showing wrong data.
        pass

    # 2. Recursive Traversal for Data Extraction
    # Map standard keys to QBO section names (or partial matches)
    # This is our "Standardized Chart of Accounts" mapping layer
    
    data_map = {
        "revenue": 0.0,
        "cogs": 0.0,
        "opex": 0.0,
        "other_income": 0.0,
        "other_expense": 0.0
    }
    # D&A identified by account name during the SAME traversal (driver_mapping
    # pattern) so the EBITDA below can be an add-back, not GP − OpEx.
    da_state = {"total": 0.0, "found": False}

    def traverse_rows(rows_list, section_type=None):
        for row in rows_list:
            r_type = row.get("type", "")
            
            # If it's a Section, identify its type and recurse
            if r_type == "Section":
                header = row.get("Header", {}).get("ColData", [])
                label = (header[0].get("value") if header else "").lower()
                
                # Determine context (Income, COGS, Expense, etc.)
                current_section = section_type # Inherit by default
                if "income" in label and "other" not in label:
                    current_section = "revenue"
                elif "cost of goods sold" in label:
                    current_section = "cogs"
                elif "expense" in label and "other" not in label:
                    current_section = "opex"
                elif "other income" in label:
                    current_section = "other_income"
                elif "other expense" in label:
                    current_section = "other_expense"
                
                # Recurse
                sub_rows = row.get("Rows", {}).get("Row", [])
                traverse_rows(sub_rows, current_section)
                
            # If it's Data, extract value if we are in a known section
            elif r_type == "Data" and section_type:
                col_data = row.get("ColData", [])
                if col_idx > -1 and col_idx < len(col_data):
                    raw = col_data[col_idx].get("value", "0")
                    val = _parse_amount_or_none(raw)
                    row_name = (col_data[0].get("value") or "").lower() if col_data else ""
                    is_da_row = any(m in row_name for m in _DA_NAME_MARKERS)
                    if is_da_row:
                        da_state["found"] = True
                    if val is None:
                        # Blank month cell = no activity (genuine absence); non-blank
                        # garbage is a LOGGED data-quality gap, never a silent 0.0.
                        if not _is_blank(raw):
                            logger.warning(
                                "data_quality: P&L period cell for %r unparseable (%r) — omitted (honest gap, not 0.0)",
                                row_name,
                                raw,
                            )
                    elif val != 0:
                        data_map[section_type] += val
                        if is_da_row:
                            da_state["total"] += abs(val)
                        # Debug for high-value items
                        # logger.debug(f"  -> Adding {val} to {section_type} from {row.get('ColData')[0].get('value')}")

    # Start traversal
    root_rows = report.get("Rows", {}).get("Row", [])
    traverse_rows(root_rows)
    
    logger.debug(f"DEBUG: P&L Period Data Extracted: {data_map}")

    # 3. Calculate Derived Metrics
    revenue = data_map["revenue"]
    cogs = data_map["cogs"]
    gp = revenue - cogs
    opex = data_map["opex"]

    # EBITDA is an ADD-BACK, never GP − OpEx (financial-modeling-correctness §1;
    # driver_mapping.py pattern) — D&A was name-identified during the traversal.
    da = da_state["total"]
    ebitda = gp - opex + da

    non_op = data_map["other_expense"]
    other_inc = data_map["other_income"]

    net_income = gp - opex + other_inc - non_op

    return {
        "revenue": revenue,
        "gross_profit": gp,
        "operating_expenses": opex,
        "ebitda": ebitda,
        "da": da,
        "ebitda_basis": _ebitda_basis(da_state["found"]),
        "non_operating_expenses": non_op,
        "net_income": net_income
    }

def extract_ytd_pl(report: Dict, period: Dict) -> Dict[str, object]:
    """Extract YTD totals from the 'Total' column of the report."""
    
    # The report passed in usually has monthly columns + a Total column at the end.
    cols = report.get("Columns", {}).get("Column", []) or []
    total_idx = len(cols) - 1 # Ensure we grab the last column
    
    data_map = {
        "sales": 0.0,
        "cogs": 0.0,
        "expenses": 0.0
    }
    # D&A identified by account name during the SAME traversal (driver_mapping
    # pattern) so the EBITDA below can be an add-back, not GP − OpEx.
    da_state = {"total": 0.0, "found": False}

    def traverse_rows_ytd(rows_list, section_type=None):
        for row in rows_list:
            r_type = row.get("type", "")
            if r_type == "Section":
                header = row.get("Header", {}).get("ColData", [])
                label = (header[0].get("value") if header else "").lower()

                current_section = section_type
                if "income" in label and "other" not in label:
                    current_section = "sales"
                elif "cost of goods sold" in label:
                    current_section = "cogs"
                elif "expense" in label and "other" not in label:
                    current_section = "expenses"

                traverse_rows_ytd(row.get("Rows", {}).get("Row", []), current_section)

            elif r_type == "Data" and section_type:
                col_data = row.get("ColData", [])
                if col_data and total_idx < len(col_data):
                    raw = col_data[total_idx].get("value", "0")
                    val = _parse_amount_or_none(raw)
                    row_name = (col_data[0].get("value") or "").lower()
                    is_da_row = any(m in row_name for m in _DA_NAME_MARKERS)
                    if is_da_row:
                        da_state["found"] = True
                    if val is None:
                        # Blank cell = no activity (genuine absence); non-blank
                        # garbage is a LOGGED data-quality gap, never a silent 0.0.
                        if not _is_blank(raw):
                            logger.warning(
                                "data_quality: YTD P&L cell for %r unparseable (%r) — omitted (honest gap, not 0.0)",
                                row_name,
                                raw,
                            )
                        continue
                    data_map[section_type] += val
                    if is_da_row:
                        da_state["total"] += abs(val)

    traverse_rows_ytd(report.get("Rows", {}).get("Row", []))

    sales = data_map["sales"]
    gp = sales - data_map["cogs"]
    # EBITDA is an ADD-BACK, never GP − OpEx (financial-modeling-correctness §1).
    da = da_state["total"]
    ebitda = gp - data_map["expenses"] + da

    return {
        "sales": sales,
        "gross_profit": gp,
        "gross_profit_ratio": gp / sales if sales else 0.0,
        "ebitda": ebitda,
        "da": da,
        "ebitda_basis": _ebitda_basis(da_state["found"]),
        "ebitda_ratio": ebitda / sales if sales else 0.0,
        "net_cash_flow": 0.0
    }

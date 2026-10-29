from __future__ import annotations

import datetime
import logging

from ..models import LboCell, LboPeriod

logger = logging.getLogger(__name__)


def _seed_row(edits, label_to_row, label_key, value, scenario_id, db):
    """Write a value to Y0 (col_index 1) for the matching row."""
    row_idx = label_to_row.get(label_key)
    if row_idx is None:
        return
    cell = db.query(LboCell).filter(
        LboCell.scenario_id == scenario_id,
        LboCell.row_index == row_idx,
        LboCell.col_index == 1,
    ).first()
    if cell:
        cell.value = value
    edits.append({"row": row_idx, "col": 1, "value": value})


def _seed_row_add(edits, label_to_row, label_key, value, scenario_id, db):
    """Add to existing value in Y0 for accumulating multiple QBO accounts into one grid row."""
    row_idx = label_to_row.get(label_key)
    if row_idx is None:
        return
    cell = db.query(LboCell).filter(
        LboCell.scenario_id == scenario_id,
        LboCell.row_index == row_idx,
        LboCell.col_index == 1,
    ).first()
    if cell:
        existing = float(cell.value or 0)
        cell.value = existing + value
    edits.append({"row": row_idx, "col": 1, "value": value})


def _parse_qbo_report_totals(report: dict) -> dict[str, float]:
    """
    Extract top-level line item names and their total values from a QBO report.
    Works with both P&L and Balance Sheet report structures.
    """
    results: dict[str, float] = {}
    if not report:
        return results

    rows = report.get("Rows", {}).get("Row", [])
    for row in rows:
        _extract_row_totals(row, results)
    return results


def _extract_row_totals(row: dict, results: dict[str, float], depth: int = 0):
    """Recursively extract account names and their totals from QBO report rows."""
    header = row.get("Header", {})
    summary = row.get("Summary", {})

    group_name = ""
    if header:
        cols = header.get("ColData", [])
        if cols:
            group_name = cols[0].get("value", "")

    if summary:
        cols = summary.get("ColData", [])
        if len(cols) >= 2 and group_name:
            try:
                value = float(cols[-1].get("value", "0") or "0")
                results[group_name] = value
            except (ValueError, TypeError):
                pass

    col_data = row.get("ColData", [])
    if col_data and len(col_data) >= 2:
        name = col_data[0].get("value", "")
        if name:
            try:
                value = float(col_data[-1].get("value", "0") or "0")
                results[name] = value
            except (ValueError, TypeError):
                pass

    sub_rows = row.get("Rows", {}).get("Row", [])
    for sub_row in sub_rows:
        _extract_row_totals(sub_row, results, depth + 1)


# ═════════════════════════════════════════════════════════════════════════════
# C2-a (plan 20260717-three-statement-spine, AC-4/AC-1) — the period-axis seed:
# fiscal-year windows, per-period statement levels, LboPeriod spine creation,
# and the period-keyed historical cell writes for init-from-qbo.
#
# The col_index convention (AC-1 — the positional read path must not break):
#   * LTM/forecast value cells keep the C1-b backfill convention exactly:
#     ``seq == col_index − 1`` (LTM seq 0 ↔ col 1; Y1..Y5 seq 1..5 ↔ cols 2..6).
#   * HISTORICAL fiscal-year cells carry ``col_index == seq`` (NEGATIVE: FY-1 →
#     −1, FY-2 → −2, FY-3 → −3). Col 0 is the label column and cols 1..6 are
#     taken, so negative stamping is the only collision-free extension — every
#     positional reader bounds-guards (`0 <= col−1 < NUM_YEAR_COLS`,
#     `1 <= col <= 6`, `col > 0`) and therefore IGNORES historical cells by
#     construction. ``period_id`` is the real key; Wave 3 retires col_index.
# ═════════════════════════════════════════════════════════════════════════════

HISTORICAL_FY_COUNT = 3  # DEC-LBO3S-3: 3 fiscal years + LTM as the historical spine
# 6 FORECAST columns (plan 20260718-fycy-absolute-axis, AC-1): FYCY (seq 1) +
# FY+1..FY+5 (seq 2..6). Coupled to grid_helpers.NUM_YEAR_COLS by the invariant
# NUM_YEAR_COLS == FORECAST_YEAR_COUNT + 1 — the two MUST move together or
# grid_qbo.py's init-persist loop (`for ci in range(NUM_YEAR_COLS): periods_by_seq[ci]`)
# raises KeyError. FYCY (the FY containing ltm_end) overlaps TTM by design (standard).
FORECAST_YEAR_COUNT = 6

# The balance-sheet template line_codes — used ONLY by the init-from-qbo diagnostic to count how
# many BS slots a historical year actually populated (a blank historical BS = 0 of these nonzero).
_HISTORICAL_BS_LINE_CODES = frozenset({
    "cash", "ar", "inventory", "other_ca", "ppe", "intangibles",
    "ap", "accrued_liab", "other_cl", "debt_std", "debt_ltd",
    "common_equity", "retained_earnings",
})

# QBO reports current-year earnings as a computed "Net Income" line inside the BS Equity section
# (and sometimes an id-less "Retained Earnings"). These carry a real value but NO account id, so the
# id-keyed aggregation drops them — understating a historical year's equity by ~the year's net income
# and breaking A=L+E for a REAL actual BS (which QBO always balances). Captured by NAME below.
_BS_EQUITY_COMPUTED_NAMES = frozenset({"net income", "net earnings", "retained earnings"})


def _idless_bs_equity_amount(bs_entries: list[dict]) -> float:
    """Sum the QBO BalanceSheet computed EQUITY lines that carry a value but NO account id — the
    lines the id-based aggregation drops. Only BS entries (never the P&L, whose own "Net Income" is
    a separate income flow, not an equity balance). Each matches at most once (QBO emits each line
    once — an id-less name here means the id-keyed path captured nothing for it, so no double-count).
    """
    total = 0.0
    for entry in bs_entries:
        if str(entry.get("qbo_account_id") or "").strip():  # SA-OK: dict.get on an in-memory parsed-report entry
            continue  # has an id → already aggregated into a line_code
        name = " ".join(str(entry.get("account_name") or "").lower().split())  # SA-OK: dict.get on an in-memory parsed-report entry
        if name in _BS_EQUITY_COMPUTED_NAMES:
            total += float(entry.get("amount") or 0.0)  # SA-OK: dict.get on an in-memory parsed-report entry
    return round(total, 2)


# The P&L report's OWN bottom-line row names (normalized) — the INDEPENDENT reference the
# realized-returns NI-reconciliation gate compares the mapped net_income cell against (plan
# 20260720-full-history-realized-returns AC-FRR-3, contract §NI-total substrate). Exact-match
# only, so "Net Operating Income" / "Net Other Income" can never collide.
_PL_NI_TOTAL_NAMES = frozenset({"net income", "net earnings"})

# The BalanceSheet report's OWN equity-section total name (normalized) — the INDEPENDENT
# reference the realized-returns EQUITY-reconciliation gate compares the mapped total_equity
# cell against (Wave-5 review remediation AC-2). Exact-match only, so "Total Liabilities and
# Equity" (and any owner-named sub-account total) can never collide.
_BS_TE_TOTAL_NAMES = frozenset({"total equity"})


def _report_named_total(report: dict, names: frozenset) -> float | None:
    """A QBO report's OWN computed total for an exact-match (normalized) row name — NOT a
    mapped sum. Walks the report rows for a row whose (whitespace-normalized, lowercased)
    first-column name is in ``names`` and returns its LAST money column, handling BOTH QBO
    shapes: the ``{"group": ..., "Summary": {...}}`` section summary and a plain Data row.
    Returns ``None`` when the report carries no such row — never derived from mapped lines
    (the whole point is an independent reference; a mapped re-sum would be tautological). The
    LAST match wins: the total is the report's final summary row for that name.
    """
    if not isinstance(report, dict):
        return None

    def _row_total(cols: list) -> float | None:
        if len(cols) < 2:
            return None
        name = " ".join(str((cols[0] or {}).get("value") or "").lower().split())  # SA-OK: dict.get on an in-memory QBO report ColData cell
        if name not in names:
            return None
        try:
            return round(float((cols[-1] or {}).get("value")), 2)  # SA-OK: dict.get on an in-memory QBO report ColData cell
        except (TypeError, ValueError):
            return None  # a value-less/malformed total row is no reference (never fabricated)

    def _walk(rows: list) -> float | None:
        found: float | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            summary_cols = (row.get("Summary") or {}).get("ColData") or []  # SA-OK: dict.get on an in-memory QBO report row
            data_cols = row.get("ColData") or []  # SA-OK: dict.get on an in-memory QBO report row
            for cols in (summary_cols, data_cols):
                val = _row_total(cols)
                if val is not None:
                    found = val
            sub = _walk((row.get("Rows") or {}).get("Row") or [])  # SA-OK: dict.get on an in-memory QBO report row
            if sub is not None:
                found = sub
        return found

    return _walk((report.get("Rows") or {}).get("Row") or [])  # SA-OK: dict.get on an in-memory QBO report payload


def _pl_report_ni_total(report: dict) -> float | None:
    """The P&L report's OWN Net Income total — QBO's computed bottom line ("Net Income"/"Net
    Earnings"), through the one named-total walker (§NI-total substrate, AC-FRR-3)."""
    return _report_named_total(report, _PL_NI_TOTAL_NAMES)


def _bs_report_te_total(report: dict) -> float | None:
    """The BalanceSheet report's OWN "Total Equity" — QBO's computed equity-section total,
    through the one named-total walker (Wave-5 AC-2, the equity-reconciliation reference)."""
    return _report_named_total(report, _BS_TE_TOTAL_NAMES)


def fiscal_year_windows(
    end_dt: datetime.date,
    fy_month: int,
    count: int = HISTORICAL_FY_COUNT,
) -> list[tuple[datetime.date, datetime.date]]:
    """The ``count`` most recent COMPLETED fiscal years strictly before the fiscal
    year containing ``end_dt`` (the LTM end), most-recent-first (FY-1 first).

    ``fy_month`` is the company's real fiscal-year start month (1..12) from the
    canonical resolver (analysis.ratios._fiscal_year_start_month) — never an
    assumed January. The fiscal year containing ``end_dt`` is the LTM column's
    territory, so FY-1 is the year before it — when the LTM window IS a full
    fiscal year the columns never duplicate. Pure date arithmetic.
    """
    anchor = datetime.date(end_dt.year, fy_month, 1)
    if anchor > end_dt:
        anchor = datetime.date(end_dt.year - 1, fy_month, 1)
    windows: list[tuple[datetime.date, datetime.date]] = []
    for offset in range(1, count + 1):
        fy_start = datetime.date(anchor.year - offset, fy_month, 1)
        fy_end = datetime.date(anchor.year - offset + 1, fy_month, 1) - datetime.timedelta(days=1)
        windows.append((fy_start, fy_end))
    return windows


def forward_fiscal_year_windows(
    end_dt: datetime.date,
    fy_month: int,
    count: int = FORECAST_YEAR_COUNT,
) -> list[tuple[datetime.date, datetime.date]]:
    """The FORWARD counterpart to :func:`fiscal_year_windows`: the fiscal year
    CONTAINING ``end_dt`` (FYCY — projected in full) followed by the next
    ``count − 1`` full fiscal years, in order (FYCY first).

    ``end_dt`` is the LTM/TTM end. The fiscal year containing it is the FIRST
    projected column (seq 1, "FYCY"); its window OVERLAPS the trailing-twelve-month
    TTM window (seq 0) by design — that is the standard LBO axis (the current
    fiscal year is shown projected in full alongside the trailing actuals). The
    subsequent windows are FY+1..FY+(count−1) (seq 2..count).

    ``fy_month`` is the company's real fiscal-year start month (1..12) from the
    canonical resolver (analysis.ratios._fiscal_year_start_month) — never an
    assumed January (AP-FIN-07). Pure date arithmetic; the FYCY window is
    deterministic for every (fy_month, end_dt) pair (no ambiguity: the anchor is
    the most recent fy_month boundary on-or-before ``end_dt``).
    """
    anchor = datetime.date(end_dt.year, fy_month, 1)
    if anchor > end_dt:
        anchor = datetime.date(end_dt.year - 1, fy_month, 1)
    windows: list[tuple[datetime.date, datetime.date]] = []
    for offset in range(count):
        fy_start = datetime.date(anchor.year + offset, fy_month, 1)
        fy_end = datetime.date(anchor.year + offset + 1, fy_month, 1) - datetime.timedelta(days=1)
        windows.append((fy_start, fy_end))
    return windows


def forward_windows_after(
    anchor_end: datetime.date,
    fy_month: int,
    count: int = FORECAST_YEAR_COUNT,
) -> list[tuple[datetime.date, datetime.date]]:
    """The FORWARD windows for a forecast that must start AFTER a post-close actuals band (plan
    20260719-consummation-anchored-model, Wave 2): the first FULL fiscal year whose window STARTS
    strictly after ``anchor_end`` (the last actual month-end), then the next ``count − 1`` full fiscal
    years, in order.

    Contrast :func:`forward_fiscal_year_windows`, whose FIRST window is the fiscal year CONTAINING
    ``end_dt`` (which OVERLAPS the trailing actuals — the FYCY-overlap this resolves). W3d (ratified D-1):
    the partial remainder between ``anchor_end`` and the first full forward FY is now emitted as the STUB
    first window (seq-1) — the tail of the split fiscal year — when ``anchor_end`` is mid-FY; a FY-boundary
    ``anchor_end`` has no stub (full years only, byte-identical to Wave 2). Pure date arithmetic;
    ``fy_month`` (1..12) is the real fiscal-year start month (never assumed January).
    """
    anchor = datetime.date(anchor_end.year, fy_month, 1)
    if anchor > anchor_end:
        anchor = datetime.date(anchor_end.year - 1, fy_month, 1)
    # ``anchor`` is the fy_month boundary on-or-before ``anchor_end``; the first FULL forward FY starts
    # at the NEXT boundary (anchor + 1 year), which is strictly after ``anchor_end`` by construction.
    first_start_year = anchor.year + 1
    # W3d (plan 20260719, ratified D-1: seq-1 = the partial split-FY stub): when ``anchor_end`` falls MID
    # fiscal year, the first forward column is the STUB — the tail of the split FY (anchor_end+1 → that
    # FY-end), a partial period of length f. The following ``count-1`` FULL fiscal years complete the axis
    # (5 full + 1 stub, the NUM_YEAR_COLS/FORECAST_YEAR_COUNT lockstep preserved — the stub is a fraction of
    # year-0, never a new column). A FY-boundary ``anchor_end`` has NO stub → ``count`` full years, byte-
    # identical to the Wave-2 behavior. ``stub_fraction`` (= (end−start+1)/365) is derived by the caller.
    split_fy_end = datetime.date(first_start_year, fy_month, 1) - datetime.timedelta(days=1)
    stub_start = anchor_end + datetime.timedelta(days=1)
    windows: list[tuple[datetime.date, datetime.date]] = []
    if stub_start <= split_fy_end:
        windows.append((stub_start, split_fy_end))  # seq-1: the partial split-FY stub (the mid-FY case)
    for offset in range(count - len(windows)):
        fy_start = datetime.date(first_start_year + offset, fy_month, 1)
        fy_end = datetime.date(first_start_year + offset + 1, fy_month, 1) - datetime.timedelta(days=1)
        windows.append((fy_start, fy_end))
    return windows


def historical_levels_from_entries(
    entries: list[dict],
    mappings: dict[str, str],
) -> tuple[dict[str, float], dict[str, float]]:
    """One fiscal year's parsed P&L+BS leaves → (per-line_code totals, per-account
    amounts). Uses the ONE shared aggregation (standard_grid_builder G4) so a
    historical column sums accounts into template slots by the identical Y0 rule.
    Accounts without an id are skipped (never guessed) — the init-from-qbo rule.
    """
    from .standard_grid_builder import aggregate_tb_by_line_code

    tb: list[dict] = []
    accounts: dict[str, float] = {}
    for entry in entries:
        acct_id = str(entry.get("qbo_account_id") or "").strip()  # SA-OK: dict.get on an in-memory parsed-report entry, NOT an ORM query
        if not acct_id:
            continue
        amount = float(entry.get("amount") or 0.0)  # SA-OK: dict.get on an in-memory parsed-report entry, NOT an ORM query
        tb.append({"id": acct_id, "balance": amount})
        accounts[acct_id] = round(accounts.get(acct_id, 0.0) + amount, 2)
    return aggregate_tb_by_line_code(tb, mappings), accounts


def _report_covers_window(report, start_iso: str | None, end_iso: str) -> bool:
    """Does the report's OWN ``Header`` confirm it covers the requested window?

    Refuse over fabricate (the suite-wide QBO date-mismatch discipline): a
    historical column may only be built from a report whose Header period
    matches what was asked for — an echoed/cached/mismatched report is NOT that
    fiscal year's statements. ``start_iso=None`` checks EndPeriod only (the
    point-in-time BalanceSheet as-of).
    """
    header = report.get("Header") if isinstance(report, dict) else None  # SA-OK: dict.get on an in-memory QBO report payload, NOT an ORM query
    if not isinstance(header, dict):
        return False
    if str(header.get("EndPeriod", ""))[:10] != end_iso:  # SA-OK: dict.get on an in-memory QBO report header, NOT an ORM query
        return False
    if start_iso is not None and str(header.get("StartPeriod", ""))[:10] != start_iso:  # SA-OK: dict.get on an in-memory QBO report header, NOT an ORM query
        return False
    return True


def fetch_historical_fy_data(
    client,
    windows: list[tuple[datetime.date, datetime.date]],
    mappings: dict[str, str],
    parse_entries,
) -> tuple[list[tuple[int, datetime.date, datetime.date]], dict[int, tuple[dict, dict]]]:
    """Fetch + parse each completed fiscal year's P&L/BS pair through the existing
    QBO client seam (AC-1). Returns (fy_present, fy_raw): the (offset, start, end)
    list of years WITH data, and offset → (input levels, per-account amounts).

    AC-3 HONEST DEGRADE (the branch written first): a fiscal year whose reports
    are unavailable, whose Header does NOT confirm the requested window
    (``_report_covers_window`` — refuse over fabricate), or which carries no
    nonzero activity is ABSENT — no period row, no fabricated zero column.
    ``parse_entries`` is the canonical report-leaf parser
    (budget.qbo_reports._parse_pl_single_month_entries), injected so this stays
    client-shape-agnostic and golden-testable.
    """
    fy_present: list[tuple[int, datetime.date, datetime.date]] = []
    fy_raw: dict[int, tuple[dict, dict]] = {}
    for offset, (fy_start, fy_end) in enumerate(windows, start=1):
        try:
            fy_pl = client.get_report("ProfitAndLoss", {
                "start_date": fy_start.isoformat(), "end_date": fy_end.isoformat(),
            })
        except Exception:  # CRAFT-OK: best-effort per-FY QBO pull — an absent year degrades to a missing column (AC-3)
            fy_pl = {}
        fy_pl_covered = _report_covers_window(fy_pl, fy_start.isoformat(), fy_end.isoformat())
        if not fy_pl_covered:
            fy_pl = {}  # unverified window → NOT this year's P&L (never fabricated)
        try:
            # Match the CANONICAL as-of BalanceSheet caller (ratios.py:318-323): a point-in-time
            # BS uses end_date as the as-of selector PLUS summarize_column_by=Total (one clean money
            # column the parser's money-col picker expects) + accounting_method + a start_date for
            # period context. The bare {"end_date"} form (post-aa386718) fetched the BS but real QBO
            # could return a multi-column / differently-shaped sheet the parser mis-reads — this
            # aligns to the battle-tested caller so the historical BS parses like the LTM BS.
            fy_bs = client.get_report("BalanceSheet", {
                "start_date": datetime.date(fy_end.year, 1, 1).isoformat(),
                "end_date": fy_end.isoformat(),
                "summarize_column_by": "Total",
                "accounting_method": "Accrual",
            })
        except Exception:  # CRAFT-OK: best-effort per-FY QBO pull — an absent year degrades to a missing column (AC-3)
            fy_bs = {}
        # Capture the RAW BS diagnostics BEFORE the window-gate may blank fy_bs.
        _bs_fetch_empty = not bool(fy_bs)  # SA-OK: truthiness of an in-memory QBO report dict
        _bs_endperiod = (
            str((fy_bs.get("Header") or {}).get("EndPeriod", ""))[:10]  # SA-OK: dict.get on the in-memory QBO report Header (a date, not the payload) — QBO-OK: single as-of date field, not the report body/Detail
            if isinstance(fy_bs, dict) else ""
        )
        fy_bs_covered = _report_covers_window(fy_bs, None, fy_end.isoformat())
        if not fy_bs_covered:
            fy_bs = {}  # unverified as-of date → NOT this year-end's BS
        bs_entries = parse_entries(fy_bs or {})
        entries = parse_entries(fy_pl or {}) + bs_entries
        fy_levels, fy_accounts = historical_levels_from_entries(entries, mappings)
        # BS-TIE (Owen 2026-07-18): fold QBO's id-less computed EQUITY lines (current-year "Net
        # Income", any id-less "Retained Earnings") into retained_earnings at their REAL QBO value.
        # Without this a historical ACTUAL BS fails A=L+E by ~the year's net income — the id-keyed
        # aggregation drops the id-less line. This uses the real figure QBO reports (never a plug),
        # so the captured equity matches QBO's actual Total Equity and the column balances.
        _idless_equity = _idless_bs_equity_amount(bs_entries)
        if abs(_idless_equity) > 1e-9:
            fy_levels["retained_earnings"] = round(  # SA-OK: dict assignment on the in-memory levels map
                fy_levels.get("retained_earnings", 0.0) + _idless_equity, 2)  # SA-OK: dict.get on the in-memory levels map
        # DIAGNOSTIC (INFO, PII-safe — counts + booleans + period-boundary DATES ONLY, never the QBO
        # payload/amounts/account names): one re-import reveals exactly where a $0 historical BS drops:
        #   bs_fetch_empty=True                      → QBO returned nothing for that as-of BS.
        #   bs_endperiod != fy_end (bs_covered=False) → QBO returned a WRONG-dated sheet (gate correct).
        #   bs_endperiod == fy_end but bs_covered=False → the gate is wrongly rejecting (fix the gate).
        #   bs_covered=True but bs_entries=0          → the parser isn't extracting BS accounts.
        #   bs_entries>0 but bs_nonzero_codes=0       → the accounts aren't mapping to BS line_codes.
        _bs_nonzero = sum(1 for _c in _HISTORICAL_BS_LINE_CODES if abs(fy_levels.get(_c, 0.0)) > 1e-9)  # SA-OK: dict.get on the in-memory levels map, NOT an ORM query
        logger.info(
            "[LBO INIT HIST] fy_end=%s pl_covered=%s bs_fetch_empty=%s bs_endperiod=%s bs_covered=%s bs_entries=%d bs_nonzero_codes=%d",
            fy_end.isoformat(), fy_pl_covered, _bs_fetch_empty, _bs_endperiod or "-",
            fy_bs_covered, len(bs_entries), _bs_nonzero,
        )
        if not any(abs(v) > 1e-9 for v in fy_accounts.values()):
            continue  # AC-3: nothing fabricated for a year without data
        fy_present.append((offset, fy_start, fy_end))
        fy_raw[offset] = (fy_levels, fy_accounts)
    return fy_present, fy_raw


def compute_fy_statement_levels(
    fy_raw: dict[int, tuple[dict, dict]],
    display_offsets: set[int] | None = None,
) -> tuple[dict[int, dict], dict[int, dict], dict[int, dict]]:
    """Per-FY computed statement columns (the period-aware G4 algebra), oldest first.

    CF deltas use the ADJACENT prior year only. ``display_offsets`` (when given) are
    the offsets to EMIT as columns; any other offset present in ``fy_raw`` is computed
    only to serve as the adjacent-prior for the oldest displayed year — the CF-base
    year (Owen 2026-07-18): it gives the oldest HISTORIC column real working-capital
    deltas instead of the no-prior 0s, WITHOUT itself becoming a visible column. With
    ``display_offsets=None`` every present offset is emitted (the pre-CF-base behavior).
    A genuinely-oldest column (no prior fetched) still takes the no-prior convention.
    Returns (levels_by_seq, accounts_by_seq, computed_by_offset) — seq = −offset.
    """
    from .standard_grid_builder import compute_statement_subtotals

    levels_by_seq: dict[int, dict] = {}
    accounts_by_seq: dict[int, dict] = {}
    computed_by_offset: dict[int, dict] = {}
    for offset in sorted(fy_raw, reverse=True):  # oldest first
        prior_levels = computed_by_offset.get(offset + 1)  # SA-OK: dict.get on the in-memory per-FY map, NOT an ORM query
        computed = compute_statement_subtotals(fy_raw[offset][0], prior_levels=prior_levels)
        computed_by_offset[offset] = computed  # always stored — a non-displayed base year is the prior for the oldest displayed year
        if display_offsets is None or offset in display_offsets:
            levels_by_seq[-offset] = computed
            accounts_by_seq[-offset] = fy_raw[offset][1]
    return levels_by_seq, accounts_by_seq, computed_by_offset


def period_axis_meta(periods_by_seq: dict[int, LboPeriod]) -> list[dict]:
    """The meta-cell period-axis descriptor (AC-2): one dict per spine column."""
    return [
        {
            "seq": p.seq, "kind": p.kind, "label": p.label,
            "period_start": p.period_start.isoformat() if p.period_start else None,
            "period_end": p.period_end.isoformat() if p.period_end else None,
            "provenance": p.provenance,
        }
        for _seq, p in sorted(periods_by_seq.items())
    ]


def historical_fy_specs(
    db,
    scenario_id: int,
    company_id: str,
    ltm_end_iso: str,
) -> tuple[list[tuple[str, str, str]], list[str] | None]:
    """The (key, start, end) fiscal-year fetch specs + chronology for the internal
    historical-data collaborator (G6).

    A period-keyed scenario derives the REAL windows from its ``LboPeriod`` spine
    (seq −k → ``fy_k``; seq order IS the chronology, returned oldest → newest with
    ``"ltm"`` last). A legacy scenario (no periods) falls back to fiscal-calendar
    windows from the canonical resolver — chronology ``None`` (the caller's
    default order applies; nothing period-derived is fabricated).
    """
    period_rows = (
        db.query(LboPeriod)
        .filter(
            LboPeriod.scenario_id == scenario_id,
            LboPeriod.company_id == company_id,
        )
        .order_by(LboPeriod.seq)
        .all()
    )
    hist_rows = [
        p for p in period_rows
        if p.kind == "historical_fy" and p.period_start and p.period_end
    ]
    if hist_rows:
        fy_specs = [
            (f"fy_{-p.seq}", p.period_start.isoformat(), p.period_end.isoformat())
            for p in hist_rows
        ]
        return fy_specs, [key for key, _s, _e in fy_specs] + ["ltm"]
    # Legacy fallback: fiscal-calendar windows (never an assumed January).
    from ..analysis.ratios import _fiscal_year_start_month

    fy_month, _fy_src = _fiscal_year_start_month(db, company_id)
    fy_specs = [
        (f"fy_{offset}", s.isoformat(), e.isoformat())
        for offset, (s, e) in enumerate(
            fiscal_year_windows(datetime.date.fromisoformat(ltm_end_iso), fy_month), start=1,
        )
    ]
    return fy_specs, None


def legacy_prior_anchors(
    client,
    start_dt: datetime.date,
    mappings: dict[str, str],
    parse_totals,
) -> tuple[float | None, float | None]:
    """The pre-period-axis prior-TTM anchors for a tenant with NO fiscal-year
    history: (net PP&E via a prior-year TrialBalance, prior revenue via a prior
    P&L) — moved verbatim from init-from-qbo (C2-a decomposition; byte-identical
    fallback behavior). Missing prior data degrades both to None so capex +
    growth fall back to their named assumption defaults (never fabricated).
    ``parse_totals`` = qbo_seed._parse_qbo_report_totals (injected by the caller).
    """
    from .seed_derivation import extract_net_ppe

    net_ppe_prior = None
    prior_revenue = None
    try:
        prior_end = start_dt - datetime.timedelta(days=1)
        prior_start = prior_end.replace(year=prior_end.year - 1) + datetime.timedelta(days=1)
        prior_tb = client.get_report("TrialBalance", {
            "start_date": prior_start.strftime("%Y-%m-%d"),
            "end_date": prior_end.strftime("%Y-%m-%d"),
        })
        net_ppe_prior = extract_net_ppe(prior_tb or {}, mappings)
        prior_pl = client.get_report("ProfitAndLoss", {
            "start_date": prior_start.strftime("%Y-%m-%d"),
            "end_date": prior_end.strftime("%Y-%m-%d"),
        })
        _prior_totals = parse_totals(prior_pl or {})
        _pr = _prior_totals.get("Total Income", _prior_totals.get("Income"))  # SA-OK: dict.get on the parsed prior-P&L totals, NOT an ORM query
        prior_revenue = abs(float(_pr)) if _pr not in (None, "") else None
    except Exception:  # CRAFT-OK: best-effort prior-period pull; capex + revenue growth degrade to their assumption defaults
        logger.debug("[LBO INIT] prior-period reports unavailable; capex + growth fall back to assumption")
    return net_ppe_prior, prior_revenue


def build_period_spine(
    db,
    scenario_id: int,
    company_id: str,
    ltm_window: tuple[datetime.date, datetime.date],
    historical: list[tuple[int, datetime.date, datetime.date]],
    fy_month: int,
    seq0_label: str = "TTM",
    actuals: list[tuple[int, str, str, datetime.date, datetime.date]] | None = None,
    forecast_anchor_end: datetime.date | None = None,
) -> dict[int, LboPeriod]:
    """Delete + recreate the scenario's ``LboPeriod`` spine; returns {seq: period}
    with ids flushed (plan 20260718-fycy-absolute-axis, AC-2 — the FYCY axis).

    The axis (10 columns, 7 positive):

    * ``historical`` = the PRESENT fiscal years as (offset, start, end) — offset 1 →
      seq −1 … offset 3 → seq −3; kind ``historical_fy``, real calendar bounds,
      provenance ``qbo_actual``. The label is the **absolute fiscal year** taken
      from ``period_end.year`` (e.g. "2021"), NOT a relative "FY-1" — the axis now
      names each historical column by its calendar year. A missing year simply has
      no row (honest degrade — fewer historical columns, nothing fabricated).
    * seq 0 = the ENTRY anchor (kind STAYS ``ltm`` — the engine anchor / consumer
      contract is unchanged; only the LABEL varies via ``seq0_label``: "At Close"
      when init-from-qbo anchored the entry at the deal consummation date, else the
      default "TTM"), its real trailing-twelve-month window, provenance
      ``qbo_actual``.
    * seq 1 = **FYCY** (the fiscal year CONTAINING ``ltm_window[1]``, projected in
      full), and seq 2..FORECAST_YEAR_COUNT = **FY+1..FY+5** — kind ``forecast``,
      provenance ``derived``, each carrying its REAL fiscal-year ``period_start`` /
      ``period_end`` from :func:`forward_fiscal_year_windows` (a later wave computes
      a dated XIRR off these — the forecast bounds are no longer NULL). ``fy_month``
      (1..12, from the canonical resolver — never assumed January) drives the
      forward windows. FYCY intentionally OVERLAPS the TTM window (standard axis).

    Delete-then-create is idempotent: init-from-qbo owns the whole grid (cells were
    just deleted or never existed), so a stale backfill spine is replaced wholesale.
    """
    db.query(LboPeriod).filter(
        LboPeriod.scenario_id == scenario_id,
        LboPeriod.company_id == company_id,
    ).delete()
    periods: dict[int, LboPeriod] = {}

    def _add(seq: int, kind: str, label: str,
             start: datetime.date | None, end: datetime.date | None,
             provenance: str) -> None:
        period = LboPeriod(
            company_id=company_id, scenario_id=scenario_id, seq=seq, kind=kind,
            label=label, period_start=start, period_end=end, provenance=provenance,
        )
        db.add(period)
        periods[seq] = period

    for offset, fy_start, fy_end in historical:
        _add(-offset, "historical_fy", str(fy_end.year), fy_start, fy_end, "qbo_actual")
    # seq 0: kind STAYS ``ltm`` (the engine anchor / consumer contract — plan 20260719 D-CA-3),
    # only the LABEL varies: "At Close" when the entry is anchored at the deal consummation date
    # (``seq0_label`` from init-from-qbo), else the default "TTM" (a deal with no resolved
    # consummation, honest-degraded to the last-closed-month trailing-twelve-month window).
    _add(0, "ltm", seq0_label, ltm_window[0], ltm_window[1], "qbo_actual")
    # Post-close actuals band (Wave 2 / AC-CA-3) — OFF-POSITIONAL rows at a reserved high seq (>=
    # BAND_COL_BASE), authored by post_close_band.build_band_spine. Real monthly actuals + annual
    # rollups between the entry and the last-closed month; invisible to every positional 1..N reader
    # by the SAME period_id-keyed mechanism as the negative-seq historicals. Empty when no band.
    for bseq, bkind, blabel, bstart, bend in (actuals or []):
        _add(bseq, bkind, blabel, bstart, bend, "qbo_actual")
    # The forward FYCY + FY+1..FY+5 windows. With a post-close band present the forecast must START
    # AFTER the last tied actual month (forecast_anchor_end) — forward_windows_after resolves the §3
    # FYCY-overlap (the partial remainder is the Wave-3 stub). With no band the windows anchor on the
    # entry end via forward_fiscal_year_windows (FYCY = the FY CONTAINING the entry — Wave-1 behavior).
    if forecast_anchor_end is not None:
        forward = forward_windows_after(forecast_anchor_end, fy_month, FORECAST_YEAR_COUNT)
    else:
        forward = forward_fiscal_year_windows(ltm_window[1], fy_month, FORECAST_YEAR_COUNT)
    for idx, (fwd_start, fwd_end) in enumerate(forward):
        seq = idx + 1
        label = "FYCY" if idx == 0 else f"FY+{idx}"
        _add(seq, "forecast", label, fwd_start, fwd_end, "derived")
    db.flush()  # ids materialize inside the caller's open transaction
    return periods


def persist_historical_cells(
    db,
    scenario_id: int,
    grid_rows: list[dict],
    periods_by_seq: dict[int, LboPeriod],
    levels_by_seq: dict[int, dict[str, float]],
    accounts_by_seq: dict[int, dict[str, float]],
) -> int:
    """Write the period-keyed historical value cells for every present fiscal year
    (AC-1: statement columns land as LboCell rows keyed by ``period_id``, stamped
    ``col_index == seq`` — negative, invisible to every positional reader).

    Template rows take the column's computed statement level for their
    ``line_code`` (inputs AND subtotal/formula rows — the period-aware G4
    algebra); per-account child rows take the account's amount for that year and
    are simply ABSENT when the account has no activity (absence ≠ 0 — nothing
    fabricated). Returns the number of cells written.
    """
    written = 0
    for seq in sorted(levels_by_seq):
        period = periods_by_seq[seq]
        levels = levels_by_seq[seq]
        accounts = accounts_by_seq.get(seq, {})  # SA-OK: dict.get on the in-memory per-period map, NOT an ORM query
        for row_data in grid_rows:
            line_code = row_data.get("line_code")  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
            acct_id = row_data.get("qbo_account_id")  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
            if line_code:
                value = levels.get(line_code)  # SA-OK: dict.get on the in-memory levels map, NOT an ORM query
            elif acct_id and str(acct_id) in accounts:
                value = accounts[str(acct_id)]
            else:
                continue  # no historical value for this row-year — absent, never 0
            dp = dict(row_data.get("driver_params", {}) or {})  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
            _plc = row_data.get("_parent_line_code")  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
            if _plc:
                dp["_parent_line_code"] = _plc
            db.add(LboCell(
                scenario_id=scenario_id,
                row_index=row_data["row"],
                col_index=seq,                      # negative — see the convention note above
                period_id=period.id,
                line_code=line_code,
                label=row_data.get("label", ""),
                value=value,
                formula=row_data.get("formula"),
                display_fmt=row_data.get("display_fmt", "currency"),
                row_type=row_data.get("row_type", "input"),
                section=row_data.get("section", ""),
                indent=row_data.get("indent", 0),
                qbo_account_id=acct_id,
                driver_type=row_data.get("driver_type", "auto"),
                driver_params=dp,
            ))
            written += 1
    return written

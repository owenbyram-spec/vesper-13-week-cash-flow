"""Post-close actuals band (plan 20260719-consummation-anchored-model, Wave 2 / AC-CA-3).

The REAL QBO months between the deal consummation (the seq-0 "At Close" entry) and the last-closed
month — shown as ACTUALS columns between the entry and the forecast. The band is OFF-POSITIONAL:
period_id-keyed ``LboCell`` rows at a reserved high ``seq == col_index`` block (``BAND_COL_BASE``),
kind ``'post_close_actual'`` (the sub-annual grain) and ``'post_close_actual_fy'`` (the annual rollup),
so every positional 1..NUM_YEAR_COLS reader (the engine seed, S&U/EV, returns denominators) is
UNTOUCHED — the band cannot leak into the ebitda/fcf/returns vectors (the correctness firewall).

The load-bearing gate: every band column fetches REAL P&L (period) + BS (as-of the period end) and MUST
tie A = L + E (refuse-over-fabricate) — a non-tying / unavailable month honest-degrades ABSENT (dropped),
never persisted unbalanced. Reuses the shipped historical-actuals machinery (``_report_covers_window``
Header gate + the ``_idless_bs_equity_amount`` BS-TIE fold + ``compute_statement_subtotals``).

Collapse-to-annual (D-CA-2): a per-line_code rollup over the tied months — FLOW codes (P&L + every ``cf_``
flow) SUM; STOCK codes (every BS line + ``cf_end_cash``) take the LAST month's snapshot; ``cf_beg_cash``
takes the FIRST month's. The rolled-up BS ties by construction (it IS one real month's already-tied
as-of BS); the tie is re-asserted. No balance sheet is ever summed.

Full history (plan 20260720-full-history-realized-returns, AC-FRR-1 / D-FRR-2): the monthly grain is
capped at ``MAX_BAND_MONTHS``, and the cap is FISCAL-YEAR-ALIGNED — the monthly band starts at the
first FY boundary on-or-after the naive most-recent-36 slice start, so the monthly band never splits
a fiscal year. The pre-monthly years (close → the monthly start) are fetched as ANNUAL fiscal-year
windows through the SAME gate set (:func:`fetch_post_close_history`), with chained CF priors:
opening(as-of-close) → annual columns → monthly columns. A DROPPED window (failed gates) BREAKS the
chain: the next present column's CF-delta lines are REFUSED (absent, ``_CF_DELTA_CODES``) — never
computed off the stale pre-gap prior, which would silently absorb the gap's ΔWC as one phantom flow —
and every drop is recorded for the meta cell's ``band_continuity``.
"""
from __future__ import annotations

import datetime
import logging

logger = logging.getLogger(__name__)

# The reserved OFF-POSITIONAL namespaces (the three-range col_index invariant): < 0 = historical_fy ·
# 1..NUM_YEAR_COLS = positional entry/forecast · >= BAND_COL_BASE = the post-close actuals band.
BAND_COL_BASE = 100          # monthly band periods at seq == col_index == 100 + i (chronological)
BAND_FY_ROLLUP_BASE = 1000   # the annual-rollup columns at 1000 + j (disjoint from the monthly block)
BAND_KIND = "post_close_actual"
BAND_FY_KIND = "post_close_actual_fy"

# Bound the band so a long-unimported deal cannot fan out unbounded monthly QBO reads / cells. Beyond
# this the OLDEST months are dropped (the recent months matter most for the forecast hand-off).
MAX_BAND_MONTHS = 36

_TIE_TOL = 0.01  # dollars; statement levels round to 2 decimals, so a real tie is < 1 cent

# The collapse-to-annual classification (compute_statement_subtotals' full line_code taxonomy):
# STOCK codes take the last month's snapshot (a balance sheet is a point-in-time, never summed);
# cf_beg_cash takes the FIRST month's; everything else (P&L inputs/subtotals + every cf_ FLOW) sums.
_STOCK_CODES = frozenset({
    "cash", "ar", "inventory", "other_ca", "ppe", "intangibles", "goodwill", "other_assets",
    "ap", "accrued_liab", "other_cl", "debt_std", "debt_ltd", "common_equity", "retained_earnings",
    "total_ca", "total_assets", "total_cl", "total_debt", "total_liab", "total_equity",
    "total_liab_equity", "balance_check", "cf_end_cash",
})

# The prior-dependent CASH-FLOW line_codes — every line whose value derives from the ADJACENT-prior
# column. When a chain break precedes a column (its adjacent prior was dropped by a gate), these
# lines are REFUSED (absent): computing them off the stale pre-gap prior would silently absorb the
# gap's ΔWC/flows into one phantom delta (the refuted phantom-flow bug — plan 20260720 AC-FRR-1,
# SYNTHESIS §1-§2). ``cf_ni``/``cf_depr`` are pure current-period flows and survive a break.
_CF_DELTA_CODES = frozenset({
    "cf_equity",  # AC-FRR-9: the clean-surplus equity flow — prior-dependent like every delta line
    "cf_ar", "cf_inv", "cf_oca", "cf_ap", "cf_accrued", "cf_ocl",
    "cf_capex", "cf_cfi", "cf_debt_std", "cf_debt_ltd",
    "cf_cfo", "cf_cff", "fcf", "cf_net_change", "cf_beg_cash", "cf_end_cash",
})


def _add_months(first_of_month: datetime.date, n: int) -> datetime.date:
    """First-of-month ``n`` months after ``first_of_month`` (leap-safe; no ``replace``)."""
    m = first_of_month.month - 1 + n
    return datetime.date(first_of_month.year + m // 12, m % 12 + 1, 1)


def _month_end(first_of_month: datetime.date) -> datetime.date:
    return _add_months(first_of_month, 1) - datetime.timedelta(days=1)


def post_close_actual_windows(
    entry_end: datetime.date,
    last_closed_end: datetime.date,
    fy_month: int,
) -> list[tuple[datetime.date, datetime.date]]:
    """The MONTHLY (first-of-month, month-end) windows for the band: every full month AFTER the entry
    (``entry_end`` is the close month-end) through the last-closed month (``last_closed_end`` is a
    month-end). Empty when the entry already IS the last-closed month (the recent-close case — no band).

    FY-ALIGNED cap (D-FRR-2): a span over ``MAX_BAND_MONTHS`` keeps the most recent months, but the
    cut aligns FORWARD to the first fiscal-year boundary on-or-after the naive most-recent-36 slice
    start — the monthly band never splits a fiscal year (≤ 36 monthly columns). The months before the
    aligned boundary are the ANNUAL fetch's territory (:func:`plan_band_windows`), so a mid-FY slice
    can never leave part of a year fetched by nobody. ``fy_month`` (1..12) is the company's real
    fiscal-year start month from the canonical resolver — never an assumed January.
    """
    if last_closed_end <= entry_end:
        return []
    windows: list[tuple[datetime.date, datetime.date]] = []
    start = entry_end + datetime.timedelta(days=1)  # entry_end is a month-end → this is a first-of-month
    while start <= last_closed_end:
        end = _month_end(start)
        if end > last_closed_end:
            break
        windows.append((start, end))
        start = _add_months(start, 1)
    if len(windows) > MAX_BAND_MONTHS:
        cut = windows[len(windows) - MAX_BAND_MONTHS][0]     # the naive most-recent-36 slice start
        aligned = datetime.date(cut.year, fy_month, 1)       # → the first FY boundary >= cut
        if aligned < cut:
            aligned = datetime.date(cut.year + 1, fy_month, 1)
        kept = [w for w in windows if w[0] >= aligned]
        logger.info("[LBO BAND] band span %d months exceeds cap %d — monthly band FY-aligned at %s "
                    "(%d monthly kept; the older months are annual-fetch territory)",
                    len(windows), MAX_BAND_MONTHS, aligned.isoformat(), len(kept))
        windows = kept
    return windows


def plan_band_windows(
    entry_end: datetime.date,
    last_closed_end: datetime.date,
    fy_month: int,
) -> tuple[list[tuple[datetime.date, datetime.date]], list[tuple[datetime.date, datetime.date]]]:
    """The FULL post-close fetch plan (AC-FRR-1): ``(annual_windows, monthly_windows)``.

    ``monthly_windows`` is :func:`post_close_actual_windows` (FY-aligned cap). ``annual_windows``
    covers the gap close → monthly start as FISCAL-YEAR windows: the first may be a PARTIAL
    close→FY-end year; the rest are full FYs ending exactly at the monthly boundary − 1 day (the
    boundary is FY-aligned by construction, so annual and monthly territory can never overlap or
    split a year between them). Empty when the cap did not engage (the whole span is monthly).
    """
    monthly = post_close_actual_windows(entry_end, last_closed_end, fy_month)
    if not monthly:
        return [], []
    boundary = monthly[0][0]
    annual: list[tuple[datetime.date, datetime.date]] = []
    cur = entry_end + datetime.timedelta(days=1)
    while cur < boundary:
        nxt = datetime.date(cur.year, fy_month, 1)           # the next FY boundary strictly after cur
        while nxt <= cur:
            nxt = datetime.date(nxt.year + 1, fy_month, 1)
        annual.append((cur, nxt - datetime.timedelta(days=1)))
        cur = nxt
    return annual, monthly


def fetch_post_close_actuals(
    client,
    windows: list[tuple[datetime.date, datetime.date]],
    mappings: dict[str, str],
    parse_entries,
    initial_prior_levels: dict | None = None,
    grain: str = "monthly",
    dropped: list[dict] | None = None,
    chain_broken: bool = False,
    pl_report_ni: dict | None = None,
    bs_report_te: dict | None = None,
    articulation: list[dict] | None = None,
    bs_accounts: dict | None = None,
) -> list[tuple[datetime.date, datetime.date, dict, dict]]:
    """Fetch + parse + TIE each band window, oldest first. Returns [(start, end, computed, accounts)] for
    ONLY the windows that are present AND tie A = L + E — a non-tying / uncovered / empty window is
    DROPPED (honest-degrade absent), never fabricated. Mirrors ``fetch_historical_fy_data``: real
    P&L(period) + BS(as-of end, canonical caller shape) through the injected canonical parser, the
    ``_report_covers_window`` Header gate (refuse-over-fabricate), the id-less computed-equity fold
    (BS-TIE). CF deltas use the ADJACENT prior period — the first window's prior is
    ``initial_prior_levels`` (the entry's real closing BS), so the working-capital roll is continuous
    from the entry. Window-length-AGNOSTIC: the same gate set serves the monthly band AND the annual
    old-FY windows (``grain`` labels the drop records only).

    Continuity (AC-FRR-1): every drop is appended to ``dropped`` (when given) as
    ``{start, end, grain, reason}`` and BREAKS the chain — the next PRESENT column is computed with
    NO prior and its ``_CF_DELTA_CODES`` lines are stripped (REFUSED, absent), never rolled off the
    stale pre-gap prior (which would silently absorb the gap's ΔWC as one phantom flow).
    ``chain_broken=True`` marks the chain broken on ENTRY (the caller's upstream adjacent window —
    e.g. the final annual FY — was dropped), refusing the first present column's CF lines by the
    same rule. The chain re-establishes at each tied column.

    NI-total substrate (AC-FRR-3, contract §NI-total substrate): when ``pl_report_ni`` is given,
    every KEPT window whose covered P&L carries QBO's own Net Income bottom-line row records
    ``{period_end ISO: total}`` into it — the INDEPENDENT reference the realized-returns
    NI-reconciliation gate compares the mapped ``net_income`` cell against. A dropped window, or
    a kept window whose report has no such row (a BS-only column), records nothing (absent, never
    fabricated — downstream refuses rather than silently passes).

    TE-total substrate (Wave-5 review remediation AC-2): symmetrically, when ``bs_report_te`` is
    given, every KEPT window whose as-of BalanceSheet carries QBO's own "Total Equity" section
    total records ``{period_end ISO: total}`` into it — the INDEPENDENT reference the
    equity-reconciliation gate compares the mapped ``total_equity`` cell against (a misrouted
    equity account keeps the tie and the NI gate green; only this reference exposes it). Same
    absent-never-fabricated rule.

    Per-account BS substrate (live-verify remediation 2026-07-21, plan 20260720 amendment —
    Owen: "there were in fact balance sheet balances in these COAs for the historic period"):
    when ``bs_accounts`` is given, every KEPT window records ``{period_end ISO:
    {qbo_account_id: as-of balance}}`` — the id-keyed BalanceSheet entries the tie gate
    consumed, BS-ONLY (a P&L account never appears — an annual child cell must be a stock
    snapshot, never a mislabeled flow). The annual columns' per-account CHILD cells snapshot
    from this map (:func:`build_band_spine`); the join to the grid's child-row registry
    happens at persist (``persist_historical_cells`` keys child rows by ``qbo_account_id``).
    An account absent from a window's BS is absent from its map — that column's child cell
    is ABSENT (never a fabricated 0.0). A dropped window records nothing.

    CF articulation (amendment AC-FRR-9/10): the initial prior NORMALIZES through the one
    statement algebra (so the first column's ``cf_equity = ΔTE − NI`` reads a REAL prior equity
    level or none at all), and every kept column runs the inline NI/TE reconciliation + the
    ending-cash tie + the impairment disclosure via :mod:`.band_articulation`, appending
    refusal/disclosure records to ``articulation`` (when given) — recorded beside the
    ``dropped`` continuity list, per column, never a drop.
    """
    from .band_articulation import gate_column_articulation, normalize_initial_prior
    from .qbo_seed import (
        _bs_report_te_total,
        _idless_bs_equity_amount,
        _pl_report_ni_total,
        _report_covers_window,
        historical_levels_from_entries,
    )
    from .standard_grid_builder import compute_statement_subtotals

    def _drop(d_start: datetime.date, d_end: datetime.date, reason: str) -> None:
        if dropped is not None:
            dropped.append({"start": d_start.isoformat(), "end": d_end.isoformat(),
                            "grain": grain, "reason": reason})

    out: list[tuple[datetime.date, datetime.date, dict, dict]] = []
    # AC-FRR-9a (refuter edit 2): the raw initial prior carries NO total_equity key — normalize
    # it through the ONE algebra (the build_t0_anchor precedent) so the first column's equity
    # flow reads a REAL prior level, or none at all ($0/absent sheet → level popped).
    prior_levels = normalize_initial_prior(initial_prior_levels, _TIE_TOL)
    broken = chain_broken
    for start, end in windows:
        try:
            pl = client.get_report("ProfitAndLoss", {"start_date": start.isoformat(), "end_date": end.isoformat()})
        except Exception:  # CRAFT-OK: best-effort per-month QBO pull — an absent month degrades to a dropped column
            pl = {}
        if not _report_covers_window(pl, start.isoformat(), end.isoformat()):
            pl = {}  # unverified window → NOT this month's P&L (never fabricated)
        try:
            bs = client.get_report("BalanceSheet", {
                "start_date": datetime.date(end.year, 1, 1).isoformat(),
                "end_date": end.isoformat(),
                "summarize_column_by": "Total",
                "accounting_method": "Accrual",
            })
        except Exception:  # CRAFT-OK: best-effort per-month QBO pull — an absent month degrades to a dropped column
            bs = {}
        bs_entries = parse_entries(bs or {})
        if not _report_covers_window(bs, None, end.isoformat()):
            bs_entries = []
        entries = parse_entries(pl or {}) + bs_entries
        levels, accounts = historical_levels_from_entries(entries, mappings)
        _idless = _idless_bs_equity_amount(bs_entries)
        if abs(_idless) > 1e-9:
            levels["retained_earnings"] = round(levels.get("retained_earnings", 0.0) + _idless, 2)  # SA-OK: dict on the in-memory levels map
        if not any(abs(v) > 1e-9 for v in accounts.values()):
            logger.info("[LBO BAND] %s window %s absent (no QBO activity) — dropped", grain, end.isoformat())
            _drop(start, end, "no_activity")
            broken = True  # the gap breaks the CF chain for the next present column
            continue  # nothing fetched for this window — absent, never fabricated
        # A broken inbound chain computes with NO prior — the stale pre-gap prior must never feed the
        # deltas; the (all-zero) delta lines are then stripped below (REFUSED absent, never a fake 0).
        computed = compute_statement_subtotals(levels, prior_levels=None if broken else prior_levels)
        if abs(float(computed.get("total_assets") or 0.0)) < _TIE_TOL:  # SA-OK: dict.get on the in-memory computed levels
            # A present P&L with an ABSENT / uncovered / wrong-dated BS yields an ALL-ZERO balance sheet
            # that TRIVIALLY ties (0 == 0). The refuse-over-fabricate firewall must reject it — a real
            # actual window has a real balance sheet; a $0-BS "tie" is a fabricated column, not data.
            logger.info("[LBO BAND] %s window %s has no balance sheet (total_assets≈0) — dropped (never a $0-BS tie)", grain, end.isoformat())
            _drop(start, end, "no_balance_sheet")
            broken = True
            continue
        bc = float(computed.get("balance_check") or 0.0)  # SA-OK: dict.get on the in-memory computed levels
        if abs(bc) > _TIE_TOL:
            # Refuse-over-fabricate: a REAL actual BS must tie (QBO always balances); a non-tie is a
            # parse/map gap, not real data — drop it rather than persist a plugged/broken column.
            logger.info("[LBO BAND] %s window %s balance_check=%.2f fails A=L+E — dropped (never plugged)", grain, end.isoformat(), bc)
            _drop(start, end, "balance_check_failed")
            broken = True
            continue
        if broken:
            # AC-FRR-1 continuity: the adjacent prior was dropped — this column's CF-delta lines are
            # REFUSED (absent). Its P&L + BS levels stand (they are this window's own real data) and
            # it becomes the next window's adjacent prior, re-establishing the chain.
            logger.info("[LBO BAND] %s window %s follows a dropped window — CF-delta lines refused (chain break)", grain, end.isoformat())
            computed = {k: v for k, v in computed.items() if k not in _CF_DELTA_CODES}
        _ni_total = _pl_report_ni_total(pl)
        _te_total = _bs_report_te_total(bs)
        if pl_report_ni is not None and _ni_total is not None:
            pl_report_ni[end.isoformat()] = _ni_total
        if bs_report_te is not None and _te_total is not None:
            bs_report_te[end.isoformat()] = _te_total
        if bs_accounts is not None:
            # The BS-ONLY per-account balances for the KEPT window (the child-cell substrate) —
            # id-keyed through the ONE shared aggregation, from the SAME bs_entries the tie
            # consumed. A kept window always has a real BS (the $0-BS gate), so this is real data.
            bs_accounts[end.isoformat()] = historical_levels_from_entries(bs_entries, mappings)[1]
        # AC-FRR-9a/10: the inline NI/TE gates (a mis-mapped column never persists a wrong
        # cf_equity — suppressed through the one algebra), the ending-cash tie disclosure,
        # and the impairment disclosure. Records land beside the continuity list.
        computed = gate_column_articulation(
            computed, levels, None if broken else prior_levels,
            report_ni=_ni_total, report_te=_te_total, period_end_iso=end.isoformat(),
            grain=grain, tie_tol=_TIE_TOL, articulation=articulation,
            recompute=compute_statement_subtotals,
        )
        out.append((start, end, computed, accounts))
        prior_levels = computed  # the next window's CF deltas roll off this tied window
        broken = False
    return out


def _fiscal_year_end_year(period_end: datetime.date, fy_month: int) -> int:
    """The label year of the fiscal year CONTAINING ``period_end`` — the calendar year of the FY-END.
    A calendar FY (fy_month==1) returns ``period_end.year``; a July FY maps Jul..Dec into the NEXT year.
    """
    if fy_month == 1:
        return period_end.year
    return period_end.year + 1 if period_end.month >= fy_month else period_end.year


def _rollup_levels(months: list[tuple[datetime.date, datetime.date, dict, dict]]) -> dict:
    """Per-line_code collapse of the tied months' COMPUTED levels: STOCK codes snapshot the LAST month,
    ``cf_beg_cash`` takes the FIRST month, every other (FLOW) code sums. No balance sheet is summed; the
    rolled-up BS ties because it IS the last month's already-tied as-of snapshot.
    """
    first, last = months[0][2], months[-1][2]
    codes: set[str] = set()
    for _s, _e, lv, _a in months:
        codes.update(lv)
    out: dict[str, float] = {}
    for code in codes:
        if code == "cf_beg_cash":
            out[code] = round(float(first.get(code) or 0.0), 2)  # SA-OK: dict.get on the in-memory first-month levels
        elif code in _STOCK_CODES:
            out[code] = round(float(last.get(code) or 0.0), 2)  # SA-OK: dict.get on the in-memory last-month levels
        else:
            out[code] = round(sum(float(lv.get(code) or 0.0) for _s, _e, lv, _a in months), 2)  # SA-OK: in-memory levels
    return out


def rollup_band_to_annual(
    band_periods: list[tuple[datetime.date, datetime.date, dict, dict]],
    fy_month: int,
) -> list[tuple[int, datetime.date, datetime.date, dict, int]]:
    """Collapse the tied band months into per-fiscal-year annual-actual columns (D-CA-2). Groups by the
    fiscal year CONTAINING each month, then per bucket applies :func:`_rollup_levels`. Returns
    [(fy_year, bucket_start, bucket_end, levels, month_count)] oldest first — ``month_count`` is the
    count of PRESENT months, so a partial year is labeled honestly ('2024 (actual, 5mo)'). A rollup
    that does not tie is dropped (never shown unbalanced). A bucket containing a CF-REFUSED month
    (a chain break — its ``cf_cfo`` is absent) refuses the ANNUAL CF lines too: summing flows across
    a gap would fabricate an annual CFO that silently omits the refused month (AC-FRR-1 continuity
    at BOTH grains).
    """
    buckets: dict[int, list[tuple[datetime.date, datetime.date, dict, dict]]] = {}
    for start, end, levels, accounts in band_periods:
        buckets.setdefault(_fiscal_year_end_year(end, fy_month), []).append((start, end, levels, accounts))

    out: list[tuple[int, datetime.date, datetime.date, dict, int]] = []
    for fy_year in sorted(buckets):
        months = sorted(buckets[fy_year], key=lambda m: m[1])  # by period_end, oldest first
        rolled = _rollup_levels(months)
        bc = float(rolled.get("balance_check") or 0.0)  # SA-OK: dict.get on the in-memory rollup levels
        if abs(bc) > _TIE_TOL:
            logger.info("[LBO BAND] annual rollup %d balance_check=%.2f — dropped", fy_year, bc)
            continue
        if any("cf_cfo" not in lv for _s, _e, lv, _a in months):
            # A CF-refused month (chain break) poisons the bucket's flow sums — refuse the annual CF
            # lines (absent), never an annual CFO summed across a gap. P&L sums + the BS snapshot
            # stand: they are per-month/as-of data, independent of the broken prior chain.
            rolled = {k: v for k, v in rolled.items() if k not in _CF_DELTA_CODES}
        elif any("cf_equity" not in lv for _s, _e, lv, _a in months):
            # AC-FRR-9a at the annual grain: a month whose cf_equity was refused (an inline-gate
            # suppression, or a first month with no prior equity level) makes the year's equity
            # flow UNKNOWN — summing the remaining months would fabricate an annual flow that
            # silently omits it. The annual cf_equity alone is refused (absent); the other CF
            # lines stand (each month's cf_cff was computed without the suppressed flow).
            rolled.pop("cf_equity", None)
        out.append((fy_year, months[0][0], months[-1][1], rolled, len(months)))
    return out


def build_band_spine(
    band_periods: list[tuple[datetime.date, datetime.date, dict, dict]],
    rollups: list[tuple[int, datetime.date, datetime.date, dict, int]],
    bs_accounts_by_end: dict[str, dict[str, float]] | None = None,
) -> tuple[list[tuple[int, str, str, datetime.date, datetime.date]], dict[int, dict], dict[int, dict]]:
    """Assign the off-positional seqs + build the spine rows + the per-seq cell maps for persistence.

    Returns ``(spine_rows, levels_by_seq, accounts_by_seq)``:
      * ``spine_rows`` — ``[(seq, kind, label, period_start, period_end)]`` for ``build_period_spine`` to
        author LboPeriod rows: monthly at ``BAND_COL_BASE + i`` (kind ``post_close_actual``, label
        "Mon YYYY"); annual rollups at ``BAND_FY_ROLLUP_BASE + j`` (kind ``post_close_actual_fy``, label
        "YYYY (actual, Nmo)").
      * ``levels_by_seq`` / ``accounts_by_seq`` — keyed by the SAME seqs, for ``persist_historical_cells``
        to stamp cells at ``col_index == seq``. Monthly columns carry per-account children (the
        merged per-window accounts, unchanged); annual columns carry the FY-END per-account BS
        snapshot from ``bs_accounts_by_end`` (live-verify remediation 2026-07-21 — stock semantics
        mirroring ``_STOCK_CODES``: a fetched annual window snapshots its own as-of sheet, a band
        rollup its LAST kept month's; an account absent at FY-end → absent, never 0.0). The
        children are DISPLAY COMPOSITION only — the parent keeps the aggregated line_code levels,
        and no tie/rollup/CF/returns math reads a child cell (they carry ``line_code=None``).
        With no capture map (a legacy caller) the annual columns stay template-only (``{}``).
    """
    spine_rows: list[tuple[int, str, str, datetime.date, datetime.date]] = []
    levels_by_seq: dict[int, dict] = {}
    accounts_by_seq: dict[int, dict] = {}
    for i, (start, end, levels, accounts) in enumerate(band_periods):
        seq = BAND_COL_BASE + i
        spine_rows.append((seq, BAND_KIND, end.strftime("%b %Y"), start, end))
        levels_by_seq[seq] = levels
        accounts_by_seq[seq] = accounts
    for j, (fy_year, start, end, levels, month_count) in enumerate(rollups):
        seq = BAND_FY_ROLLUP_BASE + j
        label = f"{fy_year} (actual, {month_count}mo)"
        spine_rows.append((seq, BAND_FY_KIND, label, start, end))
        levels_by_seq[seq] = levels
        # The FY-END child snapshot (copied — persistence must never alias the capture map).
        # ``end`` is the fetched annual window's as-of date, or the rollup's last kept month.
        accounts_by_seq[seq] = dict((bs_accounts_by_end or {}).get(end.isoformat()) or {})  # SA-OK: dict.get on the in-memory capture map
    return spine_rows, levels_by_seq, accounts_by_seq


def build_projection_seed(
    rollups: list[tuple[int, datetime.date, datetime.date, dict, int]],
) -> dict | None:
    """The Wave-3 forecast RE-SEED source (plan 20260719, D-W3-2, Owen 2026-07-20: "last full fiscal
    year"): the LAST COMPLETE post-close fiscal year of actuals.

    Returns its rollup ``levels`` — which, from :func:`_rollup_levels`, carry BOTH the ANNUAL P&L run-rate
    (flows summed over the 12 months) AND the FY-END balance sheet (stock codes snapshot the last month).
    Because both come from the SAME rollup, the projection's WC levels (AR = annual-rev × DSO/365) match
    the opening BS — no year-0 ΔNWC mismatch — and A=L+E ties by construction (the rollup BS already ties).

    Returns ``None`` when the band has NO complete post-close fiscal year (a recent close): the forecast then
    DEGRADES to the At-Close entry basis (Wave-2 behavior, byte-identical). ``rollups`` is
    :func:`rollup_band_to_annual`'s output ``[(fy_year, start, end, levels, month_count)]``; a complete FY is
    ``month_count == 12``. The base is the LAST (most recent) complete FY.
    """
    complete = [r for r in rollups if r[4] == 12]  # month_count == 12 → a full fiscal year of actuals
    if not complete:
        return None
    fy_year, _start, fy_end, levels, _month_count = complete[-1]
    return {
        "base_fy_year": fy_year,   # the label year of the run-rate FY
        "base_fy_end": fy_end,     # the FY-end date the projection opens at
        "levels": levels,          # the annual P&L run-rate + the FY-end BS snapshot (consistent, tied)
    }


def _months_between(start: datetime.date, end: datetime.date) -> int:
    """Inclusive month count of a first-of-month → month-end window (9 for Apr–Dec)."""
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _merge_fy_rollups(
    fetched_fy: list[tuple[int, datetime.date, datetime.date, dict, int]],
    band_rollups: list[tuple[int, datetime.date, datetime.date, dict, int]],
) -> list[tuple[int, datetime.date, datetime.date, dict, int]]:
    """Dedupe-by-fy_year (AC-FRR-1): NEVER both an annual-FETCH column and a band rollup for the same
    fiscal year — a full-FY fetch beats a partial band rollup (the split-FY double-cover guard; the
    FY-aligned planner makes a collision structurally impossible, this seam keeps it impossible even
    if a caller hands overlapping inputs). Chronological (fy_year) order.
    """
    fetched_years = {r[0] for r in fetched_fy}
    merged = list(fetched_fy) + [r for r in band_rollups if r[0] not in fetched_years]
    return sorted(merged, key=lambda r: (r[0], r[1]))


def fetch_post_close_history(
    client,
    entry_end: datetime.date,
    last_closed_end: datetime.date,
    fy_month: int,
    mappings: dict[str, str],
    parse_entries,
    initial_prior_levels: dict | None = None,
) -> dict:
    """The FULL post-close history fetch (AC-FRR-1) — the ONE orchestration the init band block calls.

    Plans the FY-aligned split (:func:`plan_band_windows`), runs the ANNUAL old-FY windows then the
    MONTHLY windows through the SAME gate set (:func:`fetch_post_close_actuals`) with CHAINED priors —
    opening(as-of-close) → annual columns → monthly columns (the first monthly prior is the LAST
    annual FY's computed levels) — and merges the fetched annual columns with the monthly band's own
    rollups, deduped by fy_year. A dropped FINAL annual FY breaks the boundary chain: the first
    monthly column's CF lines are refused, never rolled off the second-to-last FY.

    Returns ``{"monthly": [(start, end, computed, accounts)],
    "rollups": [(fy_year, start, end, levels, month_count)]  # fetched-annual + band, chronological,
    "dropped": [{start, end, grain, reason}],  # the band_continuity record
    "pl_report_ni": {"<period_end ISO>": float},  # QBO's OWN P&L NI total per kept window (both
    grains) — the meta ``band_pl_report_ni`` record the NI-reconciliation gate reads (AC-FRR-3)
    "bs_report_te": {"<period_end ISO>": float},  # QBO's OWN BS "Total Equity" per kept window —
    the meta ``band_bs_report_te`` record the equity-reconciliation gate reads (Wave-5 AC-2)
    "bs_accounts": {"<period_end ISO>": {qbo_account_id: balance}},  # the BS-ONLY per-account
    substrate per kept window, BOTH grains (live-verify remediation 2026-07-21) — the annual
    columns' child cells snapshot from it in build_band_spine; display composition only
    "articulation": [{period_end, grain, check, ...}]  # AC-FRR-9/10: the per-column CF-articulation
    refusal/disclosure records (inline NI/TE gate suppressions, ending-cash tie residuals, the
    impairment shape) — the meta ``band_articulation`` record, beside ``band_continuity``}``.
    """
    annual_windows, monthly_windows = plan_band_windows(entry_end, last_closed_end, fy_month)
    dropped: list[dict] = []
    pl_report_ni: dict[str, float] = {}
    bs_report_te: dict[str, float] = {}
    articulation: list[dict] = []
    bs_accounts: dict[str, dict[str, float]] = {}
    annual_cols = fetch_post_close_actuals(
        client, annual_windows, mappings, parse_entries,
        initial_prior_levels=initial_prior_levels, grain="annual", dropped=dropped,
        pl_report_ni=pl_report_ni, bs_report_te=bs_report_te, articulation=articulation,
        bs_accounts=bs_accounts,
    )
    if annual_windows:
        # The monthly chain is intact ONLY when the final annual window (the monthly band's adjacent
        # prior) survived its gates; earlier annual drops broke + re-established the chain upstream.
        tail_ok = bool(annual_cols) and annual_cols[-1][1] == annual_windows[-1][1]
        monthly_prior = annual_cols[-1][2] if tail_ok else None
        monthly_broken = not tail_ok
    else:
        monthly_prior, monthly_broken = initial_prior_levels, False
    monthly_cols = fetch_post_close_actuals(
        client, monthly_windows, mappings, parse_entries,
        initial_prior_levels=monthly_prior, grain="monthly", dropped=dropped,
        chain_broken=monthly_broken, pl_report_ni=pl_report_ni, bs_report_te=bs_report_te,
        articulation=articulation, bs_accounts=bs_accounts,
    )
    fetched_fy = [
        (_fiscal_year_end_year(end, fy_month), start, end, levels, _months_between(start, end))
        for start, end, levels, _accounts in annual_cols
    ]
    rollups = _merge_fy_rollups(fetched_fy, rollup_band_to_annual(monthly_cols, fy_month))
    return {"monthly": monthly_cols, "rollups": rollups, "dropped": dropped,
            "pl_report_ni": pl_report_ni, "bs_report_te": bs_report_te,
            "bs_accounts": bs_accounts, "articulation": articulation}


def build_t0_anchor(
    prior_levels: dict | None,
    as_of: datetime.date | None,
    source: str,
) -> dict | None:
    """The persisted t₀ equity anchor (AC-FRR-1 / SYNTHESIS §1 blind spot 3): the band's INITIAL
    prior — the true as-of-close sheet — reduced to ``{as_of, total_equity, source}`` for the meta
    cell's ``driver_params``. On the degraded-entry path seq-0 holds the LAST-CLOSED sheet
    (chronologically AFTER the first band month), so without this anchor the realized-returns track
    has no t₀ level to chain from and must refuse. An absent prior, or a $0/BS-less prior
    (``total_assets`` ≈ 0 — the refused-opening shape), yields ``None``: no anchor is ever
    fabricated from a missing sheet.
    """
    if not prior_levels or as_of is None:
        return None
    from .standard_grid_builder import compute_statement_subtotals

    computed = compute_statement_subtotals(prior_levels)
    if abs(float(computed.get("total_assets") or 0.0)) < _TIE_TOL:  # SA-OK: dict.get on the in-memory computed levels
        return None  # a $0/absent sheet is never an anchor (refuse-over-fabricate)
    return {
        "as_of": as_of.isoformat(),
        "total_equity": round(float(computed.get("total_equity") or 0.0), 2),  # SA-OK: dict.get on the in-memory computed levels
        "source": source,
    }

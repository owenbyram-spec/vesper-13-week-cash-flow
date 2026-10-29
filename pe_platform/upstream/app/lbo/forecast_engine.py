"""
app/lbo/forecast_engine.py
───────────────────────────
Core 3-statement forecast engine.
Generates Pro Forma Income Statement, Balance Sheet, and Cash Flow
using driver assumptions, payroll, capex, working capital, and debt schedules.
"""
from typing import Dict, Any, List
from .capex_depreciation import compute_capex_schedule, compute_intangible_schedule
from .engine_support import (
    assert_bs_check,
    build_sweep_terms,
    normalize_historical_periods,
    normalize_opening_wc,
    parse_ebitda_adjustments,
    reanchor_capex_drivers_tangible_only,
    resolve_goodwill_lives,
    resolve_is_line_vector,
    seed_from_periods,
    static_carry_lines,
)
from .working_capital import compute_working_capital
from .sources_uses import compute_sources_uses, oid_amortization_vectors
from .waterfall_solve import OperatingYear, solve_waterfall


# ─── Revenue Streams ────────────────────────────────────────────────────────
REVENUE_STREAMS = [
    "service", "pt", "surgical", "pain_mgmt", "telehealth",
    "mgmt_fees", "rental_income", "refunds", "other",
]

# ─── COGS Departments (payroll-driven) ──────────────────────────────────────
COGS_DEPARTMENTS = ["surgical", "pt", "pain_mgmt", "telehealth"]

# ─── OpEx Departments ──────────────────────────────────────────────────────
OPEX_DEPARTMENTS = ["ga", "sales_marketing"]

# ─── Distribution policy (AC-A2 / FIND-MONEY-002) ────────────────────────────
# Fraction of each year's POST-debt-service free cash paid out to equity as an
# interim distribution. Default 0.0 → no interim distributions, so the equity
# cash-flow vector stays single-flow and the IRR equals MOIC^(1/hold)-1 (i.e.
# byte-identical to the pre-distribution model). A non-zero driver makes the IRR
# a TRUE dated-flow IRR that legitimately diverges from the annualized-MOIC
# shortcut. Override per scenario via drivers["distribution_pct_of_fcf"].
# Source: work/money-correctness/plans/20260614-irr-interim-distributions/plan.md AC-1.
DISTRIBUTION_PCT_OF_FCF = 0.0


def compute_full_forecast(
    drivers: Dict[str, Any],
    payroll_drivers: List[Dict[str, Any]],
    capex_drivers: List[Dict[str, Any]],
    debt_tranches: List[Dict[str, Any]],
    num_years: int,
    ltm_ebitda: float = 0,
    historical_bs: Dict[str, float] | None = None,
    base_revenue: float = 0,
    historical_periods: list[dict[str, object]] | None = None,
    forecast_opening_bs: dict[str, float] | None = None,  # W3c (plan 20260719): FY-real projection opening BS; None => byte-parity
    stub_fraction: float | None = None,  # W3d: partial-first-year fraction f (seq-1 stub); None/1.0 => byte-parity
) -> Dict[str, Any]:
    """
    Compute a multi-year 3-statement LBO forecast.

    Args:
        drivers: Main driver data (revenue growth, margins, WC ratios, etc.)
        payroll_drivers: Payroll driver rows per department/year
        capex_drivers: Capex driver rows per FA category
        debt_tranches: Debt tranche definitions
        num_years: Number of projection years
        ltm_ebitda: LTM EBITDA for S&U calculation
        historical_bs: Opening balance sheet values
        base_revenue: Starting revenue (year 0 / LTM)
        historical_periods: OPTIONAL historical-period feed (C2-b, plan
            20260717-three-statement-spine AC-5) — the LboPeriod-shaped vector
            C2-a produces: pre-deal statement columns ({kind ∈ {historical_fy,
            ltm}, label, seq, pnl, bs, provenance, ...}). Each column is
            validated + A = L + E asserted (never bypassed) and echoed under
            ``historical_periods`` in the result; the LAST period (the LTM)
            fills any ABSENT forecast seed (base_revenue / ltm_ebitda /
            historical_bs) — an explicitly-passed seed always wins. Callers
            that pass none keep the pre-existing contract byte-identically.

    Returns:
        Complete forecast with years, pnl, bs, cf, debt_schedule, capex_schedule, wc_schedule
    """

    # ── 0. Historical-period feed (DEC-LBO3S-1/-3) ───────────────────────
    # Normalize + assert the fed pre-deal columns, then fill absent forecast
    # seeds from the LAST period (the LTM by contract). Explicit seeds win.
    hist_periods = normalize_historical_periods(historical_periods)
    if hist_periods:
        period_seed = seed_from_periods(hist_periods)
        if not base_revenue:
            base_revenue = float(period_seed["base_revenue"])  # type: ignore[arg-type]
        if not ltm_ebitda:
            ltm_ebitda = float(period_seed["ltm_ebitda"])  # type: ignore[arg-type]
        if historical_bs is None:
            historical_bs = period_seed["historical_bs"]  # type: ignore[assignment]

    # ── 1. Sources & Uses ────────────────────────────────────────────────
    su = compute_sources_uses(drivers, debt_tranches, ltm_ebitda, historical_bs)
    entry_equity = su["entry_equity"]

    # ── W3c (plan 20260719): the two-seam split. The VALUATION seam above stays on historical_bs (seq-0):
    # su + entry_equity (the sole returns denominator, AP-FIN-03/-04) are byte-identical across ANY re-seed.
    # Everything DOWNSTREAM reads `fbs` — the FY-real projection opening BS when re-seeding, else
    # historical_bs with its cash line replaced by the S&U pro-forma opening cash (the deal basis
    # resolved once in compute_sources_uses: a cash-free deal never opens on the seller's cash).
    if forecast_opening_bs is not None:
        fbs = forecast_opening_bs
    else:
        fbs = {**(historical_bs or {}), "cash": float(su["pf_opening_bs_asc805"]["cash"])}
    # The projected-BS equity line is DECOUPLED from entry_equity: on the re-seed it opens at the FY's real
    # contributed capital (adapter: common_equity); on the None path it IS entry_equity (byte-parity).
    equity_contributed_proj = float((forecast_opening_bs or {}).get("equity_contributed", entry_equity))

    # W3c/CORR-3 (rebase moved HERE from run_scenario_engine so EVERY path — /returns AND the sensitivity grid,
    # including per-cell scaled tranches — rebases uniformly; the duplication that skipped the grid caused a
    # broken-tie 500). In the re-seed regime the projection debt opens at the FY-real total_debt (fbs), NOT the
    # entry Σfunded_amount (which diverges once debt amortized/swept during the post-close band). Add
    # opening_balance so Σ == fbs.total_debt EXACTLY (proportional to funded, cent residual to the largest);
    # funded_amount is UNTOUCHED (the S&U/entry_equity firewall). None re-seed → no opening_balance → the debt
    # schedule opens at funded_amount (byte-parity). No funded tranches (Σfunded==0) → leave as-is (a re-seed of
    # an all-equity deal whose target carries real debt is a documented edge — the schedule has no tranche to
    # hold it; the projection degrades rather than fabricating one).
    #
    # AC-1/AC-3 (plan 20260720 live-verify round 3): SEASONED payloads (flagged by
    # engine_composition._apply_seasoned_terms — dated loans whose start pre-dates the
    # forecast anchor) NEVER re-base: each opens at its books-stated current_balance
    # (opening_balance already set upstream). Only the UNDATED (at-close S&U) tranches
    # re-base pro-rata, to the books target NET of the seasoned openings — with no
    # seasoned tranche the arithmetic below is byte-identical (AC-2 parity). Any residual
    # between the books total and what the tranche schedule opens at is a DISCLOSURE
    # (``seasoned_books_residual``, appended beside the #446 disclosures after the solve)
    # carried FLAT in the schedule totals — never a rescale of a seasoned opening, never
    # silently absorbed, and never an invented paydown/interest on terms nobody stated.
    books_debt_residual = 0.0
    if forecast_opening_bs is not None:
        _seasoned = [t for t in debt_tranches if t.get("seasoned")]
        _undated = [t for t in debt_tranches if not t.get("seasoned")]
        _target = float((fbs or {}).get("total_debt", 0) or 0)
        _seasoned_sum = round(sum(float(t.get("opening_balance", 0) or 0) for t in _seasoned), 2)
        _undated_target = round(max(_target - _seasoned_sum, 0.0), 2) if _seasoned else _target
        _funded = [float(t.get("funded_amount", 0) or 0) for t in _undated]
        _fsum = sum(_funded)
        _undated_assigned = 0.0
        if _fsum > 0:
            _reb = [round(f * _undated_target / _fsum, 2) for f in _funded]
            _j = max(range(len(_funded)), key=lambda i: _funded[i])  # cent residual -> the largest tranche
            _reb[_j] = round(_reb[_j] + round(_undated_target - sum(_reb), 2), 2)
            for _t, _ob in zip(_undated, _reb):
                _t["opening_balance"] = _ob
            _undated_assigned = round(sum(_reb), 2)
        if _seasoned:
            books_debt_residual = round(_target - _seasoned_sum - _undated_assigned, 2)

    # ── 2. Revenue Forecast ──────────────────────────────────────────────
    # Y0 GROWTH CONVENTION (AC-C4 leg 5 — ONE convention on BOTH paths): every
    # base value ({stream}_base and base_revenue) is the LTM / pre-deal actual
    # ("year −1"), and EVERY projected year — including year 0 — applies its
    # growth rate: year 0 = base × (1 + g₀). This was already the aggregate
    # path's arithmetic (and the grid-composition seed contract:
    # engine_composition derives growth chained off the grid's Y0-actuals
    # column); the per-stream path previously carried the base flat into year 0
    # — the divergence this fixes. Metamorphic gate: a single-stream deal ≡ the
    # same deal through the aggregate path (tests/lbo/test_engine_properties.py).
    rev_growth = drivers.get("revenue_growth_by_stream", {})
    rev_margin = drivers.get("margin_by_stream", {})
    tax_rate = float(drivers.get("tax_rate", 0.21))

    # Build revenue per year
    active_revenue_streams = drivers.get("revenue_streams") or REVENUE_STREAMS
    active_cogs_depts = [d.replace(" ", "_").replace("&", "") for d in (drivers.get("cogs_departments") or COGS_DEPARTMENTS)]
    active_opex_depts = [d.replace(" ", "_").replace("&", "") for d in (drivers.get("opex_departments") or OPEX_DEPARTMENTS)]

    revenue_by_year = []
    revenue_detail = []
    current_rev = base_revenue  # noqa: F841

    for yr in range(num_years):
        yr_rev = {}
        total_rev = 0.0
        for stream in active_revenue_streams:
            growth_rates = rev_growth.get(stream, [])  # SA-OK: bounded-loop 9 streams — in-memory driver dict, not a DB query
            rate = growth_rates[yr] if yr < len(growth_rates) else (growth_rates[-1] if growth_rates else 0)
            rate = float(rate)

            if yr == 0:
                # {stream}_base is the LTM actual — year 0 applies its growth
                # (the one Y0 convention, identical to the aggregate path below).
                base_split = rev_margin.get(f"{stream}_base", 0)  # SA-OK: bounded-loop 9 streams — in-memory driver dict, not a DB query
                stream_rev = float(base_split) * (1 + rate) if base_split else 0
            else:
                prev = revenue_detail[yr - 1].get(stream, 0)  # SA-OK: bounded-loop 9 streams — in-memory detail dict, not a DB query
                stream_rev = prev * (1 + rate)

            yr_rev[stream] = round(stream_rev, 2)
            total_rev += stream_rev

        # If yr 0 and no stream breakdown, use base_revenue with overall growth
        if yr == 0 and total_rev == 0 and base_revenue > 0:
            overall_growth = float(rev_growth.get("overall", [0])[0] if rev_growth.get("overall") else 0)
            total_rev = base_revenue * (1 + overall_growth)
            yr_rev["total_override"] = True
        elif yr == 0 and total_rev == 0:
            # Fallback: apply overall growth to base
            overall_rates = rev_growth.get("overall", [0.05])
            rate = float(overall_rates[0]) if overall_rates else 0.05
            total_rev = base_revenue * (1 + rate)

        if total_rev == 0 and yr > 0:
            # Fallback: grow prior year by overall rate
            overall_rates = rev_growth.get("overall", [0.05])
            rate = float(overall_rates[min(yr, len(overall_rates) - 1)]) if overall_rates else 0.05
            total_rev = revenue_by_year[yr - 1] * (1 + rate)

        revenue_by_year.append(round(total_rev, 2))
        # AC-CA-11 (plan 20260719 §Amendment) — revenue_detail vs revenue_by_year under a stub:
        # when _has_stub (the W3d block below), revenue_by_year[0] is REPLACED by the prorated
        # AGGREGATE (level_revenue_by_year[0] × f), but revenue_detail[0] keeps the UN-prorated
        # per-stream values built here (the stub rides the aggregate base_revenue path, D-W3-2).
        # A consumer must therefore NOT sum revenue_detail[0] to reconcile revenue_by_year[0] in
        # a stub year — the detail is an annualized per-stream view there, not the recognized
        # flow. Per-stream stub proration is the recorded follow-on (status.json blockers[],
        # W3d scope note); today revenue_detail is loop-internal chaining state only.
        revenue_detail.append(yr_rev)

    # ── W3d (plan 20260719, AC-CA-4): stub proration of the PARTIAL first forward period ────────────────
    # When the forecast starts mid-fiscal-year (a post-close band ends mid-FY), seq-1 is a STUB of length
    # f = (FY_end − last_actual_end)/365 (ratified: seq-1 = the partial split-FY stub). period_fraction is f
    # for year-0 and 1.0 thereafter. The run-rate grows GEOMETRICALLY over the partial period — the annualized
    # level at stub-end is base×(1+g)^f — and the RECOGNIZED P&L flow is that × f. Every following FULL year
    # chains off the ANNUALIZED level, NOT the f-scaled recognized flow (chaining off the recognized flow would
    # f-scale the ENTIRE projection). WC/capex/debt LEVELS use the annualized vector; the P&L/CF use the
    # recognized flow. f==1.0 (no stub) BRANCHES to the exact current arithmetic (no pow/×1.0 IEEE drift).
    _f = float(stub_fraction) if (stub_fraction is not None and 0.0 < float(stub_fraction) < 1.0) else 1.0
    _has_stub = _f < 1.0
    period_fraction = [_f] + [1.0] * (num_years - 1)
    if _has_stub:
        # Aggregate run-rate path (the re-seed base_revenue; D-W3-2). Grow base_revenue over the partial
        # period, then chain the annualized level through the following full years.
        _overall = rev_growth.get("overall") or [0.0]
        _g0 = float(_overall[0]) if _overall else 0.0
        level_revenue_by_year = [round(base_revenue * (1 + _g0) ** _f, 2)]
        for _yr in range(1, num_years):
            _g = float(_overall[min(_yr, len(_overall) - 1)]) if _overall else 0.0
            level_revenue_by_year.append(round(level_revenue_by_year[-1] * (1 + _g), 2))
        revenue_by_year = [round(level_revenue_by_year[_yr] * period_fraction[_yr], 2) for _yr in range(num_years)]
    else:
        level_revenue_by_year = list(revenue_by_year)  # no stub → the annualized level IS the flow (byte-parity)

    # ── 3. Payroll / COGS Forecast ───────────────────────────────────────
    payroll_by_year = []  # [{dept: {salaries, taxes, benefits, total}}]
    cogs_by_year = []
    opex_by_year = []

    for yr in range(num_years):
        yr_payroll = {}
        total_cogs_payroll = 0.0
        total_opex_payroll = 0.0

        for p in payroll_drivers:
            if int(p.get("year", -1)) != yr:
                continue
            dept = p.get("department", "").lower().replace(" ", "_").replace("&", "")
            ftes = float(p.get("ftes", 0))
            salary = float(p.get("avg_salary", 0))
            tax_pct = float(p.get("payroll_tax_pct", 0))
            ben_pct = float(p.get("benefits_pct", 0))
            merit = float(p.get("merit_increase_pct", 0))

            # Apply merit increase for years after year 0
            eff_salary = salary * ((1 + merit) ** yr) if yr > 0 else salary

            salaries = round(ftes * eff_salary, 2)
            taxes = round(salaries * tax_pct, 2)
            benefits = round(salaries * ben_pct, 2)
            total = round(salaries + taxes + benefits, 2)

            yr_payroll[dept] = {
                "ftes": ftes,
                "salaries": salaries,
                "taxes": taxes,
                "benefits": benefits,
                "total": total,
            }

            if dept in active_cogs_depts:
                total_cogs_payroll += total
            elif dept in active_opex_depts:
                total_opex_payroll += total
            else:
                total_opex_payroll += total  # Default to opex

        payroll_by_year.append(yr_payroll)

        # W3d: the stub year's payroll is a PARTIAL-period flow — ×f, so the RECOGNIZED cogs/opex == f × the
        # annualized level even when payroll is present (e.g. the sensitivity grid threads real payroll). This
        # keeps level_cogs/opex = recognized/period_fraction valid (else the fixed payroll term is over-divided
        # by f). payroll_drivers=[] (the /returns LBO path) → these are 0 → no-op; non-stub → ×1.0 → byte-parity.
        _pf_yr = period_fraction[yr] if yr < len(period_fraction) else 1.0
        total_cogs_payroll = round(total_cogs_payroll * _pf_yr, 2)
        total_opex_payroll = round(total_opex_payroll * _pf_yr, 2)

        # Non-payroll COGS (direct costs % of revenue)
        direct_cost_pct = float(drivers.get("direct_cost_pct", 0.05))
        non_payroll_cogs = round(revenue_by_year[yr] * direct_cost_pct, 2)
        total_cogs = round(total_cogs_payroll + non_payroll_cogs, 2)
        cogs_by_year.append(total_cogs)

        # Non-payroll OpEx
        facilities_pct = float(drivers.get("facilities_pct", 0.03))
        other_ga_pct = float(drivers.get("other_ga_pct", 0.02))
        facilities = round(revenue_by_year[yr] * facilities_pct, 2)
        other_ga = round(revenue_by_year[yr] * other_ga_pct, 2)
        total_opex = round(total_opex_payroll + facilities + other_ga, 2)
        opex_by_year.append(total_opex)

    # ── 4. Capex & D&A ──────────────────────────────────────────────────
    # Goodwill is the opening-BS balancing item derived in compute_sources_uses (so
    # A = L + E at entry), NOT a free driver input. AC-C4 leg 6: the engine's
    # projected BS now articulates from the ASC-805 block (``pf_opening_bs_asc805``
    # — transaction costs EXPENSED into opening retained earnings, financing fees a
    # CONTRA-DEBT OID), so the goodwill feed is the ex-fees ASC-805 plug — the same
    # opening position the grid's PF-Y0 bridge and the S&U tab present. The legacy
    # fees-capitalized block (``pf_opening_bs``) is NO LONGER the engine feed; it
    # remains in the S&U result for its other consumers (sources_uses.py docstring).
    # BOOK-vs-TAX lives are resolved explicitly (ASC 350-20 impairment-only /
    # ≤10y election vs the IRC §197 15y tax proxy — RD-1 row 17 split fix).
    su_asc805 = su["pf_opening_bs_asc805"]
    # W3c: the re-seed opens the SEPARATE goodwill line at the adapter's goodwill (0.0 — the deal goodwill
    # rides folded inside `intangibles`), avoiding a double-count; the None path keeps su_asc805 (byte-parity;
    # a QBO BS has no 'goodwill' key so fbs.get would fall through anyway, but the guard is explicit).
    goodwill_amount = float(fbs.get("goodwill", su_asc805["goodwill"])) if forecast_opening_bs is not None else float(su_asc805["goodwill"])
    gw_book_years, gw_tax_years = resolve_goodwill_lives(drivers)

    # Intangible amortization (C2-b AC-5, delta-ledger DL-5): straight-line the
    # OPENING intangibles over drivers["intangible_amort_years"] (default 0 =
    # OFF → flat static carry, byte-parity for every stored scenario). DL-5
    # double-count guard: when the caller marks the capex payload as anchored to
    # the FULL Y0 D&A run-rate (which historically INCLUDES intangible
    # amortization), the vintage anchor is re-anchored to the TANGIBLE-only
    # run-rate — otherwise the intangible slice would be expensed twice (once in
    # PP&E depreciation, once on the new labeled line).
    hbs = fbs or {}  # W3c: auto-reroutes intangibles/prev_cash/other_assets to the FY-real opening (or historical_bs when None)
    intangible_amort_years = int(drivers.get("intangible_amort_years", 0) or 0)
    opening_intangibles = round(float(hbs.get("intangibles", 0) or 0), 2)
    intangible_result = compute_intangible_schedule(
        opening_intangibles, intangible_amort_years, num_years,
        period_fraction=period_fraction,  # W3d: ×f the stub year's intangible amort
    )
    intangible_schedule = intangible_result["schedule"]
    intangible_annual = float(intangible_result["annual_amortization"])
    intangible_active = intangible_annual > 0
    effective_capex_drivers = capex_drivers
    reanchored_annual_removed = 0.0
    if intangible_active and bool(drivers.get("capex_anchor_includes_intangible_amort", False)):
        effective_capex_drivers, reanchored_annual_removed = (
            reanchor_capex_drivers_tangible_only(capex_drivers, intangible_annual)
        )

    # AC-FRR-11b (plan 20260720 §Amendment) — static_carry_lines is HOISTED here,
    # ahead of the capex schedule and therefore ahead of the waterfall solve (it
    # previously computed AFTER the solve, §9 below). Both inputs (fbs +
    # effective_capex_drivers) are final at this point, so the hoist is
    # value-identical; it exists so the vintage NPP&E floor can thread the SIGNED
    # unmodeled_fixed_assets offset in, and the curtailed depreciation then flows
    # through total_da → EBIT → the solver's NI/taxes/sweep honestly. The DISPLAYED
    # net PP&E (total_net_fa + the offset, §9) floors at 0 by construction.
    static_lines = static_carry_lines(fbs, effective_capex_drivers)  # W3c: carried lines off the FY-real BS (or historical_bs when None)

    capex_result = compute_capex_schedule(
        effective_capex_drivers, revenue_by_year, num_years, goodwill_amount,
        goodwill_book_amort_years=gw_book_years,
        goodwill_tax_amort_years=gw_tax_years,
        period_fraction=period_fraction,  # W3d: ×f the stub year's depreciation + goodwill amort
        unmodeled_net_fa_offset=static_lines["unmodeled_fixed_assets"],
    )

    # ── 5. Working Capital ──────────────────────────────────────────────
    # Opening balances normalized to the WC keys (long-form or aliased input) so
    # year-0 ΔNWC is measured against the REAL opening sheet, not zero — part of
    # the bs_check articulation fix (RD-1 row 9).
    # W3d: the annualized WC-level vectors (recognized flow ÷ period_fraction). The LBO/re-seed path passes
    # payroll_drivers=[], so cogs/opex are purely revenue-linear → dividing the recognized flow by f recovers
    # the annualized run-rate exactly. No stub → period_fraction is all 1.0 → level == flow (byte-parity).
    level_cogs_by_year = [round(cogs_by_year[yr] / period_fraction[yr], 2) for yr in range(num_years)]
    level_opex_by_year = [round(opex_by_year[yr] / period_fraction[yr], 2) for yr in range(num_years)]
    wc_result = compute_working_capital(
        drivers, revenue_by_year, cogs_by_year, opex_by_year,
        opening_wc=normalize_opening_wc(fbs),  # W3c: opening WC off the FY-real BS (or historical_bs when None)
        level_revenue_by_year=level_revenue_by_year,
        level_cogs_by_year=level_cogs_by_year,
        level_opex_by_year=level_opex_by_year,
    )

    # ── 6. Build PnL per year ────────────────────────────────────────────
    # Add-backs parsed ONCE with per-item provenance (RD-1 row 16): legacy bare
    # lists still work; the structured {"values", "provenance"} shape carries the
    # quality-of-earnings evidence per add-back.
    adj_values_by_name, adj_detail_by_name = parse_ebitda_adjustments(drivers, num_years)

    # Other income / other expense (C2-b AC-5, delta-ledger DL-2): NON-OPERATING
    # lines — they never touch EBITDA/EBIT (the operating story) but DO enter
    # EBT and the cash-tax base (the market convention: non-operating income is
    # taxable), threaded to the solver below. Scalar or per-year list; a short
    # list HOLDS ITS LAST VALUE constant (DEC-LBO3S-3: fades are manual
    # overrides only). Default absent → zeros → byte-parity.
    other_income_by_year = resolve_is_line_vector(
        drivers.get("other_income_by_year", drivers.get("other_income")), num_years
    )
    other_expense_by_year = resolve_is_line_vector(
        drivers.get("other_expense_by_year", drivers.get("other_expense")), num_years
    )
    net_other_income_by_year = [
        round(other_income_by_year[yr] - other_expense_by_year[yr], 2)
        for yr in range(num_years)
    ]

    pnl_years = []
    for yr in range(num_years):
        rev = revenue_by_year[yr]
        cogs = cogs_by_year[yr]
        gross_profit = round(rev - cogs, 2)
        opex = opex_by_year[yr]

        capex_yr = capex_result["capex_schedule"][yr] if yr < len(capex_result["capex_schedule"]) else {}
        depr = float(capex_yr.get("total_depreciation", 0))
        gw_amort = float(capex_yr.get("goodwill_amortization", 0))
        int_amort = (
            float(intangible_schedule[yr]["amortization"])  # type: ignore[index]
            if yr < len(intangible_schedule)  # type: ignore[arg-type]
            else 0.0
        )
        total_da = round(depr + gw_amort + int_amort, 2)

        ebitda = round(gross_profit - opex, 2)

        # EBITDA adjustments (values parsed above; provenance in adj_detail_by_name)
        adj_items = {}
        total_adj = 0.0
        for adj_name, adj_values in adj_values_by_name.items():
            val = adj_values[yr] if yr < len(adj_values) else 0.0
            adj_items[adj_name] = round(val, 2)
            total_adj += val

        adjusted_ebitda = round(ebitda + total_adj, 2)

        ebit = round(ebitda - total_da, 2)

        pnl_years.append({
            "year_index": yr,
            "revenue": rev,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "gross_margin": round(gross_profit / rev, 4) if rev else 0,
            "opex": opex,
            "ebitda": ebitda,
            "ebitda_margin": round(ebitda / rev, 4) if rev else 0,
            "adjustments": adj_items,
            "adjusted_ebitda": adjusted_ebitda,
            "depreciation": depr,
            "goodwill_amortization": gw_amort,
            "intangible_amortization": int_amort,
            "total_da": total_da,
            "ebit": ebit,
            # Non-operating lines (DL-2): below EBIT, inside EBT (solver-threaded).
            "other_income": other_income_by_year[yr],
            "other_expense": other_expense_by_year[yr],
            "net_other_income": net_other_income_by_year[yr],
            "payroll_detail": payroll_by_year[yr],
        })

    # ── 7. Debt Schedule + Cash Flow — the iterative waterfall solve ─────
    # The LBO is CIRCULAR: cash sweep pays down debt from FCF, FCF depends on
    # net income, NI depends on interest, interest depends on the (post-sweep)
    # debt balance. The prior one-pass ran compute_debt_schedule WITHOUT the FCF,
    # so the sweep was NEVER applied. solve_waterfall holds the operating model
    # fixed and iterates debt-schedule ⇄ FCF to a converged fixed point. For a
    # sweep-OFF deal there is no circularity → it converges in one iteration and
    # the numbers are byte-identical to the old one-pass (the regression gate).
    distribution_pct = float(drivers.get("distribution_pct_of_fcf", DISTRIBUTION_PCT_OF_FCF))

    operating_by_year: list[OperatingYear] = []
    for yr in range(num_years):
        pnl = pnl_years[yr]
        wc_yr = wc_result["schedule"][yr] if yr < len(wc_result["schedule"]) else {}
        capex_yr = capex_result["capex_schedule"][yr] if yr < len(capex_result["capex_schedule"]) else {}
        operating_by_year.append({
            # DL-2: the solver's EBT and cash-tax base = operating EBIT + net
            # other income (non-operating income is taxable — market
            # convention). The DISPLAYED P&L "ebit" stays operating-only; the
            # non-operating lines ride their own labeled P&L rows.
            "ebit": round(float(pnl["ebit"]) + net_other_income_by_year[yr], 2),
            "total_da": float(pnl["total_da"]),
            # SA-OK: bounded-loop num_years — capex_yr/wc_yr are in-memory schedule dicts, not DB rows
            "capex": float(capex_yr.get("total_capex", 0)),
            "delta_nwc": float(wc_yr.get("delta_nwc", 0)),
            "tax_rate": tax_rate,
            "distribution_pct": distribution_pct,
        })

    # Financing-fee OID vectors (AC-C4 leg 6 — ASC 835-30 contra-debt): per
    # tranche, funded × fees_pct amortizing straight-line over the tranche's
    # schedule (the SAME oid_amortization_vectors + amort_years convention the
    # grid's PF-Y0 bridge uses, routers/lbo.py — one articulation, two surfaces).
    # The amortization is BOOK-ONLY interest expense (no tax shield — the C3
    # convention): it lowers book EBT/NI, is added back in CFO as non-cash, and
    # accretes the CARRYING debt on the projected BS (face − remaining OID).
    oid_amort_by_year = [0.0] * num_years
    oid_remaining_by_year = [0.0] * num_years
    for t in debt_tranches:
        v = oid_amortization_vectors(
            float(t.get("funded_amount", 0) or 0),  # SA-OK: bounded-loop in-memory tranche dicts, not a DB query
            float(t.get("fees_pct", 0) or 0),
            t.get("amort_years"),
            num_years,
        )
        for yr in range(num_years):
            oid_amort_by_year[yr] = round(oid_amort_by_year[yr] + v["amortization"][yr], 2)
            oid_remaining_by_year[yr] = round(oid_remaining_by_year[yr] + v["remaining"][yr], 2)

    # W3c / CORR-5 (plan 20260719): in the RE-SEED regime the projection opens debt at the FY-real CARRYING
    # value (fbs.total_debt — already net of any band-period OID amortization) and the fbs opening TIES at
    # that value. Re-applying the full original OID (funded × fees_pct) here would carry debt BELOW the tied
    # opening, and because the adapter folds goodwill to 0 there is NO S&U goodwill plug to absorb it — so
    # A=L+E breaks by exactly the OID at projected year 0 (assert_bs_check raises). Suppress the projection
    # OID: carry debt at face (== the schedule ending == the opening_balance roll). The close-date financing
    # cost already amortized through the actuals band; it does NOT re-amortize in the re-seeded projection.
    # The None (At-Close) path keeps the full OID accretion → byte-parity. NB: fees_pct is UNTOUCHED here, so
    # compute_sources_uses.financing_fees → entry_equity (the valuation firewall) is unaffected.
    if forecast_opening_bs is not None:
        oid_amort_by_year = [0.0] * num_years
        oid_remaining_by_year = [0.0] * num_years

    # IRC §197 election (AC-C4 leg 3, default OFF): when the deal elects
    # tax-goodwill amortization, the CASH-TAX base is reduced by the per-year
    # book-tax difference (tax amortization − book amortization — signed, so a
    # ≤10y book election vs the 15y tax life nets correctly). Book D&A and the
    # book BS goodwill line are NEVER touched by the election.
    sec197_elected = bool(drivers.get("section_197_election", False))
    extra_tax_deductions: list[float] | None = None
    if sec197_elected:
        extra_tax_deductions = [
            round(
                float(capex_result["goodwill_tax_schedule"][yr]["tax_amortization"])
                - float(capex_result["goodwill_schedule"][yr]["amortization"]),
                2,
            )
            for yr in range(num_years)
        ]

    # Per-deal sweep terms (sweep_pct / step-downs / min-cash floor — RD-1 row 5) or a
    # revolver tranche activate the cash-ledger schedule; None keeps the byte-identical
    # parity path. The ledger's opening cash is the projection opening cash (fbs).
    sweep_terms = build_sweep_terms(drivers, fbs, debt_tranches)  # W3c: sweep opening cash off the FY-real BS (or historical_bs when None)
    solution = solve_waterfall(
        operating_by_year, debt_tranches, num_years, sweep_terms=sweep_terms,
        noncash_interest_by_year=oid_amort_by_year,
        extra_tax_deductions_by_year=extra_tax_deductions,
        period_fraction=period_fraction,  # W3d: ×f the stub year's interest/amort/commitment fee (bullet stays discrete)
    )
    debt_result = solution["debt_schedule"]
    distributions: list[float] = solution["distributions"]

    # AC-3 (plan 20260720 live-verify round 3): the seasoned-opening reconciliation.
    # Seasoned tranches opened at their books-stated balances (never rescaled), so the
    # schedule's tranche openings can differ from the books' last-actual total debt. The
    # residual is carried FLAT in the schedule TOTALS (total_beginning/total_ending) —
    # keeping A = L + E tied off the books opening, the BS debt line (total_debt_face
    # reads total_ending) honest, and the exit's remaining-debt feed complete — with no
    # interest and no paydown invented for debt whose terms nobody stated. Per-tranche
    # rows stay pure (real loans only); the gap is DISCLOSED beside the #446
    # disclosures, never silently absorbed. 0.0 on every pre-existing path (no seasoned
    # tranche / no re-seed) → byte-parity.
    if abs(books_debt_residual) >= 0.01:
        for _yr_row in debt_result["schedule"]:
            _yr_row["total_beginning"] = round(float(_yr_row["total_beginning"]) + books_debt_residual, 2)
            _yr_row["total_ending"] = round(float(_yr_row["total_ending"]) + books_debt_residual, 2)
        _dir = "exceeds" if books_debt_residual > 0 else "is below"
        debt_result.setdefault("disclosures", []).append({
            "type": "seasoned_books_residual",
            "amount": round(books_debt_residual, 2),
            "message": (
                f"The books' last-actual total debt {_dir} the scheduled tranche openings by "
                f"${abs(books_debt_residual):,.2f}. Seasoned loans open at their stored "
                "outstanding balances (never rescaled); the difference is carried flat in the "
                "schedule totals and the projected balance-sheet debt line with no invented "
                "terms — reconcile the tranche balances to the loan documents on the "
                "Sources & Uses tab."
            ),
        })

    # Populate the PnL bridge from the converged solver lines — the solver OWNS
    # the tax computation (NOL carryforward, the §197 base, the book-only OID;
    # AC-C4 legs 2/3/6) so the P&L presents THE numbers the FCF/sweep solved on:
    #   interest_expense = the schedule's cash + PIK (the engine_composition
    #     interest identity — OID rides its OWN labeled line, never this one);
    #   ebt = BOOK EBT (EBIT − interest − OID amortization);
    #   taxes = CASH taxes (NOL-offset, §197-adjusted; never negative);
    #   taxable_income / nol_used / nol_balance = the labeled tax detail.
    for yr in range(num_years):
        ds_yr = debt_result["schedule"][yr] if yr < len(debt_result["schedule"]) else {}
        interest_cash = float(ds_yr.get("total_interest_cash", 0))
        interest_pik = float(ds_yr.get("total_interest_pik", 0))
        total_interest = round(interest_cash + interest_pik, 2)

        pnl = pnl_years[yr]
        pnl["interest_expense"] = total_interest
        pnl["interest_cash"] = interest_cash
        pnl["interest_pik"] = interest_pik
        pnl["oid_amortization"] = oid_amort_by_year[yr]
        pnl["ebt"] = solution["ebt_by_year"][yr]
        pnl["taxable_income"] = solution["taxable_income_by_year"][yr]
        pnl["taxes"] = solution["taxes_by_year"][yr]
        pnl["nol_used"] = solution["nol_used_by_year"][yr]
        pnl["nol_balance"] = solution["nol_balance_by_year"][yr]
        pnl["net_income"] = solution["net_income_by_year"][yr]

    # ── 8. Cash Flow Statement ──────────────────────────────────────────
    # Interim equity distribution policy (AC-A2): a fraction of each year's
    # POST-sweep free cash residual (WS-3, the sweep is the senior claim), default
    # 0.0 (no distributions → unchanged model). solve_waterfall computed the
    # converged distributions above; we surface the CF lines off the same fixed point.
    cf_years = []
    for yr in range(num_years):
        pnl = pnl_years[yr]
        wc_yr = wc_result["schedule"][yr] if yr < len(wc_result["schedule"]) else {}
        capex_yr = capex_result["capex_schedule"][yr] if yr < len(capex_result["capex_schedule"]) else {}
        ds_yr = debt_result["schedule"][yr] if yr < len(debt_result["schedule"]) else {}

        # CFO — PIK interest AND the financing-fee OID amortization are non-cash
        # interest add-backs (both reduced NI on the P&L; neither left as cash).
        ni = pnl["net_income"]
        da = pnl["total_da"]
        pik = pnl.get("interest_pik", 0)
        oid = pnl.get("oid_amortization", 0)
        delta_wc = float(wc_yr.get("delta_nwc", 0))
        cfo = round(ni + da + pik + oid - delta_wc, 2)

        # CFI
        capex = float(capex_yr.get("total_capex", 0))
        cfi = round(-capex, 2)

        # CFF — total_amortization INCLUDES the cash sweep (and, in ledger mode, the
        # revolver repay); a revolver DRAW is a financing INFLOW so the BS cash line
        # articulates from the same schedule the debt balances came from.
        amort_paid = float(ds_yr.get("total_amortization", 0))
        revolver_draw = float(ds_yr.get("total_revolver_draw", 0))
        interest_paid = pnl.get("interest_cash", 0)  # noqa: F841

        distribution = distributions[yr]
        cff = round(-amort_paid + revolver_draw - distribution, 2)  # Interest already in CFO via NI

        net_change = round(cfo + cfi + cff, 2)

        cf_years.append({
            "year_index": yr,
            "net_income": ni,
            "depreciation_amortization": da,
            "pik_interest_addback": pik,
            "oid_amortization_addback": oid,
            "working_capital_change": -delta_wc,
            "cfo": cfo,
            "capex": -capex,
            "cfi": cfi,
            "debt_amortization": -amort_paid,
            "revolver_draw": revolver_draw,
            "distributions": -distribution,
            "cff": cff,
            "net_change_in_cash": net_change,
        })

    # ── 9. Balance Sheet — articulates from the ASC-805 opening (AC-C4 leg 6) ──
    # Opening position = ``pf_opening_bs_asc805``: goodwill EXCLUDES both fee
    # categories (the plug above), transaction costs are EXPENSED at close (so
    # retained earnings OPEN at −transaction_fees, not 0), and debt is carried
    # at FACE − unamortized OID (ASC 835-30), accreting through the P&L's
    # labeled ``oid_amortization`` line — mirroring the grid's PF-Y0 bridge
    # articulation exactly. A = L + E is asserted every year, as before.
    bs_years = []
    prev_cash = float(hbs.get("cash", 0))
    # W3c: on the re-seed prev_re opens at the FY's real retained_earnings — paired with equity_contributed_proj
    # from the SAME tied BS so equity_contributed_proj + prev_re == total_equity_FY (no double-count); the None
    # path stays the ASC 805 expensed-at-close seed (byte-parity).
    prev_re = (
        float(fbs.get("retained_earnings", su_asc805["transaction_costs_expensed"]))
        if forecast_opening_bs is not None
        else float(su_asc805["transaction_costs_expensed"])
    )  # ASC 805: expensed at close (None path)

    # Opening lines no driver models (inventory / intangibles / PP&E the capex
    # drivers don't cover) are carried STATIC so the projected sheet articulates
    # from the same balanced opening position — previously they were silently
    # dropped and bs_check drifted by their sum every year (RD-1 row 9 fix).
    # The SAME re-anchored capex list feeds the static carry, so the DL-5
    # re-anchor shifts the removed vintage slice into unmodeled_fixed_assets
    # and total PP&E is preserved (A = L + E still ties by construction).
    # ``static_lines`` is computed in §4 (the AC-FRR-11b hoist — ahead of the
    # capex schedule + solve so the NPP&E floor sees the unmodeled offset).

    for yr in range(num_years):
        pnl = pnl_years[yr]
        wc_yr = wc_result["schedule"][yr] if yr < len(wc_result["schedule"]) else {}
        capex_yr = capex_result["capex_schedule"][yr] if yr < len(capex_result["capex_schedule"]) else {}
        ds_yr = debt_result["schedule"][yr] if yr < len(debt_result["schedule"]) else {}
        cf_yr = cf_years[yr]
        gw_yr = capex_result["goodwill_schedule"][yr] if yr < len(capex_result["goodwill_schedule"]) else {}

        cash = round(prev_cash + cf_yr["net_change_in_cash"], 2)
        ar = float(wc_yr.get("ar", 0))
        other_ca = float(wc_yr.get("other_ca", 0))
        # DIO-driven inventory (DL-3) when the WC schedule carries it; static
        # opening carry (the pre-existing behavior) when the driver is off.
        inv_wc = wc_yr.get("inventory")  # SA-OK: bounded-loop num_years — in-memory WC schedule row, not a DB query
        inventory = float(inv_wc) if inv_wc is not None else static_lines["inventory"]
        total_current = round(cash + ar + inventory + other_ca, 2)

        net_fa = round(
            float(capex_yr.get("total_net_fa", 0)) + static_lines["unmodeled_fixed_assets"], 2
        )
        # Intangibles follow the amortization schedule (DL-5); with the driver
        # off the schedule's ending == the opening balance — the static carry.
        intangibles = (
            float(intangible_schedule[yr]["ending_intangibles"])  # type: ignore[index]
            if yr < len(intangible_schedule)  # type: ignore[arg-type]
            else static_lines["intangibles"]
        )
        goodwill = float(gw_yr.get("ending_goodwill", goodwill_amount))
        other_assets = float(hbs.get("other_assets", 0))
        total_assets = round(
            total_current + net_fa + intangibles + goodwill + other_assets, 2
        )

        ap = float(wc_yr.get("ap", 0))
        accrued = float(wc_yr.get("accrued", 0))
        other_cl = float(wc_yr.get("other_cl", 0))
        total_current_liab = round(ap + accrued + other_cl, 2)

        # Debt at CARRYING value (ASC 835-30): the schedule's FACE ending balance
        # less the unamortized OID contra — the P&L's oid_amortization accretes
        # it back toward face. The schedule/exit feed stays FACE (repayment is
        # at face); only this book presentation nets the contra.
        total_debt_face = float(ds_yr.get("total_ending", 0))
        oid_remaining = oid_remaining_by_year[yr] if yr < num_years else 0.0
        total_debt = round(total_debt_face - oid_remaining, 2)
        total_liabilities = round(total_current_liab + total_debt, 2)

        # Distributions paid out this year reduce retained earnings (and cash, via
        # net_change_in_cash above) — both sides drop by the same amount so the
        # A = L + E identity is preserved. Default 0.0 → retained_earnings unchanged.
        # retained_earnings OPENS at −transaction_fees (ASC 805 expensed-at-close,
        # prev_re seed above) on the None path, or the FY's real RE on the re-seed.
        # equity_contributed is equity_contributed_proj (W3c decouple): the cash S&U
        # plug on the None path (byte-parity), the FY's real contributed capital on
        # the re-seed — paired with prev_re from the SAME tied BS so the sheet ties.
        retained_earnings = round(prev_re + pnl["net_income"] - distributions[yr], 2)
        equity = round(equity_contributed_proj + retained_earnings, 2)
        total_le = round(total_liabilities + equity, 2)

        bs_check = round(total_assets - total_le, 2)
        # A = L + E holds by construction — an out-of-tolerance check is a genuine
        # articulation regression and FAILS LOUD (previously returned-not-asserted,
        # the RD-1 row 9 / AP-FIN-05 defect). Raises BalanceSheetIntegrityError.
        assert_bs_check(bs_check, yr)

        bs_years.append({
            "year_index": yr,
            "cash": cash,
            "accounts_receivable": ar,
            "inventory": inventory,
            "other_current_assets": other_ca,
            "total_current_assets": total_current,
            "net_fixed_assets": net_fa,
            "intangibles": intangibles,
            "goodwill": goodwill,
            "other_assets": other_assets,
            "total_assets": total_assets,
            "accounts_payable": ap,
            "accrued_expenses": accrued,
            "other_current_liabilities": other_cl,
            "total_current_liabilities": total_current_liab,
            "total_debt": total_debt,                       # carrying (face − OID)
            "total_debt_face": round(total_debt_face, 2),   # the schedule/exit feed
            "debt_oid_remaining": round(oid_remaining, 2),  # the unamortized contra
            "total_liabilities": total_liabilities,
            "equity_contributed": round(equity_contributed_proj, 2),  # W3c: decoupled (== entry_equity on the None path)
            "retained_earnings": retained_earnings,
            "total_equity": equity,
            "total_liabilities_equity": total_le,
            "bs_check": bs_check,
        })

        prev_cash = cash
        prev_re = retained_earnings

    # ── 10. Assemble Result ─────────────────────────────────────────────
    return {
        "num_years": num_years,
        "sources_uses": su,
        "pnl": pnl_years,
        "bs": bs_years,
        "cf": cf_years,
        "debt_schedule": debt_result,
        "capex_schedule": capex_result,
        "wc_schedule": wc_result,
        "revenue_by_year": revenue_by_year,
        "entry_equity": entry_equity,
        "distributions": distributions,
        # AC-2 (plan 20260720 live-verify) — the display-anchor echo: the FY-real
        # opening BS this projection ARTICULATED from (the D-W3-2 re-seed regime;
        # carries the debt_std/debt_ltd split via the composition adapter). The
        # grid's delta rows (grid_projection Pass C) must difference the FIRST
        # forecast column against THIS opening — never the grid's col-0 At-Close
        # position, a different anchor entirely. None on the At-Close path.
        "projection_opening_bs": (
            dict(forecast_opening_bs) if forecast_opening_bs is not None else None
        ),
        # Per-add-back provenance records (RD-1 row 16 — the QoE discipline).
        "ebitda_adjustments_detail": adj_detail_by_name,
        # The labeled IRC §197 tax-amortization proxy (never book P&L — row 17);
        # cash-tax-linked only under the explicit election (AC-C4 leg 3).
        "goodwill_tax_schedule": capex_result.get("goodwill_tax_schedule", []),
        "section_197_election": sec197_elected,
        # AC-C4 leg 6 — the financing-fee (OID) articulation vectors the BS/P&L
        # carry, and the leg-2 NOL walk (labeled tax detail, per year).
        "oid_amortization_by_year": oid_amort_by_year,
        "debt_oid_remaining_by_year": oid_remaining_by_year,
        "nol_balance_by_year": solution["nol_balance_by_year"],
        # ── C2-b (plan 20260717-three-statement-spine AC-5) ──────────────
        # The validated historical-period echo (each column A = L + E asserted;
        # [] when no feed — the pre-existing contract).
        "historical_periods": hist_periods,
        # Goodwill regime — LABELED (DEC-LBO3S-8): impairment-only hold-flat
        # default vs the private-company ≤10y straight-line election.
        "goodwill_regime": capex_result["goodwill_basis"],
        # Intangible amortization — the labeled driver decision + the DL-5
        # tangible-only re-anchor record (double-count guard).
        "intangible_amortization": {
            "active": intangible_active,
            "amort_years": intangible_amort_years,
            "opening_intangibles": opening_intangibles,
            "annual_amortization": intangible_annual,
            "schedule": intangible_schedule,
            "capex_anchor_reanchored_tangible_only": reanchored_annual_removed > 0,
            "reanchored_annual_da_removed": reanchored_annual_removed,
        },
        # Other income/expense — the labeled DL-2 decision: non-operating lines
        # enter EBT and the CASH-TAX base (taxable — market convention); they
        # never touch EBITDA/EBIT.
        "other_income_treatment": {
            "active": any(v != 0 for v in net_other_income_by_year),
            "policy": "ebt",
            "taxable": True,
            "net_other_income_by_year": net_other_income_by_year,
        },
    }

"""
app/lbo/engine_composition.py
─────────────────────────────
THE single live-path engine run (chunk C2, plan 20260716-five-domain-remediation
AC-C2 / SYNTHESIS R-Dim-3 decision D3): ``run_scenario_engine`` seeds ONE
``compute_full_forecast`` (Path A — the fixed-point interest⇄sweep solve via
``waterfall_solve``, fail-loud A=L+E via ``engine_support.assert_bs_check``) from

  * the grid Y0 actuals (the QBO-seeded col-1 input cells),
  * the grid's DERIVED forward drivers (the ``seed_derivation`` params living on
    the grid rows — yoy growth / pct_revenue / dso / dpo / pct_ebit / depr), and
  * the CONFIRMED Sources & Uses overrides (``LboSourcesUsesOverride`` — the
    same manual overlay the S&U tab merges, so the /returns denominator is
    IDENTICAL to the tab's equity plug by construction),

and every live consumer — ``/returns``, ``/debt-schedule``, and the grid's
debt / interest / tax rows in ``recalc-v2`` — derives from this one run.

Seed contract (deterministic; documented divergences are DISCLOSED, never silent):
  * Revenue: ``base_revenue`` = grid Y0; per-year "overall" growth mini-evaluated
    from the revenue row's own driver (yoy / fixed / schedule_override; any other
    driver carries Y0 flat — the historical grid-projection fallback convention).
  * COGS / SG&A: the grid pct_revenue coefficients map onto the engine's
    ``direct_cost_pct`` / ``facilities_pct`` knobs (Y0-ratio fallback when the row
    carries a non-pct driver); ``other_ga_pct`` = 0. The grid's non-operating
    other_income/other_expense lines have no engine analogue (disclosed).
  * Working capital: dso/dpo days from the grid ar/ap drivers (Y0-derived via the
    ``seed_derivation`` inverses when absent); other_ca / accrued / other_cl as
    Y0 ratios. Grid inventory (dio) has no engine ΔNWC analogue — a disclosed
    engine limitation (inventory is carried static on the engine BS).
  * D&A / capex: ONE synthesized capex driver from the ppe row's ``depr`` params
    (capex_pct_revenue + useful_life) anchored to the Y0 D&A run-rate
    (beginning_gross = y0_da × (life − 1) so the engine's vintage-0 annual equals
    the run-rate; the engine's pre-existing-vintage window is one year shorter
    than the grid's — immaterial when useful_life exceeds the horizon).
  * Tax: the taxes row's ``pct_ebit`` rate (stored ``tax_rate`` fallback). The
    ENGINE's tax base includes interest (EBT = EBIT − interest) — the fix for the
    grid's former pre-interest tax base (the grid now stamps the engine taxes).
  * Tranches: the canonical ``tranche_to_engine_payload`` with the CONFIRMED S&U
    funded-amount override and maturity-derived years-remaining amortization —
    one tranche truth for the schedule, the S&U plug, and the grid rows.
  * Horizon: from the scenario's company config (model_start/end_year), clamped
    to the grid's physical projection columns — never a hardcoded 5.

Every reconciliation check here ASSERTS (raises), never decorates: the engine's
P&L interest must equal the schedule's per-year interest, tranche-sum == totals
(``EngineReconciliationError``), and the engine's own A=L+E assertion rides along.

Payload convention: engine inputs/outputs are the opaque engine dicts the
callers thread (the waterfall_solve.DebtTranche precedent) — ``dict[str, object]``;
a grid row is the free-form in-memory dict the grid seed + stamp read/write.
"""
from __future__ import annotations

import datetime as _dt
import logging

from ..models import (
    LboCell,
    LboCompany,
    LboDebtTranche,
    LboDriver,
    LboScenario,
    LboSourcesUsesOverride,
)
from .forecast_engine import compute_full_forecast
from .grid_helpers import NUM_YEAR_COLS, _get_historical_bs
from .seed_derivation import derive_dpo, derive_dso
from .serializers import DEAL_KEYS, _default_driver_data, tranche_to_engine_payload

logger = logging.getLogger(__name__)

# The opaque engine payloads (drivers / grid rows / tranche dicts / forecast
# results) — the same convention as waterfall_solve.DebtTranche.
EnginePayload = dict[str, object]
GridRow = dict[str, object]

# Per-year interest-identity tolerance: every stored schedule value is rounded to
# cents, so |Σ round(tranche) − round(Σ)| ≤ half a cent per tranche. Two cents
# covers any realistic tranche count; a real disconnect is dollars, not cents.
_INTEREST_IDENTITY_TOL = 0.02


class EngineReconciliationError(RuntimeError):
    """A cross-statement identity failed on the composed engine run.

    P&L interest ≡ Σ schedule interest is ASSERTED (AC-C2), never returned as a
    decorative check — a model whose income statement and debt schedule disagree
    is a fabricated model and is refused, loudly.
    """


class EntryPricingError(RuntimeError):
    """A books-start entry with NO pricing basis cannot be priced (D-CA-4 / AC-CA-8, Wave 6).

    The deal's QBO file begins at consummation — there is no pre-close TTM run-rate, so
    ``ltm_ebitda`` is honestly EMPTY. Running the S&U anyway would price EV = 0 × multiple and
    plug a fabricated zero-value deal SILENTLY (every downstream assertion is an internal-
    consistency tie, so nothing else would scream). Refuse-over-fabricate: the entry requires an
    explicit pricing basis — the confirmed APA ``entry_enterprise_value`` override or a
    user-entered EV/entry-EBITDA — before any engine run.
    """


def grid_rows_from_cells(cells: list[LboCell], num_cols: int = NUM_YEAR_COLS) -> list[GridRow]:
    """Rebuild the in-memory grid rows from persisted ``LboCell`` rows.

    The one shared cell→row builder for the composition + recalc-v2 (extracted
    from the recalc-v2 inline block so the two can never drift): carries
    ``line_code``, driver_type/params, the ``_parent_line_code`` tag folded into
    driver_params at persist time, and the col-1..N values.
    """
    rows_map: dict[int, GridRow] = {}
    for c in cells:
        ri = c.row_index
        if ri not in rows_map:
            rows_map[ri] = {
                "row": ri,
                "label": "",
                "section": c.section or "",
                "display_fmt": c.display_fmt or "currency",
                "row_type": c.row_type or "input",
                "formula": None,
                "indent": c.indent or 0,
                "driver_type": c.driver_type or "auto",
                "driver_params": c.driver_params or {},
                "qbo_account_id": c.qbo_account_id,
                "line_code": c.line_code,
                "_parent_line_code": (c.driver_params or {}).get("_parent_line_code"),  # SA-OK: dict.get on the cell's JSON driver_params, NOT an ORM query
                "values": [None] * num_cols,
            }
        if c.col_index == 0:
            rows_map[ri]["label"] = c.label or ""
            rows_map[ri]["row_type"] = c.row_type or "input"
            rows_map[ri]["driver_type"] = c.driver_type or "auto"
            rows_map[ri]["driver_params"] = c.driver_params or {}
            rows_map[ri]["line_code"] = c.line_code
            rows_map[ri]["_parent_line_code"] = (c.driver_params or {}).get("_parent_line_code")  # SA-OK: dict.get on the cell's JSON driver_params, NOT an ORM query
            if c.formula:
                rows_map[ri]["formula"] = c.formula
        else:
            idx = c.col_index - 1
            if 0 <= idx < num_cols:
                rows_map[ri]["values"][idx] = float(c.value) if c.value is not None else None
            if c.formula:
                rows_map[ri]["formula"] = c.formula
    return [rows_map[k] for k in sorted(rows_map.keys())]


def _scenario_horizon(company: LboCompany | None) -> int:
    """Projection years from the scenario's company config (AC-C2 leg 3).

    ``model_end_year − model_start_year + 1`` (the /forecast convention), clamped
    to the grid's physical projection columns (``NUM_YEAR_COLS − 1``) — the grid
    cannot present more years than it has columns. Missing config degrades to the
    full grid width; never a hardcoded literal 5.
    """
    if company is not None and company.model_start_year and company.model_end_year:
        span = int(company.model_end_year) - int(company.model_start_year) + 1
        return max(1, min(span, NUM_YEAR_COLS - 1))
    return NUM_YEAR_COLS - 1


def _load_su_overrides(scenario_id: int, db) -> tuple[EnginePayload, dict[int, float]]:
    """The CONFIRMED S&U override snapshot: (overlay dict, tranche_id → funded).

    The same snapshot ``_sources_uses_payload`` merges — loading it here is what
    makes the /returns entry equity ≡ the S&U tab's override-merged plug.
    """
    row = db.query(LboSourcesUsesOverride).filter(
        LboSourcesUsesOverride.scenario_id == scenario_id  # CROSS-TENANT-OK: scenario_id is tenant-verified by _require_lbo_scenario / check_entity_tenant_access in every route before the composition runs (the file-wide LBO scenario-scoping pattern, mirrors serializers._get_tranches_list)
    ).first()
    ov: EnginePayload = dict(row.overrides_json) if row is not None and row.overrides_json else {}
    tranche_ov: dict[int, float] = {}
    tranche_map = ov.get("tranches") or {}  # SA-OK: dict.get on the in-memory override snapshot, NOT an ORM query
    for key, amount in tranche_map.items():  # type: ignore[union-attr]
        try:
            tranche_ov[int(key)] = float(amount)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue  # a malformed snapshot entry never breaks the compute path
    return ov, tranche_ov


def resolve_tranche_funded(tranche: LboDebtTranche, tranche_ov: dict[int, float]) -> float | None:
    """THE one funded-amount resolution for acquisition tranches (AC-C3 leg 3).

    Precedence: CONFIRMED S&U override  >  stored ``funded_amount`` (the
    user-entered acquisition-financing size, when positive)  >  ``None`` —
    which lets ``tranche_to_engine_payload`` fall back to the stored
    ``current_balance`` (the explicit refinance-at-par default for a tranche
    seeded from a QBO loan with no user-entered size). This makes the
    refinance boundary explicit: ``current_balance`` is the target's legacy GL
    balance (excluded from the PF opening BS as refinanced); ``funded_amount``
    is the NEW facility the deal draws — the two are no longer conflated.
    A stored ``funded_amount`` of 0 reads as "not entered" (every legacy row
    was seeded funded==current_balance, so this changes no stored deal).
    """
    if tranche.id in tranche_ov:
        return float(tranche_ov[tranche.id])
    funded = float(tranche.funded_amount or 0)
    return funded if funded > 0 else None


def _engine_tranche_pairs(
    scenario_id: int,
    db,
    tranche_ov: dict[int, float],
    model_start_year: int,
) -> list[tuple[LboDebtTranche, EnginePayload]]:
    """(DB tranche, canonical engine payload) pairs — the ONE tranche truth.

    funded_amount: ``resolve_tranche_funded`` (confirmed S&U override >
    stored user-entered funded_amount > the current_balance refinance-at-par
    default — AC-C3 leg 3). amort_years: the years-remaining-to-maturity when
    a ``maturity_date`` is stored (the wave-5 date-aware convention), else the
    stored term. term_years (AC-C4 leg 1): the SAME maturity-anchored year
    count, threaded so the engine retires an inside-horizon tranche AT its
    maturity (debt_schedule.py maturity-aware repayment); no maturity_date →
    None → the pre-existing horizon-bullet behavior.
    """
    tranches_db = (
        db.query(LboDebtTranche)
        .filter(LboDebtTranche.scenario_id == scenario_id)  # CROSS-TENANT-OK: scenario_id is tenant-verified by _require_lbo_scenario / check_entity_tenant_access in every route before the composition runs (LboDebtTranche is scenario-scoped, mirrors serializers._get_tranches_list)
        .order_by(LboDebtTranche.sort_order, LboDebtTranche.id)
        .all()
    )
    pairs: list[tuple[LboDebtTranche, EnginePayload]] = []
    for t in tranches_db:
        amort_years: int | None = None
        if t.maturity_date:
            try:
                maturity_year = _dt.date.fromisoformat(str(t.maturity_date)[:10]).year
                amort_years = max(1, maturity_year - model_start_year)
            except (ValueError, TypeError):
                amort_years = None  # unparseable date → the stored term stands
        pairs.append(
            (t, tranche_to_engine_payload(
                t, funded_amount=resolve_tranche_funded(t, tranche_ov), amort_years=amort_years,
                term_years=amort_years,  # the same maturity-anchored count (AC-C4 leg 1)
            ))
        )
    return pairs


def _add_years(d: _dt.date, years: int) -> _dt.date:
    """``d`` + ``years`` calendar years; a Feb-29 start clamps to Feb-28 when the
    landing year is not a leap year (loan dates are month-end/anniversary dates —
    the one-day clamp is the standard calendar convention, not a schedule change)."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _forecast_anchor_date(
    bl: dict | None,
    stub_fraction: float | None,
    first_forecast_year: int,
) -> _dt.date:
    """The first forecast period's START date — the day after the last actual
    month-end — on the projection's own axis (the axis the debt schedule's year
    indices run on). This is the remaining-term amortization divisor's t=0.

    Convention (AC-1, plan 20260720 live-verify round 5):
      * re-seed + stub: the stub year's FY-end = ``base_fy_end`` + 1 year; grid_qbo
        encodes ``stub_fraction = (stub_fy_end − last_actual_end).days / 365``, so the
        encode inverts EXACTLY — last_actual_end = stub_fy_end − round(f × 365) days;
        the anchor is the day after (the live deal: base FY end 2025-12-31,
        f = 184/365 → last actual 2026-06-30 → anchor 2026-07-01).
      * re-seed, no stub: the last actual IS the base FY end → ``base_fy_end`` + 1 day.
      * no re-seed (or an unparseable ``base_fy_end``): Jan 1 of the first forecast
        year — the undated regime carries no month-level anchor, so the convention
        degrades to the same year granularity ``first_forecast_year`` itself has.
    """
    fy_end: _dt.date | None = None
    if isinstance(bl, dict) and bl.get("base_fy_end"):  # SA-OK: dict.get on the in-memory base_levels meta, NOT an ORM query
        try:
            fy_end = _dt.date.fromisoformat(str(bl["base_fy_end"])[:10])
        except (ValueError, TypeError):
            fy_end = None
    if fy_end is None:
        return _dt.date(first_forecast_year, 1, 1)
    if stub_fraction is not None and 0.0 < float(stub_fraction) < 1.0:
        stub_fy_end = _add_years(fy_end, 1)
        last_actual = stub_fy_end - _dt.timedelta(days=round(float(stub_fraction) * 365.0))
        return last_actual + _dt.timedelta(days=1)
    return fy_end + _dt.timedelta(days=1)


def _apply_seasoned_terms(
    tranche_pairs: list[tuple[LboDebtTranche, EnginePayload]],
    first_forecast_year: int,
    forecast_anchor_date: _dt.date,
) -> None:
    """AC-1 (plan 20260720 live-verify rounds 3-5): SEASONED-loan forward-roll semantics.

    A tranche whose stored ``start_date`` pre-dates the first forecast calendar year is
    a SEASONED loan — an existing dated facility (the live deal's Dec-2023 refi), not
    at-close acquisition financing — and it rolls forward from its OWN contractual
    schedule, never the model-start-anchored derivation:

      * ``opening_balance`` = the stored ``current_balance`` (the user's books-stated
        outstanding). NEVER pro-rata re-based — forecast_engine's CORR-3 re-base skips
        ``seasoned`` payloads; any Σ-openings-vs-books residual is DISCLOSED (AC-3),
        never rescaled away.
      * REMAINING-TERM amortization (round 5 — the halved-pace fix): a mid-life loan
        pays its balance down over the years LEFT in its amortization schedule, never
        a fresh full term restarted at the anchor. The amortization END date =
        ``start_date`` + the STORED ``amort_years``; ``remaining_years`` = (amort end
        − ``forecast_anchor_date``) / 365.25 — a FLOAT divisor debt_schedule divides
        by directly — and the pace basis is the outstanding ``current_balance``, so
        the schedule reaches 0 exactly AT the amort end (the final partial year's
        installment clamps to the live balance via ``min(amort, beg)``), never before
        or after, with NO phantom balloon. The live refi: 2023-12-08 + 5y →
        2028-12-08; anchor 2026-07-01 → remaining ≈ 2.44y → ~$1.0M/yr — NOT the
        fresh-term 2,443,823.92 / 5 = 488,764.78/yr that under-paid two years and
        left a fabricated 1,219,903 maturity balloon. No stored ``amort_years`` → no
        scheduled amortization is invented (interest-only to maturity — refuse over
        fabricate). ``remaining_years`` ≤ 0 with the maturity still ahead (a
        balloon-period loan: the amort schedule is exhausted while the balance rides
        to a stated future bullet) → interest-only + the maturity bullet, DISCLOSED
        (``amort_exhausted_interest_only`` — debt_schedule stamps the disclosure);
        ≤ 0 with the maturity ALSO past → the #446 hold below, unchanged. A genuine
        balloon structure (maturity BEFORE the amort end) amortizes at the
        remaining-term pace until maturity and bullets the remainder AT maturity —
        that balloon is real and kept.
      * ``term_years`` = anchored to the FIRST FORECAST calendar year so the maturity
        bullet fires exactly AT the real maturity year. (The undated anchoring pins to
        ``model_start_year`` and retires one year EARLY whenever model_start_year IS
        the first forecast year — the live scenario-9 defect: the 2028-12-08 refi died
        in 2027.) A maturity already passed while the books still carry the balance →
        the #446 ``past_maturity_outstanding`` hold + disclosure.
      * ``sweep_participating`` = ``False`` (round 4): a seasoned loan is EXCLUDED
        from the deal-wide cash sweep by default — the ``sweep_pct`` 1.0 at-close
        default would prepay it from a hypothetical 100% ECF sweep the loan's own
        books disprove (the balance held for years while cash went elsewhere). Its
        paydown is the contractual schedule above ONLY; the sweep re-allocates to
        the remaining participating tranches (or accumulates as cash when none).
        ``setdefault`` preserves an upstream explicit opt-in — the documented
        per-tranche payload key ``sweep_participating: True`` re-enables the sweep
        (debt_schedule honors it over the seasoned default).

    ``forecast_anchor_date`` is the first forecast period's start date — see
    ``_forecast_anchor_date`` for the documented derivation convention.

    UNDATED tranches (no / unparseable ``start_date``, or a start at/after the anchor —
    new financing) are untouched: the at-close S&U semantics, the CORR-3 pro-rata
    re-base, and the CORR-4 band decrement apply exactly as before (AC-2 parity).
    """
    for t, p in tranche_pairs:
        if not t.start_date:
            continue
        try:
            start_date = _dt.date.fromisoformat(str(t.start_date)[:10])
        except (ValueError, TypeError):
            continue  # unparseable start → refuse to classify; the undated semantics stand
        if start_date.year >= first_forecast_year:
            continue  # starts inside the forecast window — new financing, not a seasoned roll
        opening = float(t.current_balance or 0)
        p["seasoned"] = True
        p["opening_balance"] = opening
        # Round 5: the pace basis IS the books-stated outstanding — the remaining-term
        # construction pays the LIVE balance down over the schedule years left (for a
        # loan amortizing on schedule, balance/remaining == original principal/full
        # term, so an on-schedule loan keeps its contractual installment exactly).
        p["amort_basis"] = opening
        # AC-1 round 4: seasoned loans default OUT of the deal-wide cash sweep —
        # contractual schedule only (docstring bullet 4). setdefault keeps an
        # upstream explicit ``sweep_participating: True`` opt-in intact.
        p.setdefault("sweep_participating", False)
        maturity_year: int | None = None
        if t.maturity_date:
            try:
                maturity_year = _dt.date.fromisoformat(str(t.maturity_date)[:10]).year
            except (ValueError, TypeError):
                maturity_year = None
        if maturity_year is None:
            p["term_years"] = None  # no stated maturity → the pre-existing horizon convention
        elif maturity_year >= first_forecast_year:
            # Bullet at yr == term − 1 == the REAL maturity calendar year (year 0 is
            # first_forecast_year): live refi 2028 − 2026 + 1 = 3 → bullet in 2028.
            p["term_years"] = maturity_year - first_forecast_year + 1
        else:
            # Matured before the anchor, books still outstanding: the #446 disclosed hold.
            p["term_years"] = None
            p["past_maturity_outstanding"] = True
        # REMAINING-TERM amortization pace (round 5 — docstring bullet 2).
        if not t.amort_years:
            p["amort_years"] = None  # no stored term → no invented amortization
            continue
        amort_end = _add_years(start_date, int(t.amort_years))
        remaining_years = (amort_end - forecast_anchor_date).days / 365.25
        if remaining_years > 0:
            p["amort_years"] = remaining_years  # float divisor: opening/remaining per full year
        else:
            p["amort_years"] = None
            if not p.get("past_maturity_outstanding"):  # SA-OK: dict.get on the in-memory engine payload, NOT an ORM query
                # The amort schedule ended before the anchor while the balance rides to
                # a stated future (or unstated) maturity: interest-only to the bullet —
                # DISCLOSED, never a silently-invented catch-up paydown.
                p["amort_exhausted_interest_only"] = True


def apply_seasoned_terms_to_pairs(
    tranche_pairs: list[tuple[LboDebtTranche, EnginePayload]],
    bl: dict | None,
    reseed_active: bool,
    stub_fraction: float | None,
    model_start_year: int,
) -> None:
    """Public seam for NON-run_scenario_engine callers (the sensitivity-grid lane):
    apply the SAME seasoned-loan semantics from the same meta inputs, so every
    engine path prices dated tranches off their contractual remaining-term
    schedule. Mirrors run_scenario_engine's derivation exactly: FYCY =
    ``base_fy_year`` + 1 in the re-seed regime (else ``model_start_year``), the
    anchor date from :func:`_forecast_anchor_date`'s documented convention. A
    caller that skips this seam re-bases/decrements seasoned loans — the exact
    divergence flagged at the 2026-07-21 remaining-term fix.
    """
    base_fy_year = int((bl or {}).get("base_fy_year") or model_start_year)  # SA-OK: dict.get on the in-memory base_levels meta, NOT an ORM query
    first_forecast_year = (base_fy_year + 1) if reseed_active else model_start_year
    anchor = _forecast_anchor_date(bl if reseed_active else None, stub_fraction, first_forecast_year)
    _apply_seasoned_terms(tranche_pairs, first_forecast_year, anchor)


def _row_y0(by_code: dict[str, GridRow], code: str) -> float:
    row = by_code.get(code)
    if not row:
        return 0.0
    vals = row.get("values") or []  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
    return float(vals[0]) if vals and vals[0] is not None else 0.0  # type: ignore[index,arg-type]


def _base_level(by_code: dict[str, GridRow], code: str, base_levels: dict | None) -> float:
    """The PROJECTION base value for ``code`` (plan 20260719 Wave 3): the re-seed level from
    ``base_levels`` (the last complete post-close fiscal year's run-rate + FY-end BS) when provided +
    present, else the grid Y0 (seq-0 At-Close). The valuation firewall (D-CA-3): ``ltm_ebitda`` NEVER
    routes through here — it stays a hard ``_row_y0(by_code,'ebitda')`` seq-0 read by construction.
    """
    if base_levels is not None:
        v = base_levels.get(code)  # SA-OK: dict.get on the in-memory re-seed levels, NOT an ORM query
        if v is not None:
            return float(v)
    return _row_y0(by_code, code)


def _driver_of(by_code: dict[str, GridRow], code: str) -> tuple[str, dict]:
    row = by_code.get(code) or {}
    params = row.get("driver_params") or {}  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
    return str(row.get("driver_type") or "auto"), (params if isinstance(params, dict) else {})


def _revenue_growth(by_code: dict[str, GridRow], rev0: float, num_years: int) -> list[float]:
    """Mini-evaluate the revenue row's own driver into the engine's per-year
    "overall" growth list, chained off Y0 (yoy / fixed / schedule_override; any
    other driver carries flat — the historical grid-projection fallback)."""
    driver, params = _driver_of(by_code, "revenue")
    if driver == "yoy":
        return [float(params.get("growth_rate", 0.0))] * num_years  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
    if driver == "fixed" and rev0 > 0:
        amount = float(params.get("amount", rev0))  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
        return [amount / rev0 - 1.0] + [0.0] * (num_years - 1)
    if driver == "schedule_override":
        ov = params.get("override_values")  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
        if isinstance(ov, list):
            growth: list[float] = []
            prev = rev0
            for col in range(1, num_years + 1):
                nxt = float(ov[col]) if col < len(ov) and ov[col] is not None else prev
                growth.append(nxt / prev - 1.0 if prev else 0.0)
                prev = nxt
            return growth
    return [0.0] * num_years


def _pct_of_revenue(by_code: dict[str, GridRow], code: str, rev0: float,
                    base_levels: dict | None = None) -> float:
    """A cost/asset line's revenue coefficient: the row's pct_revenue driver when
    present (the derived driver, a forward assumption on the seq-0 header — unchanged),
    else the Y0 ratio (re-seeded off ``base_levels`` when present — Wave 3), else 0.0
    for a degenerate zero-revenue grid."""
    driver, params = _driver_of(by_code, code)
    if driver == "pct_revenue":
        return float(params.get("pct", 0.0))  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
    level = _base_level(by_code, code, base_levels)
    return (level / rev0) if rev0 > 0 else 0.0


def derive_engine_seed(grid_rows: list[GridRow], num_years: int,
                       base_levels: dict | None = None) -> EnginePayload:
    """Translate the grid Y0 + derived drivers into the engine's operating seed.

    Returns ``{"drivers_patch", "capex_drivers", "base_revenue", "ltm_ebitda"}``.
    Pure over the in-memory rows (no DB) — golden-testable conftest-free.

    ``base_levels`` (plan 20260719 Wave 3, D-W3-2) RE-SEEDS the PROJECTION off the last complete
    post-close fiscal year's run-rate + FY-end BS (revenue/cost base, WC-day derivation, D&A anchor) —
    the FORWARD ASSUMPTIONS (growth rates + explicit pct/dso/dpo drivers) stay on the seq-0 header, so
    only the base LEVELS move. When ``None`` (no band / recent close) the seed is byte-identical to the
    seq-0 At-Close basis. The VALUATION FIREWALL (D-CA-3): ``ltm_ebitda`` is a HARD seq-0 read, NEVER
    re-seeded — it is the sole input to EV / entry_equity (the returns denominator).
    """
    by_code: dict[str, GridRow] = {
        r["line_code"]: r for r in grid_rows if r.get("line_code")  # type: ignore[misc]
    }

    rev0 = _base_level(by_code, "revenue", base_levels)
    cogs0 = _base_level(by_code, "cogs", base_levels)
    sga0 = _base_level(by_code, "sga", base_levels)
    y0_da = _base_level(by_code, "depreciation", base_levels)

    patch: EnginePayload = {
        "revenue_growth_by_stream": {"overall": _revenue_growth(by_code, rev0, num_years)},
        "margin_by_stream": {},
        "direct_cost_pct": _pct_of_revenue(by_code, "cogs", rev0, base_levels),
        "facilities_pct": _pct_of_revenue(by_code, "sga", rev0, base_levels),
        "other_ga_pct": 0.0,
        "other_ca_pct": _pct_of_revenue(by_code, "other_ca", rev0, base_levels),
        # Engine accrued/other-CL scale with OPEX; the grid scales them with
        # revenue — the Y0-ratio translation is exact whenever SG&A itself
        # tracks revenue (the derived-driver default).
        "accrued_pct": (_base_level(by_code, "accrued_liab", base_levels) / sga0) if sga0 > 0 else 0.0,
        "other_liab_pct": (_base_level(by_code, "other_cl", base_levels) / sga0) if sga0 > 0 else 0.0,
    }

    # Working-capital days: the derived dso/dpo drivers, else the Y0-inverse
    # (seed_derivation), else 0.0 (degenerate zero-flow grid — no WC modeled).
    ar_driver, ar_params = _driver_of(by_code, "ar")
    if ar_driver == "dso":
        patch["ar_dso"] = float(ar_params.get("days", 0.0))  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
    else:
        patch["ar_dso"] = derive_dso(_base_level(by_code, "ar", base_levels), rev0) or 0.0
    ap_driver, ap_params = _driver_of(by_code, "ap")
    if ap_driver == "dpo":
        patch["ap_dpo"] = float(ap_params.get("days", 0.0))  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
    else:
        patch["ap_dpo"] = derive_dpo(_base_level(by_code, "ap", base_levels), cogs0) or 0.0

    # Tax rate: the taxes row's pct_ebit coefficient is the live grid assumption;
    # run_scenario_engine falls back to the stored driver_data tax_rate when the
    # row carries a different driver.
    tax_driver, tax_params = _driver_of(by_code, "taxes")
    if tax_driver == "pct_ebit":
        patch["tax_rate"] = float(tax_params.get("pct", 0.0))  # SA-OK: dict.get on in-memory driver params, NOT an ORM query

    # ONE synthesized capex driver from the ppe row's depr params, anchored to
    # the Y0 D&A run-rate: engine vintage-0 annual = beginning_gross/(life−1)
    # == y0_da, mirroring the grid's historical run-rate anchor convention.
    _ppe_driver, ppe_params = _driver_of(by_code, "ppe")
    useful_life = int(round(float(ppe_params.get("useful_life", 5.0) or 5.0)))  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
    useful_life = max(useful_life, 2)
    capex_drivers: list[EnginePayload] = [{
        "category": "grid_ppe",
        "useful_life_years": useful_life,
        "capex_pct_revenue": float(ppe_params.get("capex_pct_revenue", 0.0) or 0.0),  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
        "salvage_value_pct": 0.0,
        "beginning_gross": round(y0_da * (useful_life - 1), 2),
        "beginning_accum_depr": 0.0,
    }]

    return {
        "drivers_patch": patch,
        "capex_drivers": capex_drivers,
        "base_revenue": rev0,
        "ltm_ebitda": _row_y0(by_code, "ebitda"),
    }


def assert_interest_identity(forecast: EnginePayload) -> None:
    """ASSERT P&L interest ≡ Σ schedule interest, per year (AC-C2 leg 1).

    Both identities are checked: the P&L line equals the schedule's year total,
    AND the per-tranche rows sum to that total. Raises
    :class:`EngineReconciliationError` — never returned as a decorative flag.
    """
    schedule = forecast["debt_schedule"]["schedule"]  # type: ignore[index]
    for yr, pnl_yr in enumerate(forecast["pnl"]):  # type: ignore[arg-type]
        sched_yr = schedule[yr] if yr < len(schedule) else {}
        total = round(
            float(sched_yr.get("total_interest_cash", 0.0))  # SA-OK: dict.get on the in-memory schedule row, NOT an ORM query
            + float(sched_yr.get("total_interest_pik", 0.0)),
            2,
        )
        pnl_interest = float(pnl_yr.get("interest_expense", 0.0))  # SA-OK: dict.get on the in-memory pnl row, NOT an ORM query
        tranche_sum = round(
            sum(
                float(t.get("interest_cash", 0.0)) + float(t.get("interest_pik", 0.0))  # SA-OK: dict.get on in-memory tranche rows, NOT an ORM query
                for t in (sched_yr.get("tranches") or {}).values()
            ),
            2,
        )
        if abs(pnl_interest - total) > _INTEREST_IDENTITY_TOL or abs(tranche_sum - total) > _INTEREST_IDENTITY_TOL:
            raise EngineReconciliationError(
                f"P&L interest does not reconcile to the debt schedule in projected year {yr}: "
                f"pnl={pnl_interest:.2f} schedule_total={total:.2f} tranche_sum={tranche_sum:.2f} "
                f"(tolerance ${_INTEREST_IDENTITY_TOL:.2f}); refusing to return a disconnected model"
            )


def projection_opening_bs_from_levels(levels: dict) -> dict:
    """W3c (plan 20260719): map the band-rollup STATEMENT-code levels -> the opening-BS vocab
    compute_full_forecast consumes. The producer (compute_statement_subtotals) folds goodwill INTO the
    'intangibles' line and has no goodwill/other_assets slot (GRID_TEMPLATE), so 'goodwill'/'other_assets'
    are emitted 0.0 DELIBERATELY: that forces the forecast_engine goodwill reroute to open the separate
    goodwill line at 0 (goodwill rides inside intangibles) — dropping the 0.0 emit re-introduces the
    su-goodwill double-count and breaks A=L+E. Remaps: accrued_liab->accrued, ppe->net_fixed_assets,
    common_equity->equity_contributed, debt_std+debt_ltd surfaced as total_debt."""
    g = lambda k: float(levels.get(k) or 0.0)  # noqa: E731 · SA-OK: dict.get on the in-memory rollup levels, NOT an ORM query
    return {
        "cash": g("cash"), "ar": g("ar"), "ap": g("ap"),
        "other_ca": g("other_ca"), "other_cl": g("other_cl"),
        "accrued": g("accrued_liab"),
        "inventory": g("inventory"),
        "net_fixed_assets": g("ppe"),
        "intangibles": g("intangibles"),   # combined intangibles+goodwill (producer fold)
        "goodwill": 0.0,                    # LOAD-BEARING: keep 0.0 (folded into intangibles; avoids su double-count)
        "other_assets": 0.0,               # no producer slot
        "retained_earnings": g("retained_earnings"),
        "equity_contributed": g("common_equity"),
        "total_debt": g("total_debt"),
        # AC-2 (plan 20260720 live-verify): the std/ltd SPLIT rides through so the
        # display seam (grid_projection) can anchor the FIRST forecast column's
        # Δdebt rows at the SAME books-real opening the engine's debt re-base uses
        # — col 0 (At-Close) is the PF close stack, a different anchor entirely.
        "debt_std": g("debt_std"),
        "debt_ltd": g("debt_ltd"),
    }


def run_scenario_engine(
    scenario_id: int,
    db,
    grid_rows: list[GridRow] | None = None,
    base_levels_override: dict | None = None,
    num_years_override: int | None = None,
    suppress_stub: bool = False,
) -> EnginePayload:
    """THE one live-path engine run (AC-C2). See the module docstring for the seed
    contract. ``grid_rows`` lets recalc-v2 pass its in-memory grid; the read
    endpoints (/returns, /debt-schedule) load the persisted cells.

    Backtest overlay (AC-FRR-7, plan 20260720-full-history-realized-returns): the
    three overlay params default ``None``/``None``/``False`` — the live path is
    byte-identical (parity-goldened, tests/lbo/test_backtest.py). With
    ``base_levels_override`` (the ``{base_fy_year, base_fy_end, levels}``
    projection-seed shape — :func:`post_close_band.build_projection_seed`) EVERY
    meta-cell-coupled read keys off the OVERRIDE dict, not the persisted meta:
    the projection ``levels`` (seed + ``forecast_opening_bs`` + the in-engine
    debt re-base target) AND ``base_fy_year`` (the ``term_years`` maturity
    decrement below). ``num_years_override`` pins the horizon (a backtest pair
    scores exactly ONE forward year). ``suppress_stub=True`` forces
    ``stub_fraction`` ``None`` — the stub derives INTERNALLY from the meta cell,
    so a plain levels override cannot reach it, and a pair must score a complete
    un-prorated FY. The valuation firewall is untouched: ``ltm_ebitda`` stays a
    hard seq-0 read under any override (``derive_engine_seed``).

    Returns ``{"forecast", "drivers", "tranche_pairs", "num_years",
    "ltm_ebitda", "historical_bs"}`` — the forecast is the full
    ``compute_full_forecast`` result (pnl / bs / cf / debt_schedule /
    sources_uses / entry_equity / distributions), already interest-asserted.
    """
    scenario = db.query(LboScenario).filter(
        LboScenario.id == scenario_id  # CROSS-TENANT-OK: scenario_id is tenant-verified by _require_lbo_scenario / check_entity_tenant_access in every route before the composition runs (the file-wide pattern)
    ).first()
    company = None
    if scenario is not None:
        company = db.query(LboCompany).filter(
            LboCompany.id == scenario.lbo_company_id  # CROSS-TENANT-OK: company resolved from the tenant-authorized scenario
        ).first()
    num_years = (
        max(1, int(num_years_override))
        if num_years_override is not None
        else _scenario_horizon(company)
    )
    model_start_year = (
        int(company.model_start_year)
        if company is not None and company.model_start_year
        else _dt.date.today().year
    )

    if grid_rows is None:
        cells = (
            db.query(LboCell)
            .filter(LboCell.scenario_id == scenario_id, LboCell.row_index >= 0)  # CROSS-TENANT-OK: scenario_id is tenant-verified by the calling route; LboCell is scenario-scoped (the file-wide pattern, mirrors grid_helpers._get_historical_bs)
            .order_by(LboCell.row_index, LboCell.col_index)
            .all()
        )
        grid_rows = grid_rows_from_cells(cells, NUM_YEAR_COLS)

    drivers_row = db.query(LboDriver).filter(
        LboDriver.scenario_id == scenario_id  # CROSS-TENANT-OK: scenario_id is tenant-verified by the calling route (mirrors serializers._get_drivers_dict)
    ).first()
    dd = (
        drivers_row.driver_data
        if drivers_row is not None and drivers_row.driver_data
        else _default_driver_data()
    )

    drivers: EnginePayload = {}
    for key in DEAL_KEYS:
        val = dd.get(key)  # SA-OK: dict.get on the in-memory driver_data dict, NOT an ORM query
        if val is not None:
            drivers[key] = val
    # Goodwill book/tax lives (+ the pre-split legacy key) ride through so
    # engine_support.resolve_goodwill_lives sees the stored election.
    for key in ("goodwill_book_amort_years", "goodwill_tax_amort_years", "goodwill_amort_years"):
        val = dd.get(key)  # SA-OK: dict.get on the in-memory driver_data dict, NOT an ORM query
        if val is not None:
            drivers[key] = val

    # CONFIRMED S&U overrides merged OVER the stored drivers — identical to the
    # S&U tab's merge, so the entry-equity plug is one number everywhere.
    ov, tranche_ov = _load_su_overrides(scenario_id, db)
    if ov.get("enterprise_value") is not None:
        drivers["entry_enterprise_value"] = float(ov["enterprise_value"])  # type: ignore[arg-type]
    if ov.get("transaction_fees_pct") is not None:
        drivers["transaction_fees_pct"] = float(ov["transaction_fees_pct"])  # type: ignore[arg-type]

    # ── W3b (plan 20260719): load the projection seed (base_levels) from the grid_meta cell (row -1).
    # A dedicated query is required — grid_rows_from_cells drops col_index >= 100 (band invisible) and the
    # grid_rows load filters row_index >= 0 (:439), both excluding the meta cell (row_index -1, col_index 0).
    meta = db.query(LboCell).filter(
        LboCell.scenario_id == scenario_id,  # CROSS-TENANT-OK: scenario_id is tenant-verified by the calling route; LboCell is scenario-scoped (the file-wide pattern, mirrors the grid_rows load above)
        LboCell.row_index == -1, LboCell.col_index == 0,
    ).first()
    # AC-FRR-7 overlay: with ``base_levels_override`` active, _bl IS the override dict, so EVERY
    # downstream _bl read — the ['levels'] extraction (seed / forecast_opening_bs / debt re-base
    # target) AND the ['base_fy_year'] term-decrement anchor below — keys off the OVERRIDE; the
    # persisted meta base_levels is never consulted. None → the live meta path, byte-identical.
    if base_levels_override is not None:
        _bl = base_levels_override
    else:
        _bl = (meta.driver_params or {}).get("base_levels") if meta is not None else None  # SA-OK: dict.get on the in-memory JSON driver_params, NOT an ORM query
    # MUST extract ['levels'] — passing the whole {base_fy_year, base_fy_end, levels} dict makes every
    # _base_level read fall back to seq-0 (keys don't match) → the re-seed silently no-ops while A=L+E still
    # ties, so nothing screams (the contract-mismatch trap).
    base_levels = _bl.get("levels") if isinstance(_bl, dict) else None
    # W3d: the partial split-FY stub fraction f (seq-1), stashed at init; None → no stub → byte-parity.
    # AC-FRR-7: stub_fraction derives INTERNALLY from the meta cell — an override cannot reach it —
    # so ``suppress_stub=True`` forces it OFF (a backtest pair scores a complete, un-prorated FY);
    # False keeps the live meta read byte-identical.
    if suppress_stub:
        stub_fraction = None
    else:
        _sf = (meta.driver_params or {}).get("stub_fraction") if meta is not None else None  # SA-OK: dict.get on in-memory JSON driver_params
        # AC-4 (plan 20260720 Wave 5) — the overlay stub-LEAK state: an override run that
        # did NOT suppress the stub would prorate the OVERRIDE's seed by the PERSISTED
        # meta's stub_fraction (levels from one source, proration from another — a
        # silently mis-prorated year every internal tie still blesses). Refuse loudly;
        # the backtest route always passes suppress_stub=True, so its behavior is unchanged.
        if base_levels_override is not None and _sf is not None:
            raise ValueError(
                "base_levels_override requires suppress_stub=True when the persisted grid "
                "meta carries a stub_fraction: the override's seed levels would be prorated "
                "by the live meta's stub — an overlay run scores complete un-prorated fiscal "
                "years, never a cross-source mix"
            )
        stub_fraction = float(_sf) if _sf is not None else None

    seed = derive_engine_seed(grid_rows, num_years, base_levels=base_levels)

    # ── Wave 6 (D-CA-4 / AC-CA-8): the entry gate — a books-start deal needs BOTH bases ──
    # (1) a PRICING basis — the confirmed-APA / S&U-override entry_enterprise_value (EV = 0 ×
    #     multiple would price a zero-value deal silently), AND
    # (2) a PROJECTION base — a complete post-close fiscal year (D-W3-2 base_levels) or explicit
    #     entry operating assumptions (nonzero seq-0 revenue/EBITDA typed on the At-Close column).
    # An EV alone prices the ENTRY but projects NOTHING: running the engine off the honestly-empty
    # run-rate produced revenue-0 statements against real-sized debt tranches — phantom interest,
    # collapsing cash, every internal tie green (the 2026-07-20 live regression on the first Wave-6
    # deploy). AC-CA-8's contract is "forecast columns render only from a priced entry" — refuse the
    # run, never fabricate it. Scenarios without the flag (every pre-Wave-6 grid) are byte-identical.
    _entry_pricing = (meta.driver_params or {}).get("entry_pricing") if meta is not None else None  # SA-OK: dict.get on in-memory JSON driver_params
    if _entry_pricing == "unpriced_books_start":
        if drivers.get("entry_enterprise_value") in (None, "", 0):
            raise EntryPricingError(
                "cannot price the entry: the deal's books begin at consummation (no pre-close "
                "run-rate) — supply the acquisition agreement's enterprise value or entry "
                "assumptions on the Sources & Uses tab"
            )
        if base_levels is None and float(seed["base_revenue"]) == 0.0 and float(seed["ltm_ebitda"]) == 0.0:  # type: ignore[arg-type]
            raise EntryPricingError(
                "cannot project the deal: the books begin at consummation, so there is no run-rate "
                "to forecast from — enter entry operating assumptions (revenue/EBITDA on the "
                "At-Close column) or let a complete post-close fiscal year of actuals accrue (the "
                "model re-seeds automatically on the next import)"
            )
    drivers.update(seed["drivers_patch"])  # type: ignore[arg-type]
    if "tax_rate" not in drivers:
        stored_tax = dd.get("tax_rate")  # SA-OK: dict.get on the in-memory driver_data dict, NOT an ORM query
        if stored_tax is not None:
            drivers["tax_rate"] = float(stored_tax)

    # DL-5 seam (plan 20260717-three-statement-spine AC-3): the capex vintage
    # synthesized above (derive_engine_seed: beginning_gross = y0_da × (life−1))
    # is anchored to the FULL Y0 D&A run-rate, which historically INCLUDES the
    # intangible amortization also booked in D&A. Flag it so the engine's
    # C2-b intangible-amort driver re-anchors the vintage to the TANGIBLE-only
    # run-rate (engine_support.reanchor_capex_drivers_tangible_only) — otherwise
    # the intangible slice is expensed twice. The engine can't self-detect the
    # vintage's provenance; the composition, which owns the synthesis, must.
    drivers["capex_anchor_includes_intangible_amort"] = True

    tranche_pairs = _engine_tranche_pairs(scenario_id, db, tranche_ov, model_start_year)
    historical_bs = _get_historical_bs(scenario_id, db)

    # ── AC-1 (plan 20260720 live-verify round 3): SEASONED-loan forward roll. The FIRST
    # FORECAST calendar year — FYCY (base_fy_year + 1) in the re-seed regime, else
    # model_start_year — anchors the seasoned resolution: a dated pre-anchor tranche rolls
    # its OWN contractual schedule (books opening, own amort pace, bullet at the REAL
    # maturity year) instead of the model-start-anchored derivation. Undated tranches are
    # untouched (AC-2 parity).
    base_fy_year = int((_bl or {}).get("base_fy_year") or model_start_year)  # SA-OK: dict.get on the in-memory base_levels meta, NOT an ORM query
    first_forecast_year = (base_fy_year + 1) if base_levels is not None else model_start_year
    # AC-1 round 5: the remaining-term amortization divisor needs the anchor DATE (the
    # first forecast period's start — the day after the last actual month-end), not
    # just the year; derived from the same base_fy/stub meta this branch already reads
    # (_forecast_anchor_date documents the convention). Passing _bl only when the
    # levels extracted keeps the anchor regime aligned with first_forecast_year's.
    _anchor_date = _forecast_anchor_date(
        _bl if base_levels is not None else None, stub_fraction, first_forecast_year)
    _apply_seasoned_terms(tranche_pairs, first_forecast_year, _anchor_date)

    # ── W3c (plan 20260719): build the projection opening BS + re-base debt — ONLY in the re-seed regime
    # (base_levels present). None → forecast_opening_bs stays None → compute_full_forecast is byte-identical.
    forecast_opening_bs = None
    if base_levels is not None:
        forecast_opening_bs = projection_opening_bs_from_levels(base_levels)
        # The opening_balance debt RE-BASE (Σ == FY-real total_debt, CORR-3) now lives INSIDE
        # compute_full_forecast, so /returns AND the sensitivity grid (+ every per-cell scaled tranche) rebase
        # uniformly (no duplication). HERE we only decrement term_years — the MATURITY index (bullet fires at
        # yr==term-1), NOT amort_years — by the band years elapsed (CORR-4), so a tranche does not survive its
        # maturity band-years late; this needs band_years, which the engine has no context for. funded_amount
        # is UNCHANGED → compute_sources_uses/entry_equity firewall.
        band_years = max(0, base_fy_year - model_start_year)
        for _t, p in tranche_pairs:
            if p.get("seasoned"):  # SA-OK: dict.get on the in-memory engine payload, NOT an ORM query
                # AC-1: a seasoned term is already anchored to first_forecast_year —
                # band-decrementing it again would retire the loan band-years early.
                continue
            if p.get("term_years"):  # SA-OK: dict.get on the in-memory engine payload, NOT an ORM query
                _remaining = int(p["term_years"]) - band_years
                if _remaining >= 1:
                    p["term_years"] = _remaining
                else:
                    # AC-2 (plan 20260720 live-verify): the tranche's stated maturity
                    # already PASSED at/before the projection anchor (base_fy_year) —
                    # yet the band's FY-real BS (the re-base target this same regime
                    # anchors the opening to) still carries the balance. The old
                    # ``max(1, …)`` clamp forced term_years=1 → the maturity bullet
                    # fired at year 0 and retired the ENTIRE re-based stack in FYCY
                    # (the −$12.5M live defect: an invented catch-up paydown the
                    # credit docs no longer describe). A schedule that says matured
                    # while the books say outstanding is a DISCLOSED discrepancy,
                    # never a silent $0: hold the books-anchored balance (interest
                    # still accrues; a sweep-participating tranche may still pay
                    # down from real FCF) and let debt_schedule stamp the
                    # ``past_maturity_outstanding`` disclosure.
                    p["term_years"] = None
                    p["past_maturity_outstanding"] = True

    forecast = compute_full_forecast(
        drivers=drivers,
        payroll_drivers=[],
        capex_drivers=seed["capex_drivers"],  # type: ignore[arg-type]
        debt_tranches=[payload for _t, payload in tranche_pairs],
        num_years=num_years,
        ltm_ebitda=float(seed["ltm_ebitda"]),  # type: ignore[arg-type]
        historical_bs=historical_bs,
        base_revenue=float(seed["base_revenue"]),  # type: ignore[arg-type]
        forecast_opening_bs=forecast_opening_bs,   # W3c: FY-real opening (None => byte-parity)
        stub_fraction=stub_fraction,               # W3d: partial split-FY year-0 proration (None => byte-parity)
    )
    assert_interest_identity(forecast)

    return {
        "forecast": forecast,
        "drivers": drivers,
        "tranche_pairs": tranche_pairs,
        "num_years": num_years,
        "ltm_ebitda": seed["ltm_ebitda"],
        "historical_bs": historical_bs,
    }

"""
app/lbo/seed_derivation.py
──────────────────────────
F-B4e Phase 2 — pure history→driver DERIVATION functions + the provenance stamp.
C2-a (plan 20260717-three-statement-spine) extends the same inverses ACROSS N
historical periods (``derive_period_drivers`` / ``derive_forecast_defaults``).

Each function inverts the forward driver math (now in the engine seed/stamp) / ``working_capital``
to recover the FORWARD driver parameter from one period of QBO actuals:
  - ``derive_dso``/``derive_dpo``/``derive_dio`` invert ``balance = (base/365) × days``
    (working_capital.py:49; the forward WC driver math).
  - ``derive_capex`` = ``(net_ppe_now − net_ppe_prior) + depreciation`` (WS-2 — the QBO BS
    gives NET PP&E; ΔNet alone understates capex by the full depreciation load).
  - ``derive_effective_tax`` recovers the ``pct_ebit`` rate as ``taxes / pre_tax``.

Correctness contract (financial-modeling.md AP-FIN-09 metric-name fidelity + AP-FIN-03
never-fabricate; error-logging.md §5 no-silent-fallback): a derived driver must be
**RIGHT or ABSENT**. Every ratio guards its denominator — a ``<= 0``/absent denominator
returns ``None`` (the caller keeps the ``assumption`` default), NEVER a fabricated 0.
``derive_capex`` is signed (a disposal year is valid negative capex) and None only when
the PP&E endpoints are unavailable.

Pure: imports only the stdlib. No DB, no FastAPI, no QBO client — so the golden runs
conftest-free.
"""

# The provenance tags stamped on ``driver_params["source"]`` (AC-PROVENANCE). The later
# FE-badge phase reads this machine-readable tag. Three values, no migration —
# ``LboCell.driver_params`` is ``Column(JSON)`` (models.py:1757) so arbitrary keys persist.
SOURCE_QBO_ACTUAL = "qbo_actual"   # a Y0 LEVEL straight from QBO (e.g. the seeded AR balance)
SOURCE_DERIVED = "derived"         # a FORWARD driver derived from QBO history (a DSO, a tax rate)
SOURCE_ASSUMPTION = "assumption"   # a template/user default (never derived; never clobbered)

_VALID_SOURCES = frozenset({SOURCE_QBO_ACTUAL, SOURCE_DERIVED, SOURCE_ASSUMPTION})

# Days in the standard accounting year used by the DSO/DPO/DIO conventions. Named, not
# buried inline (AP-FIN-08): the forward engines use 365 (working_capital.py:49,
# the forward dso constant), so the inverse must use the same constant to round-trip.
DAYS_IN_YEAR = 365

# PLAN-EXEMPT: AC-4 LBO v2-path remediation (Owen work order 2026-06-29) — NAMED, overridable
# forward-IS assumption defaults (AP-FIN-08), used ONLY when the QBO actuals can't yield the
# ratio. Stamped source="assumption" (FE-editable; a re-seed never clobbers a user override) —
# a transparent assumption, never a fabricated actual (the empty-params alternative froze
# revenue flat and projected COGS/SG&A to ZERO — the v2 defect the AC fixed).
REVENUE_GROWTH_DEFAULT = 0.05   # 5% YoY when no prior-period revenue is available to derive growth
COGS_PCT_REVENUE_DEFAULT = 0.50  # COGS = 50% of revenue when Y0 revenue is non-positive (degenerate)
SGA_PCT_REVENUE_DEFAULT = 0.20   # SG&A = 20% of revenue when Y0 revenue is non-positive (degenerate)


def derive_dso(ar: float, revenue: float, days: int = DAYS_IN_YEAR) -> float | None:
    """Days Sales Outstanding from one period: ``DSO = (AR / Revenue) × days``.

    Inverts working_capital.py:49 ``ar = revenue × dso / 365``. Returns ``None`` when
    ``revenue <= 0`` (no fabricated 0-day DSO — the caller keeps the template default).
    """
    if revenue <= 0:
        return None
    return round(ar / revenue * days, 2)


def derive_dpo(ap: float, cogs: float, days: int = DAYS_IN_YEAR) -> float | None:
    """Days Payable Outstanding from one period: ``DPO = (AP / COGS) × days``.

    Inverts the forward dpo math ``ap = cogs × dpo / 365``. Returns ``None`` when
    ``cogs <= 0`` (no fabricated 0-day DPO).
    """
    if cogs <= 0:
        return None
    return round(ap / cogs * days, 2)


def derive_dio(inventory: float, cogs: float, days: int = DAYS_IN_YEAR) -> float | None:
    """Days Inventory Outstanding from one period: ``DIO = (Inventory / COGS) × days``.

    Same inverse as ``derive_dpo`` (the forward dpo math shares the cogs base for
    dpo + dio). Returns ``None`` when ``cogs <= 0`` (no fabricated 0-day DIO).
    """
    if cogs <= 0:
        return None
    return round(inventory / cogs * days, 2)


def derive_capex(
    net_ppe_now: float | None,
    net_ppe_prior: float | None,
    depreciation: float | None,
    intangibles_now: float | None = None,
    intangibles_prior: float | None = None,
) -> float | None:
    """Gross capital expenditure for the period from the roll-forward of NET PP&E.

    ``capex = (net_ppe_now − net_ppe_prior) + depreciation``  (WS-2).

    The QBO Balance Sheet reports NET PP&E (gross less accumulated depreciation), so the
    PP&E roll-forward is ``net_now = net_prior + capex − depreciation`` ⇒
    ``capex = Δnet_ppe + depreciation``. Deriving capex from ΔNet PP&E ALONE understates it
    by the full D&A load — the depreciation add-back is load-bearing. The result is SIGNED:
    a genuine disposal / contraction year is a valid NEGATIVE capex.

    AC-FRR-9b (amendment 2026-07-20) — the OPTIONAL intangibles-endpoints split, used ONLY by
    the CF-articulation call site (``compute_statement_subtotals``); the forecast-driver
    consumers (``derive_period_drivers`` / ``apply_derived_drivers``) never pass them and stay
    byte-identical. With BOTH endpoints given, the D&A line is split before the PP&E
    roll-forward — QBO's "Depreciation & Amortization" includes intangibles amortization,
    which the plain formula mis-attributes to PP&E capex (the live 2020 phantom-capex defect):

        amort_int = max(−Δintangibles, 0)        # amortization runs the intangibles DOWN
        depr_ppe  = depreciation − amort_int     # kept EXACT (may go negative on the
                                                 # impairment shape — the CALLER discloses it;
                                                 # clamping here would break the articulation
                                                 # identity derived_end ≡ ΔCash)
        capex     = ΔNetPP&E + depr_ppe + max(Δintangibles, 0)   # purchases are investing

    A zero Δintangibles makes the split a no-op (byte-identical to the plain formula).

    Returns ``None`` only when ALL THREE PP&E inputs are absent (``None``) — i.e. there is no
    PP&E signal at all, so the caller keeps the template assumption rather than fabricating
    a capex of 0. A missing prior BS (``net_ppe_prior is None``) means ΔNet PP&E is not
    derivable, so this returns ``None`` and the caller degrades to the assumption default
    (never a fabricated capex driver).
    """
    if net_ppe_now is None and net_ppe_prior is None and depreciation is None:
        return None
    # ΔNet PP&E requires both endpoints; a missing prior period makes capex underivable.
    if net_ppe_now is None or net_ppe_prior is None:
        return None
    depr = depreciation if depreciation is not None else 0.0
    if intangibles_now is not None and intangibles_prior is not None:
        delta_int = intangibles_now - intangibles_prior
        amort_int = max(-delta_int, 0.0)
        depr_ppe = depr - amort_int
        purchases = max(delta_int, 0.0)
        return round((net_ppe_now - net_ppe_prior) + depr_ppe + purchases, 2)
    return round((net_ppe_now - net_ppe_prior) + depr, 2)


def derive_effective_tax(taxes: float, pre_tax: float) -> float | None:
    """Effective tax RATE (the ``pct_ebit`` driver coefficient): ``taxes / pre_tax``.

    Inverts the ``pct_ebit`` projection ``taxes = max(0, ebit) × rate`` (the forward pct_ebit tax math).
    Returns ``None`` when ``pre_tax <= 0`` (a non-positive pre-tax base makes the rate
    undefined / meaningless — no fabricated 0% rate; the caller keeps the template default).
    Rounded to 4 dp to match the Y0 ``tax rate`` seed precision (lbo.py:1087).
    """
    if pre_tax <= 0:
        return None
    return round(taxes / pre_tax, 4)


# ── C2-a (plan 20260717-three-statement-spine AC-2/AC-4): per-period drivers ──
# Basis labels (AC-3 "labeled fallbacks"): every forecast default carries
# {value, source, basis, periods_used} so a consumer SEES what it stands on.
BASIS_3YR_AVG = "3yr_avg"                     # >= 3 historical fiscal years contributed
BASIS_AVAILABLE_AVG = "available_period_avg"  # fewer than 3 FYs — the honest-degrade label
BASIS_HOLD_LAST = "hold_last_year"            # IS ratios: latest (LTM) actual held constant
BASIS_LTM_SINGLE = "ltm_single_period"        # empty FY pool — the LTM-only derivation


def _lvl(levels: dict | None, code: str) -> float | None:
    """A named level from one period's {line_code: value} map; None when absent/non-numeric."""
    if not isinstance(levels, dict):
        return None
    v = levels.get(code)  # SA-OK: dict.get on an in-memory period-levels map, NOT an ORM query
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def derive_period_drivers(period_levels: list[dict]) -> list[dict]:
    """AC-2: the driver vector PER historical period (growth, margins, DSO on revenue,
    DIO/DPO on COGS, capex %, effective tax) from chronological statement columns
    (oldest → newest, LTM last). Every metric is RIGHT or ABSENT (None) — the first
    column has no prior, so its growth/capex are honestly None, never 0. Entries are
    provenance-tagged ``derived`` (ratios of qbo_actual levels). Pure.
    """
    out: list[dict] = []
    prior: dict | None = None
    for levels in period_levels:
        revenue, cogs, sga = _lvl(levels, "revenue"), _lvl(levels, "cogs"), _lvl(levels, "sga")
        depr, taxes = _lvl(levels, "depreciation"), _lvl(levels, "taxes")
        ar, ap, inv, ppe = _lvl(levels, "ar"), _lvl(levels, "ap"), _lvl(levels, "inventory"), _lvl(levels, "ppe")
        prior_rev = _lvl(prior, "revenue")
        entry: dict = {"provenance": SOURCE_DERIVED}
        entry["growth"] = (
            round(revenue / prior_rev - 1.0, 6)
            if revenue is not None and prior_rev is not None and prior_rev > 0 else None
        )
        pos_rev = revenue is not None and revenue > 0
        entry["gross_margin"] = round((revenue - (cogs or 0.0)) / revenue, 6) if pos_rev else None
        entry["ebitda_margin"] = round((revenue - (cogs or 0.0) - (sga or 0.0)) / revenue, 6) if pos_rev else None
        entry["cogs_pct"] = round(cogs / revenue, 6) if (pos_rev and cogs is not None) else None
        entry["sga_pct"] = round(sga / revenue, 6) if (pos_rev and sga is not None) else None
        entry["dso"] = derive_dso(ar, revenue) if (ar is not None and revenue is not None) else None
        entry["dpo"] = derive_dpo(ap, cogs) if (ap is not None and cogs is not None) else None
        entry["dio"] = derive_dio(inv, cogs) if (inv is not None and cogs is not None) else None
        capex = derive_capex(ppe, _lvl(prior, "ppe"), depr)
        entry["capex"] = capex
        entry["capex_pct"] = round(capex / revenue, 6) if (capex is not None and pos_rev) else None
        pre_tax = (revenue - cogs - (sga or 0.0) - (depr or 0.0)) if (revenue is not None and cogs is not None) else None
        entry["effective_tax"] = (
            derive_effective_tax(taxes, pre_tax) if (taxes is not None and pre_tax is not None) else None
        )
        out.append(entry)
        prior = levels
    return out


def derive_forecast_defaults(period_drivers: list[dict]) -> dict:
    """DEC-LBO3S-3-as-amended forecast defaults from the per-period vector.

    Days ratios (dso/dpo/dio) = average over the HISTORICAL FISCAL-YEAR entries (the
    vector minus its final LTM entry) — basis ``3yr_avg`` when >= 3 FYs contributed,
    else the labeled ``available_period_avg``. ``capex_pct`` joins the averaging family
    (single-year capex is lumpy — decision recorded per AC-2). IS ratios (growth,
    cogs%, sga%, effective tax) = hold-last-year (the LTM entry). NO fade defaults —
    fades are manual overrides only. An empty FY pool degrades to the labeled LTM
    single period, else the key is absent (nothing fabricated).
    """
    if not period_drivers:
        return {}
    fy_entries, last = period_drivers[:-1], period_drivers[-1]
    out: dict = {}
    for key, nd in (("dso", 2), ("dpo", 2), ("dio", 2), ("capex_pct", 6)):
        pool = [e[key] for e in fy_entries if e.get(key) is not None]
        if pool:
            out[key] = {
                "value": round(sum(pool) / len(pool), nd), "source": SOURCE_DERIVED,
                "basis": BASIS_3YR_AVG if len(pool) >= 3 else BASIS_AVAILABLE_AVG,
                "periods_used": len(pool),
            }
        elif last.get(key) is not None:
            out[key] = {"value": last[key], "source": SOURCE_DERIVED,
                        "basis": BASIS_LTM_SINGLE, "periods_used": 1}
    for key in ("growth", "cogs_pct", "sga_pct", "effective_tax"):
        if last.get(key) is not None:
            out[key] = {"value": last[key], "source": SOURCE_DERIVED,
                        "basis": BASIS_HOLD_LAST, "periods_used": 1}
    return out


# Fit-source tags (live-verify remediation 2026-07-21, plan 20260720 amendment): WHICH
# history the forecast defaults were fitted over. Pre-close = the C2-a historical FY
# columns + LTM (the original path, unchanged). Post-close = the band's COMPLETE fiscal
# years — the books-start path, the same D-W3-2 ground truth the projection re-seeds from.
FIT_SOURCE_PRE_CLOSE = "pre_close_history"
FIT_SOURCE_POST_CLOSE = "post_close_band"


def derive_forecast_defaults_full(period_drivers: list[dict]) -> dict:
    """Post-close-actuals variant of :func:`derive_forecast_defaults` (the books-start fit).

    Every entry here is a COMPLETE fiscal year (a band rollup) — there is NO trailing-LTM
    tail to exclude — so the days ratios (+ capex%) average over ALL entries and the IS
    ratios hold the NEWEST year. Implemented through the ONE defaults derivation with the
    newest FY standing in as its own "last" entry: ``fy_entries`` is then exactly the full
    vector (nothing double-counted — the duplicate is sliced off by ``[:-1]``) and the
    hold-last reads the newest FY. Empty vector → ``{}`` (nothing fabricated).
    """
    if not period_drivers:
        return {}
    return derive_forecast_defaults(period_drivers + [period_drivers[-1]])


def stamp_source(driver_params: dict, source: str) -> dict:
    """Stamp ``driver_params["source"] = source`` IN PLACE and return it (AC-PROVENANCE).

    ``source`` must be one of the three provenance tags; an unknown tag is a programming
    error and raises (fail-loud — a silently-wrong provenance tag would mislead the FE
    badge). Mutates + returns so a caller can ``row["driver_params"] = stamp_source({...}, ...)``
    in one expression.
    """
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"unknown provenance source {source!r}; expected one of {sorted(_VALID_SOURCES)}"
        )
    driver_params["source"] = source
    return driver_params


# ── The grid-apply orchestrator (derive forward drivers + stamp provenance) ────
# Pure: operates on the in-memory grid-row dicts + the derivation fns above. No DB,
# no QBO client — so the golden runs conftest-free (it must NOT import a Session).

# Sources a re-seed MAY overwrite (WS-6): a default. A user-edited / already-derived
# driver carries a different source and is never clobbered.
_OVERWRITABLE_SOURCES = frozenset({None, SOURCE_ASSUMPTION})
# Structural rows carry no derivable "value" provenance.
_STRUCTURAL_ROW_TYPES = frozenset({"formula", "subtotal", "header", "blank", "pct"})


def _y0_level(by_code: dict, code: str) -> float | None:
    """The Y0 (actuals) value of a recognized category row, or None if absent.

    Pure dict access (``by_code`` is an in-memory {line_code: row} map, NOT a DB session).
    """
    if code not in by_code:
        return None
    row = by_code[code]
    vals = row["values"] if "values" in row else []
    return float(vals[0]) if vals and vals[0] is not None else None


# PLAN-EXEMPT: AC-4 LBO v2-path remediation (Owen work order 2026-06-29) — forward-IS driver setter.
def _set_overwritable_driver(
    by_code: dict,
    code: str,
    driver_type: str,
    params: dict,
    source: str,
    applied: dict,
) -> None:
    """Set a row's forward driver + params + provenance, but ONLY if the row's current driver
    is still a default (source None/assumption) — never clobber a user-edited / already-derived
    driver (WS-6). Mutates the row in place and records into ``applied``.
    """
    row = by_code.get(code)
    if row is None:
        return
    dp = row["driver_params"] if "driver_params" in row else None
    existing = dp if isinstance(dp, dict) else {}
    src = existing["source"] if "source" in existing else None
    if src not in _OVERWRITABLE_SOURCES:
        return
    row["driver_type"] = driver_type
    row["driver_params"] = stamp_source(dict(params), source)
    applied[code] = driver_type


# PLAN-EXEMPT: AC-4 LBO v2-path remediation (Owen work order 2026-06-29) — forward IS drivers.
def apply_derived_drivers(
    grid_rows: list,
    net_ppe_prior: float | None,
    prior_revenue: float | None = None,
    period_levels: list[dict] | None = None,
    post_close_fy_levels: list[dict] | None = None,
    post_close_fy_years: list[int] | None = None,
) -> dict:
    """Derive forward driver params from the grid's Y0 QBO levels (+ a prior-period NET
    PP&E for capex, + a prior-period revenue for growth) and SET them on the PARENT category
    rows; Phase-1 ``parent_share`` children inherit via the rollup. Stamp
    ``driver_params["source"]`` on every value row (AC-PROVENANCE). DEFAULT-ONLY (WS-6): a row
    whose driver is already user/derived is NEVER overwritten. PURE — mutates ``grid_rows`` in
    place, returns a summary dict.

    AC-4 (forward IS drivers): revenue grows (``yoy`` — derived from prior-period revenue when
    available, else the ``REVENUE_GROWTH_DEFAULT`` assumption) and cogs/sga track revenue
    (``pct_revenue`` — derived from the Y0 ratio when revenue > 0, else the named assumption).
    Without these the template's empty params leave revenue flat and COGS/SG&A at ZERO.

    C2-a (AC-2, DEC-LBO3S-3-as-amended): ``period_levels`` = chronological historical
    statement columns (oldest → newest, LTM LAST). With >= 2 columns the days drivers
    (+ capex%) default to the FY average and growth to hold-last-year (cogs/sga/tax
    hold-last-year == the unchanged Y0-ratio derivation below), and the summary also
    carries ``period_drivers`` + ``forecast_defaults``. Absent/short ``period_levels``
    leaves behavior byte-identical to before.

    Books-start fallback (live-verify remediation 2026-07-21, plan 20260720 amendment):
    ``post_close_fy_levels`` = the band's COMPLETE fiscal-year rollup levels (chronological,
    oldest → newest — the same D-W3-2 ground truth the projection re-seeds from). Consulted
    ONLY when ``period_levels`` is absent/short (pre-close history always wins — byte-parity):
    the days ratios average over ALL entries (:func:`derive_forecast_defaults_full` — no LTM
    tail), the IS ratios hold the newest FY, and ``post_close_fy_years`` (when aligned)
    stamps ``fit_window_fy_years`` for the backtest's explicit fit-window doorway. The
    summary then carries ``fit_source == FIT_SOURCE_POST_CLOSE``. Finally, a template days
    row ("dso"/"dpo"/"dio") that received NO fit is DEMOTED to "auto" — the engine seam
    reads a days row's ``params["days"]`` with a 0.0 default, so an empty-params days row
    silently projects AR/AP/inventory to a fabricated $0 (the Collective-RCM books-start
    live defect); "auto" makes the engine derive the days from its projection base levels
    (right-or-absent), never a silent 0-days driver.
    """
    by_code: dict = {}
    for r in grid_rows:
        lc = r["line_code"] if "line_code" in r else None
        if lc:
            by_code[lc] = r

    revenue = _y0_level(by_code, "revenue")
    cogs = _y0_level(by_code, "cogs")
    sga = _y0_level(by_code, "sga")
    depreciation = _y0_level(by_code, "depreciation")
    taxes = _y0_level(by_code, "taxes")
    ar = _y0_level(by_code, "ar")
    ap = _y0_level(by_code, "ap")
    inventory = _y0_level(by_code, "inventory")
    ppe = _y0_level(by_code, "ppe")

    # EBIT (pre-tax) from the Y0 levels: (revenue − cogs) − (sga + depreciation).
    pre_tax: float | None = None
    if revenue is not None and cogs is not None:
        pre_tax = revenue - cogs - (sga or 0.0) - (depreciation or 0.0)

    capex = derive_capex(ppe, net_ppe_prior, depreciation)

    # C2-a: the multi-period forecast defaults (only with >= 2 chronological columns).
    # Books-start (2026-07-21): with NO pre-close history the fit falls back to the
    # POST-CLOSE band's complete fiscal years — real actuals, never the $0 entry column.
    period_driver_vector: list[dict] = []
    fc_defaults: dict = {}
    fit_source: str | None = None
    if period_levels and len(period_levels) >= 2:
        period_driver_vector = derive_period_drivers(period_levels)
        fc_defaults = derive_forecast_defaults(period_driver_vector)
        fit_source = FIT_SOURCE_PRE_CLOSE
    elif post_close_fy_levels:
        period_driver_vector = derive_period_drivers(post_close_fy_levels)
        fc_defaults = derive_forecast_defaults_full(period_driver_vector)
        fit_source = FIT_SOURCE_POST_CLOSE
        if post_close_fy_years and len(post_close_fy_years) == len(period_driver_vector):
            # Backtest fit-window metadata (backtest.fit_window_for_driver's explicit
            # ``fit_window_fy_years`` doorway): the averaging family records the FY years
            # whose entry contributed; the hold-last family records the newest FY.
            for _key, _d in fc_defaults.items():
                if _key in ("dso", "dpo", "dio", "capex_pct"):
                    _yrs = [
                        y for y, e in zip(post_close_fy_years, period_driver_vector)
                        if e.get(_key) is not None  # SA-OK: dict.get on in-memory driver entries, NOT an ORM query
                    ]
                else:
                    _yrs = post_close_fy_years[-1:]
                if _yrs:
                    _d["fit_window_fy_years"] = _yrs

    def _hist_params(key: str, value_key: str) -> dict | None:
        """A stamped-params dict from a forecast default (labeled basis), or None."""
        d = fc_defaults.get(key)  # SA-OK: dict.get on the in-memory defaults map, NOT an ORM query
        if not isinstance(d, dict) or d.get("value") is None:  # SA-OK: dict.get on the in-memory defaults map, NOT an ORM query
            return None
        out = {value_key: d["value"], "basis": d["basis"], "periods_used": d["periods_used"]}
        if "fit_window_fy_years" in d:
            out["fit_window_fy_years"] = d["fit_window_fy_years"]
        return out

    # (line_code, driver_type, params) plan; each derivation guards its own inputs (→ None).
    # Days drivers prefer the labeled multi-period default (AC-2); the single-Y0 inversion
    # is the unchanged fallback (and the ONLY path when no historical columns exist).
    plan: list = []
    for code, key, bal, base, invert in (
        ("ar", "dso", ar, revenue, derive_dso),
        ("ap", "dpo", ap, cogs, derive_dpo),
        ("inventory", "dio", inventory, cogs, derive_dio),
    ):
        hist = _hist_params(key, "days")
        if hist is not None:
            plan.append((code, key, hist))
        elif bal is not None and base is not None:
            days = invert(bal, base)
            if days is not None:
                plan.append((code, key, {"days": days}))
    # capex → the ``depr`` driver: capex_pct_revenue = capex/revenue, useful_life = ppe/depr
    # (the forward depr driver depr branch consumes those two keys, NOT a raw capex$).
    capex_hist = _hist_params("capex_pct", "capex_pct_revenue")
    if capex_hist is not None and depreciation and depreciation > 0 and ppe is not None:
        capex_hist["useful_life"] = round(ppe / depreciation, 2)
        plan.append(("ppe", "depr", capex_hist))
    elif capex_hist is not None and fit_source == FIT_SOURCE_POST_CLOSE and post_close_fy_levels:
        # Books-start: the seq-0 entry column has no D&A/PP&E to anchor useful_life —
        # anchor it to the NEWEST band FY (the same levels the capex% fit consumed).
        _fy_depr = _lvl(post_close_fy_levels[-1], "depreciation")
        _fy_ppe = _lvl(post_close_fy_levels[-1], "ppe")
        if _fy_depr and _fy_depr > 0 and _fy_ppe is not None:
            capex_hist["useful_life"] = round(_fy_ppe / _fy_depr, 2)
            plan.append(("ppe", "depr", capex_hist))
    elif (
        capex is not None and revenue and revenue > 0
        and depreciation and depreciation > 0 and ppe is not None
    ):
        plan.append(("ppe", "depr", {
            "capex_pct_revenue": round(capex / revenue, 6),
            "useful_life": round(ppe / depreciation, 2),
        }))
    eff_tax = (
        derive_effective_tax(taxes, pre_tax)
        if (taxes is not None and pre_tax is not None) else None
    )
    if eff_tax is not None:
        plan.append(("taxes", "pct_ebit", {"pct": eff_tax}))
    elif fit_source == FIT_SOURCE_POST_CLOSE:
        # Books-start: the $0 entry P&L makes the Y0 rate underivable — hold the newest
        # band FY's real effective rate (labeled), never a silent template default.
        tax_hist = _hist_params("effective_tax", "pct")
        if tax_hist is not None:
            plan.append(("taxes", "pct_ebit", tax_hist))

    applied: dict = {}
    for code, driver_type, params in plan:
        if code not in by_code:
            continue
        row = by_code[code]
        dp = row["driver_params"] if "driver_params" in row else None
        existing = dp if isinstance(dp, dict) else {}
        src = existing["source"] if "source" in existing else None
        # WS-6: only overwrite a still-default driver, never a user/derived one.
        if src not in _OVERWRITABLE_SOURCES:
            continue
        row["driver_type"] = driver_type
        row["driver_params"] = stamp_source(dict(params), SOURCE_DERIVED)
        applied[code] = driver_type

    # PLAN-EXEMPT: AC-4 — forward Income-Statement drivers. revenue grows (yoy), cogs/sga track
    # revenue (pct_revenue). Derived from QBO actuals where the inputs allow; else a NAMED,
    # overridable assumption default (stamped "assumption") — never a fabricated 0 that freezes
    # revenue / zeroes COGS/SG&A. _set_overwritable_driver respects WS-6 (never clobber a user edit).
    growth_hist = _hist_params("growth", "growth_rate")
    if growth_hist is not None:
        # AC-2 hold-last-year: the latest observed YoY (LTM vs the newest FY), labeled.
        _set_overwritable_driver(by_code, "revenue", "yoy", growth_hist, SOURCE_DERIVED, applied)
    elif prior_revenue is not None and prior_revenue > 0 and revenue is not None:
        growth = round(revenue / prior_revenue - 1.0, 6)
        _set_overwritable_driver(by_code, "revenue", "yoy", {"growth_rate": growth}, SOURCE_DERIVED, applied)
    else:
        _set_overwritable_driver(by_code, "revenue", "yoy", {"growth_rate": REVENUE_GROWTH_DEFAULT}, SOURCE_ASSUMPTION, applied)

    # Books-start (2026-07-21): the hold-last band-FY ratio is a REAL actual — it beats the
    # canned 50%/20% assumption whenever the $0 entry P&L makes the Y0 ratio underivable.
    cogs_hist = _hist_params("cogs_pct", "pct") if fit_source == FIT_SOURCE_POST_CLOSE else None
    sga_hist = _hist_params("sga_pct", "pct") if fit_source == FIT_SOURCE_POST_CLOSE else None
    if revenue is not None and revenue > 0 and cogs is not None:
        _set_overwritable_driver(by_code, "cogs", "pct_revenue", {"pct": round(cogs / revenue, 6)}, SOURCE_DERIVED, applied)
    elif cogs_hist is not None:
        _set_overwritable_driver(by_code, "cogs", "pct_revenue", cogs_hist, SOURCE_DERIVED, applied)
    else:
        _set_overwritable_driver(by_code, "cogs", "pct_revenue", {"pct": COGS_PCT_REVENUE_DEFAULT}, SOURCE_ASSUMPTION, applied)

    if revenue is not None and revenue > 0 and sga is not None:
        _set_overwritable_driver(by_code, "sga", "pct_revenue", {"pct": round(sga / revenue, 6)}, SOURCE_DERIVED, applied)
    elif sga_hist is not None:
        _set_overwritable_driver(by_code, "sga", "pct_revenue", sga_hist, SOURCE_DERIVED, applied)
    else:
        _set_overwritable_driver(by_code, "sga", "pct_revenue", {"pct": SGA_PCT_REVENUE_DEFAULT}, SOURCE_ASSUMPTION, applied)

    # PLAN-EXEMPT: LBO field-bug wave-2 (Owen 2026-06-29) — every remaining BS input gets an
    # explicit driver: operating WC lines scale with revenue (pct_revenue from the Y0 ratio),
    # structural lines (Intangibles/Goodwill, Paid-in Equity) hold fixed at Y0. The derived
    # ar/inventory/ppe/ap stay untouched; debt rides the schedule override; RE rolls with NI.
    # PLAN-EXEMPT: wave-3 — other_income/other_expense (below-EBITDA, non-operating) likewise
    # scale with revenue (pct_revenue from the Y0 ratio).
    for wc_code in ("other_ca", "other_cl", "accrued_liab", "other_income", "other_expense"):
        lvl = _y0_level(by_code, wc_code)
        if lvl is None:
            continue
        if revenue and revenue > 0:
            _set_overwritable_driver(by_code, wc_code, "pct_revenue", {"pct": round(lvl / revenue, 6)}, SOURCE_DERIVED, applied)
        else:
            _set_overwritable_driver(by_code, wc_code, "fixed", {"amount": lvl}, SOURCE_ASSUMPTION, applied)
    for fixed_code in ("intangibles", "common_equity"):
        lvl = _y0_level(by_code, fixed_code)
        if lvl is not None:
            _set_overwritable_driver(by_code, fixed_code, "fixed", {"amount": lvl}, SOURCE_ASSUMPTION, applied)

    # Live-verify remediation (2026-07-21): NEVER leave a template days-driver row with EMPTY
    # params. The engine seam (engine_composition.derive_engine_seed) reads a "dso"/"dpo" row's
    # ``params["days"]`` with a 0.0 default, so a days-typed row that received no fit projects
    # AR/AP/inventory to a fabricated $0 every forecast year (the Collective-RCM books-start
    # defect). Demote the unfitted row to "auto": the engine then derives the days from its
    # projection base levels (the D-W3-2 re-seed ground truth — a real fit), or the run is
    # refused upstream (EntryPricingError) when no base exists. Right-or-absent, never 0-days.
    for code, days_type in (("ar", "dso"), ("ap", "dpo"), ("inventory", "dio")):
        row = by_code.get(code)  # SA-OK: dict.get on the in-memory code map, NOT an ORM query
        if row is None or str(row.get("driver_type") or "") != days_type:  # SA-OK: dict.get on an in-memory grid row, NOT an ORM query
            continue
        dp_days = row["driver_params"] if "driver_params" in row else None
        existing_days = dp_days if isinstance(dp_days, dict) else {}
        if existing_days.get("days") is not None:  # SA-OK: dict.get on in-memory driver params, NOT an ORM query
            continue  # a real fitted/typed days parameter stands
        if (existing_days["source"] if "source" in existing_days else None) not in _OVERWRITABLE_SOURCES:
            continue  # WS-6: a user-owned driver is never demoted
        row["driver_type"] = "auto"

    # AC-PROVENANCE: stamp every non-structural row lacking a source — a QBO-sourced row
    # (a per-account child, or a category whose Y0 came from QBO) → qbo_actual; else assumption.
    for row in grid_rows:
        rtype = row["row_type"] if "row_type" in row else "input"
        if rtype in _STRUCTURAL_ROW_TYPES:
            continue
        if "driver_params" not in row or not isinstance(row["driver_params"], dict):
            row["driver_params"] = {}
        params = row["driver_params"]
        psrc = params["source"] if "source" in params else None
        if psrc:
            continue
        lc = row["line_code"] if ("line_code" in row and row["line_code"]) else ""
        qid = row["qbo_account_id"] if "qbo_account_id" in row else None
        from_qbo = bool(qid) or _y0_level(by_code, lc) is not None
        stamp_source(params, SOURCE_QBO_ACTUAL if from_qbo else SOURCE_ASSUMPTION)

    summary: dict = {"derived": applied, "net_ppe_prior": net_ppe_prior}
    if period_driver_vector:
        # C2-a (AC-2): expose the per-period vector + the labeled defaults so the caller
        # can persist them (grid meta) — keys ABSENT on the no-history path (the existing
        # exact-dict golden contract stays byte-identical). fit_source is stamped ONLY on
        # the books-start band fit (the pre-close summary shape stays byte-identical).
        summary["period_drivers"] = period_driver_vector
        summary["forecast_defaults"] = fc_defaults
        if fit_source == FIT_SOURCE_POST_CLOSE:
            summary["fit_source"] = fit_source
    return summary


def _report_value(col: dict) -> float:
    """Parse a QBO ColData cell's numeric ``value`` (str or num); 0.0 on absent/non-numeric."""
    raw = col["value"] if "value" in col else 0
    try:
        return float(raw or 0.0)
    except (ValueError, TypeError):
        return 0.0


def extract_net_ppe(tb_report: dict, mappings: dict) -> float | None:
    """Sum NET PP&E from a QBO TrialBalance report — the accounts mapped to ``ppe`` — as the
    prior-period anchor for ``capex = ΔNetPP&E + depreciation``. Returns ``None`` when no ppe
    account is present (so capex degrades to the assumption default, never a fabricated 0).
    PURE: parses the report dict; no DB / QBO client (golden-testable conftest-free).
    """
    container = tb_report["Rows"] if isinstance(tb_report, dict) and "Rows" in tb_report else {}
    rows = container["Row"] if isinstance(container, dict) and "Row" in container else []
    total = 0.0
    seen = False
    for row in rows:
        cols = row["ColData"] if isinstance(row, dict) and "ColData" in row else []
        if len(cols) < 3:
            continue
        acct_id = cols[0]["id"] if "id" in cols[0] else None
        if acct_id is None:
            continue
        mcat = mappings[str(acct_id)] if str(acct_id) in mappings else None
        if mcat != "ppe":
            continue
        total += abs(_report_value(cols[1]) - _report_value(cols[2]))
        seen = True
    return round(total, 2) if seen else None

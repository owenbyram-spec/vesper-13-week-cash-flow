"""
app/lbo/engine_support.py
─────────────────────────
Pure helpers backing the 3-statement forecast engine (Phase 2A CFO-grade extension,
plan 20260706-cfo-grade-financial-workflows AC-2A.1). Split out of forecast_engine.py
along concern boundaries (code-quality file-size discipline): assumption resolution,
opening-BS normalization, add-back provenance parsing, sweep-term construction, the
covenant fixed-add-back applier, and the fail-loud balance-sheet assertion.

Everything here is PURE (no DB, no Pydantic) so each helper is golden-testable
conftest-free (tests/test_lbo_cfo_grade.py). The opaque driver / balance-sheet /
tranche payloads follow the waterfall_solve.py precedent: ``dict[str, object]``
(the engine forwards them; it never trusts their inner shape).
"""
from __future__ import annotations

# The opaque engine payloads (drivers / historical BS / tranche dicts / add-back lines) —
# same convention as waterfall_solve.DebtTranche.
EnginePayload = dict[str, object]

# A = L + E is an identity the engine constructs; every line is rounded to cents, so
# accumulated rounding across ~12 BS lines stays well under a dollar. Anything above
# the tolerance is a genuine articulation regression, never noise.
BS_CHECK_TOLERANCE = 1.0


class BalanceSheetIntegrityError(ValueError):
    """The projected balance sheet failed A = L + E beyond tolerance.

    AC-2A.1 (RD-1 row 9): ``bs_check`` was previously returned-but-never-asserted —
    decorative. A model whose sheet does not tie is a fabricated model; fail loud,
    never ship the unbalanced grid (financial-modeling-correctness §5).
    """


def assert_bs_check(bs_check: float, year_index: int, period_label: str | None = None) -> None:
    """Raise :class:`BalanceSheetIntegrityError` when |bs_check| exceeds tolerance.

    ``period_label`` (C2-b, plan 20260717-three-statement-spine AC-5) names a
    non-projected column — a fed historical period — so the failure message says
    WHICH spine column failed. Default (None) keeps the pre-existing
    projected-year wording for every existing caller.
    """
    if abs(bs_check) > BS_CHECK_TOLERANCE:
        where = period_label if period_label is not None else f"projected year {year_index}"
        raise BalanceSheetIntegrityError(
            f"balance sheet does not tie in {where}: "
            f"assets - (liabilities + equity) = {bs_check:.2f} "
            f"(tolerance ${BS_CHECK_TOLERANCE:.2f}); refusing to return an unbalanced model"
        )


# Long-form historical-BS keys → the short keys compute_working_capital reads.
# "inventory" is its own key on both sides (C2-b DIO driver): it passes through
# so the DIO ΔNWC year-0 delta is measured against the REAL opening inventory.
_OPENING_WC_ALIASES = {
    "ar": "accounts_receivable",
    "ap": "accounts_payable",
    "accrued": "accrued_expenses",
    "other_ca": "other_current_assets",
    "other_cl": "other_current_liabilities",
    "inventory": "inventory",
}


def normalize_opening_wc(historical_bs: EnginePayload | None) -> dict[str, float] | None:
    """Map a historical BS (long-form or aliased keys) to the WC opening-balance keys.

    ``compute_working_capital`` reads {ar, ap, other_ca, accrued, other_cl}. The grid
    extractor (_get_historical_bs) mirrors those aliases, but a caller passing only the
    long-form keys previously seeded opening WC at 0 — year-0 ΔNWC was then measured
    against zero instead of the real opening balances, breaking cash articulation
    (part of the pre-fix bs_check drift). Explicit short keys win over long-form.
    """
    if not historical_bs:
        return None
    out: dict[str, float] = {}
    for short, long_form in _OPENING_WC_ALIASES.items():
        if short in historical_bs:
            out[short] = float(historical_bs[short] or 0)  # SA-OK: bounded-loop 5 in-memory dict lookups, not a DB query
        elif long_form in historical_bs:
            out[short] = float(historical_bs[long_form] or 0)
    return out or None


def static_carry_lines(
    historical_bs: EnginePayload | None, capex_drivers: list[EnginePayload]
) -> dict[str, float]:
    """Opening asset lines the projection carries STATIC (no driver models them).

    The PF opening BS (goodwill plug) includes inventory, intangibles, and the full
    opening net PP&E — but the projected sheet previously dropped inventory and
    intangibles entirely and only carried the PP&E the capex drivers seed
    (beginning_gross − beginning_accum). Every dropped opening line was a permanent
    A = L + E gap in every projected year (the verified bs_check defect, RD-1 row 9).

    These are carried flat — an honest disclosed simplification (no inventory-days or
    intangible-amortization driver exists), mirroring the existing static
    ``other_assets`` carry. ``unmodeled_fixed_assets`` is the SIGNED difference
    between the opening net PP&E and what the capex schedule starts with, so the
    identity holds whether the capex drivers under- or over-cover the opening base.
    """
    hbs = historical_bs or {}
    inventory = round(float(hbs.get("inventory", 0) or 0), 2)
    intangibles = round(float(hbs.get("intangibles", 0) or 0), 2)
    opening_net_ppe = float(
        hbs.get(
            "net_fixed_assets",
            float(hbs.get("gross_fixed_assets", 0) or 0)
            - float(hbs.get("accumulated_depreciation", 0) or 0),
        )
        or 0
    )
    capex_opening_net = sum(
        float(d.get("beginning_gross", 0) or 0) - float(d.get("beginning_accum_depr", 0) or 0)  # SA-OK: bounded-loop in-memory capex-driver dicts, not a DB query
        for d in capex_drivers
    )
    return {
        "inventory": inventory,
        "intangibles": intangibles,
        "unmodeled_fixed_assets": round(opening_net_ppe - capex_opening_net, 2),
    }


def resolve_goodwill_lives(drivers: EnginePayload) -> tuple[int, int]:
    """Resolve the goodwill BOOK vs TAX amortization lives (RD-1 row 17, the split fix).

    - BOOK (ASC 350-20): default is impairment-only — NO amortization (0). A positive
      ``goodwill_book_amort_years`` is the private-company election (≤ 10 years,
      validated at the schema boundary).
    - TAX (IRC §197): 15-year ratable — a cash-tax proxy schedule, NEVER the book P&L
      line. The pre-split ``goodwill_amort_years`` key WAS the §197 15-year life
      mislabeled as book; when a stored legacy payload carries it and the new tax key
      is absent, it maps to the TAX life.
    """
    book_raw = drivers.get("goodwill_book_amort_years")
    book_years = int(book_raw) if book_raw is not None else 0  # type: ignore[call-overload]
    tax_raw = drivers.get("goodwill_tax_amort_years")
    if tax_raw is None:
        legacy = drivers.get("goodwill_amort_years")  # pre-split key == the §197 tax life
        tax_raw = legacy if legacy is not None else 15
    return book_years, int(tax_raw)  # type: ignore[call-overload]


def parse_ebitda_adjustments(
    drivers: EnginePayload, num_years: int
) -> tuple[dict[str, list[float]], dict[str, EnginePayload]]:
    """Parse ``ebitda_adjustments`` into per-year values + per-item PROVENANCE (RD-1 row 16).

    Two accepted shapes per add-back name:
      - legacy: ``{name: [v0, v1, ...]}`` (no provenance — recorded as such, never
        silently dressed as documented);
      - structured: ``{name: {"values": [...], "provenance": {...}}}`` where provenance
        carries the quality-of-earnings fields (what/why/evidence/period — e.g. the GL
        account or invoice the add-back ties to).

    Returns ``(values_by_name, detail_by_name)`` where detail carries the values AND the
    provenance record (``provenance`` is None for a legacy bare list — an add-back with
    no stated evidence is surfaced as undocumented, not fabricated).
    """
    raw = drivers.get("ebitda_adjustments", {}) or {}
    values_by_name: dict[str, list[float]] = {}
    detail_by_name: dict[str, EnginePayload] = {}
    for name, payload in raw.items():  # type: ignore[union-attr]
        provenance: EnginePayload | None = None
        if isinstance(payload, dict):
            values = payload.get("values", [])  # SA-OK: bounded-loop dict.get on the in-memory adjustments payload, not a DB query
            prov_raw = payload.get("provenance")
            provenance = dict(prov_raw) if isinstance(prov_raw, dict) else None
        else:
            values = payload or []
        per_year = [
            round(float(values[yr]), 2) if yr < len(values) else 0.0 for yr in range(num_years)
        ]
        values_by_name[name] = per_year
        detail_by_name[name] = {"values": per_year, "provenance": provenance}
    return values_by_name, detail_by_name


def build_sweep_terms(
    drivers: EnginePayload,
    historical_bs: EnginePayload | None,
    debt_tranches: list[EnginePayload],
) -> EnginePayload | None:
    """Assemble the cash-ledger sweep terms from the per-deal assumptions (RD-1 row 5).

    Returns ``None`` when the deal carries only the parity defaults (flat 100% sweep of
    the current year's excess, no minimum-cash floor, no revolver) — the pre-existing
    memoryless-sweep semantics, kept byte-identical for every stored scenario. Any
    non-default sweep input (sweep_pct < 1, a step-down list, a min-cash floor) or a
    revolver tranche activates the cash-ledger schedule. Sweep % and step-downs are
    per-deal credit-agreement inputs — NO market grid is encoded (gap G1: the folklore
    50/25/0 grid was not groundable to a primary source; refuse over fabricate).
    """
    sweep_pct = float(drivers.get("sweep_pct", 1.0))
    by_year_raw = drivers.get("sweep_pct_by_year") or []
    sweep_pct_by_year = [float(v) for v in by_year_raw]  # type: ignore[union-attr]
    min_cash_floor = float(drivers.get("min_cash_floor", 0.0))
    has_revolver = any(bool(t.get("revolver_flag")) for t in debt_tranches)  # SA-OK: bounded-loop in-memory tranche dicts, not a DB query
    if not has_revolver and sweep_pct == 1.0 and not sweep_pct_by_year and min_cash_floor == 0.0:
        return None
    opening_cash = float((historical_bs or {}).get("cash", 0) or 0)
    return {
        "sweep_pct": sweep_pct,
        "sweep_pct_by_year": sweep_pct_by_year,
        "min_cash_floor": min_cash_floor,
        "opening_cash": opening_cash,
    }


def resolve_is_line_vector(raw: object, num_years: int) -> list[float]:
    """Resolve an income-statement driver input into a per-year vector (C2-b).

    Accepts a scalar (applied to every year) or a per-year list. A list shorter
    than the horizon HOLDS ITS LAST VALUE CONSTANT for the remaining years —
    the DEC-LBO3S-3-as-amended forecast-default convention: IS-ratio/IS-line
    defaults hold the last known year constant; a fade curve is a MANUAL
    override written out per year, never a fabricated default (a defaulted fade
    dresses a guess as a trend). ``None`` / empty → zeros (driver off).
    """
    if raw is None:
        return [0.0] * num_years
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return [round(float(raw), 2)] * num_years
    values = [round(float(v), 2) for v in raw]  # type: ignore[union-attr]
    if not values:
        return [0.0] * num_years
    if len(values) >= num_years:
        return values[:num_years]
    return values + [values[-1]] * (num_years - len(values))


# ── Historical-period feed (C2-b, plan 20260717-three-statement-spine AC-5) ──
# The engine accepts the LboPeriod-shaped historical vector C2-a produces:
# each element {kind, label, seq, period_start, period_end, provenance,
# pnl: {...}, bs: {...}}. Only PRE-DEAL statement columns are feedable —
# pf_open and forecast periods are ENGINE-PRODUCED, so feeding one is a caller
# bug and fails loud.
_HISTORICAL_PERIOD_KINDS = ("historical_fy", "ltm")

# The op-line keys a historical period's BS contributes to the engine's opening
# seed. Historical debt + equity are EXCLUDED by construction (purchase
# accounting: the target's debt is refinanced and its equity eliminated at
# close) — the same rule grid_helpers.historical_bs_from_rows applies.
_PERIOD_BS_SEED_KEYS = (
    "cash", "accounts_receivable", "inventory", "other_current_assets",
    "gross_fixed_assets", "accumulated_depreciation", "net_fixed_assets",
    "intangibles", "other_assets",
    "accounts_payable", "accrued_expenses", "other_current_liabilities",
)


def _historical_bs_identity(bs: EnginePayload) -> tuple[float, float, float]:
    """(assets, liabilities, equity) freshly summed from a period's BS lines.

    Totals are always DERIVED from the lines (never trusted from the feed) so
    the A = L + E assert checks the lines themselves. Equity: ``total_equity``
    (or ``equity``) when stated, else the sum of the stated equity components.
    """
    def f(key: str) -> float:
        return float(bs.get(key, 0) or 0)

    net_fa = float(
        bs.get("net_fixed_assets", f("gross_fixed_assets") - f("accumulated_depreciation")) or 0
    )
    assets = round(
        f("cash") + f("accounts_receivable") + f("inventory") + f("other_current_assets")
        + net_fa + f("intangibles") + f("goodwill") + f("other_assets"), 2
    )
    liabilities = round(
        f("accounts_payable") + f("accrued_expenses") + f("other_current_liabilities")
        + f("total_debt") + f("other_liabilities"), 2
    )
    if "total_equity" in bs:
        equity = f("total_equity")
    elif "equity" in bs:
        equity = f("equity")
    else:
        equity = f("common_equity") + f("equity_contributed") + f("retained_earnings")
    return assets, liabilities, round(equity, 2)


def normalize_historical_periods(
    historical_periods: list[EnginePayload] | None,
) -> list[EnginePayload]:
    """Validate + normalize the historical-period feed; assert every column ties.

    Contract (the C2-a ↔ C2-b seam, plan 20260717-three-statement-spine AC-4/AC-5):
      - ``kind`` ∈ {historical_fy, ltm}; anything else raises ``ValueError``.
      - Periods sort by ``seq`` (stable; missing seq keeps list order); the LTM,
        when present, must be the LAST period (it is the forecast seed column).
      - ``pnl['opex']`` is D&A-EXCLUSIVE (the engine's own P&L vocabulary), so a
        missing ``ebitda`` derives as revenue − cogs − opex.
      - Every period's BS is asserted A = L + E via :func:`assert_bs_check` —
        NEVER bypassed or tolerance-widened: an unbalanced historical column
        would poison the whole spine (honest-degrade — feeding fewer periods —
        is the caller's move, not feeding broken ones).

    Returns normalized copies with computed ``total_assets`` /
    ``total_liabilities`` / ``total_equity`` / ``bs_check`` stamped on each BS.
    """
    if not historical_periods:
        return []
    indexed = list(enumerate(historical_periods))
    indexed.sort(key=lambda pair: (int(pair[1].get("seq", pair[0]) or 0), pair[0]))  # type: ignore[union-attr]

    out: list[EnginePayload] = []
    ltm_seen = False
    for idx, (orig_i, p) in enumerate(indexed):
        kind = str(p.get("kind") or "")  # SA-OK: bounded-loop dict.get on in-memory period dicts, not a DB query
        if kind not in _HISTORICAL_PERIOD_KINDS:
            raise ValueError(
                f"historical period {idx} has kind '{kind}' — the engine feed accepts only "
                f"{list(_HISTORICAL_PERIOD_KINDS)}; pf_open/forecast columns are engine-produced"
            )
        if ltm_seen:
            raise ValueError(
                "the LTM period must be the LAST historical period (it is the forecast "
                "seed column); a historical_fy after the LTM is a mis-ordered feed"
            )
        if kind == "ltm":
            ltm_seen = True

        pnl = dict(p.get("pnl") or {})
        bs = dict(p.get("bs") or {})
        if "ebitda" not in pnl and "revenue" in pnl:
            # Feed contract: opex is D&A-exclusive, so this IS pre-D&A EBITDA
            # (the engine's own gross_profit − opex construction, which carries
            # its D&A on a separate labeled line).
            pnl["ebitda"] = round(  # FIN-OK: feed opex is D&A-exclusive by contract — this subtraction is pre-D&A EBITDA, not the GP−Opex EBIT conflation
                float(pnl.get("revenue", 0) or 0)
                - float(pnl.get("cogs", 0) or 0)
                - float(pnl.get("opex", 0) or 0),
                2,
            )

        label = str(p.get("label") or kind)
        assets, liabilities, equity = _historical_bs_identity(bs)
        bs_check = round(assets - liabilities - equity, 2)
        assert_bs_check(bs_check, idx, period_label=f"historical period '{label}'")
        bs["total_assets"] = assets
        bs["total_liabilities"] = liabilities
        bs["total_equity"] = equity
        bs["bs_check"] = bs_check

        out.append({
            "kind": kind,
            "label": label,
            "seq": int(p.get("seq", orig_i) or 0),
            "period_start": p.get("period_start"),
            "period_end": p.get("period_end"),
            "provenance": str(p.get("provenance") or "qbo_actual"),
            "pnl": pnl,
            "bs": bs,
        })
    return out


def seed_from_periods(normalized_periods: list[EnginePayload]) -> EnginePayload:
    """The forecast seeds the LAST historical period implies (LTM by contract).

    Returns ``{base_revenue, ltm_ebitda, historical_bs}``. The historical-BS
    seed carries op lines only (:data:`_PERIOD_BS_SEED_KEYS`) — debt/equity are
    excluded by purchase-accounting construction. The feed NEVER overrides an
    explicitly-passed seed: ``compute_full_forecast`` fills only absent/zero
    seeds from here.
    """
    if not normalized_periods:
        return {"base_revenue": 0.0, "ltm_ebitda": 0.0, "historical_bs": None}
    last = normalized_periods[-1]
    pnl = last["pnl"]
    bs = last["bs"]
    seed_bs = {
        k: float(bs.get(k, 0) or 0)  # type: ignore[union-attr]  # SA-OK: bounded-loop dict.get over 12 op-line keys of an in-memory period BS, not a DB query
        for k in _PERIOD_BS_SEED_KEYS
        if k in bs  # type: ignore[operator]
    }
    return {
        "base_revenue": round(float(pnl.get("revenue", 0) or 0), 2),  # type: ignore[union-attr]
        "ltm_ebitda": round(float(pnl.get("ebitda", 0) or 0), 2),  # type: ignore[union-attr]
        "historical_bs": seed_bs or None,
    }


def reanchor_capex_drivers_tangible_only(
    capex_drivers: list[EnginePayload], intangible_annual_amort: float
) -> tuple[list[EnginePayload], float]:
    """DL-5 double-count guard: re-anchor vintage-0 D&A to the TANGIBLE-only run-rate.

    The composition synthesizes its capex vintage from the FULL Y0 D&A run-rate
    (``beginning_gross = y0_da × (life − 1)``, engine_composition.py), and a
    historical Y0 D&A line INCLUDES intangible amortization. Once the engine
    carries an explicit intangible-amortization driver, keeping the full-run-rate
    anchor double-counts the intangible slice inside PP&E depreciation
    (delta-ledger DL-5). This helper subtracts the intangible annual from the
    aggregate vintage-0 annual (allocated pro-rata across drivers by their
    vintage-0 share) by shrinking ``beginning_gross`` — new vintages
    (capex_pct_revenue) are untouched. The removed slice shifts into the STATIC
    ``unmodeled_fixed_assets`` carry (static_carry_lines is called with the SAME
    re-anchored list), so total PP&E is preserved and A = L + E still ties.

    Applied by ``compute_full_forecast`` only when the caller marks the payload
    ``drivers['capex_anchor_includes_intangible_amort'] = True`` — the C3-a
    composition sets it for its run-rate-synthesized vintage (accum 0); a
    genuine tangible-only QBO capex driver never carries the flag. The removal
    clamps at the available vintage-0 annual (an anchor cannot go negative).

    Returns ``(re-anchored copy, annual D&A actually removed)``.
    """
    annual = round(float(intangible_annual_amort or 0), 2)
    if annual <= 0 or not capex_drivers:
        return list(capex_drivers), 0.0

    vintage0: list[tuple[float, float, int, float]] = []
    total_annual = 0.0
    for d in capex_drivers:
        gross = float(d.get("beginning_gross", 0) or 0)  # SA-OK: bounded-loop dict.get on in-memory capex-driver dicts, not a DB query
        salvage = float(d.get("salvage_value_pct", 0) or 0)
        life = max(int(d.get("useful_life_years", 5) or 5), 1)
        remaining = max(life - 1, 1)
        a = (gross * (1 - salvage) / remaining) if gross > 0 else 0.0
        vintage0.append((gross, salvage, remaining, a))
        total_annual += a

    if total_annual <= 0:
        return list(capex_drivers), 0.0

    removal = min(annual, round(total_annual, 2))
    out: list[EnginePayload] = []
    for d, (_gross, salvage, remaining, a) in zip(capex_drivers, vintage0):
        d2 = dict(d)
        if a > 0:
            new_annual = a - removal * (a / total_annual)
            denom = 1 - salvage
            d2["beginning_gross"] = round(new_annual * remaining / denom, 2) if denom > 0 else 0.0
        out.append(d2)
    return out, round(removal, 2)


def apply_fixed_addbacks(
    ebitda_by_year: list[float | None], addbacks: list[EnginePayload] | None
) -> tuple[list[float | None], float, list[str]]:
    """Apply a covenant's MAPPED fixed add-back lines to a projected EBITDA vector.

    Covenant EBITDA is contractually defined = book EBITDA + the covenant's negotiated
    add-backs — explicit account MAPPING, never keyword inference (the ratified
    SYNTHESIS §2A decision). On a PROJECTED grid only ``kind == "fixed"`` lines (a
    stated dollar amount, signed add/subtract) can be applied; ``kind == "account"``
    lines are live-QBO account evaluations with no projected-grid analogue — they are
    returned as *unprojected* labels and NOT applied (an inferred or extrapolated
    add-back would fabricate covenant headroom).

    Returns ``(covenant_ebitda_by_year, fixed_total, unprojected_labels)``;
    a ``None`` EBITDA year stays ``None`` (honest NO_DATA is preserved).
    """
    fixed_total = 0.0
    unprojected: list[str] = []
    for line in addbacks or []:
        kind = str(line.get("kind") or "").lower()  # SA-OK: bounded-loop dict.get on in-memory covenant add-back lines, not a DB query
        sign = -1.0 if str(line.get("sign") or "add").lower() == "subtract" else 1.0
        amount = line.get("amount")
        if kind == "fixed" and amount is not None:
            fixed_total += sign * float(amount)  # type: ignore[arg-type]
        else:
            unprojected.append(str(line.get("label") or line.get("account_ref") or "unnamed"))
    fixed_total = round(fixed_total, 2)
    adjusted = [None if e is None else round(e + fixed_total, 2) for e in ebitda_by_year]
    return adjusted, fixed_total, unprojected

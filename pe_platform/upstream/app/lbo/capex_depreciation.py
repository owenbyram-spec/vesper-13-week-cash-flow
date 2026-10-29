"""
app/lbo/capex_depreciation.py
──────────────────────────────
Capex, Depreciation & Amortization schedule with vintage-based tracking,
plus the definite-lived intangible amortization schedule (C2-b, plan
20260717-three-statement-spine AC-5).
"""
from typing import Dict, Any, List


def compute_intangible_schedule(
    opening_intangibles: float,
    amort_years: int,
    num_years: int,
    period_fraction: list[float] | None = None,
) -> dict[str, object]:  # CRAFT-OK: engine-payload boundary — same opaque dict shape as the sibling schedules
    """Straight-line amortization of the OPENING intangibles balance (C2-b).

    Closes the disclosed grid-vs-engine divergence DL-5 (the engine previously
    carried intangibles STATIC with no amortization —
    ``engine_support.static_carry_lines``). Semantics:

    - ``amort_years <= 0`` (the default, no driver set) or a non-positive
      opening balance → ZERO amortization, flat carry every year — byte-parity
      with the pre-existing static-carry behavior for every stored scenario.
    - ``amort_years > 0`` → the opening balance straight-lines over that life,
      capped at the remaining balance (the tail year never over-amortizes).

    The expense is BOOK amortization: it joins the P&L ``total_da`` (reducing
    EBIT → EBT → taxes, i.e. it CARRIES a tax shield, unlike the no-shield OID
    convention) and is a non-cash CFO add-back via the D&A line. The BS
    intangibles line follows ``ending_intangibles``.

    DL-5 double-count hazard: when the capex vintage was synthesized from the
    FULL Y0 D&A run-rate, activating this schedule requires re-anchoring that
    vintage to the tangible-only run-rate —
    ``engine_support.reanchor_capex_drivers_tangible_only``.
    """
    opening = round(float(opening_intangibles or 0), 2)
    years = int(amort_years or 0)
    annual = round(opening / years, 2) if years > 0 and opening > 0 else 0.0

    schedule: list[dict[str, object]] = []
    remaining = opening
    for yr in range(num_years):
        # W3d: the stub year's amort ×f (partial period); default None → ×1.0 → byte-parity.
        _f = period_fraction[yr] if (period_fraction and yr < len(period_fraction)) else 1.0
        amort = min(annual * _f, remaining)
        remaining = round(remaining - amort, 2)
        schedule.append({
            "year_index": yr,
            "amortization": round(amort, 2),
            "ending_intangibles": remaining,
        })

    return {
        "schedule": schedule,
        "annual_amortization": annual,
        "amort_years": years,
        "opening_intangibles": opening,
    }


def compute_capex_schedule(
    capex_drivers: List[Dict[str, Any]],
    revenue_by_year: List[float],
    num_years: int,
    goodwill_amount: float = 0,
    goodwill_book_amort_years: int = 0,
    goodwill_tax_amort_years: int = 15,
    period_fraction: list[float] | None = None,
    unmodeled_net_fa_offset: float = 0.0,
) -> dict[str, object]:  # CRAFT-OK: pre-existing dynamic engine-payload boundary (same opaque dict shape as HEAD)
    """
    Compute capex additions, vintage-based depreciation, and the goodwill schedules.

    NPP&E floor (AC-FRR-11b, plan 20260720 §Amendment): the DISPLAYED net PP&E the
    forecast BS presents is ``total_net_fa + unmodeled_net_fa_offset`` (the SIGNED
    static-carry difference between the real opening net PP&E and what the capex
    vintages seed — ``engine_support.static_carry_lines``). A run-rate-synthesized
    vintage-0 anchor ignores ``beginning_accum``, so uncapped vintage depreciation
    can drive that displayed sum NEGATIVE — an asset cannot depreciate below zero
    book value. The floor is enforced HERE, on the vintage schedule (never a
    display clamp): each year's vintage depreciation is CURTAILED (pro-rata across
    categories by their share of the year's depreciation) by exactly the amount
    that would push the displayed sum below 0. The curtailed (lower)
    ``total_depreciation`` then flows through total_da → EBIT → NI/taxes/sweep in
    the forecast engine, and ``depreciation_curtailed`` is emitted per year (0.0
    when the floor never binds — byte-parity for every scenario whose displayed
    PP&E stays non-negative).

    Goodwill is BOOK-vs-TAX split (RD-1 row 17 — the fix for the pre-split 15-year
    conflation, ``assumptions.py:58``):

    - BOOK (ASC 350-20, binding): goodwill is NOT amortized (impairment-only) unless
      the private-company alternative is elected — ``goodwill_book_amort_years`` > 0
      (≤ 10, validated at the schema boundary) straight-lines it. The BOOK schedule
      is what flows into the P&L (``total_da``) and the balance-sheet goodwill line.
    - TAX (IRC §197, binding): goodwill amortizes ratably over 15 years FOR TAX. The
      TAX schedule is emitted separately (``goodwill_tax_schedule``) as a labeled
      cash-tax proxy — it never masquerades as book amortization.

    Args:
        capex_drivers: List of capex driver dicts per FA category
        revenue_by_year: Revenue per year for capex_pct_revenue calc
        num_years: Number of projection years
        goodwill_amount: Total goodwill (the ASC 805 residual from the S&U build)
        goodwill_book_amort_years: 0 = impairment-only (ASC 350-20 default);
            1..10 = the private-company election straight-line life
        goodwill_tax_amort_years: the IRC §197 ratable tax life (15)

    Returns:
        Per-year capex, depreciation by category, net PP&E, and BOTH goodwill
        schedules (book: ``goodwill_schedule``; tax: ``goodwill_tax_schedule``)
    """
    # ── Initialize per-category tracking ─────────────────────────────────
    categories = []
    for drv in capex_drivers:
        cat = {
            "name": drv.get("category", "Other"),
            "useful_life": max(int(drv.get("useful_life_years", 5)), 1),
            "capex_pct": float(drv.get("capex_pct_revenue", 0)),
            "salvage_pct": float(drv.get("salvage_value_pct", 0)),
            "beginning_gross": float(drv.get("beginning_gross", 0)),
            "beginning_accum": float(drv.get("beginning_accum_depr", 0)),
            # Vintage tracking: list of (capex_amount, year_added, useful_life)
            "vintages": [],
        }
        # Add existing assets as "vintage 0". On the net-book-value basis (a stated
        # remaining life, or opening_basis "net_book_value") the opening NET book
        # value (gross less salvage less accumulated depreciation) depreciates over
        # the remaining life, so a partly or fully depreciated opening base is never
        # depreciated twice; otherwise the legacy convention spreads gross less
        # salvage over max(life - 1, 1).
        stated_remaining = drv.get("remaining_life_years")
        if stated_remaining or drv.get("opening_basis") == "net_book_value":
            net_depreciable = max(
                cat["beginning_gross"] * (1 - cat["salvage_pct"]) - cat["beginning_accum"], 0.0
            )
            if net_depreciable > 0 and not stated_remaining:
                raise ValueError(
                    f"capex category {cat['name']!r}: a net-book-value opening with a remaining "
                    "depreciable balance needs remaining_life_years"
                )
            remaining_life = max(int(stated_remaining or 1), 1)
        else:
            net_depreciable = cat["beginning_gross"] * (1 - cat["salvage_pct"])
            remaining_life = max(cat["useful_life"] - 1, 1)  # Assume avg half-life
        if cat["beginning_gross"] > 0 and net_depreciable > 0:
            cat["vintages"].append({
                "amount": net_depreciable,
                "year_added": -1,  # Pre-model
                "useful_life": remaining_life,
                "annual_depr": net_depreciable / remaining_life if remaining_life > 0 else 0,
            })
        categories.append(cat)

    # ── Build year-by-year schedule ──────────────────────────────────────
    yearly = []
    running_gross = {c["name"]: c["beginning_gross"] for c in categories}
    running_accum = {c["name"]: c["beginning_accum"] for c in categories}
    # W3d: per-year run-rate fraction — the stub year's D&A / goodwill amort ×f (new capex is already
    # f-scaled via the recognized revenue). Default all 1.0 → ×1.0 exact → byte-parity.
    _pf = period_fraction or [1.0] * num_years
    _frac = lambda yr: (_pf[yr] if yr < len(_pf) else 1.0)  # noqa: E731

    _offset = round(float(unmodeled_net_fa_offset or 0.0), 2)

    for yr in range(num_years):
        rev = revenue_by_year[yr] if yr < len(revenue_by_year) else 0
        year_data = {
            "year_index": yr,
            "categories": {},
            "total_capex": 0.0,
            "total_depreciation": 0.0,
        }

        # ── Phase 1: capex additions + the year's UNCURTAILED vintage depreciation
        pending: list[tuple[dict, float, float]] = []  # (category, capex_add, depr_raw)
        for cat in categories:
            cname = cat["name"]
            # New capex for this year
            capex_add = round(rev * cat["capex_pct"], 2)
            year_data["total_capex"] += capex_add

            # Add new vintage
            if capex_add > 0:
                net_depreciable = capex_add * (1 - cat["salvage_pct"])
                cat["vintages"].append({
                    "amount": net_depreciable,
                    "year_added": yr,
                    "useful_life": cat["useful_life"],
                    "annual_depr": net_depreciable / cat["useful_life"],
                })

            # Compute total depreciation from all active vintages
            depr_this_year = 0.0
            for v in cat["vintages"]:
                years_since = yr - v["year_added"]
                if 0 < years_since <= v["useful_life"]:
                    depr_this_year += v["annual_depr"]
                elif v["year_added"] == -1:
                    # Pre-existing: depreciate from year 0
                    if yr < v["useful_life"]:
                        depr_this_year += v["annual_depr"]

            depr_this_year = round(depr_this_year * _frac(yr), 2)  # W3d: partial-period stub-year depreciation
            running_gross[cname] = round(running_gross[cname] + capex_add, 2)
            pending.append((cat, capex_add, depr_this_year))

        # ── Phase 2: the NPP&E floor (AC-FRR-11b) — curtail the year's vintage
        # depreciation by exactly the amount that would push the DISPLAYED sum
        # (total_net_fa + the signed unmodeled offset) below 0. curtail == 0.0
        # (the floor not binding) reproduces the pre-existing per-category
        # arithmetic to the cent — byte-parity for every unaffected scenario.
        total_uncurtailed = round(sum(d for _, _, d in pending), 2)
        prospective_net = round(
            sum(running_gross.values())
            - (sum(running_accum.values()) + total_uncurtailed),
            2,
        )
        displayed_net = round(prospective_net + _offset, 2)
        curtail_total = min(total_uncurtailed, -displayed_net) if displayed_net < 0 else 0.0

        curtail_by_cat = [0.0] * len(pending)
        if curtail_total > 0 and total_uncurtailed > 0:
            for i, (_, _, d) in enumerate(pending):
                curtail_by_cat[i] = round(curtail_total * d / total_uncurtailed, 2)
            # The rounding residual (cents) lands on the largest-depreciation
            # category, capped at that category's own uncurtailed depreciation.
            _j = max(range(len(pending)), key=lambda i: pending[i][2])
            _resid = round(curtail_total - sum(curtail_by_cat), 2)
            curtail_by_cat[_j] = round(min(curtail_by_cat[_j] + _resid, pending[_j][2]), 2)

        curtailed_this_year = 0.0
        for i, (cat, capex_add, depr_raw) in enumerate(pending):
            cname = cat["name"]
            depr_final = round(depr_raw - curtail_by_cat[i], 2)
            curtailed_this_year += curtail_by_cat[i]
            year_data["total_depreciation"] += depr_final
            running_accum[cname] = round(running_accum[cname] + depr_final, 2)

            year_data["categories"][cname] = {
                "capex": capex_add,
                "depreciation": depr_final,
                "gross_fa": running_gross[cname],
                "accum_depr": running_accum[cname],
                "net_fa": round(running_gross[cname] - running_accum[cname], 2),
            }

        year_data["total_capex"] = round(year_data["total_capex"], 2)
        year_data["total_depreciation"] = round(year_data["total_depreciation"], 2)
        # The AC-FRR-11b disclosure line: how much vintage depreciation the floor
        # curtailed this year (0.0 whenever the floor never binds).
        year_data["depreciation_curtailed"] = round(curtailed_this_year, 2)

        # Aggregate totals
        year_data["total_gross_fa"] = round(sum(running_gross.values()), 2)
        year_data["total_accum_depr"] = round(sum(running_accum.values()), 2)
        year_data["total_net_fa"] = round(
            year_data["total_gross_fa"] - year_data["total_accum_depr"], 2
        )

        yearly.append(year_data)

    # ── Goodwill — BOOK schedule (ASC 350-20: impairment-only unless the ≤10y
    # private-company election is made). THIS schedule feeds the P&L (total_da)
    # and the BS goodwill line. book years == 0 → zero amortization, flat goodwill.
    gw_annual = (
        round(goodwill_amount / goodwill_book_amort_years, 2)
        if goodwill_book_amort_years > 0
        else 0
    )
    goodwill_schedule = []
    gw_remaining = goodwill_amount
    for yr in range(num_years):
        amort = min(gw_annual * _frac(yr), gw_remaining)  # W3d: partial-period stub-year book goodwill amort
        gw_remaining = round(gw_remaining - amort, 2)
        goodwill_schedule.append({
            "year_index": yr,
            "amortization": round(amort, 2),
            "ending_goodwill": gw_remaining,
        })
        # Also add to yearly depreciation totals
        if yr < len(yearly):
            yearly[yr]["goodwill_amortization"] = round(amort, 2)
            yearly[yr]["total_da"] = round(
                yearly[yr]["total_depreciation"] + amort, 2
            )

    # ── Goodwill — TAX schedule (IRC §197 ratable). Labeled, NEVER folded into
    # book D&A or the book BS goodwill line. When the deal ELECTS §197 treatment
    # (drivers["section_197_election"], AC-C4 leg 3) the forecast engine feeds
    # the per-year book-tax difference (tax_amortization − book amortization)
    # into the waterfall solver's CASH-TAX base — the linkage lives there; this
    # schedule stays the labeled source vector either way.
    gw_tax_annual = (
        round(goodwill_amount / goodwill_tax_amort_years, 2)
        if goodwill_tax_amort_years > 0
        else 0
    )
    goodwill_tax_schedule = []
    gw_tax_remaining = goodwill_amount
    for yr in range(num_years):
        tax_amort = min(gw_tax_annual * _frac(yr), gw_tax_remaining)  # W3d: partial-period stub-year §197 tax amort
        gw_tax_remaining = round(gw_tax_remaining - tax_amort, 2)
        goodwill_tax_schedule.append({
            "year_index": yr,
            "tax_amortization": round(tax_amort, 2),
            "remaining_tax_basis": gw_tax_remaining,
        })

    return {
        "capex_schedule": yearly,
        "goodwill_schedule": goodwill_schedule,
        "goodwill_annual_amort": gw_annual,
        "goodwill_tax_schedule": goodwill_tax_schedule,
        "goodwill_tax_annual_amort": gw_tax_annual,
        "goodwill_basis": {
            "book_treatment": (
                "private_company_election"
                if goodwill_book_amort_years > 0
                else "impairment_only"
            ),
            "book_amort_years": goodwill_book_amort_years,
            "tax_amort_years": goodwill_tax_amort_years,
        },
    }

"""
app/lbo/sources_uses.py
────────────────────────
Computes LBO Sources & Uses and the Pro-Forma Opening Balance Sheet.

Two opening-BS presentations ride in the result (AC-C3 leg 2, plan
20260716-five-domain-remediation):

  * ``pf_opening_bs`` — the LEGACY presentation (transaction + financing fees
    capitalized into the goodwill plug, debt at face, equity = the full S&U
    plug). NO LONGER the engine feed (AC-C4 leg 6 closed the C3-named residual:
    forecast_engine.py now articulates its projected BS from the ASC-805 block
    — goodwill ex-fees, opening RE = −transaction fees, carrying debt = face −
    OID). RETAINED byte-stable because consumers remain: the sensitivity-grid
    opening-cash read (app/lbo/sensitivity_grids.py returns_attribution), the
    S&U tab payload the frontend renders both blocks from
    (frontend/components/lbo/SourcesUsesTab.tsx), and the goldens that pin the
    metamorphic fee-exclusion identity. Retiring it is a router/frontend-side
    change, out of this plan's engine footprint.
  * ``pf_opening_bs_asc805`` — the CORRECT ASC 805 fee treatment, the block the
    grid PF-Y0 bridge, the S&U tab, AND (since AC-C4 leg 6) the forecast
    engine's projected BS articulate from: transaction costs EXPENSED at
    close (opening retained earnings, NOT goodwill — ASC 805-10-25-23);
    financing fees a CONTRA-DEBT OID reducing carrying debt (ASC 835-30, ASU
    2015-03), amortized straight-line over the tranche schedule (practical
    expedient; effective-interest not modeled — disclosed). The goodwill plug
    EXCLUDES both fee categories:
      goodwill_asc805 = goodwill_legacy − transaction_fees − financing_fees
    (the metamorphic identity the golden asserts).

``entry_equity`` (the returns denominator) is IDENTICAL under both — fees are
cash Uses funded by the sponsor, so the CASH equity plug is unchanged; only the
opening BOOK presentation differs.

Deal basis (how the target's pre-deal cash is priced) is resolved ONCE, in
:func:`resolve_deal_basis`, and every consumer reads its result off the S&U
payload (the PF opening cash line, the ``cash_to_balance_sheet`` Use, the
``deal_basis`` block) — the forecast opening, the grid PF bridge, the opening
bridge and the workbench packer never re-derive it:

  * ``cash_free_debt_free`` (DEFAULT) — EV is the price of the business free of
    cash and debt. The target's pre-deal book cash is retained by the seller and
    leaves the PF opening BS; the only opening cash is the explicit
    ``minimum_operating_cash`` driver, which the sponsor funds as a Use.
  * ``equity_purchase`` — the buyer pays equity = EV + book cash − book debt and
    refinances the book debt, so total consideration is EV + book cash: the
    target's cash stays on the PF opening BS and is paid for as a Use.

Either way, changing the target's book cash at a fixed EV cannot change exit
equity minus entry equity (the metamorphic identity the tests pin).

Payload convention: the opaque engine dicts the callers thread
(``dict[str, object]`` — the waterfall_solve.DebtTranche precedent).
"""

# The opaque engine payloads (drivers / tranche dicts / result blocks).
EnginePayload = dict[str, object]

DEAL_BASIS_CASH_FREE = "cash_free_debt_free"
DEAL_BASIS_EQUITY_PURCHASE = "equity_purchase"
DEAL_BASES = (DEAL_BASIS_CASH_FREE, DEAL_BASIS_EQUITY_PURCHASE)
_DEAL_BASIS_LABELS = {
    DEAL_BASIS_CASH_FREE: "Cash-free / debt-free",
    DEAL_BASIS_EQUITY_PURCHASE: "Equity purchase (EV + cash − debt)",
}


class DealBasisError(ValueError):
    """The stated deal basis / minimum operating cash cannot be applied."""


def resolve_deal_basis(
    drivers: EnginePayload,
    historical_bs: dict[str, float] | None,
    target_book_debt: float | None = None,
) -> EnginePayload:
    """Resolve how the target's pre-deal cash is priced (the ONE place).

    Returns the cash the PF opening BS carries, the cash the sponsor funds as a
    Use (``cash_to_balance_sheet``), and the disclosure fields the S&U payload
    shows. Raises :class:`DealBasisError` for an unknown basis, a negative
    minimum operating cash, or a minimum operating cash on the equity-purchase
    basis (that basis acquires the target's own cash; a second cash figure
    would be a guess at which one is meant).
    """
    basis = str(drivers.get("deal_basis") or DEAL_BASIS_CASH_FREE)
    if basis not in DEAL_BASES:
        raise DealBasisError(
            f"deal_basis must be one of {', '.join(DEAL_BASES)} (got {basis!r})"
        )
    raw_min = drivers.get("minimum_operating_cash")
    try:
        min_cash = round(float(raw_min or 0), 2)
    except (TypeError, ValueError) as exc:
        raise DealBasisError("minimum_operating_cash must be a number") from exc
    if min_cash < 0:
        raise DealBasisError("minimum_operating_cash cannot be negative")
    book_cash = round(float((historical_bs or {}).get("cash", 0) or 0), 2)
    debt = None if target_book_debt is None else round(float(target_book_debt), 2)

    if basis == DEAL_BASIS_CASH_FREE:
        pf_cash = min_cash
        cash_to_bs = min_cash
        retained_by_seller = book_cash
        purchase_price_extra = 0.0
    else:
        if min_cash:
            raise DealBasisError(
                "minimum_operating_cash applies to the cash-free/debt-free basis only; "
                "an equity purchase acquires the target's book cash"
            )
        pf_cash = book_cash
        cash_to_bs = book_cash
        retained_by_seller = 0.0
        purchase_price_extra = book_cash

    return {
        "basis": basis,
        "label": _DEAL_BASIS_LABELS[basis],
        "target_book_cash": book_cash,
        "cash_retained_by_seller": retained_by_seller,
        "minimum_operating_cash": min_cash if basis == DEAL_BASIS_CASH_FREE else 0.0,
        "cash_to_balance_sheet": cash_to_bs,
        "pf_opening_cash": pf_cash,
        "target_book_debt": debt,
        "_purchase_price_extra": purchase_price_extra,
    }


def oid_amortization_vectors(
    funded_amount: float,
    fees_pct: float,
    amort_years: int | None,
    num_periods: int,
) -> EnginePayload:
    """Straight-line OID vectors for ONE tranche's financing fees (ASC 835-30).

    The OID (= funded × fees_pct) amortizes straight-line over the tranche's
    schedule (``amort_years``; a tranche with no stated schedule amortizes over
    the model horizon — the honest available window, never a fabricated term).
    Returns per-model-year vectors (1-indexed periods 1..num_periods):
      ``total`` — the OID at close;
      ``amortization[k-1]`` — the year-k amortization (non-cash interest);
      ``remaining[k-1]`` — unamortized OID after year k (carrying debt =
      face − remaining).
    Remaining is computed as round(total × (1 − k/n)) and amortization as the
    delta of consecutive remainders, so Σ amortization ≡ total to the cent.
    """
    oid_total = round(float(funded_amount) * float(fees_pct), 2)
    n = int(amort_years) if amort_years and int(amort_years) > 0 else max(int(num_periods), 1)
    amortization: list[float] = []
    remaining: list[float] = []
    prev = oid_total
    for k in range(1, num_periods + 1):
        rem = round(oid_total * max(0.0, 1.0 - k / n), 2)
        amortization.append(round(prev - rem, 2))
        remaining.append(rem)
        prev = rem
    return {"total": oid_total, "amortization": amortization, "remaining": remaining}


def compute_sources_uses(
    drivers: EnginePayload,
    debt_tranches: list[EnginePayload],
    ltm_ebitda: float,
    historical_bs: dict[str, float] | None = None,
    target_book_debt: float | None = None,
) -> EnginePayload:
    """
    Compute Sources & Uses for an LBO transaction.

    Args:
        drivers: Main driver data (entry_ebitda_multiple, transaction_fees_pct,
            deal_basis, minimum_operating_cash, etc.)
        debt_tranches: List of debt tranche dicts (funded_amount, fees_pct, etc.)
        ltm_ebitda: Last twelve months EBITDA
        historical_bs: Historical balance sheet snapshot for PF opening BS
        target_book_debt: the target's pre-deal book debt, when the caller has it
            (shown as the equity-purchase price split; never changes the totals —
            the PF BS always refinances the book debt)

    Returns:
        Dict with sources, uses, equity_check, deal_basis and pf_opening_bs
    """
    mode = str(drivers.get("mode") or "transaction_lbo").lower()
    if mode not in ("operating", "transaction_lbo"):
        raise ValueError(f"unsupported model mode {mode!r}")
    if mode == "operating":
        # ── Operating 3-Statement Mode ──────────────────────────────────────
        # Bypasses transaction purchase price, transaction fees, and buyout goodwill plug.
        # Articulates directly from the historical balance sheet book values.
        hbs = historical_bs or {}
        cash = round(float(hbs.get("cash", 0)), 2)
        accounts_receivable = round(float(hbs.get("accounts_receivable", hbs.get("ar", 0))), 2)
        inventory = round(float(hbs.get("inventory", 0)), 2)
        other_current_assets = round(float(hbs.get("other_current_assets", hbs.get("other_ca", 0))), 2)
        gross_fixed_assets = round(float(hbs.get("gross_fixed_assets", hbs.get("ppe", 0))), 2)
        accumulated_depreciation = round(float(hbs.get("accumulated_depreciation", 0)), 2)
        net_fixed_assets = round(
            float(hbs.get("net_fixed_assets", hbs.get("ppe", gross_fixed_assets - accumulated_depreciation))), 2
        )
        intangibles = round(float(hbs.get("intangibles", 0)), 2)
        other_assets = round(float(hbs.get("other_assets", 0)), 2)
        identifiable_assets = round(
            cash + accounts_receivable + inventory + other_current_assets
            + net_fixed_assets + intangibles + other_assets, 2
        )
        accounts_payable = round(float(hbs.get("accounts_payable", hbs.get("ap", 0))), 2)
        accrued_expenses = round(float(hbs.get("accrued_expenses", hbs.get("accrued_liab", 0))), 2)
        other_current_liabilities = round(float(hbs.get("other_current_liabilities", hbs.get("other_cl", 0))), 2)

        total_debt = sum(float(t.get("funded_amount", t.get("opening_balance", 0))) for t in debt_tranches)
        total_liabilities = round(accounts_payable + accrued_expenses + other_current_liabilities + total_debt, 2)
        goodwill = round(float(hbs.get("goodwill", 0.0)), 2)
        total_assets = round(identifiable_assets + goodwill, 2)
        equity = round(total_assets - total_liabilities, 2)

        op_bs = {
            "cash": cash,
            "accounts_receivable": accounts_receivable,
            "inventory": inventory,
            "other_current_assets": other_current_assets,
            "gross_fixed_assets": gross_fixed_assets,
            "accumulated_depreciation": accumulated_depreciation,
            "net_fixed_assets": net_fixed_assets,
            "intangibles": intangibles,
            "goodwill": goodwill,
            "other_assets": other_assets,
            "total_assets": total_assets,
            "accounts_payable": accounts_payable,
            "accrued_expenses": accrued_expenses,
            "other_current_liabilities": other_current_liabilities,
            "total_debt_face": round(total_debt, 2),
            "debt_oid": 0.0,
            "total_debt": round(total_debt, 2),
            "total_liabilities": total_liabilities,
            "contributed_equity": equity,
            "transaction_costs_expensed": 0.0,
            "equity": equity,
            "total_liabilities_equity": round(total_liabilities + equity, 2),
            "bs_check": 0.0,
        }
        return {
            "entry_equity": equity,
            "sources": {"total_sources": 0.0, "total_debt": total_debt, "equity_contribution": equity, "tranches": []},
            "uses": {"total_uses": 0.0, "enterprise_value": 0.0, "transaction_fees": 0.0, "financing_fees": 0.0},
            "credit_stats": {
                "entry_multiple": 0.0,
                "total_leverage": round(total_debt / ltm_ebitda, 2) if ltm_ebitda else 0.0,
                "equity_pct": 1.0,
                "debt_pct": 0.0,
            },
            "pf_opening_bs": op_bs,
            "pf_opening_bs_asc805": op_bs,
        }

    entry_multiple = float(drivers.get("entry_ebitda_multiple", 0))
    fees_pct = float(drivers.get("transaction_fees_pct", 0.03))

    # ── Uses ──────────────────────────────────────────────────────────────
    # An explicit entry_enterprise_value (seeded from a confirmed APA, where the
    # purchase price is contractually fixed) wins over the EBITDA-multiple
    # derivation, so a signed price does not silently drift if LTM EBITDA is later
    # revised. Falls back to ltm_ebitda * entry_multiple when no explicit EV is set.
    explicit_ev = drivers.get("entry_enterprise_value")
    if explicit_ev not in (None, "", 0):
        enterprise_value = float(explicit_ev)
    else:
        enterprise_value = ltm_ebitda * entry_multiple
    # The displayed entry multiple tracks the EV actually used (the implied multiple
    # when an explicit EV is set); identical to entry_multiple on the fallback path.
    effective_multiple = (enterprise_value / ltm_ebitda) if ltm_ebitda else entry_multiple
    transaction_fees = enterprise_value * fees_pct

    # Financing fees per tranche
    total_financing_fees = 0.0
    for t in debt_tranches:
        total_financing_fees += float(t.get("funded_amount", 0)) * float(t.get("fees_pct", 0))

    # Deal basis (module docstring): the cash the PF opening BS carries is funded
    # here as a Use — the minimum operating cash on a cash-free deal, the target's
    # acquired book cash on an equity purchase. Never received for free.
    hbs = historical_bs or {}
    if target_book_debt is None and "total_debt" in hbs:
        target_book_debt = float(hbs.get("total_debt") or 0)
    basis = resolve_deal_basis(drivers, hbs, target_book_debt)
    cash_to_balance_sheet = float(basis["cash_to_balance_sheet"])

    total_uses = enterprise_value + transaction_fees + total_financing_fees + cash_to_balance_sheet

    # ── Sources ───────────────────────────────────────────────────────────
    total_debt = sum(float(t.get("funded_amount", 0)) for t in debt_tranches)
    equity_contribution = total_uses - total_debt

    # Management rollover (RD-1 row 15 build-new): seller/management proceeds rolled
    # into the new entity's equity. A SOURCE that reduces the sponsor's CASH equity —
    # never a Use of cash (modeling it cash-out-then-back-in corrupts fee bases and
    # ownership splits). Total equity (the S&U plug) is unchanged; the rollover is a
    # second equity class inside it, clamped to the plug so Sources ≡ Uses holds.
    management_rollover = min(
        max(float(drivers.get("management_rollover_amount", 0) or 0), 0.0),
        max(equity_contribution, 0.0),
    )
    sponsor_equity = equity_contribution - management_rollover

    sources = {
        "total_debt": round(total_debt, 2),
        "equity_contribution": round(equity_contribution, 2),
        "sponsor_equity": round(sponsor_equity, 2),
        "management_rollover": round(management_rollover, 2),
        "total_sources": round(total_uses, 2),
        "tranches": [],
    }
    for t in debt_tranches:
        sources["tranches"].append({
            "name": t.get("tranche_name", ""),
            "amount": round(float(t.get("funded_amount", 0)), 2),
            "rate": float(t.get("interest_rate", 0)),
            "fees": round(float(t.get("funded_amount", 0)) * float(t.get("fees_pct", 0)), 2),
        })

    uses = {
        "enterprise_value": round(enterprise_value, 2),
        "transaction_fees": round(transaction_fees, 2),
        "financing_fees": round(total_financing_fees, 2),
        "cash_to_balance_sheet": round(cash_to_balance_sheet, 2),
        "total_uses": round(total_uses, 2),
    }

    purchase_price_extra = float(basis.pop("_purchase_price_extra"))
    basis["purchase_price"] = round(enterprise_value + purchase_price_extra, 2)
    if basis["basis"] == DEAL_BASIS_EQUITY_PURCHASE and basis["target_book_debt"] is not None:
        # Equity price paid to the seller; the book debt is refinanced alongside it.
        basis["equity_purchase_price"] = round(
            enterprise_value + float(basis["target_book_cash"]) - float(basis["target_book_debt"]), 2
        )
    else:
        basis["equity_purchase_price"] = None

    # ── Credit Stats ─────────────────────────────────────────────────────
    credit_stats = {
        "entry_multiple": round(effective_multiple, 2),
        "total_leverage": round(total_debt / ltm_ebitda, 2) if ltm_ebitda else 0,
        "equity_pct": round(equity_contribution / total_uses, 4) if total_uses else 0,
        "debt_pct": round(total_debt / total_uses, 4) if total_uses else 0,
    }

    # ── Pro-Forma Opening Balance Sheet ──────────────────────────────────
    # Purchase-accounting opening BS: identifiable assets + liabilities are carried at the
    # target's historical book values, equity is the Sources & Uses plug, and GOODWILL is
    # the balancing item — derived so total_assets == total_liabilities + equity BY
    # CONSTRUCTION (goodwill = equity consideration + assumed liabilities − identifiable net
    # assets). Goodwill is NOT a free input: a free goodwill alongside the free equity plug
    # left the opening sheet unbalanced (bs_check ≠ 0) and the gap carried into every
    # projected year. A negative result (a bargain purchase, identifiable net assets above
    # the consideration) is left as-is rather than fabricated to zero. Cash is the
    # deal-basis PF opening cash resolved above, never the target's raw book cash.
    cash = round(float(basis["pf_opening_cash"]), 2)
    accounts_receivable = round(float(hbs.get("accounts_receivable", 0)), 2)
    inventory = round(float(hbs.get("inventory", 0)), 2)
    other_current_assets = round(float(hbs.get("other_current_assets", 0)), 2)
    gross_fixed_assets = round(float(hbs.get("gross_fixed_assets", 0)), 2)
    accumulated_depreciation = round(float(hbs.get("accumulated_depreciation", 0)), 2)
    # The grid carries NET PP&E directly (line_code "ppe"); fall back to gross − accumulated
    # depreciation for any source that splits them.
    net_fixed_assets = round(
        float(hbs.get("net_fixed_assets", gross_fixed_assets - accumulated_depreciation)), 2
    )
    intangibles = round(float(hbs.get("intangibles", 0)), 2)
    other_assets = round(float(hbs.get("other_assets", 0)), 2)
    # COMPLETE identifiable assets — every captured historical asset line, so goodwill is the
    # genuine acquisition premium rather than a plug silently absorbing dropped lines. (Historical
    # "Intangibles & Goodwill" is carried here; writing off a target's material existing goodwill
    # pre-close is a future refinement.)
    identifiable_assets = round(
        cash + accounts_receivable + inventory + other_current_assets
        + net_fixed_assets + intangibles + other_assets, 2
    )

    accounts_payable = round(float(hbs.get("accounts_payable", 0)), 2)
    accrued_expenses = round(float(hbs.get("accrued_expenses", 0)), 2)
    other_current_liabilities = round(float(hbs.get("other_current_liabilities", 0)), 2)
    total_liabilities = round(
        accounts_payable + accrued_expenses + other_current_liabilities + total_debt, 2
    )

    # Goodwill is the plug that closes A = L + E at entry.
    goodwill = round(total_liabilities + equity_contribution - identifiable_assets, 2)
    total_assets = round(identifiable_assets + goodwill, 2)
    total_liabilities_equity = round(total_liabilities + equity_contribution, 2)

    pf_bs = {
        "cash": cash,
        "accounts_receivable": accounts_receivable,
        "inventory": inventory,
        "other_current_assets": other_current_assets,
        "gross_fixed_assets": gross_fixed_assets,
        "accumulated_depreciation": accumulated_depreciation,
        "net_fixed_assets": net_fixed_assets,
        "intangibles": intangibles,
        "goodwill": goodwill,
        "other_assets": other_assets,
        "total_assets": total_assets,
        "accounts_payable": accounts_payable,
        "accrued_expenses": accrued_expenses,
        "other_current_liabilities": other_current_liabilities,
        "total_debt": round(total_debt, 2),
        "total_liabilities": total_liabilities,
        "equity": round(equity_contribution, 2),
        "total_liabilities_equity": total_liabilities_equity,
        "bs_check": round(total_assets - total_liabilities_equity, 2),
    }

    # ── ASC-805 Pro-Forma Opening Balance Sheet (AC-C3 leg 2) ────────────
    # Correct fee treatment (see the module docstring for why the legacy block
    # above is retained as the engine feed):
    #   * transaction costs → EXPENSED at close: opening retained earnings
    #     = −transaction_fees (ASC 805-10-25-23), presented in the equity section;
    #   * financing fees → CONTRA-DEBT OID: carrying debt = face − financing
    #     fees (ASC 835-30 / ASU 2015-03), amortized over the schedule by
    #     oid_amortization_vectors;
    #   * goodwill = the plug over the SAME identifiable assets with BOTH fee
    #     categories excluded — algebraically goodwill_legacy − txn − fin.
    # Identifiable assets and operating liabilities are the book values already
    # captured above; the plug closes A = L + E by construction, so
    # bs_check_asc805 ≡ 0 (asserted line-by-line in the golden).
    debt_oid = round(total_financing_fees, 2)
    total_debt_carrying = round(total_debt - debt_oid, 2)
    transaction_costs_expensed = round(-transaction_fees, 2)
    total_equity_asc805 = round(equity_contribution + transaction_costs_expensed, 2)
    total_liabilities_asc805 = round(
        accounts_payable + accrued_expenses + other_current_liabilities + total_debt_carrying, 2
    )
    goodwill_asc805 = round(
        total_liabilities_asc805 + total_equity_asc805 - identifiable_assets, 2
    )
    total_assets_asc805 = round(identifiable_assets + goodwill_asc805, 2)
    total_le_asc805 = round(total_liabilities_asc805 + total_equity_asc805, 2)
    pf_bs_asc805 = {
        "cash": cash,
        "accounts_receivable": accounts_receivable,
        "inventory": inventory,
        "other_current_assets": other_current_assets,
        "gross_fixed_assets": gross_fixed_assets,
        "accumulated_depreciation": accumulated_depreciation,
        "net_fixed_assets": net_fixed_assets,
        "intangibles": intangibles,
        "goodwill": goodwill_asc805,
        "other_assets": other_assets,
        "total_assets": total_assets_asc805,
        "accounts_payable": accounts_payable,
        "accrued_expenses": accrued_expenses,
        "other_current_liabilities": other_current_liabilities,
        "total_debt_face": round(total_debt, 2),
        "debt_oid": debt_oid,
        "total_debt": total_debt_carrying,          # carrying value (face − OID)
        "total_liabilities": total_liabilities_asc805,
        "contributed_equity": round(equity_contribution, 2),
        "transaction_costs_expensed": transaction_costs_expensed,
        "equity": total_equity_asc805,
        "total_liabilities_equity": total_le_asc805,
        "bs_check": round(total_assets_asc805 - total_le_asc805, 2),
    }

    return {
        "sources": sources,
        "uses": uses,
        "deal_basis": basis,
        "credit_stats": credit_stats,
        "pf_opening_bs": pf_bs,
        "pf_opening_bs_asc805": pf_bs_asc805,
        "ltm_ebitda": round(ltm_ebitda, 2),
        # entry_equity stays the TOTAL equity plug (both classes) — the one BS/returns
        # denominator source of truth. The sponsor-vs-rollover split rides alongside.
        "entry_equity": round(equity_contribution, 2),
        "sponsor_equity": round(sponsor_equity, 2),
        "management_rollover": round(management_rollover, 2),
    }


class BridgeConstructionError(ValueError):
    """The Book→Dr/Cr→PF opening bridge could not be constructed as a balanced
    journal-entry set footing to the engine's ASC-805 block. An unbalanced
    adjustments vector is a construction bug (usually an unbalanced or incomplete
    pre-deal book) — fail loud, never plug a balancing difference."""


def derive_opening_bridge(
    su: EnginePayload,
    historical_bs: dict[str, float] | None = None,
    *,
    book_debt: float = 0.0,
    book_common_equity: float = 0.0,
    book_retained_earnings: float = 0.0,
) -> EnginePayload:
    """Project a :func:`compute_sources_uses` result into the Book | Adjustments |
    Pro Forma opening bridge (C2-c, plan 20260717-three-statement-spine AC-6).

    The adjustments vector is journal-entry pairs DERIVED from the S&U/PPA outputs
    already in ``su`` — debt raise (per tranche at face), financing-fee OID
    contra-debt, pre-deal debt refinance, goodwill recognition (the ASC-805
    residual, read from the engine block — never recomputed here), pre-deal equity
    elimination, transaction costs expensed, sponsor equity + management rollover,
    and the deal-basis cash legs (seller-retained pre-deal cash out, funded minimum
    operating cash in — :func:`resolve_deal_basis`). The Pro Forma column is READ from ``su["pf_opening_bs_asc805"]`` line-by-line —
    the bridge is a PROJECTION of engine truth, never a second computation.

    ``historical_bs`` is the same book dict the engine consumed (identifiable
    assets + operating liabilities); ``book_debt`` / ``book_common_equity`` /
    ``book_retained_earnings`` are the pre-deal lines that dict deliberately
    excludes (refinanced/eliminated at close — see
    serializers.book_debt_equity_from_rows, the companion extractor).

    Fails loud (:class:`BridgeConstructionError`) if total debits != total credits
    or any row's Book + Adjustments != Pro Forma to the cent.
    """
    asc = su["pf_opening_bs_asc805"]
    uses = su["uses"]
    sources = su["sources"]
    hbs = historical_bs or {}

    # ── Book column: the pre-deal balance sheet, read exactly as the engine read it.
    gross = float(hbs.get("gross_fixed_assets", 0))
    accum = float(hbs.get("accumulated_depreciation", 0))
    book = {
        "cash": round(float(hbs.get("cash", 0)), 2),
        "accounts_receivable": round(float(hbs.get("accounts_receivable", 0)), 2),
        "inventory": round(float(hbs.get("inventory", 0)), 2),
        "other_current_assets": round(float(hbs.get("other_current_assets", 0)), 2),
        "net_fixed_assets": round(float(hbs.get("net_fixed_assets", gross - accum)), 2),
        "intangibles": round(float(hbs.get("intangibles", 0)), 2),
        "goodwill": 0.0,  # pre-existing book goodwill rides in the intangibles line
        "other_assets": round(float(hbs.get("other_assets", 0)), 2),
        "accounts_payable": round(float(hbs.get("accounts_payable", 0)), 2),
        "accrued_expenses": round(float(hbs.get("accrued_expenses", 0)), 2),
        "other_current_liabilities": round(float(hbs.get("other_current_liabilities", 0)), 2),
        "total_debt": round(float(book_debt), 2),
        "contributed_equity": round(float(book_common_equity), 2),
        "transaction_costs_expensed": round(float(book_retained_earnings), 2),
    }

    # ── The journal-entry vector (each entry labeled with its S&U source line).
    entries: list[EnginePayload] = []

    def _entry(label: str, source: str, line_code: str, side: str, amount: object) -> None:
        amt = round(float(amount or 0), 2)
        if amt == 0.0:
            return  # a zero-amount entry carries no information
        if amt < 0.0:  # a negative magnitude posts on the opposite side (bargain-purchase goodwill, accumulated deficit)
            side = "credit" if side == "debit" else "debit"
            amt = -amt
        entries.append({
            "entry_id": f"adj-{len(entries) + 1}", "label": label, "source": source,
            "line_code": line_code, "side": side, "amount": amt,
        })

    for t in sources["tranches"]:
        name = t.get("name") or "tranche"  # SA-OK: dict.get on the in-memory S&U tranche dicts, NOT an ORM query
        _entry(f"Debt raise — {name} (funded at face)",
               "sources.tranches", "total_debt", "credit",
               t.get("amount"))  # SA-OK: dict.get on the in-memory S&U tranche dicts, NOT an ORM query
    _entry("Financing fees — OID contra-debt (ASC 835-30)",
           "uses.financing_fees", "total_debt", "debit", uses["financing_fees"])
    _entry("Pre-deal debt refinanced at close",
           "book.debt", "total_debt", "debit", book["total_debt"])
    _entry("Goodwill recognized (ASC 805 residual — never a free input)",
           "pf_opening_bs_asc805.goodwill", "goodwill", "debit", asc["goodwill"])
    _entry("Pre-deal common equity eliminated (ASC 805)",
           "book.common_equity", "contributed_equity", "debit", book["contributed_equity"])
    _entry("Sponsor equity contribution (S&U plug, cash class)",
           "sources.sponsor_equity", "contributed_equity", "credit", sources["sponsor_equity"])
    _entry("Management rollover equity (a Source, not a Use)",
           "sources.management_rollover", "contributed_equity", "credit",
           sources["management_rollover"])
    _entry("Pre-deal retained earnings eliminated (ASC 805)",
           "book.retained_earnings", "transaction_costs_expensed", "debit",
           book["transaction_costs_expensed"])
    _entry("Transaction costs expensed at close (ASC 805-10-25-23)",
           "uses.transaction_fees", "transaction_costs_expensed", "debit",
           uses["transaction_fees"])
    # Deal basis: a cash-free deal leaves the target's book cash with the seller and
    # opens on the minimum operating cash the sponsor funds; an equity purchase keeps
    # the acquired cash (paid for inside the sponsor equity above), so no cash entry.
    basis = su.get("deal_basis") or {}
    if basis.get("basis") == DEAL_BASIS_CASH_FREE:
        _entry("Pre-deal cash retained by the seller (cash-free / debt-free)",
               "deal_basis.cash_retained_by_seller", "cash", "credit",
               basis.get("cash_retained_by_seller"))
        _entry("Minimum operating cash funded at close (a Use)",
               "uses.cash_to_balance_sheet", "cash", "debit",
               uses.get("cash_to_balance_sheet"))

    total_debits = round(sum(e["amount"] for e in entries if e["side"] == "debit"), 2)
    total_credits = round(sum(e["amount"] for e in entries if e["side"] == "credit"), 2)
    if abs(round(total_debits - total_credits, 2)) > 0.01:
        raise BridgeConstructionError(
            f"adjustment entries do not balance: debits {total_debits:.2f} != credits "
            f"{total_credits:.2f} (difference {total_debits - total_credits:+.2f}). The pre-deal "
            "book must balance: assets = operating liabilities + book debt + book equity."
        )

    # ── Rows: Book + Adjustments must foot to the engine's PF line, per row.
    def _net(line_code: str, nature: str) -> float:
        increase = "debit" if nature == "asset" else "credit"
        return round(sum(
            e["amount"] if e["side"] == increase else -e["amount"]
            for e in entries if e["line_code"] == line_code
        ), 2)

    rows: list[EnginePayload] = []

    def _row(line_code: str, label: str, nature: str, book_value: float,
             pf_value: object, adjustment: float | None = None,
             is_total: bool = False) -> float:
        adj = _net(line_code, nature) if adjustment is None else round(adjustment, 2)
        bkv = round(float(book_value), 2)
        pfv = round(float(pf_value), 2)
        if abs(round(bkv + adj - pfv, 2)) > 0.01:
            raise BridgeConstructionError(
                f"bridge row '{line_code}' does not foot: book {bkv:.2f} + adjustments "
                f"{adj:+.2f} != pro forma {pfv:.2f}"
            )
        rows.append({
            "line_code": line_code, "label": label, "nature": nature, "book": bkv,
            "adjustment": adj, "pro_forma": pfv, "is_total": is_total,
            "entry_ids": [] if is_total else
            [e["entry_id"] for e in entries if e["line_code"] == line_code],
        })
        return adj

    asset_leaves = [
        ("cash", "Cash"), ("accounts_receivable", "Accounts Receivable"),
        ("inventory", "Inventory"), ("other_current_assets", "Other Current Assets"),
        ("net_fixed_assets", "Net PP&E"), ("intangibles", "Intangibles (book)"),
        ("goodwill", "Goodwill (ASC 805 residual)"), ("other_assets", "Other Assets"),
    ]
    asset_adj = 0.0
    for code, label in asset_leaves:
        asset_adj += _row(code, label, "asset", book[code], asc[code])
    book_assets = round(sum(book[code] for code, _ in asset_leaves), 2)
    _row("total_assets", "Total Assets", "asset", book_assets, asc["total_assets"],
         adjustment=asset_adj, is_total=True)

    liab_leaves = [
        ("accounts_payable", "Accounts Payable"), ("accrued_expenses", "Accrued Expenses"),
        ("other_current_liabilities", "Other Current Liabilities"),
        ("total_debt", "Debt (carrying: face − OID)"),
    ]
    liab_adj = 0.0
    for code, label in liab_leaves:
        liab_adj += _row(code, label, "liability_equity", book[code], asc[code])
    book_liabs = round(sum(book[code] for code, _ in liab_leaves), 2)
    _row("total_liabilities", "Total Liabilities", "liability_equity", book_liabs,
         asc["total_liabilities"], adjustment=liab_adj, is_total=True)

    ce_adj = _row("contributed_equity", "Contributed Equity (the S&U plug)",
                  "liability_equity", book["contributed_equity"], asc["contributed_equity"])
    re_adj = _row("transaction_costs_expensed",
                  "Retained Earnings — transaction costs expensed at close",
                  "liability_equity", book["transaction_costs_expensed"],
                  asc["transaction_costs_expensed"])
    equity_adj = round(ce_adj + re_adj, 2)
    book_equity = round(book["contributed_equity"] + book["transaction_costs_expensed"], 2)
    _row("equity", "Total Equity", "liability_equity", book_equity, asc["equity"],
         adjustment=equity_adj, is_total=True)
    _row("total_liabilities_equity", "Total Liabilities & Equity", "liability_equity",
         round(book_liabs + book_equity, 2), asc["total_liabilities_equity"],
         adjustment=round(liab_adj + equity_adj, 2), is_total=True)

    return {
        "entries": entries,
        "rows": rows,
        "totals": {
            "total_debits": total_debits,
            "total_credits": total_credits,
            "book_bs_check": round(book_assets - book_liabs - book_equity, 2),
            "pf_bs_check": round(float(asc["bs_check"]), 2),
        },
    }

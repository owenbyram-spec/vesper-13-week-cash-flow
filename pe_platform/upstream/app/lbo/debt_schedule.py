"""
app/lbo/debt_schedule.py
────────────────────────
Multi-tranche debt schedule engine with amortization, PIK, bullet payments,
optional cash sweep, and (Phase 2A, RD-1 rows 5-6) a revolver / liquidity
facility plus cash-ledger sweep realism (sweep % < 100, per-year step-downs,
a minimum-operating-cash floor, explicit seniority ranks).

TWO sweep semantics, selected by ``sweep_terms``:

- ``sweep_terms is None`` and no revolver tranche → the PRE-EXISTING memoryless
  flat sweep: 100% of the CURRENT year's excess (fcf − scheduled amortization)
  pays down participating tranches in list order. Byte-identical to the prior
  engine — every stored scenario and golden keeps its numbers.
- ``sweep_terms`` provided (or a revolver tranche present) → the CASH-LEDGER
  schedule: a running cash balance (seeded from ``opening_cash``) absorbs each
  year's excess; the revolver draws on a shortfall below the minimum-cash floor
  and is repaid FIRST from surplus; the term-loan sweep then takes
  ``sweep_pct`` (or the per-year step-down list) of the year's positive excess,
  capped by the cash actually above the floor, in seniority order.

Sweep % / step-downs / min-cash floor are PER-DEAL credit-agreement inputs —
NO market default grid is encoded (research gap G1; refuse over fabricate).

MATURITY-AWARE repayment (AC-C4 leg 1): a tranche payload may carry
``term_years`` — the number of MODEL years until the tranche's contractual
maturity (year index ``term_years − 1``). When the maturity falls INSIDE the
model horizon (``0 < term_years < num_years``) the ENTIRE remaining balance
(scheduled-amort residual + bullet + capitalized PIK) is repaid at the maturity
year — debt cannot survive its maturity — and the balance is zero every year
after. When ``term_years`` is absent or the maturity sits at/after the horizon,
behavior is unchanged: the ``bullet_pct`` payment fires at the FINAL model year
(the pre-existing convention). Funding convention for the maturity repayment:
it is a MANDATORY outflow included in ``total_amortization`` — in the legacy
memoryless mode the model assumes it is funded/refinanced from available cash
(exactly like the pre-existing horizon bullet, which never checked cash
sufficiency); in cash-ledger mode it flows through the ledger, so a shortfall
draws the revolver and any unmet floor is REPORTED as ``liquidity_shortfall``,
never hidden.

PAST-MATURITY-OUTSTANDING hold (AC-2, plan 20260720 live-verify): a tranche
payload flagged ``past_maturity_outstanding`` (set by the engine composition when
a stated maturity pre-dates the books-anchored projection start while the books
still carry the balance) is HELD: interest accrues, but no scheduled amortization
and no bullet is invented — the contractual schedule is exhausted, so any
catch-up paydown would be a fabrication. The hold is DISCLOSED (per-year tranche
rows + the top-level ``disclosures`` list), never silent; the cash sweep (a
cash-driven, not schedule-driven, paydown) still applies where flagged.

AMORT-EXHAUSTED interest-only disclosure (plan 20260720 live-verify round 5): a
payload flagged ``amort_exhausted_interest_only`` (engine_composition — a seasoned
loan whose amortization schedule ended before the projection anchor while a stated
maturity is still ahead) carries NO scheduled amortization (the composition
withholds ``amort_years``); interest accrues and the stated maturity bullet
retires the balance. The shape is DISCLOSED (per-year rows + ``disclosures``) —
a balance riding interest-only to a bullet is a statement about the loan documents
the user should verify, never a silent schedule. The flag itself changes no math
here; it is the disclosure marker for a schedule the composition already set.

SEASONED sweep exclusion (AC-1, plan 20260720 live-verify round 4): a payload
flagged ``seasoned`` (engine_composition._apply_seasoned_terms — a dated loan
whose start pre-dates the forecast anchor) is EXCLUDED from the cash sweep by
default in BOTH sweep modes: its paydown is the contractual schedule (amort
installments + the real maturity bullet) ONLY. The deal-wide ``sweep_pct`` 1.0
default is an at-close LBO modeling assumption the loan's own history disproves
— the books held the balance for years while excess cash went elsewhere. The
exclusion removes the tranche from the sweep ALLOCATION BASE (the excess flows
to the remaining participating tranches in order, or accumulates as cash when
none participate) — never a silently-zeroed share. The EXPLICIT payload key
``sweep_participating`` overrides in either direction: ``True`` re-enables the
sweep on a seasoned tranche (the documented per-tranche opt-in); ``False``
excludes any tranche; absent → participation is the stored ``cash_sweep_flag``
for undated payloads (byte-parity) and ``False`` for ``seasoned`` ones. The
#446 hold above is orthogonal: a held NON-seasoned tranche keeps its flagged
sweep participation (the sweep is its only exit).

Payload convention: the tranche definitions / year rows are the opaque engine
dicts the callers thread (the waterfall_solve.DebtTranche precedent) —
``dict[str, object]``.
"""

# The opaque tranche-definition / live-state / year-row payloads.
TranchePayload = dict[str, object]


def _init_tranche_state(debt_tranches: list[TranchePayload]) -> list[TranchePayload]:
    """Materialize the per-tranche live state from the tranche assumption dicts."""
    tranche_state = []
    seen_names: set[str] = set()
    for t in debt_tranches:
        # AC-B1: the per-tranche detail maps in compute_debt_schedule are keyed by
        # ts["name"] (~10 sites), but the upstream name is NOT unique — an unnamed
        # tranche defaults to the literal "Tranche". Two same-named tranches would
        # collide on one map key, the dict would keep only the LAST, and the
        # interest-identity guard (engine_composition.assert_interest_identity)
        # would then see Σtranche < total and refuse the whole model. Guarantee a
        # UNIQUE name at build: the first occurrence keeps the raw name; each later
        # collision is disambiguated "Tranche" → "Tranche (2)" → "Tranche (3)" → …
        # (advancing past any suffix that is itself already taken). Names only —
        # no math and no other field is touched.
        raw_name = t.get("tranche_name", "Tranche")  # SA-OK: bounded-loop dict.get on in-memory tranche dicts, not a DB query
        unique_name = raw_name
        _dup = 2
        while unique_name in seen_names:
            unique_name = f"{raw_name} ({_dup})"
            _dup += 1
        seen_names.add(unique_name)
        tranche_state.append({
            "name": unique_name,
            # W3c (plan 20260719): the PROJECTION opens each tranche at its re-based FY balance
            # (Σ opening_balance == the FY-real total_debt); absent it (no re-seed) falls back to
            # funded_amount → byte-parity. opening_balance is read ONLY here, NEVER by compute_sources_uses
            # (which reads funded_amount) — the valuation firewall.
            "balance": float(t.get("opening_balance", t.get("funded_amount", 0))),
            # AC-1 (plan 20260720 live-verify): the CONTRACTUAL-schedule basis — scheduled
            # amortization (basis / amort_years) and the horizon bullet (basis × bullet_pct)
            # pace off the SAME balance the tranche opens at. In the re-base regime the
            # opening is scaled to the books total while funded_amount stays the original
            # commitment; pacing amort off funded then pays a re-based balance at an
            # un-re-based rate and retires every tranche in year 0 (the live scenario-9
            # defect: opening 485,578.32 vs funded/2 = 1,221,912/yr). No opening_balance
            # (no re-seed) → basis == funded_amount → byte-parity.
            # AC-1 round 5 (SEASONED loans): an EXPLICIT ``amort_basis`` payload key wins —
            # a seasoned (dated, pre-anchor) loan opens AND paces at its books-stated
            # outstanding over a REMAINING-TERM float divisor (engine_composition
            # _apply_seasoned_terms sets basis = current_balance and amort_years =
            # (amort_end − anchor) / 365.25, so the balance retires exactly AT the loan's
            # own amortization end — never a fresh full term restarted at the anchor).
            # Absent the explicit key, the pre-existing opening/funded fallback is
            # byte-identical.
            "amort_basis": float(t.get("amort_basis", t.get("opening_balance", t.get("funded_amount", 0)))),
            "rate": float(t.get("interest_rate", 0)),
            "rate_type": t.get("rate_type", "fixed"),
            "base_rate": float(t.get("base_rate", 0)),
            "spread": float(t.get("spread", 0)),
            "amort_years": t.get("amort_years"),
            "bullet_pct": float(t.get("bullet_pct", 0)),
            "pik": bool(t.get("pik_flag", False)),
            # AC-1 (plan 20260720 live-verify round 4): SEASONED tranches default OUT
            # of the cash sweep — contractual amort + the real maturity bullet only
            # (module docstring). The explicit ``sweep_participating`` key wins in
            # either direction; absent it, an undated payload keeps its stored
            # cash_sweep_flag byte-identically and a ``seasoned`` payload is excluded.
            "cash_sweep": bool(
                t["sweep_participating"] if "sweep_participating" in t
                else (t.get("cash_sweep_flag", False) and not t.get("seasoned"))  # SA-OK: bounded-loop dict.get on in-memory tranche dicts, not a DB query
            ),
            # Revolver / liquidity facility (RD-1 row 6 build-new): draws on a cash
            # shortfall up to `commitment`, repaid first from surplus. `funded_amount`
            # is the initially-drawn balance (usually 0).
            "revolver": bool(t.get("revolver_flag", False)),
            "commitment": float(t.get("commitment", 0)),
            "commitment_fee_pct": float(t.get("commitment_fee_pct", 0)),
            # Explicit seniority rank for the sweep order (lower = more senior);
            # None → list order (the pre-existing convention).
            "seniority_rank": t.get("seniority_rank"),
            # Model years to the tranche's contractual maturity (AC-C4 leg 1);
            # None → no stated maturity → the pre-existing horizon-bullet behavior.
            "term_years": int(t["term_years"]) if t.get("term_years") else None,
            # AC-2 (plan 20260720 live-verify): the stated maturity already passed
            # BEFORE the projection anchor while the books-real opening still
            # carries the balance (engine_composition's re-seed decrement sets
            # this). A DISCLOSED discrepancy: the balance is HELD (no scheduled
            # amort, no bullet — the contractual schedule is exhausted, so any
            # catch-up paydown would be invented); interest still accrues and a
            # sweep-participating tranche may still pay down from real FCF.
            "past_maturity_outstanding": bool(t.get("past_maturity_outstanding", False)),
            # Round 5: the amort schedule ended pre-anchor while a stated maturity
            # is still ahead — interest-only to the maturity bullet, DISCLOSED
            # (module docstring). Disclosure-only: the composition already withheld
            # amort_years, so no flow math branches on this flag.
            "amort_exhausted_interest_only": bool(t.get("amort_exhausted_interest_only", False)),
        })
    return tranche_state


def _tranche_year_flows(ts: TranchePayload, yr: int, num_years: int, fraction: float = 1.0) -> dict[str, float]:
    """One tranche's contractual flows for one year: interest, mandatory amort, bullet.

    A revolver has no scheduled amortization / bullet / PIK; its interest is cash on
    the drawn balance plus a commitment fee on the undrawn commitment.

    ``fraction`` (W3d, plan 20260719) prorates the RUN-RATE flows — interest, mandatory
    amort, commitment fee — for a partial-period stub year (f<1); default 1.0 → byte-parity
    (×1.0 is exact in IEEE). The BULLET is NOT prorated (a discrete principal/maturity event,
    not a run-rate flow), and per-tranche detail AND totals scale together so the P&L
    interest identity (derived from these totals) stays green.
    """
    beg = ts["balance"]
    if ts["rate_type"] == "floating":
        eff_rate = ts["base_rate"] + ts["spread"]
    else:
        eff_rate = ts["rate"]

    if ts["revolver"]:
        undrawn = max(ts["commitment"] - beg, 0.0)
        commitment_fee = round(undrawn * ts["commitment_fee_pct"] * fraction, 2)  # W3d: partial-period stub
        return {
            "beg": beg, "eff_rate": eff_rate,
            "interest_cash": round(beg * eff_rate * fraction + commitment_fee, 2),
            "interest_pik": 0.0, "amort": 0.0, "bullet": 0.0,
            "commitment_fee": commitment_fee,
        }

    interest = round(beg * eff_rate * fraction, 2)  # W3d: ×f for the partial-period stub year
    interest_cash = 0.0 if ts["pik"] else interest
    interest_pik = interest if ts["pik"] else 0.0

    # Past-maturity-outstanding hold (AC-2): the contractual schedule is exhausted
    # but the books still carry the balance — NO scheduled amort and NO bullet is
    # invented; the balance holds (interest above still accrues; the sweep, a
    # cash-driven — not schedule-driven — paydown, still applies where flagged).
    if ts.get("past_maturity_outstanding"):  # SA-OK: dict.get on the in-memory tranche state, not a DB query
        return {
            "beg": beg, "eff_rate": eff_rate, "interest_cash": interest_cash,
            "interest_pik": interest_pik, "amort": 0.0, "bullet": 0.0,
            "commitment_fee": 0.0,
        }

    amort = 0.0
    if ts["amort_years"] and ts["amort_years"] > 0:
        # AC-1: equal installments off amort_basis — the RE-BASED opening in the re-seed
        # regime (== funded_amount otherwise), so the pace scales with the balance it pays.
        amort = round(ts["amort_basis"] / ts["amort_years"] * fraction, 2)  # W3d: partial-period installment
        amort = min(amort, beg)  # Can't overpay

    # Maturity-aware repayment (AC-C4 leg 1): with a stated term INSIDE the model
    # horizon, the maturity year retires the ENTIRE remaining balance (bullet +
    # residual principal + capitalized PIK) — debt cannot survive its maturity.
    # No/late maturity keeps the pre-existing horizon-bullet convention exactly.
    term = ts["term_years"]
    matures_inside = term is not None and 0 < term < num_years

    bullet = 0.0
    if matures_inside and yr == term - 1:
        bullet = round(max(beg + interest_pik - amort, 0.0), 2)
    elif not matures_inside and ts["bullet_pct"] > 0 and yr == num_years - 1:
        # AC-1: bullet_pct is a fraction of principal — same amort_basis scaling as the
        # installments, so the whole contractual schedule re-bases by ONE factor.
        bullet = round(ts["amort_basis"] * ts["bullet_pct"], 2)
        bullet = min(bullet, beg - amort)

    return {
        "beg": beg, "eff_rate": eff_rate, "interest_cash": interest_cash,
        "interest_pik": interest_pik, "amort": amort, "bullet": bullet,
        "commitment_fee": 0.0,
    }


def _sweep_order(tranche_state: list[TranchePayload]) -> list[TranchePayload]:
    """Participating term tranches in sweep order.

    Explicit ``seniority_rank`` ascending where present; unranked tranches follow
    every ranked one in their original list order (stable — the pre-existing
    list-order convention is the fallback, RD-1 row 5 seniority delta).
    """
    participating = [ts for ts in tranche_state if ts["cash_sweep"] and not ts["revolver"]]
    ranked = sorted(
        enumerate(participating),
        key=lambda pair: (
            pair[1]["seniority_rank"] is None,
            pair[1]["seniority_rank"] if pair[1]["seniority_rank"] is not None else 0,
            pair[0],
        ),
    )
    return [pair[1] for pair in ranked]


def compute_debt_schedule(
    debt_tranches: list[TranchePayload],
    num_years: int,
    fcf_by_year: list[float] | None = None,
    sweep_terms: TranchePayload | None = None,
    cash_outflows_by_year: list[float] | None = None,
    period_fraction: list[float] | None = None,
) -> dict[str, object]:
    """
    Compute a full multi-tranche debt schedule.

    Args:
        debt_tranches: List of tranche definitions (see _init_tranche_state; a
            ``revolver_flag`` tranche is the liquidity facility).
        num_years: Number of projection years.
        fcf_by_year: Free cash flow per year (for the cash sweep / revolver ledger).
        sweep_terms: None → the pre-existing memoryless 100% sweep. A dict of
            per-deal terms → the cash-ledger schedule: ``sweep_pct`` (0..1 flat),
            ``sweep_pct_by_year`` (step-downs; wins over the flat pct where present),
            ``min_cash_floor`` (operating cash never swept below), ``opening_cash``.
        cash_outflows_by_year: Cash paid out below the debt waterfall (interim equity
            distributions), fed back by the solver so the ledger's cash is real.

    Returns:
        Per-year, per-tranche schedule with beginning/ending balances, interest,
        amortization, sweep, revolver draw/repay, and totals. In ledger mode each
        year also carries ``cash_balance``, ``distributable_surplus``, and
        ``liquidity_shortfall`` (an UNMET floor after the revolver is exhausted —
        represented honestly, never clamped away).
    """
    fcf = fcf_by_year or [0.0] * num_years
    _pf = period_fraction or [1.0] * num_years  # W3d: per-year run-rate fraction (stub year<1); default all 1.0
    tranche_state = _init_tranche_state(debt_tranches)
    has_revolver = any(ts["revolver"] for ts in tranche_state)
    ledger_mode = sweep_terms is not None or has_revolver

    # AC-2 disclosures — collected at the OPENING (balances mutate through the year
    # loop): a tranche whose stated maturity passed before the projection anchor
    # while its books-anchored opening is still outstanding is surfaced, never a
    # silent hold. A zero-opening flagged tranche (books carry no debt) discloses
    # nothing — there is no discrepancy to report.
    disclosures = [
        {
            "type": "past_maturity_outstanding",
            "tranche": ts["name"],
            "opening_balance": round(float(ts["balance"]), 2),
            "message": (
                f"{ts['name']}: stated maturity passed before the projection start, but the "
                "books still carry this balance — held at the books-anchored level (interest "
                "accrues; no contractual paydown is scheduled). Reconcile the tranche terms "
                "to the loan documents on the Sources & Uses tab."
            ),
        }
        for ts in tranche_state
        if ts.get("past_maturity_outstanding") and float(ts["balance"]) > 0
    ]
    # Round 5: the amort-exhausted interest-only shape is disclosed beside the hold —
    # same zero-opening suppression (no balance → no discrepancy to report).
    disclosures += [
        {
            "type": "amort_exhausted_interest_only",
            "tranche": ts["name"],
            "opening_balance": round(float(ts["balance"]), 2),
            "message": (
                f"{ts['name']}: the amortization schedule ended before the projection "
                "start, but a stated maturity is still ahead — the balance rides "
                "interest-only to the maturity bullet. Verify the loan's amortization "
                "terms on the Sources & Uses tab."
            ),
        }
        for ts in tranche_state
        if ts.get("amort_exhausted_interest_only") and float(ts["balance"]) > 0
    ]

    terms = sweep_terms or {}
    flat_pct = float(terms.get("sweep_pct", 1.0))
    pct_by_year = [float(v) for v in (terms.get("sweep_pct_by_year") or [])]
    floor = float(terms.get("min_cash_floor", 0.0))
    cash = float(terms.get("opening_cash", 0.0))
    outflows = cash_outflows_by_year or [0.0] * num_years

    yearly = []
    for yr in range(num_years):
        year_data: dict[str, object] = {
            "year_index": yr,
            "tranches": {},
            "total_beginning": 0.0,
            "total_interest_cash": 0.0,
            "total_interest_pik": 0.0,
            "total_amortization": 0.0,
            "total_ending": 0.0,
        }

        for ts in tranche_state:
            flows = _tranche_year_flows(ts, yr, num_years, fraction=(_pf[yr] if yr < len(_pf) else 1.0))
            year_data["total_beginning"] += flows["beg"]

            end_bal = round(
                flows["beg"] + flows["interest_pik"] - flows["amort"] - flows["bullet"], 2
            )
            end_bal = max(end_bal, 0)
            ts["balance"] = end_bal

            year_data["total_interest_cash"] += flows["interest_cash"]
            year_data["total_interest_pik"] += flows["interest_pik"]
            year_data["total_amortization"] += flows["amort"] + flows["bullet"]

            year_data["tranches"][ts["name"]] = {
                "beginning_balance": round(flows["beg"], 2),
                "interest_rate": round(flows["eff_rate"], 6),
                "interest_cash": round(flows["interest_cash"], 2),
                "interest_pik": round(flows["interest_pik"], 2),
                "scheduled_amort": round(flows["amort"], 2),
                "bullet_payment": round(flows["bullet"], 2),
                "ending_balance": round(end_bal, 2),
            }
            if ts.get("past_maturity_outstanding") and flows["beg"] > 0:  # SA-OK: dict.get on in-memory tranche state, not a DB query
                # AC-2: the disclosed hold rides every year row so /debt-schedule
                # consumers see WHY this tranche neither amortizes nor bullets.
                year_data["tranches"][ts["name"]]["past_maturity_outstanding"] = True
            if ts.get("amort_exhausted_interest_only") and flows["beg"] > 0:  # SA-OK: dict.get on in-memory tranche state, not a DB query
                # Round 5: the interest-only-to-bullet shape rides every live year row
                # so consumers see WHY this tranche accrues but never amortizes.
                year_data["tranches"][ts["name"]]["amort_exhausted_interest_only"] = True
            if ts["revolver"]:
                year_data["tranches"][ts["name"]]["is_revolver"] = True
                year_data["tranches"][ts["name"]]["commitment_fee"] = flows["commitment_fee"]

        sweep_applied = 0.0
        if not ledger_mode:
            # ── PRE-EXISTING memoryless flat sweep (excess FCF pays down senior debt
            # first, in list order) — byte-identical to the prior engine. ──────────
            if fcf and yr < len(fcf):
                excess = fcf[yr] - year_data["total_amortization"]
                if excess > 0:
                    for ts in tranche_state:
                        if ts["cash_sweep"] and ts["balance"] > 0:
                            sweep = min(excess, ts["balance"])
                            ts["balance"] = round(ts["balance"] - sweep, 2)
                            sweep_applied += sweep
                            excess -= sweep
                            year_data["tranches"][ts["name"]]["cash_sweep"] = round(sweep, 2)
                            year_data["tranches"][ts["name"]]["ending_balance"] = round(ts["balance"], 2)
        else:
            # ── CASH-LEDGER mode: running cash, revolver draw/repay, pct sweep,
            # min-cash floor (RD-1 rows 5-6). ─────────────────────────────────────
            excess = (fcf[yr] if yr < len(fcf) else 0.0) - year_data["total_amortization"]
            cash_avail = round(cash + excess, 2)
            revolvers = [ts for ts in tranche_state if ts["revolver"]]

            # 1. Draw on the revolver to restore the floor (a represented liquidity
            #    crunch — the prior engine could not draw at all).
            draw = 0.0
            if cash_avail < floor:
                need = round(floor - cash_avail, 2)
                for rv in revolvers:
                    headroom = round(rv["commitment"] - rv["balance"], 2)
                    take = min(need, max(headroom, 0.0))
                    if take > 0:
                        rv["balance"] = round(rv["balance"] + take, 2)
                        draw += take
                        need = round(need - take, 2)
                        year_data["tranches"][rv["name"]]["revolver_draw"] = round(take, 2)
                        year_data["tranches"][rv["name"]]["ending_balance"] = rv["balance"]
                cash_avail = round(cash_avail + draw, 2)
            # An unmet floor after the commitment is exhausted is REPORTED, not hidden.
            liquidity_shortfall = round(max(floor - cash_avail, 0.0), 2)

            # 2. Repay the revolver FIRST from surplus above the floor (the market
            #    convention: the facility is the most flexible, cheapest paydown).
            surplus = max(round(cash_avail - floor, 2), 0.0)
            repay = 0.0
            for rv in revolvers:
                take = min(surplus, rv["balance"])
                if take > 0:
                    rv["balance"] = round(rv["balance"] - take, 2)
                    repay += take
                    surplus = round(surplus - take, 2)
                    year_data["tranches"][rv["name"]]["revolver_repay"] = round(take, 2)
                    year_data["tranches"][rv["name"]]["ending_balance"] = rv["balance"]
            cash_avail = round(cash_avail - repay, 2)

            # 3. Term-loan sweep: sweep_pct × the year's POSITIVE excess cash flow
            #    (the credit-agreement ECF base), capped by the cash actually above
            #    the floor, applied in seniority order.
            pct = pct_by_year[yr] if yr < len(pct_by_year) else flat_pct
            sweep_budget = min(round(pct * max(excess, 0.0), 2), surplus)
            for ts in _sweep_order(tranche_state):
                if sweep_budget <= 0:
                    break
                take = min(sweep_budget, ts["balance"])
                if take > 0:
                    ts["balance"] = round(ts["balance"] - take, 2)
                    sweep_applied += take
                    sweep_budget = round(sweep_budget - take, 2)
                    year_data["tranches"][ts["name"]]["cash_sweep"] = round(take, 2)
                    year_data["tranches"][ts["name"]]["ending_balance"] = round(ts["balance"], 2)
            cash_avail = round(cash_avail - sweep_applied, 2)

            # 4. Below-waterfall cash outflows (interim distributions, fed back by
            #    the solver) leave the ledger AFTER the sweep — the same dollar is
            #    never spent twice.
            distributable_surplus = round(max(cash_avail - floor, 0.0), 2)
            outflow = round(outflows[yr] if yr < len(outflows) else 0.0, 2)
            cash = round(cash_avail - outflow, 2)

            year_data["total_amortization"] += repay  # a revolver repay is a paydown
            year_data["total_revolver_draw"] = round(draw, 2)
            year_data["total_revolver_repay"] = round(repay, 2)
            year_data["cash_balance"] = cash
            year_data["distributable_surplus"] = distributable_surplus
            year_data["liquidity_shortfall"] = liquidity_shortfall
            year_data["sweep_pct_applied"] = pct

        year_data["total_ending"] = round(sum(t["balance"] for t in tranche_state), 2)
        year_data["total_interest_cash"] = round(year_data["total_interest_cash"], 2)
        year_data["total_interest_pik"] = round(year_data["total_interest_pik"], 2)
        year_data["total_amortization"] = round(year_data["total_amortization"] + sweep_applied, 2)
        year_data["cash_sweep_applied"] = round(sweep_applied, 2)

        yearly.append(year_data)

    return {
        "schedule": yearly,
        "tranche_names": [ts["name"] for ts in tranche_state],
        # AC-2: [] on every pre-existing path (no flagged tranche) — parity-safe.
        "disclosures": disclosures,
    }

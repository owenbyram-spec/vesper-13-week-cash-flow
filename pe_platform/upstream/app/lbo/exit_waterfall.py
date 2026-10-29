"""
app/lbo/exit_waterfall.py
──────────────────────────
Exit waterfall: computes EV, equity value, MOIC, IRR, realized-distribution
metrics (DPI / cash-on-cash), and the equity allocation across the sponsor and
management-rollover classes.

Phase 2A (plan 20260706-cfo-grade-financial-workflows AC-2A.1/AC-2A.3):

- **Net-of-trapped-cash exit (the verified defect fix, RD-1 row 13/§12):** exit
  equity = EV − fees − NET debt (remaining debt − accumulated BS cash). The prior
  ``EV − fees − gross remaining debt`` let every unswept, undistributed dollar of
  cash vanish at exit, understating returns. This is now the ONE exit policy,
  shared with the FP&A path (app/fpa/exit_planning.py, which already netted cash).
  Callers that supply no cash (``cash_by_year`` absent AND no ``cash`` key on the
  exit-year forecast row) get cash = 0 — gross == net, the pre-fix arithmetic.
- **Exit EBITDA basis (RD-1 row 16):** ``drivers["exit_ebitda_basis"]`` selects
  "book" (default, parity) or "adjusted" — an explicit stated choice.
- **Realized metrics (RD-1 row 14, ILPA-shaped):** cumulative-distribution DPI per
  year plus at-exit cash-on-cash (gross and net-of-carry).
- **Management rollover (RD-1 row 15 build-new):** a second equity class. Proceeds
  split pro-rata by entry ownership; carry applies to the SPONSOR gain only —
  rollover holders pay no carry.

AC-C4 leg 4 (plan 20260716-five-domain-remediation) — honest multiple labeling +
the optional preferred hurdle:

- **``tvpi`` is the HEADLINE multiple:** TVPI = (Σ interim distributions + exit
  equity) ÷ entry equity — total value to paid-in, the ILPA-standard headline.
  ``gross_moic`` (and ``net_moic``) remain available UNCHANGED as the
  separately-labeled EXIT-ONLY multiples (exit proceeds ÷ entry, distributions
  excluded) — no silent redefinition; ``tvpi_net`` is the net-of-carry twin.
  For a deal with zero interim distributions TVPI ≡ gross MOIC by identity.
- **Preferred-return hurdle (optional, default OFF):**
  ``drivers["preferred_return_pct"]`` — an annual rate compounded over the hold
  on the SPONSOR entry. When set, carry accrues only on sponsor proceeds ABOVE
  ``sponsor_entry × (1 + pref)^hold`` (a HARD hurdle: no GP catch-up is modeled,
  and interim distributions are not credited against the hurdle — both are
  per-deal waterfall terms, disclosed simplifications). ``0.0`` reproduces the
  pre-existing 1× carry base exactly.


Decision D4 (plan 20260718-fycy-absolute-axis, AC-5) — DATED (transaction-date)
returns:

- The equity IRR anchors on the deal's EXPECTED TRANSACTION CLOSE DATE and uses the
  real forecast period-end dates the FYCY period spine populates on every column.
  When ``transaction_close_date`` + ``period_end_dates`` are supplied, ``_equity_irr``
  solves a day-count-accurate ``returns.xirr`` over those real dates instead of the
  uniform-annual-period ``returns.irr``. This is the whole point of D4: a mid-fiscal-
  year close (hold NOT an integer number of years) is priced on the actual day-count,
  which the period-count IRR cannot see. Absent the dates (a pure-logic call with no
  period spine — the 2-D sensitivity grid, or a legacy scenario whose forecast dates
  degraded to NULL under D5), it falls back to the period-indexed IRR, the correct
  answer when no calendar information exists. MOIC / cash-on-cash are timing-blind
  and are unchanged.

- The hold is a USER input in years beyond FYCY: ``exit_year`` (the driver) is the
  count of fiscal years after FYCY, so the exit lands on forecast index ``exit_year``
  (FYCY is forecast index 0). Changing the hold moves BOTH the exit column and — via
  the real exit period-end date — the dated IRR.
"""
import datetime

from .returns import irr, xirr, IRRError


def _equity_irr(
    entry_equity: float,
    hold_period: int,
    proceeds: float,
    distributions: list[float] | None = None,
    *,
    entry_date: datetime.date | None = None,
    flow_dates: list[datetime.date] | None = None,
) -> float:
    """True IRR for the equity position over its cash-flow vector.

    Builds ``[-entry, d_1, ..., d_{n-1}, proceeds + d_n]`` where ``d_k`` are the
    interim equity distributions (forecast years 0..hold-1; the exit-year
    distribution lands in the same period as the exit proceeds). Solved as a TRUE
    IRR (FIND-MONEY-002) — never the closed-form MOIC^(1/hold)-1 shortcut. With
    ``distributions`` None / all-zero the vector reduces to
    ``[-entry, 0, ..., proceeds]``, whose IRR equals the annualised-MOIC value by
    identity (so this stays fully backward-compatible).

    Decision D4 — dated flows: when ``entry_date`` and ``flow_dates`` are BOTH
    supplied, the flows are stamped with their real calendar dates (entry at
    ``entry_date`` = the transaction close date; the year-``k`` flow at
    ``flow_dates[k]`` = forecast period-``k`` period-end) and solved as a
    day-count-accurate ``returns.xirr``. ``flow_dates`` must carry at least
    ``hold_period`` dates (index-aligned to the forecast years). Absent either,
    the uniform-annual-period ``returns.irr`` is used.

    A position with no positive flow anywhere (total wipeout, no interim
    distributions) returns ``-1.0`` (-100%, limited liability). On a genuinely
    non-convergent vector the typed ``IRRError`` is surfaced as ``-1.0`` — NEVER a
    silent fall back to the shortcut.
    """
    if entry_equity <= 0 or hold_period <= 0:
        return 0.0
    interim = list(distributions or [])[:hold_period]
    flows = [-float(entry_equity)] + [0.0] * hold_period
    for k, dist in enumerate(interim):
        flows[k + 1] += float(dist)
    flows[hold_period] += float(proceeds)
    if all(f <= 0 for f in flows):
        return -1.0  # total loss: no positive flow anywhere (-100%, limited liability)

    use_dates = (
        entry_date is not None
        and flow_dates is not None
        and len(flow_dates) >= hold_period
    )
    try:
        if use_dates:
            # Entry at the transaction close date; flows[k+1] at forecast period-k's
            # real period-end (flow_dates[k]). The exit flow (flows[hold_period]) lands
            # on flow_dates[hold_period-1] = the exit period-end — same date as the
            # exit-year distribution, by construction.
            dated: list[tuple[datetime.date, float]] = [(entry_date, flows[0])]
            for k in range(hold_period):
                dated.append((flow_dates[k], flows[k + 1]))
            return round(xirr(dated), 4)
        return round(irr(flows), 4)
    except IRRError:
        return -1.0  # no economically-meaningful IRR; explicit — NOT a shortcut fallback


def _realized_metrics(
    entry_equity: float,
    hold_period: int,
    interim: list[float],
    equity_value: float,
    net_to_lps: float,
) -> dict[str, object]:
    """Cash-on-cash / DPI realized-distribution metrics (RD-1 row 14, ILPA-shaped).

    DPI = cumulative distributions ÷ paid-in capital — the REALIZED track, per year
    through the hold. At exit the proceeds realize, so the at-exit cash-on-cash
    multiple = (Σ interim distributions + exit proceeds) ÷ entry equity, reported
    gross AND net-of-carry. Entry ≤ 0 → zeros (no denominator is fabricated; the
    upstream compute already refuses a defaulted entry equity).
    """
    if entry_equity <= 0:
        return {
            "total_interim_distributions": round(sum(interim), 2),
            "dpi_by_year": [0.0] * hold_period,
            "cash_on_cash_gross": 0.0,
            "cash_on_cash_net": 0.0,
        }
    dpi_by_year: list[float] = []
    cumulative = 0.0
    for yr in range(hold_period):
        cumulative += interim[yr] if yr < len(interim) else 0.0
        dpi_by_year.append(round(cumulative / entry_equity, 4))
    total_dist = round(sum(interim), 2)
    return {
        "total_interim_distributions": total_dist,
        "dpi_by_year": dpi_by_year,
        "cash_on_cash_gross": round((total_dist + equity_value) / entry_equity, 3),
        "cash_on_cash_net": round((total_dist + net_to_lps) / entry_equity, 3),
    }


def compute_exit(
    forecast_years: list[dict[str, object]],
    debt_schedule_years: list[dict[str, object]],
    drivers: dict[str, object],
    entry_equity: float,
    exit_year_index: int | None = None,
    distributions: list[float] | None = None,
    cash_by_year: list[float] | None = None,
    transaction_close_date: datetime.date | None = None,
    period_end_dates: list[datetime.date] | None = None,
) -> dict[str, object]:
    """
    Compute exit returns and the equity waterfall.

    Args:
        forecast_years: Per-year forecast dicts (must contain 'ebitda'; may carry
            'adjusted_ebitda' and 'cash')
        debt_schedule_years: Per-year debt schedule dicts
        drivers: Main drivers (exit_ebitda_multiple, transaction_fees_pct,
            equity_carry_pct, exit_ebitda_basis, management_rollover_amount,
            preferred_return_pct — the optional annual carry hurdle, default 0.0)
        entry_equity: TOTAL equity invested at entry (sponsor + rollover)
        exit_year_index: Which forecast year to exit. Defaults to ``drivers.exit_year``
            — the HOLD in years beyond FYCY (Decision D4): FYCY is forecast index 0, so
            a hold of ``h`` exits at forecast index ``h`` (hold=5 → FY+5), capped at the
            last forecast year. Changing the hold moves BOTH the exit column and the IRR.
        distributions: Interim equity distributions per forecast year
        cash_by_year: BS cash per forecast year (wins over the forecast row's
            'cash' key); absent everywhere → cash 0 (gross == net)
        transaction_close_date: The deal's EXPECTED transaction close date — the entry
            anchor for the dated XIRR (Decision D4). Paired with ``period_end_dates``.
        period_end_dates: Real forecast period-end dates, index-aligned to
            ``forecast_years`` (the FYCY period spine). When BOTH this and
            ``transaction_close_date`` are present the equity IRR is a day-count-accurate
            XIRR over the real dates; absent either, the uniform-annual-period IRR is used.

    Returns:
        Exit waterfall with EV, NET-debt equity value, the headline ``tvpi`` /
        ``tvpi_net`` (distributions included), the exit-only MOICs, IRR, realized
        (DPI / cash-on-cash) metrics, the preferred hurdle, per-class allocation,
        and the sensitivity table.
    """
    exit_multiple = float(drivers.get("exit_ebitda_multiple", 0))
    fees_pct = float(drivers.get("transaction_fees_pct", 0.03))
    equity_carry_pct = float(drivers.get("equity_carry_pct", 0.20))

    if exit_year_index is None:
        # ``exit_year`` = the hold in years beyond FYCY (Decision D4). FYCY is forecast
        # index 0, so a hold of h exits at forecast index h; the clamp below caps it at
        # the last forecast year (the 5 forward years FY+1..FY+5 plus FYCY).
        exit_year_index = int(drivers.get("exit_year", len(forecast_years) - 1))

    # Clamp
    exit_year_index = min(exit_year_index, len(forecast_years) - 1)
    if exit_year_index < 0:
        exit_year_index = 0

    # Dated-XIRR anchor (Decision D4): the entry date + the real forecast period-end
    # dates enable a day-count-accurate equity IRR. Both must be present AND the spine
    # must cover the held years; otherwise the uniform-annual-period IRR is used (a
    # legacy NULL-date scenario degrades honestly per D5 — never a fabricated date).
    _hold = exit_year_index + 1
    _dates_ready = (
        transaction_close_date is not None
        and period_end_dates is not None
        and len(period_end_dates) >= _hold
        and all(period_end_dates[k] is not None for k in range(_hold))
    )
    _entry_date = transaction_close_date if _dates_ready else None
    _flow_dates = period_end_dates if _dates_ready else None

    # ── Exit EBITDA (explicit basis: book default, adjusted opt-in) ──────
    exit_year_data = forecast_years[exit_year_index]
    exit_basis = str(drivers.get("exit_ebitda_basis", "book"))
    if exit_basis == "adjusted":
        exit_ebitda = float(
            exit_year_data.get("adjusted_ebitda", exit_year_data.get("ebitda", 0)) or 0
        )
    else:
        exit_basis = "book"
        exit_ebitda = float(exit_year_data.get("ebitda", 0) or 0)

    # ── Enterprise Value ─────────────────────────────────────────────────
    ev = round(exit_ebitda * exit_multiple, 2)

    # ── Transaction Fees ─────────────────────────────────────────────────
    fees = round(ev * fees_pct, 2)

    # ── Remaining Debt / trapped cash → NET debt (the row-13 defect fix) ──
    remaining_debt = 0.0
    if exit_year_index < len(debt_schedule_years):
        remaining_debt = float(debt_schedule_years[exit_year_index].get("total_ending", 0))
    if cash_by_year is not None and exit_year_index < len(cash_by_year):
        cash_at_exit = float(cash_by_year[exit_year_index] or 0)
    else:
        cash_at_exit = float(exit_year_data.get("cash", 0) or 0)
    net_debt = round(remaining_debt - cash_at_exit, 2)

    # ── Equity Value — net of trapped cash ───────────────────────────────
    equity_value = round(ev - fees - net_debt, 2)

    # ── Returns ──────────────────────────────────────────────────────────
    hold_period = exit_year_index + 1

    gross_moic = round(equity_value / entry_equity, 3) if entry_equity > 0 else 0
    # True IRR over the equity flow vector incl. interim distributions
    # (FIND-MONEY-002) — see _equity_irr. Dated XIRR when the real close date + period
    # spine are present (D4); uniform-period otherwise. None/all-zero dist → single-flow.
    gross_irr = _equity_irr(
        entry_equity, hold_period, equity_value, distributions,
        entry_date=_entry_date, flow_dates=_flow_dates,
    )

    # ── Equity classes: sponsor + management rollover (RD-1 row 15) ──────
    # The rollover is a second equity class INSIDE the total entry equity. Proceeds
    # and interim distributions split pro-rata by entry ownership; carry is charged
    # on the SPONSOR gain only (rollover holders are not fund LPs).
    rollover_entry = min(
        max(float(drivers.get("management_rollover_amount", 0) or 0), 0.0),
        max(entry_equity, 0.0),
    )
    sponsor_entry = round(entry_equity - rollover_entry, 2)
    sponsor_share = (sponsor_entry / entry_equity) if entry_equity > 0 else 1.0
    rollover_share = 1.0 - sponsor_share

    interim = [float(d) for d in (distributions or [])][:hold_period]
    sponsor_dist = [round(d * sponsor_share, 2) for d in interim]
    rollover_dist = [round(d * rollover_share, 2) for d in interim]

    sponsor_proceeds = round(equity_value * sponsor_share, 2)
    rollover_proceeds = round(equity_value - sponsor_proceeds, 2)

    # ── Carry: on the sponsor gain above the hurdle, only ─────────────────
    # Default hurdle = 1× (return of sponsor capital — the pre-existing base).
    # An optional preferred return (AC-C4 leg 4) compounds annually on the
    # sponsor entry over the hold: a HARD hurdle (no GP catch-up; interim
    # distributions not credited — disclosed simplifications, see module doc).
    preferred_return_pct = max(float(drivers.get("preferred_return_pct", 0.0) or 0.0), 0.0)
    preferred_hurdle = round(sponsor_entry * (1.0 + preferred_return_pct) ** hold_period, 2)
    carry_base = max(sponsor_proceeds - preferred_hurdle, 0)
    carry_amount = round(carry_base * equity_carry_pct, 2)
    net_to_lps = round(equity_value - carry_amount, 2)

    net_moic = round(net_to_lps / entry_equity, 3) if entry_equity > 0 else 0
    # Interim distributions flow to equity gross of carry (carry is applied to the
    # exit gain only, carry_base above); modeling carry on interim distributions is
    # a deeper-waterfall concern out of scope here.
    net_irr = _equity_irr(
        entry_equity, hold_period, net_to_lps, distributions,
        entry_date=_entry_date, flow_dates=_flow_dates,
    )

    equity_classes = {
        "sponsor": {
            "entry": sponsor_entry,
            "proceeds_gross": sponsor_proceeds,
            "carry_paid": carry_amount,
            "proceeds_net": round(sponsor_proceeds - carry_amount, 2),
            "moic_gross": round(sponsor_proceeds / sponsor_entry, 3) if sponsor_entry > 0 else 0,
            "irr_gross": _equity_irr(
                sponsor_entry, hold_period, sponsor_proceeds, sponsor_dist,
                entry_date=_entry_date, flow_dates=_flow_dates,
            ),
            "moic_net": (
                round((sponsor_proceeds - carry_amount) / sponsor_entry, 3)
                if sponsor_entry > 0
                else 0
            ),
            "irr_net": _equity_irr(
                sponsor_entry, hold_period, sponsor_proceeds - carry_amount, sponsor_dist,
                entry_date=_entry_date, flow_dates=_flow_dates,
            ),
        },
        "management_rollover": {
            "entry": round(rollover_entry, 2),
            "proceeds": rollover_proceeds,
            "moic": round(rollover_proceeds / rollover_entry, 3) if rollover_entry > 0 else 0,
            "irr": _equity_irr(
                rollover_entry, hold_period, rollover_proceeds, rollover_dist,
                entry_date=_entry_date, flow_dates=_flow_dates,
            ),
        },
    }

    # ── Realized-distribution metrics (DPI / cash-on-cash) ────────────────
    realized = _realized_metrics(entry_equity, hold_period, interim, equity_value, net_to_lps)

    # ── TVPI — the headline multiple (AC-C4 leg 4) ────────────────────────
    # TVPI = (Σ interim distributions + exit equity) ÷ entry equity — computed
    # ONCE in _realized_metrics (cash_on_cash_gross/net are the same identity);
    # surfaced top-level under its honest name. gross_moic/net_moic stay the
    # EXIT-ONLY multiples (distributions excluded) — separately labeled, not
    # silently redefined.
    tvpi = float(realized["cash_on_cash_gross"])
    tvpi_net = float(realized["cash_on_cash_net"])

    # ── Sensitivity Table ────────────────────────────────────────────────
    # MOIC at different exit multiples (same NET-debt policy in every cell)
    sensitivity_multiples = [
        exit_multiple - 2, exit_multiple - 1, exit_multiple,
        exit_multiple + 1, exit_multiple + 2,
    ]
    sensitivity = []
    for m in sensitivity_multiples:
        if m <= 0:
            continue
        s_ev = exit_ebitda * m
        s_fees = s_ev * fees_pct
        s_eq = s_ev - s_fees - net_debt
        s_moic = round(s_eq / entry_equity, 3) if entry_equity > 0 else 0
        s_irr = _equity_irr(
            entry_equity, hold_period, s_eq, distributions,
            entry_date=_entry_date, flow_dates=_flow_dates,
        )
        sensitivity.append({
            "exit_multiple": round(m, 1),
            "ev": round(s_ev, 2),
            "equity_value": round(s_eq, 2),
            "moic": s_moic,
            "irr": s_irr,
        })

    return {
        "exit_year_index": exit_year_index,
        "hold_period_years": hold_period,
        "exit_ebitda": round(exit_ebitda, 2),
        "exit_ebitda_basis": exit_basis,
        "exit_multiple": exit_multiple,
        "enterprise_value": ev,
        "transaction_fees": fees,
        "remaining_debt": round(remaining_debt, 2),
        "cash_at_exit": round(cash_at_exit, 2),
        "net_debt": net_debt,
        "equity_value": equity_value,
        # TVPI — the headline multiple: includes interim distributions (leg 4).
        "tvpi": tvpi,
        "tvpi_net": tvpi_net,
        # Exit-only multiples (distributions EXCLUDED) — the pre-existing values,
        # separately labeled; ≡ tvpi when the deal pays no interim distributions.
        "gross_moic": gross_moic,
        "gross_irr": gross_irr,
        "carry_pct": equity_carry_pct,
        "carry_amount": carry_amount,
        # The optional preferred-return hurdle (0.0 → the 1× base, pre-existing).
        "preferred_return_pct": preferred_return_pct,
        "preferred_hurdle": preferred_hurdle,
        "net_to_lps": net_to_lps,
        "net_moic": net_moic,
        "net_irr": net_irr,
        "entry_equity": round(entry_equity, 2),
        "equity_classes": equity_classes,
        "realized": realized,
        "sensitivity": sensitivity,
    }

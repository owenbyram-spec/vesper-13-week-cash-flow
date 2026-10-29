"""Historical-band CF-articulation gates + disclosures (plan 20260720, §AMENDMENT AC-FRR-9/10).

The band's derived cash flow now articulates OWNER EQUITY FLOWS (``cf_equity = ΔTE − NI``, the
clean-surplus identity) and splits intangibles amortization out of capex — so its derived Ending
Cash equals the BS cash level whenever both columns tie (the refuter-proved identity). This module
owns the per-column REFUSE/DISCLOSE machinery around that articulation, called from the band fetch
loop (:func:`app.lbo.post_close_band.fetch_post_close_actuals`), which holds the QBO reports'
own NI/TE totals in hand:

1. **Prior normalization** (AC-FRR-9a, refuter edit 2): the fetch's ``initial_prior_levels``
   normalizes through the ONE statement algebra (the ``build_t0_anchor`` precedent) so a REAL
   prior carries its computed ``total_equity`` level — a raw degraded-entry prior has no
   ``total_equity`` key, and reading it as 0 would fabricate the first column's flow (TE₁ − NI₁).
2. **Inline NI + TE reconciliation gates** (AC-FRR-9a): the mapped ``net_income`` /
   ``total_equity`` cells must tie (± the realized track's OWN tolerance) to the P&L report's NI
   bottom line / the BS report's Total Equity total, PER COLUMN — a failed column gets NO
   ``cf_equity`` (recomputed through the one algebra with the equity level withheld, so ``cf_cff``
   / ``cf_net_change`` / ``cf_end_cash`` stay consistent), and the failure is recorded beside
   ``band_continuity``. A mis-mapped column never persists a wrong equity flow into the displayed
   grid while only /returns refuses.
3. **The ending-cash tie disclosure** (AC-FRR-10): where the gates held AND ``cf_equity`` is
   present, ``|derived Ending Cash − BS cash| ≤ 2 × the band tie tolerance`` (the residual bound
   of two legitimately-tied columns — each column's ``balance_check`` may sit at ±tol, and the
   residual is exactly ``bc_prior − bc_now``); a larger residual is DISCLOSED naming the column
   and the residual — it does NOT drop the column (the BS tie gates own keep/drop).
4. **The impairment disclosure** (AC-FRR-9b): ``amort_int > depreciation`` (an intangibles
   write-down beyond the period's whole D&A line) is DISCLOSED, never silently absorbed as
   disposal proceeds — the split's algebra stays EXACT so the articulation identity holds.

PURE: dict math over the fetch loop's in-memory levels + report totals. No DB, no QBO client.
"""
from __future__ import annotations

import logging

# ONE reconciliation tolerance (± $0.011) — imported from the realized track, never forked: the
# inline gates and the /returns gates must agree on what "reconciles" means, or a column could
# display a cf_equity the realized track refuses (or vice versa).
from .realized_returns import _NI_RECON_TOL

logger = logging.getLogger(__name__)

# AC-FRR-10: the ending-cash articulation bound = 2 × the band tie tolerance. Derivation (the
# amendment crit-eval edit 6): residual = derived_end − bs_cash ≡ bc_prior − bc_now for fully
# articulated columns, and each kept column's |balance_check| may legitimately sit AT the tie
# tolerance — so two legitimately-tied columns can produce |residual| up to 2×tol. The tighter
# NI tolerance would false-alarm on that legitimate rounding shape.
ARTICULATION_TOL_MULTIPLIER = 2

# The disclosure/refusal record `check` vocabulary (persisted beside band_continuity).
CHECK_NI = "ni_reconciliation"
CHECK_TE = "te_reconciliation"
CHECK_ENDING_CASH = "ending_cash_tie"
CHECK_IMPAIRMENT = "amortization_exceeds_depreciation"


def normalize_initial_prior(initial_prior_levels: dict | None, tie_tol: float) -> dict | None:
    """Normalize the band fetch's initial prior through the ONE statement algebra (AC-FRR-9a).

    The ``build_t0_anchor`` precedent: a raw prior (the degraded-entry opening-ladder levels)
    carries input codes only — NO ``total_equity`` key — so the first column's cf_equity would
    otherwise be measured against a fabricated $0 basis. Normalizing computes the REAL equity
    level from the prior's own equity accounts. The refused-opening shape (``total_assets`` ≈ 0
    — the same gate ``build_t0_anchor`` refuses an anchor on) carries NO equity level: its
    ``total_equity`` is POPPED so cf_equity stays ABSENT off it, while the remaining levels
    still serve the (unchanged) working-capital-delta behavior. ``None`` passes through.
    Idempotent on an already-computed prior (the chained annual→monthly hand-off).
    """
    if initial_prior_levels is None:
        return None
    from .standard_grid_builder import compute_statement_subtotals

    prior = compute_statement_subtotals(initial_prior_levels)
    if abs(float(prior.get("total_assets") or 0.0)) < tie_tol:  # SA-OK: dict.get on the in-memory normalized levels
        prior.pop("total_equity", None)  # refuse-over-fabricate: no sheet → no equity basis
    return prior


def _record(articulation: list | None, entry: dict) -> None:
    if articulation is not None:
        articulation.append(entry)


def gate_column_articulation(
    computed: dict,
    levels: dict,
    prior_levels: dict | None,
    *,
    report_ni: float | None,
    report_te: float | None,
    period_end_iso: str,
    grain: str,
    tie_tol: float,
    articulation: list | None,
    recompute,
) -> dict:
    """Run the per-column articulation gates + disclosures for one KEPT band column.

    ``computed`` is the column's already-tied statement levels (the one algebra's output);
    ``recompute`` is that same algebra (``compute_statement_subtotals``), re-invoked with the
    equity level withheld on a gate failure so the suppression can never fork the derivation.
    Returns the (possibly re-derived) ``computed``; disclosure/refusal records append to
    ``articulation`` (when given) as ``{"period_end", "grain", "check", ...}`` rows.
    """
    # (4) The impairment disclosure — the split ran whenever the column has a real prior
    # (cf_capex present). amort_int beyond the whole D&A line means depr_ppe went negative:
    # the EXACT algebra keeps the articulation identity, but the excess reads like disposal
    # proceeds inside cf_capex — disclose it, never absorb it silently.
    if prior_levels is not None and "cf_capex" in computed:
        _prior_int = prior_levels.get("intangibles")  # SA-OK: dict.get on the in-memory prior-column levels
        _cur_int = float(computed.get("intangibles") or 0.0)  # SA-OK: dict.get on the in-memory computed levels
        _amort_int = max(-(_cur_int - float(_prior_int or 0.0)), 0.0)
        _depr = float(computed.get("depreciation") or 0.0)  # SA-OK: dict.get on the in-memory computed levels
        if _amort_int > _depr + tie_tol:
            logger.info(
                "[LBO BAND] %s window %s intangibles amortization %.2f exceeds the D&A line %.2f "
                "— impairment shape DISCLOSED (cf_capex carries the exact algebra)",
                grain, period_end_iso, _amort_int, _depr,
            )
            _record(articulation, {
                "period_end": period_end_iso, "grain": grain, "check": CHECK_IMPAIRMENT,
                "amort_intangibles": round(_amort_int, 2), "depreciation": round(_depr, 2),
            })

    # (2) The inline NI + TE reconciliation gates — only a column that EMITTED cf_equity needs
    # them (chain-broken / no-prior-equity columns already carry none).
    if "cf_equity" in computed:
        mapped_ni = float(computed.get("net_income") or 0.0)  # SA-OK: dict.get on the in-memory computed levels
        mapped_te = float(computed.get("total_equity") or 0.0)  # SA-OK: dict.get on the in-memory computed levels
        ni_ok = report_ni is not None and abs(mapped_ni - report_ni) <= _NI_RECON_TOL
        te_ok = report_te is not None and abs(mapped_te - report_te) <= _NI_RECON_TOL
        if not (ni_ok and te_ok):
            if not ni_ok:
                _record(articulation, {
                    "period_end": period_end_iso, "grain": grain, "check": CHECK_NI,
                    "mapped": round(mapped_ni, 2),
                    "reference": round(report_ni, 2) if report_ni is not None else None,
                    "action": "cf_equity_suppressed",
                })
            if not te_ok:
                _record(articulation, {
                    "period_end": period_end_iso, "grain": grain, "check": CHECK_TE,
                    "mapped": round(mapped_te, 2),
                    "reference": round(report_te, 2) if report_te is not None else None,
                    "action": "cf_equity_suppressed",
                })
            logger.info(
                "[LBO BAND] %s window %s failed the inline NI/TE reconciliation (ni_ok=%s te_ok=%s)"
                " — cf_equity refused for this column (recomputed with the equity level withheld)",
                grain, period_end_iso, ni_ok, te_ok,
            )
            # Suppress through the ONE algebra: withholding the prior equity level makes
            # cf_equity absent AND re-derives cf_cff/cf_net_change/cf_end_cash without it —
            # never a hand-patched aggregate that could fork from compute_statement_subtotals.
            gated_prior = {k: v for k, v in (prior_levels or {}).items() if k != "total_equity"}
            computed = recompute(levels, prior_levels=gated_prior)

    # (3) AC-FRR-10: the ending-cash tie — gates held AND cf_equity present. Disclosed, never
    # a drop: the BS tie gates own keep/drop; this check names the residual the articulation
    # cannot explain (an un-modeled BS motion, or a beyond-rounding opening).
    if "cf_equity" in computed:
        derived_end = float(computed.get("cf_end_cash") or 0.0)  # SA-OK: dict.get on the in-memory computed levels
        bs_cash = float(computed.get("cash") or 0.0)  # SA-OK: dict.get on the in-memory computed levels
        residual = round(derived_end - bs_cash, 2)
        if abs(residual) > ARTICULATION_TOL_MULTIPLIER * tie_tol:
            logger.info(
                "[LBO BAND] %s window %s derived ending cash %.2f vs BS cash %.2f — residual %.2f "
                "DISCLOSED (column kept; the BS tie gates own keep/drop)",
                grain, period_end_iso, derived_end, bs_cash, residual,
            )
            _record(articulation, {
                "period_end": period_end_iso, "grain": grain, "check": CHECK_ENDING_CASH,
                "derived_end_cash": round(derived_end, 2), "bs_cash": round(bs_cash, 2),
                "residual": residual,
            })
    return computed

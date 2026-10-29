"""Realized-to-date returns from the books themselves (plan 20260720-full-history-realized-returns,
AC-FRR-3 / SYNTHESIS §1).

The clean-surplus construction: every tied band column persists ``total_equity`` (the as-of stock,
carrying QBO's REAL equity via the id-less fold) and ``net_income`` (the period's mapped flow), so

    sponsor flow_t = −(ΔTotalEquity_t − NetIncome_t)

derives contributions/distributions from levels alone — immune to QBO's NI→RE auto-close and to the
per-account children's incompleteness. A POSITIVE flow is a net distribution (sponsor inflow), a
NEGATIVE flow a contribution (sponsor outflow); the t₀ basis outflow leads the vector and the ONE
dated solver (:func:`app.lbo.returns.xirr`) prices it — no sibling solver exists here.

REFUSE-over-fabricate — the three contract gates (interface-w2-returns-payload.md §Refusal
gates; gate 3 added by the Wave-5 review remediation AC-2):

1. **Series adjacency** — no t₀ anchor → ``no_t0_anchor``; a legacy grid without a continuity
   record, a non-empty ``dropped`` list, or ANY date gap (anchor→first column, column→column) →
   ``history_incomplete`` with "re-import required". A gapped series would lump the gap's NI into
   one phantom flow — never an XIRR off that. A column whose period_end is on-or-before the
   anchor's as-of is EMBODIED by the anchor (the books-start shape) and is excluded, not a gap.
2. **NI reconciliation** — a period whose mapped ``net_income`` cell differs from QBO's OWN P&L
   Net Income total (the persisted ``band_pl_report_ni`` reference, §NI-total substrate) beyond
   rounding → ``ni_reconciliation_failed`` naming the period(s). An unmapped/misrouted P&L account
   makes ΔTE−NI a recurring phantom flow while the BS-only tie gate stays green — this gate is the
   independent check that kills it. A column with NO persisted reference (pre-substrate import) →
   ``history_incomplete`` "re-import required" — never a silent pass.
3. **Equity reconciliation** — a period whose mapped ``total_equity`` cell differs from the
   BS report's OWN Total Equity (the persisted ``band_bs_report_te`` reference) beyond the same
   tolerance → ``equity_reconciliation_failed`` naming the period(s). A misrouted EQUITY account
   (e.g. an equity account mapped to debt) keeps the tie AND the NI gate green while silently
   hiding real contributions/distributions inside ΔTE — this gate is the independent check that
   kills it. No persisted reference → ``history_incomplete`` "re-import required".

The D-FRR-1 basis policy lives in :mod:`app.lbo.realized_basis` (split along the concern
boundary): a labeled, provenance-gated selector over confirmed S&U / books opening equity /
APA cash, refusing ``no_viable_basis`` when no candidate is a positive sponsor outflow.

PURE: no DB, no Pydantic, no QBO client — the router assembles persisted inputs and serializes the
outputs; every function here is golden-testable with hand-computed literals.
"""
from __future__ import annotations

import datetime

from .returns import IRRError, xirr

# The contract's disclosed plausibility threshold: a single-period flow above this percentage of
# the period's OPENING equity is flagged for review (it catches LARGE anomalies — e.g. a
# direct-to-RE correction read as a flow; the small-gap producers are owned by the two gates).
PLAUSIBILITY_THRESHOLD_PCT = 25

# NI-reconciliation tolerance (dollars): statement cells are cents-rounded, so a genuine tie
# differs only by float noise; one cent of rounding is tolerated, two cents is a real mismatch.
_NI_RECON_TOL = 0.011

# A clean-surplus residual at-or-below the reconciliation tolerance is rounding, not an equity
# transaction — ALIGNED with _NI_RECON_TOL (Wave-5 AC-1) so a residual too small for the gates
# to distinguish from statement rounding can never surface as a flow.
_FLOW_EPS = _NI_RECON_TOL

REASON_NO_T0_ANCHOR = "no_t0_anchor"
REASON_HISTORY_INCOMPLETE = "history_incomplete"
REASON_NI_RECONCILIATION = "ni_reconciliation_failed"
REASON_EQUITY_RECONCILIATION = "equity_reconciliation_failed"
REASON_NO_VIABLE_BASIS = "no_viable_basis"

COMBINED_NOTE = "realized-to-date + projected exit"

_FY_GRAIN_NOTE = (
    "Annual-grain flows compress intra-year timing to the fiscal-year end; the realized XIRR "
    "treats each such year's net equity motion as a single year-end flow."
)


class RealizedRefusalError(Exception):
    """The realized track REFUSES — carries the contract's ``realized_refusal`` shape.

    Raised by the gates; the caller renders ``realized_track: null`` plus this payload —
    never a number off a gapped or unreconciled series.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail

    def payload(self) -> dict:
        return {"reason_code": self.reason_code, "detail": self.detail}


def _num(value) -> float | None:
    """Best-effort numeric coercion for driver_data JSON values; None when not a number."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _column_name(col: dict) -> str:
    """The human name a refusal detail uses for a column: its label plus the period-end date."""
    end = col.get("period_end")  # SA-OK: dict.get on an in-memory series-column dict
    end_iso = end.isoformat() if isinstance(end, datetime.date) else str(end)
    label = str(col.get("label") or "").strip()  # SA-OK: dict.get on an in-memory series-column dict
    return f"{label} ({end_iso})" if label else end_iso


def extract_equity_series(
    t0_anchor: dict | None,
    band_continuity: dict | None,
    columns: list[dict],
) -> dict:
    """Gate + order the persisted equity series (the clean-surplus substrate).

    ``t0_anchor`` is the persisted meta ``band_t0_anchor`` (``{as_of, total_equity, source}``);
    ``band_continuity`` the persisted ``band_continuity`` (``{"dropped": [...]}``); ``columns``
    the router-assembled per-column dicts ``{period_start, period_end, grain, label,
    total_equity, net_income, total_debt}`` for the series columns (band monthly + fetched-annual
    FY — never a rollup that a monthly column already covers).

    Returns ``{"as_of": date, "t0_equity": float, "columns": [...]}`` ordered oldest-first, or
    raises :class:`RealizedRefusalError` per contract gate 1 (adjacency). A series with ZERO
    columns is valid (a recent close — no full post-close month yet): the track renders the t₀
    outflow alone and degrades its XIRR honestly downstream.

    Books-start anchor collision (Wave-5 AC-5): a band column whose ``period_end`` is
    on-or-before the anchor's as-of is EMBODIED by the anchor (the books-start shape — the
    anchor IS that month's tied sheet), so it is EXCLUDED from the series and adjacency then
    expects the next column at as-of + 1 day. Without the exclusion that whole deal class
    refused forever (the anchor and column 1 shared a date, which read as a gap).
    """
    as_of_raw = (t0_anchor or {}).get("as_of")  # SA-OK: dict.get on the in-memory persisted anchor JSON
    te_raw = (t0_anchor or {}).get("total_equity")  # SA-OK: dict.get on the in-memory persisted anchor JSON
    try:
        as_of = datetime.date.fromisoformat(str(as_of_raw)) if as_of_raw else None
    except ValueError:
        as_of = None
    t0_equity = _num(te_raw)
    if as_of is None or t0_equity is None:
        raise RealizedRefusalError(
            REASON_NO_T0_ANCHOR,
            "no t₀ equity anchor is persisted for this scenario — re-import to capture the "
            "as-of-close balance sheet (the realized track has no level to chain from).",
        )

    if not isinstance(band_continuity, dict) or "dropped" not in band_continuity:
        raise RealizedRefusalError(
            REASON_HISTORY_INCOMPLETE,
            "history incomplete — this grid predates the continuity record (legacy import); "
            "re-import required.",
        )
    dropped = band_continuity.get("dropped") or []  # SA-OK: dict.get on the in-memory persisted continuity JSON
    if dropped:
        names = ", ".join(
            f"{d.get('start')}..{d.get('end')} ({d.get('reason')})" for d in dropped  # SA-OK: dict.get on in-memory drop records
        )
        raise RealizedRefusalError(
            REASON_HISTORY_INCOMPLETE,
            f"history incomplete — {len(dropped)} window(s) were dropped by the fetch gates "
            f"[{names}]; a gapped series would fabricate phantom flows. Re-import required.",
        )

    # AC-5: columns the anchor already embodies (period_end <= as_of) are excluded, not gaps.
    ordered = sorted(
        (c for c in columns if c["period_end"] > as_of), key=lambda c: c["period_end"]
    )
    for col in ordered:
        if _num(col.get("total_equity")) is None or _num(col.get("net_income")) is None:  # SA-OK: dict.get on in-memory series columns
            raise RealizedRefusalError(
                REASON_HISTORY_INCOMPLETE,
                f"history incomplete — column {_column_name(col)} lacks a persisted "
                "total_equity/net_income cell; re-import required.",
            )

    prev_end = as_of
    prev_name = f"the t₀ anchor ({as_of.isoformat()})"
    for col in ordered:
        expected_start = prev_end + datetime.timedelta(days=1)
        if col["period_start"] != expected_start:
            raise RealizedRefusalError(
                REASON_HISTORY_INCOMPLETE,
                f"history incomplete — gap between {prev_name} and {_column_name(col)} "
                f"(expected the next column to start {expected_start.isoformat()}); "
                "re-import required.",
            )
        prev_end = col["period_end"]
        prev_name = _column_name(col)

    return {"as_of": as_of, "t0_equity": round(float(t0_equity), 2), "columns": ordered}


def clean_surplus_flows(
    series: dict,
    pl_report_ni: dict | None,
    basis_amount: float,
    threshold_pct: float = PLAUSIBILITY_THRESHOLD_PCT,
    *,
    bs_report_te: dict | None = None,
) -> list[dict]:
    """The dated sponsor flows: the t₀ basis outflow + one clean-surplus flow per series column.

    Runs contract gates 2 and 3 FIRST: every column's mapped ``net_income`` must tie
    (± rounding) to the persisted P&L-report NI total (``pl_report_ni``, keyed by period-end
    ISO), and every column's mapped ``total_equity`` must tie to the persisted BS-report Total
    Equity (``bs_report_te``, same keying — Wave-5 AC-2: a misrouted EQUITY account keeps the
    tie and the NI gate green while silently hiding real contributions/distributions inside
    ΔTE). A column with NO persisted reference on either gate refuses ``history_incomplete``
    ("re-import required" — the pre-substrate adoption boundary); a mismatch refuses
    ``ni_reconciliation_failed`` / ``equity_reconciliation_failed`` naming EVERY mismatched
    period. Then per column: ``flow = −(ΔTE − NI)`` (positive = distribution, negative =
    contribution), dated at the column's period end, grain-labeled, plausibility-flagged when
    ``|flow| > threshold_pct% × |opening equity|``. Residuals within the reconciliation
    tolerance are rounding, not flows, and are skipped. The t₀ outflow is dated at the anchor's
    as-of date — the same chain point the series levels are measured from.
    """
    ni_map = pl_report_ni or {}
    missing: list[str] = []
    mismatched: list[str] = []
    for col in series["columns"]:
        key = col["period_end"].isoformat()
        reference = _num(ni_map.get(key))  # SA-OK: dict.get on the in-memory persisted NI-total JSON
        if reference is None:
            missing.append(_column_name(col))
            continue
        if abs(float(col["net_income"]) - reference) > _NI_RECON_TOL:
            mismatched.append(
                f"{_column_name(col)}: mapped {float(col['net_income']):.2f} vs "
                f"P&L-report {reference:.2f}"
            )
    if missing:
        raise RealizedRefusalError(
            REASON_HISTORY_INCOMPLETE,
            "history incomplete — no persisted P&L-report NI total for "
            f"{', '.join(missing)}; re-import required.",
        )
    if mismatched:
        raise RealizedRefusalError(
            REASON_NI_RECONCILIATION,
            "mapped net income failed to reconcile to the P&L report's own total for "
            f"{'; '.join(mismatched)} — an unmapped or misrouted P&L account would fabricate "
            "recurring phantom flows.",
        )

    # Gate 3 (Wave-5 AC-2): the mapped total_equity cell vs the BS report's OWN Total Equity.
    te_map = bs_report_te or {}
    te_missing: list[str] = []
    te_mismatched: list[str] = []
    for col in series["columns"]:
        key = col["period_end"].isoformat()
        reference = _num(te_map.get(key))  # SA-OK: dict.get on the in-memory persisted TE-total JSON
        if reference is None:
            te_missing.append(_column_name(col))
            continue
        if abs(float(col["total_equity"]) - reference) > _NI_RECON_TOL:
            te_mismatched.append(
                f"{_column_name(col)}: mapped {float(col['total_equity']):.2f} vs "
                f"BS-report {reference:.2f}"
            )
    if te_missing:
        raise RealizedRefusalError(
            REASON_HISTORY_INCOMPLETE,
            "history incomplete — no persisted BS-report Total Equity for "
            f"{', '.join(te_missing)}; re-import required.",
        )
    if te_mismatched:
        raise RealizedRefusalError(
            REASON_EQUITY_RECONCILIATION,
            "mapped total equity failed to reconcile to the balance sheet's own Total Equity "
            f"for {'; '.join(te_mismatched)} — a misrouted equity account would silently hide "
            "real contributions/distributions inside the clean-surplus deltas.",
        )

    flows: list[dict] = [{
        "date": series["as_of"].isoformat(),
        "amount": round(-float(basis_amount), 2),
        "kind": "t0_outflow",
        "grain": "t0",
        "plausibility_flag": False,
    }]
    prev_te = float(series["t0_equity"])
    for col in series["columns"]:
        te = float(col["total_equity"])
        ni = float(col["net_income"])
        flow = round(-((te - prev_te) - ni), 2)
        opening = prev_te
        prev_te = te
        if abs(flow) <= _FLOW_EPS:
            continue  # no equity transaction this period — nothing to record
        flows.append({
            "date": col["period_end"].isoformat(),
            "amount": flow,
            "kind": "distribution" if flow > 0 else "contribution",
            "grain": col["grain"],
            "plausibility_flag": abs(flow) > (float(threshold_pct) / 100.0) * abs(opening),
        })
    return flows


def realized_metrics(flows: list[dict]) -> dict:
    """Contributed / distributed / DPI / realized XIRR over the dated sponsor flows.

    ``contributed`` = Σ outflow magnitudes (the t₀ basis + later contributions);
    ``distributed`` = Σ inflows; ``dpi`` = distributed ÷ contributed — ``None`` (never 0.0)
    when nothing was contributed (Wave-5 AC-1: a fabricated 0.0x reads as a real multiple).
    The XIRR runs through THE one dated solver; a typed :class:`IRRError` (single flow, no
    sign change, unbracketable) degrades to ``xirr: null`` with the solver's own reason —
    never a fabricated rate.
    """
    contributed = round(sum(-f["amount"] for f in flows if f["amount"] < 0), 2)
    distributed = round(sum(f["amount"] for f in flows if f["amount"] > 0), 2)
    dpi = round(distributed / contributed, 4) if contributed > 0 else None
    xirr_value: float | None = None
    xirr_null_reason: str | None = None
    try:
        dated = [(datetime.date.fromisoformat(f["date"]), float(f["amount"])) for f in flows]
        xirr_value = round(xirr(dated), 4)
    except IRRError as exc:
        xirr_null_reason = str(exc)
    return {
        "contributed": contributed,
        "distributed": distributed,
        "dpi": dpi,
        "xirr": xirr_value,
        "xirr_null_reason": xirr_null_reason,
    }


def seller_note_schedule(columns: list[dict]) -> list[dict]:
    """COMPANY debt motion, serialized SEPARATELY from the sponsor flows (never folded in).

    The note amortizes inside the company, so its paydown must not read as a sponsor
    distribution; per SYNTHESIS §1 the schedule derives from the band's persisted total-debt
    level deltas. Pre-W3 no tranche carries a human seller-note classification (D-FRR-3), so each
    entry is labeled as the unclassified company total-debt delta — data for the reviewer, never
    a fabricated classification. A column without a persisted ``total_debt`` cell breaks the
    delta chain (absent, never fabricated).
    """
    out: list[dict] = []
    prev_td: float | None = None
    for col in columns:
        td = _num(col.get("total_debt"))  # SA-OK: dict.get on an in-memory series-column dict
        if td is None:
            prev_td = None
            continue
        if prev_td is not None:
            delta = round(td - prev_td, 2)
            if abs(delta) > _FLOW_EPS:
                out.append({
                    "date": col["period_end"].isoformat(),
                    "amount": delta,
                    "note": (
                        "company total-debt delta over the period — seller-note "
                        "classification pending human review (D-FRR-3)"
                    ),
                })
        prev_td = td
    return out


def build_disclosures(diagnostic: dict | None, flows: list[dict]) -> dict:
    """The contract's ``disclosures`` block: the PERSISTED AC-FRR-4 diagnostic verbatim
    (``not_run`` when absent — /returns never calls live QBO), the FY-grain timing note when any
    fy-grain flow is present, and the disclosed plausibility threshold."""
    if diagnostic is None:
        status = "not_run"
    else:
        status = "findings" if diagnostic.get("findings") else "clean"  # SA-OK: dict.get on the in-memory persisted diagnostic JSON
    grain_note = _FY_GRAIN_NOTE if any(f["grain"] == "fy" for f in flows) else ""
    return {
        "diagnostic_status": status,
        "diagnostic": diagnostic,
        "grain_note": grain_note,
        "plausibility_threshold_pct": PLAUSIBILITY_THRESHOLD_PCT,
    }


def combined_track(
    flows: list[dict],
    projected_distributions: list[float] | None,
    exit_equity_value: float,
    exit_year_index: int,
    period_end_dates: list[datetime.date | None] | None,
) -> dict:
    """Realized-to-date + projected exit — the contract's ``combined_track`` block.

    Paid-in = the realized outflows (the t₀ basis + later contributions). Total value = realized
    distributions + the engine's projected interim distributions through the exit year + the
    projected exit equity. TVPI is timing-blind — and ``None`` (never 0.0) when nothing was
    contributed (Wave-5 AC-1); the combined XIRR stacks the projected flows (dated at the real
    forecast period ends) onto the realized dated vector through THE one solver. Missing
    forecast dates (a legacy NULL-date spine) or an unbracketable vector degrade to
    ``xirr: null`` with the reason — never a fabricated rate.

    Non-overlap guard (Wave-5 AC-6): the projected leg must POSTDATE the realized window. When
    the first projected flow lands on-or-before the last realized flow, the two legs cover the
    same months twice (the realized band and the forecast both carry that period's economics),
    so the combined track REFUSES — ``tvpi``/``xirr`` null with the named reason — rather than
    serve a double-counted multiple.
    """
    contributed = round(sum(-f["amount"] for f in flows if f["amount"] < 0), 2)
    realized_inflows = round(sum(f["amount"] for f in flows if f["amount"] > 0), 2)
    interim = [float(d) for d in (projected_distributions or [])][: exit_year_index + 1]
    total_value = round(realized_inflows + sum(interim) + float(exit_equity_value), 2)
    tvpi = round(total_value / contributed, 4) if contributed > 0 else None

    xirr_value: float | None = None
    xirr_null_reason: str | None = None
    dates = list(period_end_dates or [])
    exit_date = dates[exit_year_index] if exit_year_index < len(dates) else None
    needed_ok = exit_date is not None and all(
        dates[k] is not None
        for k, d in enumerate(interim)
        if abs(d) > _FLOW_EPS and k < len(dates)
    ) and all(k < len(dates) for k, d in enumerate(interim) if abs(d) > _FLOW_EPS)
    if not needed_ok:
        xirr_null_reason = (
            "forecast period-end dates unavailable — the dated combined XIRR needs the "
            "period spine (legacy NULL-date scenario)"
        )
    else:
        # AC-6: refuse on overlap — never a double-counted realized+projected multiple.
        last_realized = (
            max(datetime.date.fromisoformat(f["date"]) for f in flows) if flows else None
        )
        projected_dates = [
            dates[k] for k, d in enumerate(interim) if abs(d) > _FLOW_EPS
        ] + [exit_date]
        first_projected = min(projected_dates)
        if last_realized is not None and first_projected <= last_realized:
            return {
                "tvpi": None,
                "xirr": None,
                "xirr_null_reason": (
                    "projected flows overlap the realized window — the first projected flow "
                    f"({first_projected.isoformat()}) does not postdate the last realized flow "
                    f"({last_realized.isoformat()}); combining would double-count the overlap"
                ),
                "note": COMBINED_NOTE,
            }
        dated = [(datetime.date.fromisoformat(f["date"]), float(f["amount"])) for f in flows]
        for k, d in enumerate(interim):
            if abs(d) > _FLOW_EPS:
                dated.append((dates[k], d))
        dated.append((exit_date, float(exit_equity_value)))
        try:
            xirr_value = round(xirr(dated), 4)
        except IRRError as exc:
            xirr_null_reason = str(exc)

    return {
        "tvpi": tvpi,
        "xirr": xirr_value,
        "xirr_null_reason": xirr_null_reason,
        "note": COMBINED_NOTE,
    }

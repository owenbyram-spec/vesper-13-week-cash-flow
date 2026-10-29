# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns app/services/review_checks/** (plan.md Appendix A); the active-plan.json surface_scope entry carries an inline comment inside the glob string so the gate matcher misses it — scope is ratified, the state string is malformed.
"""flux_check — actual-vs-prior flux (analytical review) at ACCOUNT level.

Chunk C2C-CLOSE (plan 20260706-cfo-grade-financial-workflows, AC-2C.1; RD-3
Topic 4 + month-end-close SKILL §2.4). Every close gets an actual-vs-prior
comparison on BOTH the P&L and the balance sheet, gated by a DUAL two-sided
threshold, with the offsetting-error guard:

  - DUAL two-sided: a line trips the PROPOSAL gate when ``abs(pct) >= pct_floor
    AND abs($) >= abs_floor`` (the AND-shape is the proposal-NOISE gate —
    RD-3 [11][12]; the repo's budget lineage ``pct>10 AND abs>=1000``,
    proactive.py:764 → Materiality.dual). Two-sided by construction — favorable
    variances hide errors too (materiality.py:117-118,131).
  - OR coverage: independent of the proposal gate, EVERY line moving at least
    the dollar floor is surfaced in the payload coverage list (the OR-shape is
    the review-COVERAGE gate — RD-3 Topic 4(1); a $30k swing in a $10M line is
    0.3% and still material).
  - OFFSETTING-ERROR GUARD: flux runs at the LEAF-ACCOUNT level (not statement
    lines), so two wrong entries netting flat inside a parent line are visible;
    additionally, pairs of individually-material opposite-sign account moves
    whose pair-net falls under the dollar floor are called out explicitly
    (payload ``offsetting_pairs``) — the ±$20k-nets-to-zero class (RD-3 4(4a)).
  - NEAR-ZERO DENOMINATOR: a line with a zero prior base reports ``pct: "n/m"``
    — never a fabricated percent (RD-3 4(4b)). A 0→X move exceeds any finite
    percent floor by construction, so such a line clears the percent leg
    vacuously and is gated on the dollar floor alone.

MATERIALITY FLOW: this is a MULTI-LINE check, so the per-line gate consults the
INJECTED ``ctx.materiality`` (the registered, Phase-E-overridable DUAL floor) —
the floor still flows in through registration, never hardcoded in ``scan``
(review_checks/materiality.py header). ``run_check``'s outer single-value gate
re-checks the worst tripped line's values.

REUSE (qbo-integration one-shared-parser rule): P&L sides parse through the ONE
canonical ``_parse_pl_single_month_entries`` (app/budget/qbo_reports.py:84); BS
sides through ``report_lines.bs_leaf_lines`` (the ``coa_formula._flatten_rows``
walk). Report fetches are the same ``qbo_client.get_report`` shape the
subledger-recon detector uses (accrual basis pinned).

PROPOSAL-FIRST: ``scan`` classifies only; ``run_check`` drafts the PENDING
ProposedAction. Nothing here writes the GL, completes a task, or locks a period.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal

from . import CheckTier, register_check
from .materiality import Materiality
from .protocol import CheckContext, Finding, FindingSeverity
from .report_lines import bs_leaf_lines

logger = logging.getLogger(__name__)

# Bound the payload so one noisy tenant cannot blow up the proposal row.
_MAX_PAYLOAD_LINES = 25

TASK_TYPE_REVIEW_FLUX = "REVIEW_FLUX"


def period_bounds(period: str) -> tuple[date, date] | None:
    """(first_day, last_day) of a "YYYY-MM" period; None when malformed."""
    try:
        year_str, month_str = period.split("-", 1)
        year, month = int(year_str), int(month_str)
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last)
    except (ValueError, TypeError):
        return None


def prior_period(period: str) -> str | None:
    """The "YYYY-MM" immediately before ``period``; None when malformed."""
    bounds = period_bounds(period)
    if bounds is None:
        return None
    first, _ = bounds
    prior_last_day = first.replace(day=1)
    prior = prior_last_day.replace(
        year=first.year if first.month > 1 else first.year - 1,
        month=first.month - 1 if first.month > 1 else 12,
    )
    return prior.strftime("%Y-%m")


def _pct_change(current: float, prior: float) -> float | None:
    """Percent change vs the prior base; None ("n/m") on a zero base —
    never a fabricated percent (RD-3 Topic 4(4b))."""
    if prior == 0:
        return None
    return round((current - prior) / abs(prior) * 100.0, 2)


def _pl_account_map(report: dict) -> dict[str, tuple[str, float]]:
    """{identity: (account_name, amount)} via the ONE canonical P&L parser."""
    from ...budget.qbo_reports import _parse_pl_single_month_entries

    out: dict[str, tuple[str, float]] = {}
    for entry in _parse_pl_single_month_entries(report):
        key = str(entry.get("qbo_account_id") or entry.get("account_name") or "").strip()  # SA-OK: dict access on parsed report entries, not a DB query
        if not key:
            continue
        name = str(entry.get("account_name") or key)  # SA-OK: dict access on parsed report entries, not a DB query
        out[key] = (name, float(entry.get("amount") or 0.0))  # SA-OK: dict access on parsed report entries, not a DB query
    return out


def _bs_account_map(report: dict) -> dict[str, tuple[str, float]]:
    """{identity: (label, as-of balance)} via the shared BS leaf walk."""
    out: dict[str, tuple[str, float]] = {}
    for line in bs_leaf_lines(report):
        key = line["account_id"] or line["label"]
        out[key] = (line["label"], line["value"])
    return out


def _flux_lines(
    statement: str,
    current: dict[str, tuple[str, float]],
    prior: dict[str, tuple[str, float]],
) -> list[dict]:
    """Account-level variances for one statement. A line absent on one side is
    treated as 0 on that side (a NEW or VANISHED account — its whole balance IS
    the flux; the pct side then reports "n/m" via the zero-base rule)."""
    lines: list[dict] = []
    for key in sorted(set(current) | set(prior)):
        cur_name, cur_val = current.get(key, ("", 0.0))  # SA-OK: in-memory dict lookup over parsed report lines, not a DB query
        pri_name, pri_val = prior.get(key, ("", 0.0))  # SA-OK: in-memory dict lookup over parsed report lines, not a DB query
        variance = round(cur_val - pri_val, 2)
        if variance == 0:
            continue
        lines.append(
            {
                "statement": statement,
                "account": cur_name or pri_name or key,
                "account_key": key,
                "prior": round(pri_val, 2),
                "current": round(cur_val, 2),
                "variance": variance,
                "pct": _pct_change(cur_val, pri_val),  # None ⇒ "n/m"
            }
        )
    return lines


def _offsetting_pairs(tripped: list[dict], abs_floor: Decimal | None) -> list[dict]:
    """Opposite-sign pairs of individually-material moves whose pair-net falls
    under the dollar floor — the offsetting-error signature (two wrong entries
    netting flat pass a net-level gate; RD-3 Topic 4(4a))."""
    if abs_floor is None:
        return []
    floor = float(abs_floor)
    pairs: list[dict] = []
    for i, a in enumerate(tripped):
        for b in tripped[i + 1 :]:
            if a["statement"] != b["statement"]:
                continue
            if (a["variance"] > 0) == (b["variance"] > 0):
                continue
            if abs(a["variance"] + b["variance"]) < floor:
                pairs.append(
                    {
                        "statement": a["statement"],
                        "accounts": [a["account"], b["account"]],
                        "variances": [a["variance"], b["variance"]],
                        "net": round(a["variance"] + b["variance"], 2),
                        "note": "opposite-sign material moves netting under the floor — review for an offsetting error",
                    }
                )
    return pairs


class ActualVsPriorFluxCheck:
    """Actual-vs-prior flux on P&L + BS at account level (DUAL two-sided gate,
    OR coverage list, offsetting-error guard, n/m zero-base discipline)."""

    check_id = "actual_vs_prior_flux"

    def scan(self, ctx: CheckContext) -> Finding:
        if ctx.qbo_client is None:
            return Finding.could_not_verify(
                self.check_id,
                message="No live QuickBooks client — actual-vs-prior flux cannot be computed.",
            )

        cur_bounds = period_bounds(ctx.period)
        prior = prior_period(ctx.period)
        if cur_bounds is None or prior is None:
            return Finding.could_not_verify(
                self.check_id,
                message=f"period {ctx.period!r} is not a parseable YYYY-MM period.",
            )
        pri_bounds = period_bounds(prior)
        if pri_bounds is None:  # unreachable given prior_period(); defensive
            return Finding.could_not_verify(
                self.check_id, message=f"prior period for {ctx.period!r} could not be derived."
            )

        try:
            reports = self._fetch_reports(ctx, cur_bounds, pri_bounds)
        except Exception as e:  # a failed report fetch is NOT a clean pass
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"flux report fetch failed: {redact_to_string(e)}",
            )

        pl_lines = _flux_lines("P&L", _pl_account_map(reports["pl_cur"]), _pl_account_map(reports["pl_pri"]))
        bs_lines = _flux_lines("BS", _bs_account_map(reports["bs_cur"]), _bs_account_map(reports["bs_pri"]))
        all_lines = pl_lines + bs_lines

        if not all_lines and not pl_lines and not bs_lines:
            # No movement anywhere is only a VERIFIED clean pass when the reports
            # actually parsed to lines; empty parses on both sides of both
            # statements means the source was unreadable — degrade honestly.
            if not _pl_account_map(reports["pl_cur"]) and not _bs_account_map(reports["bs_cur"]):
                return Finding.could_not_verify(
                    self.check_id,
                    message="Both P&L and BS parsed to zero account lines — flux source unreadable.",
                )

        abs_floor = ctx.materiality.abs_floor
        tripped: list[dict] = []
        coverage: list[dict] = []
        for line in all_lines:
            abs_var = abs(line["variance"])
            # OR coverage gate (review coverage): anything over the dollar floor.
            if abs_floor is not None and Decimal(str(abs_var)) >= abs_floor:
                coverage.append(line)
            # DUAL proposal gate (two-sided, injected floor). A zero-base line
            # (pct None ⇒ "n/m") clears the percent leg VACUOUSLY — a 0→X move
            # exceeds any finite percent floor — and gates on dollars alone.
            if line["pct"] is None:
                if abs_floor is not None and Decimal(str(abs_var)) >= abs_floor:
                    tripped.append(line)
            elif ctx.materiality.is_material(amount=abs_var, pct=line["pct"]):
                tripped.append(line)

        if not tripped:
            n_cov = len(coverage)
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"No account flux beyond the dual threshold vs {prior}"
                    + (f" ({n_cov} line(s) over the dollar floor alone — reviewed, below the percent leg)." if n_cov else ".")
                ),
            )

        pairs = _offsetting_pairs(tripped, abs_floor)
        tripped.sort(key=lambda ln: abs(ln["variance"]), reverse=True)
        coverage.sort(key=lambda ln: abs(ln["variance"]), reverse=True)

        # Gate-carrier for run_check's outer DUAL gate: the largest tripped line
        # WITH a defined pct when one exists. When ONLY zero-base ("n/m") lines
        # tripped, the pct leg is vacuously true (0→X exceeds any finite floor):
        # carry the registered pct_floor as the gate token so the outer gate
        # agrees — the REPORTED value stays "n/m" in the payload/message; this
        # token is never rendered as a percent to a human.
        with_pct = [ln for ln in tripped if ln["pct"] is not None]
        worst = with_pct[0] if with_pct else tripped[0]
        gate_pct = Decimal(str(worst["pct"])) if worst["pct"] is not None else ctx.materiality.pct_floor

        pct_text = f"{worst['pct']:+.1f}%" if worst["pct"] is not None else "n/m (new/zero prior base)"
        offset_note = f"; {len(pairs)} offsetting pair(s) flagged" if pairs else ""
        return Finding.hit(
            self.check_id,
            message=(
                f"{len(tripped)} account line(s) flux beyond the dual threshold vs {prior} "
                f"(worst: {worst['statement']} {worst['account']} "
                f"{worst['variance']:+,.2f} / {pct_text}){offset_note}. "
                "Each tripped line needs a driver-tied explanation, not a restatement of the number."
            ),
            severity=FindingSeverity.WARNING,
            amount=Decimal(str(abs(worst["variance"]))),
            pct=gate_pct,
            payload={
                "period": ctx.period,
                "prior_period": prior,
                "tripped_lines": [
                    {**ln, "pct": ln["pct"] if ln["pct"] is not None else "n/m"}
                    for ln in tripped[:_MAX_PAYLOAD_LINES]
                ],
                "coverage_lines_over_dollar_floor": [
                    {**ln, "pct": ln["pct"] if ln["pct"] is not None else "n/m"}
                    for ln in coverage[:_MAX_PAYLOAD_LINES]
                ],
                "offsetting_pairs": pairs[:_MAX_PAYLOAD_LINES],
                "threshold_shape": "dual two-sided (abs(pct) AND abs($)); coverage list is the OR gate",
            },
        )

    @staticmethod
    def _fetch_reports(ctx: CheckContext, cur_bounds: tuple[date, date], pri_bounds: tuple[date, date]) -> dict:
        """Fetch the four report sides (P&L + BS, current + prior), accrual basis
        pinned — the same get_report shape the subledger-recon detector uses."""
        cur_start, cur_end = cur_bounds
        pri_start, pri_end = pri_bounds

        def _pl(start: date, end: date) -> dict:
            return ctx.qbo_client.get_report(
                "ProfitAndLoss",
                {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "accounting_method": "Accrual",
                },
            )

        def _bs(end: date) -> dict:
            return ctx.qbo_client.get_report(
                "BalanceSheet",
                {
                    "start_date": f"{end.year}-01-01",
                    "end_date": end.isoformat(),
                    "accounting_method": "Accrual",
                },
            )

        return {
            "pl_cur": _pl(cur_start, cur_end),
            "pl_pri": _pl(pri_start, pri_end),
            "bs_cur": _bs(cur_end),
            "bs_pri": _bs(pri_end),
        }


# Registration — the DUAL floor carries the repo's budget-gate lineage
# (pct>10 AND abs>=1000, proactive.py:764; RD-3 Topic 4 [11][12] name the dual
# design as the practitioner consensus but publish no universal number, so the
# in-repo lineage is the grounded default; Phase E overrides per company with
# zero check-code change). Two-sided by the Materiality gate's abs(pct).
register_check(
    ActualVsPriorFluxCheck(),
    materiality=Materiality.dual(
        pct=Decimal("10"), abs_floor=Decimal("1000"), label="flux dual floor (actual-vs-prior)"
    ),
    task_types=[TASK_TYPE_REVIEW_FLUX, "flux_analysis"],
    tier=CheckTier.STANDARD,  # core analytical review — runs at balanced + aggressive
)

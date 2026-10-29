# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns app/services/review_checks/** (plan.md Appendix A); the active-plan.json surface_scope entry carries an inline comment inside the glob string so the gate matcher misses it — scope is ratified, the state string is malformed.
"""rollforward_coverage — full-BS roll-forward coverage/completeness report.

Chunk C2C-CLOSE (plan 20260706-cfo-grade-financial-workflows, AC-2C.1 extend;
RD-3 Topic 5 + month-end-close SKILL §2.5). The roll-forward discipline
(`beginning + additions − reductions = ending`) is only as strong as its
COVERAGE: a control account with no continuity schedule is substantiated
from scratch every period — the chain is lost and a March error is invisible
by June. The GENERIC schedule substrate (schedule_types.py:22-27) lets ANY BS
account carry a schedule; what was missing at HEAD is the completeness VIEW —
which material BS accounts have no schedule at all.

WHAT THIS CHECK DOES: reads the Balance Sheet as of period end, excludes the
account families whose continuity is owned by a DIFFERENT close control (bank
rec owns cash; the sub-ledger tie-out owns AR/AP; equity articulates from the
P&L), and compares the remaining carrying accounts against the realm's
AccountingSchedule rows (matched by QBO account id when the report supplies
one, else by account name). A material account with no schedule is a coverage
gap — the proposal asks a human to schedule it (GENERIC preset), NOT to post
anything.

MATERIALITY: multi-line — the per-account floor consults the INJECTED
``ctx.materiality`` (never hardcoded in scan); the Finding carries the LARGEST
uncovered balance for run_check's outer gate.

PROPOSAL-FIRST: scan classifies; run_check drafts the PENDING ProposedAction.
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

TASK_TYPE_REVIEW_ROLLFORWARD_COVERAGE = "REVIEW_ROLLFORWARD_COVERAGE"

# BS families whose period-over-period continuity is owned by a DIFFERENT close
# control (never double-owned here): bank rec (cash), sub-ledger tie-out (AR/AP),
# and equity (articulates from the P&L close, not a schedule).
_EXCLUDED_LABEL_KEYWORDS = (
    "cash",
    "bank",
    "checking",
    "savings",
    "money market",
    "undeposited",
    "accounts receivable",
    "accounts payable",
    "retained earnings",
    "net income",
    "opening balance equity",
    "owner",  # owner's equity / draws / contributions
    "capital stock",
    "common stock",
    "equity",
)

# Schedule lifecycle states that COUNT as coverage (an active or completed
# continuity schedule both carry the chain; DETECTED/PENDING are not yet cover).
_COVERING_SCHEDULE_STATUSES = ("ACTIVE", "FULLY_AMORTIZED")


def _is_excluded(label: str) -> bool:
    lowered = label.lower()
    return any(keyword in lowered for keyword in _EXCLUDED_LABEL_KEYWORDS)


class BSRollforwardCoverageCheck:
    """Full-BS roll-forward coverage: material carrying accounts w/o a schedule."""

    check_id = "bs_rollforward_coverage"

    def scan(self, ctx: CheckContext) -> Finding:
        if ctx.qbo_client is None:
            return Finding.could_not_verify(
                self.check_id,
                message="No live QuickBooks client — BS roll-forward coverage cannot be computed.",
            )
        try:
            year_str, month_str = ctx.period.split("-", 1)
            year, month = int(year_str), int(month_str)
            period_end = date(year, month, calendar.monthrange(year, month)[1])
        except (ValueError, TypeError):
            return Finding.could_not_verify(
                self.check_id,
                message=f"period {ctx.period!r} is not a parseable YYYY-MM period.",
            )

        try:
            report = ctx.qbo_client.get_report(
                "BalanceSheet",
                {
                    "start_date": f"{period_end.year}-01-01",
                    "end_date": period_end.isoformat(),
                    "accounting_method": "Accrual",
                },
            )
        except Exception as e:  # a failed BS fetch is NOT a clean pass
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"BalanceSheet fetch failed: {redact_to_string(e)}",
            )

        leaves = bs_leaf_lines(report)
        if not leaves:
            return Finding.could_not_verify(
                self.check_id,
                message="BalanceSheet parsed to zero account lines — coverage source unreadable.",
            )

        try:
            from ...models import AccountingSchedule

            schedules = (
                ctx.db.query(
                    AccountingSchedule.qbo_account_id, AccountingSchedule.qbo_account_name
                )
                .filter(
                    AccountingSchedule.realm_id == ctx.realm_id,
                    AccountingSchedule.status.in_(_COVERING_SCHEDULE_STATUSES),
                )
                .all()
            )
        except Exception as e:  # a read failure is NOT a clean pass
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"schedule coverage read failed: {redact_to_string(e)}",
            )

        covered_ids = {str(s[0]).strip() for s in schedules if s[0]}
        covered_names = {str(s[1]).strip().lower() for s in schedules if s[1]}

        in_scope: list[dict] = []
        uncovered: list[dict] = []
        for leaf in leaves:
            if _is_excluded(leaf["label"]):
                continue
            if leaf["value"] == 0:
                continue  # a zero carrying balance has no chain to lose this period
            covered = bool(
                (leaf["account_id"] and leaf["account_id"] in covered_ids)
                or leaf["label"].strip().lower() in covered_names
            )
            entry = {
                "account": leaf["label"],
                "account_id": leaf["account_id"],
                "balance": leaf["value"],
                "covered": covered,
            }
            in_scope.append(entry)
            # Per-account materiality: the INJECTED floor, never hardcoded here.
            if not covered and ctx.materiality.is_material(amount=abs(leaf["value"])):
                uncovered.append(entry)

        if not in_scope:
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"No roll-forward-scoped BS accounts carry a balance as of {period_end.isoformat()} "
                    "(cash / AR / AP / equity are owned by their own close controls)."
                ),
            )
        if not uncovered:
            covered_count = sum(1 for e in in_scope if e["covered"])
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"Full-BS roll-forward coverage clean: {covered_count}/{len(in_scope)} scoped "
                    f"account(s) scheduled; no MATERIAL unscheduled balance as of {period_end.isoformat()}."
                ),
            )

        uncovered.sort(key=lambda e: abs(e["balance"]), reverse=True)
        worst = uncovered[0]
        coverage_pct = round(
            100.0 * sum(1 for e in in_scope if e["covered"]) / len(in_scope), 1
        )
        return Finding.hit(
            self.check_id,
            message=(
                f"{len(uncovered)} material BS account(s) carry a balance with NO roll-forward "
                f"schedule as of {period_end.isoformat()} (largest: {worst['account']} "
                f"${worst['balance']:,.2f}; scoped coverage {coverage_pct}%). Without the "
                "continuity schedule the beginning→ending chain is lost — schedule these on the "
                "GENERIC substrate; never force a tie by adjusting a beginning balance."
            ),
            severity=FindingSeverity.WARNING,
            amount=Decimal(str(abs(worst["balance"]))),
            payload={
                "period": ctx.period,
                "as_of": period_end.isoformat(),
                "scoped_accounts": len(in_scope),
                "coverage_pct": coverage_pct,
                "uncovered_material": uncovered[:25],
                "excluded_families": "cash/bank (bank rec), AR/AP (sub-ledger tie-out), equity (P&L articulation)",
            },
        )


# Registration — ABSOLUTE per-account floor carrying the $1,000 lineage (the
# budget DUAL abs / subledger-break floor family, new_checks.py registrations).
# Phase E overrides per company with zero check-code change.
register_check(
    BSRollforwardCoverageCheck(),
    materiality=Materiality.absolute(Decimal("1000"), label="uncovered roll-forward balance floor"),
    task_types=[TASK_TYPE_REVIEW_ROLLFORWARD_COVERAGE, "rollforward_coverage"],
    tier=CheckTier.STANDARD,  # completeness view — runs at balanced + aggressive
)

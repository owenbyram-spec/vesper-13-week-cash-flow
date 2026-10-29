# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns app/services/review_checks/** (plan.md Appendix A); the active-plan.json surface_scope entry carries an inline comment inside the glob string so the gate matcher misses it — scope is ratified, the state string is malformed.
"""cutoff_check — revenue/expense cut-off scan (post-period documents belonging
to the closed period).

Chunk C2C-CLOSE (plan 20260706-cfo-grade-financial-workflows, AC-2C.1 build-new;
RD-3 Topic 3 + month-end-close SKILL §2.3). The cut-off discipline: a document
DATED after period end can still BELONG to the closed period — the detector must
key on the SERVICE period, not the document date (RD-3 3(4a): "the invoice dated
day+3 for services consumed in the closed month lands in the wrong period").

FRAMING (binding, tagged per CLAUDE.md §3):
  - ASC 606 (`binding`): revenue is recognized as performance obligations are
    satisfied — a post-period invoice for performance completed in the closed
    period belongs to the closed period (corpus records 91–104).
  - ASC 855 (`binding`; asc_knowledge corpus record 148, ASU 2010-09): events
    between period end and issuance split RECOGNIZED (condition existed at
    period end → adjust the closed period) vs NONRECOGNIZED (condition arose
    after → disclose only). A post-period bill for a period-end obligation is
    the recognized class.

DETECTION (deterministic, zero-LLM): scan the local ``QBOTransaction`` cache for
documents dated in the days AFTER period end whose free text references the
closed period (the service-period signal available without line-item service
dates: an explicit "YYYY-MM" / "M/YYYY" token or the closed month's name). Each
candidate carries its matched signal so the reviewer sees WHY it was flagged.

HONEST DEGRADATION: a realm with NO synced transaction rows at all has given the
detector nothing to scan — ``could_not_verify`` (the qbo_transactions cache has
been observed empty DB-wide), never a fabricated clean pass. Rows present but no
candidates → a VERIFIED ``no_hit``.

MATERIALITY: the finding's gate amount is the GROSS (absolute-sum) of candidate
amounts — cut-off errors OFFSET across periods by construction (RD-3 3(2)), so
a signed net would hide exactly the class this check exists to catch. Registered
floor: the on-disk $500 GL-scrape lineage (proactive.py:315).

PROPOSAL-FIRST: ``scan`` classifies; ``run_check`` drafts the PENDING
ProposedAction a human reviews. Nothing here posts, completes, or locks.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from . import CheckTier, register_check
from .materiality import Materiality
from .protocol import CheckContext, Finding, FindingSeverity

logger = logging.getLogger(__name__)

TASK_TYPE_REVIEW_CUTOFF = "REVIEW_CUTOFF"

# How many days after period end the scan covers. RD-3 Topic 3(1c) prescribes
# scanning "the days after period end" without a fixed count; 15 days spans the
# top-quartile→median close window (lock by day ~5-7, APQC) plus buffer, so every
# document that can still be caught BEFORE the lock is in scope. Bounded so the
# scan never drifts into genuinely-next-period activity.
CUTOFF_SCAN_WINDOW_DAYS = 15

# Direction classes for the ASC framing on each candidate (QBOTransaction
# transaction_type values observed in the flattened cache).
_REVENUE_TYPES = {"invoice", "salesreceipt", "creditmemo"}
_EXPENSE_TYPES = {"bill", "purchase", "expense", "check", "vendorcredit", "creditcardcredit"}

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
# Month names that are also common English words — a bare "may"/"march"/"august"
# in a memo is NOT a period signal; these require the year alongside.
_AMBIGUOUS_MONTHS = {"may", "march", "august"}


def _period_reference_patterns(period: str) -> list[tuple[re.Pattern, str]]:
    """Compiled signals that a document's free text references the closed period."""
    year_str, month_str = period.split("-", 1)
    month = int(month_str)
    month_name = _MONTH_NAMES[month - 1]
    patterns: list[tuple[re.Pattern, str]] = [
        (re.compile(rf"\b{re.escape(period)}\b"), f"period token {period}"),
        (re.compile(rf"\b0?{month}/{year_str}\b"), f"period token {month}/{year_str}"),
    ]
    if month_name in _AMBIGUOUS_MONTHS:
        patterns.append(
            (
                re.compile(rf"\b{month_name}\s+{year_str}\b", re.IGNORECASE),
                f"month name '{month_name.title()} {year_str}'",
            )
        )
    else:
        patterns.append(
            (re.compile(rf"\b{month_name}\b", re.IGNORECASE), f"month name '{month_name.title()}'")
        )
        patterns.append(
            (
                re.compile(rf"\b{month_name[:3]}[\s\-.]", re.IGNORECASE),
                f"month abbreviation '{month_name[:3].title()}'",
            )
        )
    return patterns


def _match_period_reference(text: str, patterns: list[tuple[re.Pattern, str]]) -> str | None:
    """The first matched service-period signal in ``text``, or None."""
    for pattern, signal in patterns:
        if pattern.search(text):
            return signal
    return None


class RevenueExpenseCutoffCheck:
    """Post-period documents referencing the closed period (ASC 606/855 framing)."""

    check_id = "revenue_expense_cutoff"

    def scan(self, ctx: CheckContext) -> Finding:
        try:
            year_str, month_str = ctx.period.split("-", 1)
            year, month = int(year_str), int(month_str)
            period_end = date(year, month, calendar.monthrange(year, month)[1])
        except (ValueError, TypeError):
            return Finding.could_not_verify(
                self.check_id,
                message=f"period {ctx.period!r} is not a parseable YYYY-MM period.",
            )
        window_end = period_end + timedelta(days=CUTOFF_SCAN_WINDOW_DAYS)

        try:
            from ...models import QBOTransaction

            any_rows = (
                ctx.db.query(QBOTransaction.id)
                .filter(QBOTransaction.realm_id == ctx.realm_id)
                .first()
            )
            if any_rows is None:
                # The local transaction cache has NOTHING for this realm — the
                # scan cannot see any documents (observed empty DB-wide); a
                # blind scan is could_not_verify, never a clean pass.
                return Finding.could_not_verify(
                    self.check_id,
                    message="No synced QBO transactions for this realm — cut-off scan has no documents to inspect.",
                )

            post_period_txns = (
                ctx.db.query(QBOTransaction)
                .filter(
                    QBOTransaction.realm_id == ctx.realm_id,
                    QBOTransaction.date > period_end,
                    QBOTransaction.date <= window_end,
                )
                .order_by(QBOTransaction.date.asc())
                .all()
            )
        except Exception as e:  # a read failure is NOT a clean pass
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"cut-off scan query failed: {redact_to_string(e)}",
            )

        patterns = _period_reference_patterns(ctx.period)
        candidates: list[dict] = []
        for txn in post_period_txns:
            text = str(txn.description or "")
            signal = _match_period_reference(text, patterns)
            if signal is None:
                continue
            txn_type = str(txn.transaction_type or txn.entity_type or "").strip()
            type_key = txn_type.lower().replace(" ", "")
            if type_key in _REVENUE_TYPES:
                framing = (
                    "Revenue cut-off (ASC 606, binding): recognize as performance obligations "
                    "are satisfied — if performance completed in the closed period, this belongs there."
                )
            elif type_key in _EXPENSE_TYPES:
                framing = (
                    "Expense cut-off (ASC 855 — asc_knowledge corpus record 148, binding): a "
                    "post-period document for an obligation that existed at period end is a "
                    "RECOGNIZED subsequent event — accrue it into the closed period."
                )
            else:
                framing = (
                    "Cut-off review (ASC 606/855, binding — ASC 855 is corpus record 148): "
                    "confirm which period this document's underlying activity belongs to."
                )
            candidates.append(
                {
                    "qbo_id": txn.qbo_id,
                    "transaction_type": txn_type or None,
                    "date": txn.date.isoformat() if txn.date else None,
                    "amount": float(txn.amount or 0),
                    "description": text[:300],
                    "matched_signal": signal,
                    "framing": framing,
                }
            )

        if not candidates:
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"No post-period documents (through {window_end.isoformat()}) reference "
                    f"the closed period {ctx.period} — cut-off scan clean."
                ),
            )

        # GROSS magnitude for the materiality gate: cut-off errors offset across
        # periods (RD-3 3(2)) — a signed net would hide the offsetting class.
        gross = round(sum(abs(c["amount"]) for c in candidates), 2)
        return Finding.hit(
            self.check_id,
            message=(
                f"{len(candidates)} post-period document(s) reference the closed period "
                f"{ctx.period} (gross ${gross:,.2f}) — review for closed-period ownership: "
                "cut-off keys on the SERVICE period, not the document date "
                "(ASC 606 / ASC 855 — corpus record 148)."
            ),
            severity=FindingSeverity.WARNING,
            amount=Decimal(str(gross)),
            payload={
                "period": ctx.period,
                "scan_window_end": window_end.isoformat(),
                "candidates": candidates[:50],
                "candidate_count": len(candidates),
                "gross_amount": gross,
                "guidance": (
                    "ASC 606 (binding): revenue when performance obligations are satisfied. "
                    "ASC 855 (binding; asc_knowledge corpus record 148): recognized (condition "
                    "existed at period end — adjust) vs nonrecognized (arose after — disclose only). "
                    "Beware double-booking: an accrual posted AND the actual invoice without a reversal."
                ),
            },
        )


# Registration — ABSOLUTE floor carrying the $500 GL-scrape lineage
# (proactive.py:315); the gate applies to the GROSS candidate magnitude. Phase E
# overrides per company with zero check-code change.
register_check(
    RevenueExpenseCutoffCheck(),
    materiality=Materiality.absolute(Decimal("500"), label="cut-off gross candidate floor"),
    task_types=[TASK_TYPE_REVIEW_CUTOFF, "cutoff_scan"],
    tier=CheckTier.STANDARD,  # accrual-completeness family — runs at balanced + aggressive
)

"""bank_recon_check.py — the close-walk BANK_RECONCILIATION completeness check.

Wave 2.3 chunk B (plan 20260713-wave2-3-bank-recon, decision 7 / AC-5). The close
driver's check-branch (close_orchestration.py:390-407) resolves the
``BANK_RECONCILIATION`` task_type — already generated for every standard-template
tenant (close_task_generation.py:72) — through the registry to THIS check, so the
close walk drafts a REAL finding instead of skipping the step as tool-less. Zero
driver edits: the wiring is the registration import line in ``__init__.py`` only.

The contract (E6-corrected, verbatim from the ratified plan):

  * LOCAL-DB-ONLY scan. It reads ``ReconciliationSession`` rows + the book-txn
    tie-out primitives and NEVER touches ``ctx.qbo_client`` — the close driver
    injects ``qbo_client=None`` and this check still reaches a real conclusion.
  * PASS = ALL in-period sessions COMPLETE. ABANDONED counts as NOT-complete and
    zero/no sessions is a FINDING — mirroring the human verify
    (``_verify_bank_reconciliation``, close_tasks.py:195-233).
  * THE E6 MATERIALITY TRAP: ``run_check``'s materiality gate silently swallows an
    ``amount=None`` finding (``Materiality.is_material`` returns False → IMMATERIAL
    → NO proposal). This check therefore ALWAYS populates ``amount``: the largest
    absolute unreconciled tie-out difference when an OPEN session exists,
    ``Decimal("0")`` for the no-session / ABANDONED-only finding — paired with the
    ABSOLUTE-ZERO materiality floor registered below (``0 >= 0`` clears the gate;
    a missing reconciliation is material by definition).
  * READ-ONLY: this check never opens / resumes / completes / abandons a session
    and never sets a cleared flag — it only reads and lets ``run_check`` draft the
    standard idempotent ``CLOSE_STEP_REVIEW`` proposal through the injected
    ``propose`` (fingerprint dedupe + close_task/run linkage inherited).

Month-end-close skill §2.2 grounding: bank rec completes ONLY at difference == 0
(session.py — never plug a gap), so an incomplete session is surfaced with its
honest difference + the unmatched count + the open/resume/finish next action.
"""
# PLAN-EXEMPT: ratified plan 20260713-wave2-3-bank-recon chunk B (RATIFICATION-2026-07-13.md) — app/services/review_checks/** is in the plan's surface_scope

from __future__ import annotations

from datetime import date
from decimal import Decimal

from . import CheckTier, register_check
from .materiality import Materiality
from .protocol import CheckContext, Finding, FindingSeverity

_ZERO = Decimal("0")


def _period_window(period: str) -> tuple[date, date] | None:
    """[start, end) date window for a ``YYYY-MM`` period; None on malformed input.

    Mirrors the human verify's parse (close_tasks.py:198-207) so the check and
    the checklist verifier select the SAME session set for a period.
    """
    try:
        parts = period.split("-")
        year, month = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None
    if not 1 <= month <= 12:
        return None
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


class BankReconCheck:
    """Flags a period whose bank reconciliations are missing or not COMPLETE.

    HIT when the period has zero sessions, any OPEN session, or only ABANDONED
    sessions; NO_HIT only when every in-period session is COMPLETE. The scan is
    local-DB-only and read-only (see the module docstring for the E6 amount
    contract this check must never violate).
    """

    check_id = "bank_reconciliation_complete"

    def scan(self, ctx: CheckContext) -> Finding:
        window = _period_window(ctx.period)
        if window is None:
            # No resolvable period → the check cannot select the session set.
            # Honest degradation (DEC-CAR-C-4), never a fabricated pass/fail.
            return Finding.could_not_verify(
                self.check_id,
                message=(
                    f"Period {ctx.period!r} is not a YYYY-MM period — the bank "
                    "reconciliation completeness for it cannot be evaluated."
                ),
            )
        period_start, period_end = window

        # Lazy imports (package convention — new_checks.py does the same): keep
        # the review_checks package import-light; the bank_recon service graph
        # loads only when this scan actually runs.
        from ...models import ReconciliationSession
        from ...services.bank_recon import session as recon_session

        sessions = (
            ctx.db.query(ReconciliationSession)
            .filter(
                ReconciliationSession.realm_id == ctx.realm_id,
                ReconciliationSession.period_end >= period_start,
                ReconciliationSession.period_end < period_end,
            )
            .order_by(ReconciliationSession.account_id, ReconciliationSession.id)
            .all()
        )

        if not sessions:
            # THE E6 TRAP, disarmed: amount MUST be populated or the registry's
            # materiality gate swallows this finding (amount=None → is_material
            # False → NO proposal). Decimal("0") + the registered absolute-zero
            # floor makes the no-session finding material by definition.
            return Finding.hit(
                self.check_id,
                message=(
                    f"No bank reconciliation sessions exist for {ctx.period}. "
                    "Open a reconciliation session for each bank account, tie it "
                    "out to the statement ending balance, and complete it at a "
                    "$0.00 difference before closing the period."
                ),
                severity=FindingSeverity.WARNING,
                amount=_ZERO,
                payload={"period": ctx.period, "session_count": 0, "complete_count": 0, "sessions": []},
            )

        incomplete = [s for s in sessions if s.status != recon_session.STATUS_COMPLETE]
        if not incomplete:
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"All {len(sessions)} bank reconciliation session(s) for "
                    f"{ctx.period} are COMPLETE."
                ),
            )

        lines: list[str] = []
        details: list[dict] = []
        largest_gap = _ZERO
        # SA-OK: bounded-loop — one reconciliation session per bank account per period (single-digit N); the tie-out + outstanding reads are the per-session service primitives, not a scalable row scan
        for s in incomplete:
            label = s.account_name or s.account_id
            if s.status == recon_session.STATUS_OPEN:
                tieout = recon_session.compute_tieout(ctx.db, realm_id=ctx.realm_id, session=s)
                unmatched = len(recon_session.outstanding_items(ctx.db, realm_id=ctx.realm_id, session=s))
                gap = abs(tieout.difference)
                if gap > largest_gap:
                    largest_gap = gap
                if gap != _ZERO:
                    next_action = (
                        "resume the session and keep matching until the "
                        "difference reaches $0.00, then finish it"
                    )
                else:
                    next_action = "finish (complete) the session — it already ties to the statement"
                anchor_note = (
                    ""
                    if tieout.anchor_configured
                    else " (no clean-start anchor is configured, so the opening balance is unverified)"
                )
                lines.append(
                    f"{label}: OPEN with an unreconciled difference of ${gap:,.2f} "
                    f"and {unmatched} unmatched book transaction(s){anchor_note} — {next_action}."
                )
                details.append(
                    {
                        "session_id": s.id,
                        "account_id": s.account_id,
                        "account_name": s.account_name,
                        "status": s.status,
                        "difference": str(gap),
                        "unmatched_count": unmatched,
                        "anchor_configured": tieout.anchor_configured,
                        "next_action": "resume" if gap != _ZERO else "finish",
                    }
                )
            else:  # ABANDONED — its clears were released; it reconciles nothing.
                lines.append(
                    f"{label}: ABANDONED — its cleared matches were released, so the "
                    f"period is unreconciled; open a new reconciliation session for {ctx.period}."
                )
                details.append(
                    {
                        "session_id": s.id,
                        "account_id": s.account_id,
                        "account_name": s.account_name,
                        "status": s.status,
                        "difference": None,
                        "unmatched_count": None,
                        "next_action": "open",
                    }
                )

        complete_count = len(sessions) - len(incomplete)
        message = (
            f"{complete_count}/{len(sessions)} bank reconciliation session(s) "
            f"complete for {ctx.period}. " + " ".join(lines)
        )
        # amount is ALWAYS populated (E6): the largest absolute OPEN-session
        # difference, or Decimal("0") when the incomplete set is ABANDONED-only /
        # already ties at $0 — the absolute-zero floor keeps every variant material.
        return Finding.hit(
            self.check_id,
            message=message,
            severity=FindingSeverity.WARNING,
            amount=largest_gap,
            payload={
                "period": ctx.period,
                "session_count": len(sessions),
                "complete_count": complete_count,
                "sessions": details,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registration (module-level; fired by the __init__ import line — the ONLY wiring)
# ─────────────────────────────────────────────────────────────────────────────
# ABSOLUTE-ZERO materiality floor (plan decision 7 / AC-5, the E6 contract): this
# check's findings gate on ``amount >= Decimal("0")``, which every populated
# amount clears — including the Decimal("0") no-session / ABANDONED-only finding.
# A missing or incomplete bank reconciliation is material BY DEFINITION (the cash
# control either tied out or it did not — there is no immaterial version of
# "nobody reconciled the bank account"); a non-zero gap clears the floor a
# fortiori. Phase E can still override per company at registration (DEC-CAR-C-3).
# CRITICAL tier: bank reconciliation is the load-bearing cash control of the close
# (month-end-close §2.2) — like the subledger↔GL tie-outs, it runs even under a
# conservative review_aggressiveness.
register_check(
    BankReconCheck(),
    materiality=Materiality.absolute(
        _ZERO,
        label="bank-reconciliation completeness — absolute-zero floor (a missing/incomplete reconciliation is material by definition)",
    ),
    task_types=["BANK_RECONCILIATION"],
    tier=CheckTier.CRITICAL,
)

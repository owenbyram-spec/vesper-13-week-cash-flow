"""new_checks.py — the four NEW analytical-review checks (Phase C, chunk C2).

Each Check wraps a pure ``app/analysis`` detector and maps its dict output into a
``Finding`` (hit / no_hit / could_not_verify) per the C0 framework
(app/services/review_checks/protocol.py). ``scan`` owns STAGE 1 ONLY — the
deterministic detector call + classification; ``run_check`` (C0) runs the
materiality / closed-period / cap / single-LLM / propose gates and is the ONLY
place a PENDING ProposedAction is drafted. NO check here writes the GL
(proposal-first, CLAUDE.md §5).

The four NEW checks (SYNTHESIS Area 2):
  1. UnusualJEStructureCheck   → app/analysis/je_structure.py (TWO-SIDED median/MAD)
  2. ARAPDeteriorationCheck     → app/analysis/ar_ap_deterioration.py (reuses aging)
  3. SubledgerReconCheck        → app/analysis/subledger_recon.py (FAIL-CLOSED tie-out)
  4. CovenantProjectionCheck    → app/analysis/covenant_projection.py (4C eval-only)

HONEST DEGRADATION (DEC-CAR-C-4): a detector that returns ``available=False``
(missing / unreadable / un-computable source) is mapped to
``Finding.could_not_verify`` — NEVER ``no_hit`` and NEVER a fabricated clean pass.
A live QBO client is required by every detector here; when ``ctx.qbo_client is
None`` (the Phase B close driver passes None today) the scan returns
could_not_verify rather than crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import CheckTier, register_check
from .materiality import Materiality
from .protocol import CheckContext, Finding, FindingSeverity

# Mirror covenant_projection's horizon cap so the DERIVED urgency math lines up
# (urgency = MAX_PROJECTION_PERIODS - periods_until_breach).
MAX_PROJECTION_PERIODS = 12

# ─────────────────────────────────────────────────────────────────────────────
# 0. Unposted schedule amortization (F-B2) — pure core + the check
# ─────────────────────────────────────────────────────────────────────────────
# At close, detect ACTIVE amortization schedules with amortization left to move —
# EITHER sign: under-amortized, a negative (credit-adjustment) schedule, or one
# already PAST zero — but NO entry for the period yet, and surface the SIGNED
# amount that SHOULD post (run_check proposes — the scan NEVER writes the GL).
# Amount math REUSES the engine helpers (schedule_engine.compute_monthly_amount /
# period_in_schedule_range / is_final_schedule_period) — one source of
# amortization truth. WS-1: the engine's SIGN-AWARE over-amortization clamp
# (plan 20260701-pilot-fixes-batch-3 Wave 1) is a closure inside
# generate_month_entries (_remaining_amortizable + the corrective / final-plug /
# mirror-clamp branch), not importable; the pure core RE-EXPRESSES that SAME
# branch — the four rules are spelled out on detect_unposted_schedule_amortization
# — and ``scan`` computes the posted-outside-period sum (a realm-scoped SQL sum
# mirroring the engine's amortized_sums bulk fetch). The generator is untouched;
# the goldens (tests/test_unposted_schedule_amortization_golden.py) + the
# engine-parity lane (tests/test_unposted_schedule_amortization_engine_parity.py)
# pin the two formulas identical. Deliberately OUT of scope (unchanged from the
# original check): a schedule PAST its window with a lingering balance — the
# engine's B5-7 past-window catch-up true-up owns that one-time posting; this
# check evaluates in-range periods only.


@dataclass(frozen=True)
class _ScheduleAmortizationInput:
    """One schedule's DB-free inputs to the pure detector (the golden seeds these).
    ``posted_outside_period`` = sum of non-SKIPPED entry amounts for OTHER periods
    (the over-amortization guard's input — mirrors the engine's amortized_sums
    bulk fetch inside generate_month_entries). ``has_period_entry`` = a non-SKIPPED
    entry already exists FOR the target period (WS-2: a PENDING_APPROVAL / DRAFT
    entry counts as 'has an entry')."""

    schedule_id: int
    schedule_type: str
    original_amount: float
    accumulated_amount: float
    useful_life_months: int | None
    start_date_ym: str  # "YYYY-MM" of start_date (day is always 1 in range math)
    end_date_ym: str | None  # "YYYY-MM" of end_date, or None (open-ended)
    expense_account_id: str | None
    posted_outside_period: float
    has_period_entry: bool


@dataclass
class _ProposedAmortization:
    """Duck-typed stand-in carrying ONLY the fields the engine helpers read, so the
    pure core can call the REAL helpers without a live ORM row (the golden then
    exercises the same math the generator uses)."""

    original_amount: float
    useful_life_months: int | None
    start_date: object
    end_date: object | None


def _ym_to_date(ym: str):
    from datetime import date as _date

    return _date(int(ym[:4]), int(ym[5:7]), 1)


# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
def _period_end_date(period: str):
    """Last day of a "YYYY-MM" period, or None on a malformed period (the
    caller then lets the detector use its own default — no fabricated date)."""
    import calendar as _calendar
    from datetime import date as _date

    try:
        year_str, month_str = period.split("-", 1)
        year, month = int(year_str), int(month_str)
        return _date(year, month, _calendar.monthrange(year, month)[1])
    except (ValueError, TypeError):
        return None


def detect_unposted_schedule_amortization(
    schedules: list[_ScheduleAmortizationInput],
    period: str,
) -> tuple[bool, float, list[dict]]:
    """PURE core (no DB / no QBO / no ctx) → ``(hit, net_total, payload)``.

    Re-expresses generate_month_entries' SIGN-AWARE over-amortization clamp (the
    non-importable closure branch, plan 20260701-pilot-fixes-batch-3 Wave 1) for
    every in-range schedule (period_in_schedule_range) with NO existing non-SKIPPED
    entry for ``period``. With ``remaining = round(original − accumulated −
    posted_outside_period, 2)`` (the engine's _remaining_amortizable) and
    ``monthly = compute_monthly_amount`` (SIGNED — negative for a credit-adjustment
    schedule), the engine's rules, in the engine's order:

      0. ``monthly == 0`` (zero original / non-positive life) → nothing (the
         engine's no_monthly skip — checked BEFORE remaining, as the engine does).
      1. ``|remaining| <= 0.005`` → fully amortized, nothing to propose (BOTH
         signs — the engine's abs() check).
      2. sign(monthly) != sign(remaining) → ONE corrective month equal to the
         SIGNED remaining: an over-amortized positive schedule proposes a NEGATIVE
         reversal; an over-reversed negative schedule a POSITIVE one. Never a
         skip, never an unclamped repeat of ±monthly.
      3. final in-range period → plug to EXACTLY ``remaining`` (both signs, both
         directions) so the series sums to original_amount — kills the
         cent-rounding drift of round(original/life, 2).
      4. otherwise → magnitude clamp, never past zero in either direction:
         ``min(monthly, remaining)`` for a positive schedule,
         ``max(monthly, remaining)`` for a negative one.

    ``net_total`` is the SIGNED sum of the proposals (what would post); each
    payload item carries its signed ``amount`` plus a ``corrective`` flag.
    ``hit=False`` when nothing qualifies — zero schedules (WS-5) and
    fully-amortized included. Never raises on empty input; a query failure is the
    caller's could_not_verify."""
    from ...services.schedule_engine import (
        compute_monthly_amount,
        is_final_schedule_period,
        period_in_schedule_range,
    )

    payload_items: list[dict] = []
    total = 0.0
    for s in schedules:
        if s.has_period_entry:  # WS-2: period already covered → do not re-propose.
            continue

        proxy = _ProposedAmortization(
            original_amount=s.original_amount,
            useful_life_months=s.useful_life_months,
            start_date=_ym_to_date(s.start_date_ym),
            end_date=_ym_to_date(s.end_date_ym) if s.end_date_ym else None,
        )
        if not period_in_schedule_range(proxy, period):  # out-of-range → nothing
            continue  # (the engine's B5-7 past-window true-up is NOT this check's scope).

        monthly = compute_monthly_amount(proxy)  # SIGNED; engine order: monthly first.
        if monthly == 0:  # the engine's no_monthly skip (zero original / bad life).
            continue

        remaining = round(  # the engine's _remaining_amortizable formula.
            float(s.original_amount or 0)
            - float(s.accumulated_amount or 0)
            - float(s.posted_outside_period or 0),
            2,
        )
        if abs(remaining) <= 0.005:  # fully amortized, EITHER sign → nothing.
            continue

        if (monthly > 0) != (remaining > 0):
            # One-time corrective reversal (sign mismatch): the schedule is
            # already past zero in its amortization direction; the SIGNED
            # remaining plugs it back to exactly 0.
            amount = remaining
            corrective = True
        elif is_final_schedule_period(proxy, period):
            # Final-period plug: exactly what's left, both signs.
            amount = remaining
            corrective = False
        else:
            # Same-sign magnitude clamp: never step past zero in either direction.
            amount = min(monthly, remaining) if monthly > 0 else max(monthly, remaining)
            corrective = False

        amount = round(amount, 2)
        if abs(amount) <= 0.005:
            continue

        total += amount
        payload_items.append(
            {
                "schedule_id": s.schedule_id,
                "period": period,
                "amount": amount,
                "corrective": corrective,
                "expense_account_id": s.expense_account_id,
            }
        )

    return (len(payload_items) > 0), round(total, 2), payload_items


class UnpostedScheduleAmortizationCheck:
    """Detects open amortization schedules with no entry for the closing period and
    surfaces the SIGNED amortization that should post — including a NEGATIVE
    corrective for an over-amortized schedule and the mirror clamp for negative
    (credit-adjustment) schedules (proposal-first; the scan does NOT write the GL —
    ``run_check`` drafts the PENDING ProposedAction). ``scan`` does the realm-scoped
    DB read, maps each row to a DB-free ``_ScheduleAmortizationInput``, then calls
    the PURE ``detect_unposted_schedule_amortization`` core (WS-4). The schedule's
    tracked ``remaining`` drives the amount — NO live QBO read, so the scan needs
    no qbo_client (WS-3). The Finding's materiality ``amount`` is the GROSS
    magnitude of the proposals (the ABSOLUTE gate is one-sided; a signed net would
    let a corrective slip under the floor). A query failure → could_not_verify
    (honest degradation, DEC-CAR-C-4), never a fabricated clean pass."""

    check_id = "unposted_schedule_amortization"

    def scan(self, ctx: CheckContext) -> Finding:
        try:
            from sqlalchemy import func as sa_func

            from ...models import AccountingSchedule, ScheduleEntry

            # The generator's universe (generate_month_entries scans ACTIVE +
            # FULLY_AMORTIZED so a corrective can still fire on a "done" row).
            schedules = (
                ctx.db.query(AccountingSchedule)
                .filter(
                    AccountingSchedule.realm_id == ctx.realm_id,
                    AccountingSchedule.status.in_(["ACTIVE", "FULLY_AMORTIZED"]),
                )
                .all()
            )
            if not schedules:  # WS-5: a VERIFIED clean pass (nothing to amortize).
                return Finding.no_hit(
                    self.check_id,
                    message=f"No active amortization schedules for {ctx.period}.",
                )

            sched_ids = [s.id for s in schedules]

            # This-period non-SKIPPED entries → "has an entry" (WS-2). realm_id pins
            # the tenant; schedule_id.in_(sched_ids) is already realm-bounded.
            period_entry_ids = {
                row[0]
                for row in ctx.db.query(ScheduleEntry.schedule_id)
                .filter(
                    ScheduleEntry.realm_id == ctx.realm_id,
                    ScheduleEntry.schedule_id.in_(sched_ids),
                    ScheduleEntry.period == ctx.period,
                    ScheduleEntry.status != "SKIPPED",
                )
                .all()
            }

            # Posted-outside-period sums — the over-amortization guard's input
            # (mirrors the engine's amortized_sums bulk fetch in generate_month_entries).
            outside_sums = dict(
                ctx.db.query(ScheduleEntry.schedule_id, sa_func.sum(ScheduleEntry.amount))
                .filter(
                    ScheduleEntry.realm_id == ctx.realm_id,
                    ScheduleEntry.schedule_id.in_(sched_ids),
                    ScheduleEntry.status != "SKIPPED",
                    ScheduleEntry.period != ctx.period,
                )
                .group_by(ScheduleEntry.schedule_id)
                .all()
            )
        except Exception as e:  # honest degradation — a read failure is NOT clean.
            from ..redaction import redact_to_string

            return Finding.could_not_verify(
                self.check_id,
                message=f"schedule amortization scan failed: {redact_to_string(e)}",
            )

        inputs = [
            _ScheduleAmortizationInput(
                schedule_id=s.id,
                schedule_type=s.schedule_type or "PREPAID",
                original_amount=float(s.original_amount or 0),
                accumulated_amount=float(s.accumulated_amount or 0),
                useful_life_months=s.useful_life_months,
                start_date_ym=s.start_date.strftime("%Y-%m"),
                end_date_ym=s.end_date.strftime("%Y-%m") if s.end_date else None,
                expense_account_id=s.expense_account_id,
                posted_outside_period=float(outside_sums.get(s.id) or 0),
                has_period_entry=s.id in period_entry_ids,
            )
            for s in schedules
        ]

        hit, total, items = detect_unposted_schedule_amortization(inputs, ctx.period)
        if not hit:
            return Finding.no_hit(
                self.check_id,
                message=f"All active schedules have a {ctx.period} entry or are fully amortized.",
            )

        # GROSS magnitude for the materiality gate, NOT the signed net: the
        # ABSOLUTE gate is one-sided (amount >= floor), and a −$1,132.29
        # corrective reversal is every bit as material as a +$1,132.29 posting —
        # a signed net (or an offsetting mix of signs) would slip under the floor.
        gross = round(sum(abs(i["amount"]) for i in items), 2)
        n_corrective = sum(1 for i in items if i["corrective"])
        corrective_note = (
            f", including {n_corrective} corrective reversal(s)" if n_corrective else ""
        )
        return Finding.hit(
            self.check_id,
            message=(
                f"{len(items)} amortization schedule(s) have no {ctx.period} entry "
                f"(net ${total:,.2f} unposted{corrective_note})."
            ),
            severity=FindingSeverity.WARNING,
            amount=Decimal(str(gross)),
            payload={
                "period": ctx.period,
                "schedules": items,
                "total_amount": total,  # SIGNED net — what would post
                "gross_amount": gross,  # magnitude — the materiality basis
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Unusual JE structure
# ─────────────────────────────────────────────────────────────────────────────


class UnusualJEStructureCheck:
    """Flags structurally-unusual journal entries (round/weekend/backdate/
    unbalanced/suspense/duplicate + a TWO-SIDED median/MAD amount outlier)."""

    check_id = "unusual_je_structure"

    def scan(self, ctx: CheckContext) -> Finding:
        if ctx.qbo_client is None:
            return Finding.could_not_verify(
                self.check_id,
                message="No live QuickBooks client — JE structure cannot be evaluated.",
            )
        from ...analysis.je_structure import journal_entry_structure

        result = journal_entry_structure(ctx.qbo_client)
        if not result.get("available"):
            return Finding.could_not_verify(
                self.check_id,
                message=result.get("reason", "Journal-entry data unavailable."),
            )

        anomalies = result.get("anomalies", [])
        if not anomalies:
            return Finding.no_hit(
                self.check_id,
                message=f"No structurally-unusual journal entries among {result.get('total_entries', 0)} reviewed.",
            )

        # Materiality gates on the largest flagged entry's amount (ABSOLUTE shape).
        max_amt = max((a.get("amount", 0.0) for a in anomalies), default=0.0)
        return Finding.hit(
            self.check_id,
            message=(
                f"{len(anomalies)} journal entr{'y' if len(anomalies) == 1 else 'ies'} "
                f"with unusual structure (largest ${max_amt:,.2f}); "
                f"signals across: {result.get('method')}."
            ),
            severity=FindingSeverity.WARNING,
            amount=Decimal(str(max_amt)),
            payload={"anomalies": anomalies, "total_entries": result.get("total_entries")},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. AR/AP aging deterioration
# ─────────────────────────────────────────────────────────────────────────────


class ARAPDeteriorationCheck:
    """Flags a rising >90-day aging SHARE period-over-period (AR or AP)."""

    def __init__(self, side: str = "AR") -> None:
        self.side = side.upper()
        self.check_id = f"{self.side.lower()}_aging_deterioration"

    def scan(self, ctx: CheckContext) -> Finding:
        if ctx.qbo_client is None:
            return Finding.could_not_verify(
                self.check_id,
                message=f"No live QuickBooks client — {self.side} aging deterioration cannot be evaluated.",
            )
        from ...analysis.ar_ap_deterioration import ar_ap_aging_deterioration

        result = ar_ap_aging_deterioration(ctx.qbo_client, side=self.side)
        if not result.get("available"):
            return Finding.could_not_verify(
                self.check_id,
                message=result.get("reason", f"{self.side} aging deterioration unavailable."),
            )

        delta_pp = result.get("delta_share_pct_points", 0.0)
        # A FALLING or flat worst-bucket share is a clean pass (no deterioration).
        if delta_pp <= 0:
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"{self.side} >90-day share did not deteriorate "
                    f"({result.get('prior_worst_share')} → {result.get('current_worst_share')})."
                ),
            )

        # Materiality gates on the percentage-point rise (PERCENT shape, two-sided
        # in the gate; the deterioration direction is enforced here).
        return Finding.hit(
            self.check_id,
            message=(
                f"{self.side} >90-day share rose {delta_pp:+.2f} pts "
                f"({result.get('prior_worst_share')} → {result.get('current_worst_share')}) "
                f"as of {result.get('as_of')}."
            ),
            severity=FindingSeverity.WARNING,
            pct=Decimal(str(delta_pp)),
            amount=Decimal(str(result.get("current_total") or 0)),
            payload=result,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Subledger-to-GL reconciliation break
# ─────────────────────────────────────────────────────────────────────────────


class SubledgerReconCheck:
    """Flags a subledger-to-GL control-account reconciliation break (AR or AP).
    FAIL-CLOSED: a missing side → could_not_verify, never a fabricated tie-out."""

    def __init__(self, side: str = "AR") -> None:
        self.side = side.upper()
        self.check_id = f"{self.side.lower()}_subledger_gl_recon"

    def scan(self, ctx: CheckContext) -> Finding:
        # PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
        if ctx.qbo_client is None:
            return Finding.could_not_verify(
                self.check_id,
                message=f"No live QuickBooks client — {self.side} subledger/GL recon cannot be tied out.",
            )
        from ...analysis.subledger_recon import subledger_to_gl_recon

        # SAME-AS-OF-DATE discipline (AC-2C.1; month-end-close SKILL §2.6): at
        # close, BOTH tie-out sides (aging subledger + BS control) run as of the
        # CLOSING period's end — never "today". A malformed/absent period falls
        # back to the detector's own default (today) for the nightly caller.
        as_of = _period_end_date(ctx.period)
        result = subledger_to_gl_recon(ctx.qbo_client, side=self.side, as_of_date=as_of)
        if not result.get("available"):
            return Finding.could_not_verify(
                self.check_id,
                message=result.get("reason", f"{self.side} subledger/GL recon could not be tied out."),
            )

        if result.get("tied_out"):
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"{self.side} subledger ties to the GL control "
                    f"(${result.get('subledger_total'):,.2f} vs ${result.get('gl_control_balance'):,.2f})."
                ),
            )

        break_amount = result.get("break_amount", 0.0)
        return Finding.hit(
            self.check_id,
            message=(
                f"{self.side} subledger does NOT tie to the GL control: "
                f"${result.get('subledger_total'):,.2f} subledger vs "
                f"${result.get('gl_control_balance'):,.2f} GL — break ${break_amount:,.2f}."
            ),
            severity=FindingSeverity.CRITICAL,
            amount=Decimal(str(break_amount)),
            payload=result,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Covenant forward-projection (periods-until-breach)
# ─────────────────────────────────────────────────────────────────────────────


class CovenantProjectionCheck:
    """Projects a covenant metric forward off live-QBO trend and flags an
    imminent breach. REUSE = 4C eval-only (compute_builtin + _compute_status).

    The covenant params (metric_type / threshold / breach_direction) ride in
    ``ctx.extra`` — the close-task / nightly caller supplies the covenant under
    review; absent, the scan could_not_verifies (it has no covenant to project)."""

    check_id = "covenant_forward_projection"

    def scan(self, ctx: CheckContext) -> Finding:
        if ctx.qbo_client is None:
            return Finding.could_not_verify(
                self.check_id,
                message="No live QuickBooks client — covenant projection cannot be computed.",
            )
        metric_type = ctx.extra.get("metric_type")
        threshold = ctx.extra.get("threshold")
        breach_direction = ctx.extra.get("breach_direction", "BELOW")
        if not metric_type or threshold is None:
            return Finding.could_not_verify(
                self.check_id,
                message="No covenant (metric_type / threshold) supplied to project.",
            )
        from ...analysis.covenant_projection import covenant_periods_until_breach

        result = covenant_periods_until_breach(
            ctx.qbo_client,
            metric_type=str(metric_type),
            threshold=float(threshold),
            breach_direction=str(breach_direction),
            # PLAN-EXEMPT: item-3 add-backs — bank EBITDA in the trend when supplied (ratified 2026-06-25)
            addbacks=ctx.extra.get("ebitda_addbacks"),
        )
        if not result.get("available"):
            return Finding.could_not_verify(
                self.check_id,
                message=result.get("reason", "Covenant trend could not be computed (NO_DATA)."),
            )

        periods = result.get("periods_until_breach")
        if periods is None:
            return Finding.no_hit(
                self.check_id,
                message=(
                    f"{metric_type} is not trending toward a breach "
                    f"(value {result.get('current_value')}, slope {result.get('slope_per_period')})."
                ),
            )

        # DERIVED materiality: gates on the runway (periods) — a near-term breach
        # is material, a far-off projection is not. amount = periods so the
        # DERIVED floor (amount <= N) machinery can gate; we invert below to
        # 'fewer periods is more material' by passing a derived urgency amount.
        urgency = Decimal(str(MAX_PROJECTION_PERIODS - periods))
        sev = FindingSeverity.CRITICAL if periods <= 1 else FindingSeverity.WARNING
        return Finding.hit(
            self.check_id,
            message=(
                f"{metric_type} projected to breach its {breach_direction} threshold "
                f"{threshold} in ~{periods} period(s) "
                f"(value {result.get('current_value')} → trend {result.get('slope_per_period')}/period)."
            ),
            severity=sev,
            amount=urgency,
            payload=result,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registration (module-level — C3 wires __init__; we register here per the plan)
# ─────────────────────────────────────────────────────────────────────────────
# Each materiality is the Phase-C on-disk DEFAULT; Phase E overrides per company
# at registration with ZERO check-code change (DEC-CAR-C-3). The floors echo the
# on-disk inline floors the materiality value-object generalizes.

# TIER (Phase E AC-3): the two LOAD-BEARING families — subledger↔GL reconciliation
# (a control-account tie-out break is a hard correctness defect) and covenant
# projection (an imminent breach is a binding-agreement risk) — are CRITICAL so
# they run even under a conservative review_aggressiveness. The structural/aging
# checks are STANDARD (run at balanced + aggressive). NONE is OPTIONAL → balanced
# (the default) runs every existing check → zero default regression.

register_check(
    UnusualJEStructureCheck(),
    # Accrual/GL-scrape absolute floor lineage (proactive.py:315 $500).
    materiality=Materiality.absolute(Decimal("500"), label="unusual-JE largest-entry floor"),
    task_types=["unusual_je_review", "je_structure_review"],
    tier=CheckTier.STANDARD,  # structural JE review — runs at balanced + aggressive
)

register_check(
    ARAPDeteriorationCheck(side="AR"),
    # Two-sided percent floor: a >90-day share rise of >= 5 pts is material.
    materiality=Materiality.percent(Decimal("5"), label="AR aging-deterioration pct-point floor"),
    task_types=["ar_aging_review", "ar_aging_deterioration"],
    tier=CheckTier.STANDARD,  # aging-trend watch — runs at balanced + aggressive
)

register_check(
    ARAPDeteriorationCheck(side="AP"),
    materiality=Materiality.percent(Decimal("5"), label="AP aging-deterioration pct-point floor"),
    task_types=["ap_aging_review", "ap_aging_deterioration"],
    tier=CheckTier.STANDARD,  # aging-trend watch — runs at balanced + aggressive
)

# PLAN-EXEMPT: ratified plan 20260706-cfo-grade-financial-workflows chunk C2C-CLOSE owns review_checks/** (Appendix A); active-plan.json glob string is malformed (inline comment), scope itself is ratified.
# SUBLEDGER_TIEOUT_AR/AP are the CLOSE-CHECKLIST task_types (STANDARD_TASKS,
# close_tasks.py) wiring the tie-out into the close walker's step coverage
# (AC-2C.1 extend row: both sides were fetchable but no close step ran the
# comparator); the lowercase aliases are the pre-existing nightly mappings.
register_check(
    SubledgerReconCheck(side="AR"),
    # A subledger/GL break >= $1,000 is material (the budget DUAL abs lineage).
    materiality=Materiality.absolute(Decimal("1000"), label="AR subledger/GL break floor"),
    task_types=["ar_subledger_recon", "subledger_recon_ar", "SUBLEDGER_TIEOUT_AR"],
    tier=CheckTier.CRITICAL,  # load-bearing: a subledger↔GL tie-out break runs even under conservative
)

register_check(
    SubledgerReconCheck(side="AP"),
    materiality=Materiality.absolute(Decimal("1000"), label="AP subledger/GL break floor"),
    task_types=["ap_subledger_recon", "subledger_recon_ap", "SUBLEDGER_TIEOUT_AP"],
    tier=CheckTier.CRITICAL,  # load-bearing: a subledger↔GL tie-out break runs even under conservative
)

register_check(
    CovenantProjectionCheck(),
    # DERIVED urgency floor: urgency = (MAX_PROJECTION_PERIODS - periods); a breach
    # within ~6 periods (urgency >= 6) clears the floor. Runway/derived shape.
    materiality=Materiality.derived(Decimal("6"), label="covenant projection urgency floor"),
    task_types=["covenant_projection", "covenant_forward_projection"],
    tier=CheckTier.CRITICAL,  # load-bearing: an imminent covenant breach runs even under conservative
)

# Unposted schedule amortization (F-B2): ABSOLUTE floor mirroring the accrual
# per-entry $100 (proactive.py:179/:266 → MissingAccrualCheck registration). scan
# passes the GROSS magnitude of the signed proposals as ``amount`` (a corrective
# reversal is as material as a posting; a signed net would hide it from the
# one-sided ABSOLUTE gate); run_check proposes when gross >= $100. Phase E
# overrides per company with zero check-code
# change. STANDARD tier — routine completeness review (runs at balanced +
# aggressive), not a load-bearing CRITICAL control like the recon/covenant checks.
register_check(
    UnpostedScheduleAmortizationCheck(),
    materiality=Materiality.absolute(Decimal("100"), label="unposted-amortization total floor"),
    task_types=["unposted_schedule_amortization", "schedule_amortization_review"],
    tier=CheckTier.STANDARD,  # routine schedule-completeness review — runs at balanced + aggressive
)

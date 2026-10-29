"""
app/services/schedule_engine.py
────────────────────────────────
AI-managed schedule → journal entry proposal generation engine.

At month-end, scans all ACTIVE schedules for a company, computes the monthly
amount for each, and creates ScheduleEntry + ProposedAction records.
Users approve via the proposals workflow → JE posts to QBO.
"""
from __future__ import annotations

import logging
from .proposal_provenance import human_provenance

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from typing import TypedDict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .errors import safe_error_detail
from .redaction import redact_to_string
from .schedule_types import (
    DEFAULT_SCHEDULE_TYPE,
    DIRECTION_CREDIT_PL,
)

logger = logging.getLogger(__name__)

# Per-type JE descriptions / memo templates / BS-account patterns now live in
# the schedule-type descriptor registry (app/services/schedule_types.py) —
# the former JE_DESCRIPTIONS / MEMO_TEMPLATES / BS_ACCOUNT_PATTERNS module
# dicts collapsed onto it (plan 20260701-phase-a-generic-schedules AC-1).


def _resolve_account_id(
    db: Session,
    realm_id: str,
    account_name: str,
    account_id: Optional[str] = None,
    maps: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[str]:
    """
    Resolve a QBO account ID from either the stored ID or by name lookup.
    Handles display names like '7620: General & Admin:Insurance:Insurance - Liability'
    by trying multiple matching strategies:
      1. Use account_id directly if provided
      2. Exact name match
      3. Extract acct_num from '7620: ...' prefix and match on acct_num
      4. Extract fully qualified name portion and match on fully_qualified_name
    """
    if account_id:
        return account_id

    if not account_name:
        return None

    # Try pre-fetched memory maps, otherwise fallback to DB
    if maps is not None:
        if account_name in maps["name"]:
            return maps["name"][account_name]
        if ": " in account_name:
            parts = account_name.split(": ", 1)
            acct_num = parts[0].strip()
            fqn = parts[1].strip()
            if acct_num in maps["num"]:
                return maps["num"][acct_num]
            if fqn in maps["fqn"]:
                return maps["fqn"][fqn]
        return None

    # Try local account cache (QBOAccount table)
    try:
        from ..models import QBOAccount

        # Strategy 1: exact name match
        acct = db.query(QBOAccount).filter(
            QBOAccount.realm_id == realm_id,
            QBOAccount.name == account_name,
        ).first()
        if acct:
            return acct.qbo_id

        # Strategy 2: parse "NNNN: fully:qualified:name" format
        if ": " in account_name:
            parts = account_name.split(": ", 1)
            acct_num = parts[0].strip()
            fqn = parts[1].strip()

            # Try acct_num lookup
            acct = db.query(QBOAccount).filter(
                QBOAccount.realm_id == realm_id,
                QBOAccount.acct_num == acct_num,
            ).first()
            if acct:
                return acct.qbo_id

            # Try fully_qualified_name match
            acct = db.query(QBOAccount).filter(
                QBOAccount.realm_id == realm_id,
                QBOAccount.fully_qualified_name == fqn,
            ).first()
            if acct:
                return acct.qbo_id
    except Exception:
        pass

    logger.warning(f"Could not resolve account ID for '{account_name}' (realm {realm_id})")
    return None


def salvage_value(schedule) -> float:
    """The schedule's salvage floor (Phase-B AC-1, plan
    20260702-phase-b-fa-lifecycle). Non-zero only for salvage-applicable
    registry types (FIXED_ASSET / INTANGIBLE — DTO-enforced) and only
    meaningful for a positive-original schedule: a negative credit-adjustment
    schedule has no residual value, so salvage is treated as 0 there and the
    sign-aware mirror math is untouched. Every pre-Phase-B row carries the
    column default 0 → byte-identical behavior (golden-locked)."""
    if float(getattr(schedule, "original_amount", 0) or 0) <= 0:
        return 0.0
    return round(float(getattr(schedule, "salvage_value", 0) or 0), 2)


def compute_monthly_amount(schedule) -> float:
    """
    Compute the monthly depr/amort amount for a schedule.
    Uses the same formula as the schedule grid:
        (original_amount − salvage_value) / useful_life_months
    (the depreciable BASE excludes salvage — Phase-B AC-1; salvage is 0 for
    every non-asset type and every pre-Phase-B row, reducing to the legacy
    original / life).
    Returns a SIGNED value — negative means the schedule has a negative
    original amount (e.g. a credit adjustment) and should generate a
    credit to the expense account instead of a debit.
    """
    original = float(schedule.original_amount or 0)
    depreciable_base = round(original - salvage_value(schedule), 2)
    life_months = schedule.useful_life_months or 12

    if life_months <= 0 or depreciable_base == 0:
        return 0.0

    monthly = round(depreciable_base / life_months, 2)
    return monthly


def _schedule_end_month(schedule) -> date:
    """First-of-month date of the schedule's final in-range period."""
    from dateutil.relativedelta import relativedelta

    start = date(schedule.start_date.year, schedule.start_date.month, 1)
    if schedule.end_date:
        return date(schedule.end_date.year, schedule.end_date.month, 1)
    if schedule.useful_life_months:
        return start + relativedelta(months=schedule.useful_life_months - 1)
    return start + relativedelta(months=11)  # Default 12 months


def period_in_schedule_range(schedule, period: str) -> bool:
    """
    Check if a YYYY-MM period falls within a schedule's active date range.
    Uses start_date through end_date (or start_date + useful_life_months - 1).
    """
    period_date = date(int(period[:4]), int(period[5:7]), 1)
    start = date(schedule.start_date.year, schedule.start_date.month, 1)
    return start <= period_date <= _schedule_end_month(schedule)


def is_final_schedule_period(schedule, period: str) -> bool:
    """True when YYYY-MM `period` is the schedule's last in-range month."""
    return date(int(period[:4]), int(period[5:7]), 1) == _schedule_end_month(schedule)


def current_period(when: date | None = None) -> str:
    """The current accounting period (``YYYY-MM``). Injectable for tests."""
    return (when or date.today()).strftime("%Y-%m")


def resolve_written_status(
    period: str, *, requested_status: str = "POSTED", when: date | None = None
) -> str:
    """Clamp a to-be-written ScheduleEntry status so a FUTURE period is never
    marked POSTED (B2-5b).

    A POSTED ScheduleEntry asserts the amortization for that month has already
    been recognized. A period AFTER the current accounting period has NOT — so
    an import / grid-save / assistant path that writes ``status="POSTED"`` for a
    whole amortization table (start → end) silently posts the entire remaining
    LIFE of the schedule (the Collective RCM intangible: 180 POSTED rows through
    2034, ~93 of them future). That collapses ``remaining`` to one month and
    reads 99% done years early.

    The rule: a POSTED write for a period ``> current_period`` is downgraded to
    ``DRAFT`` (unposted — it still populates the amortization table and the grid
    projection, it just isn't asserted-as-recognized). A period ``<=`` the
    current period keeps the requested status (genuine catch-up / historical
    recognition on a pre-Peregrine schedule is legitimate). A non-POSTED
    requested status is returned unchanged — this only ever DOWNGRADES a POSTED
    write, never upgrades anything.
    """
    if requested_status != "POSTED":
        return requested_status
    return "POSTED" if period[:7] <= current_period(when) else "DRAFT"


def generate_month_entries(
    db: Session,
    realm_id: str,
    period: str,  # "YYYY-MM" format
    *,
    skip_manual_policy: bool = False,
    human_preparer_user_id: int | None = None,
) -> Dict[str, Any]:
    """
    Main engine: scan all ACTIVE schedules for realm_id and generate
    ONE consolidated ProposedAction (journal entry) per schedule_type
    for the given period.

    Each schedule row becomes a debit line (with vendor_name).
    A single credit line hits the mapped BS account per schedule type.

    skip_manual_policy (Wave 2 AC-5): when True — the beat-task path —
    schedules with automation_policy == "manual" are excluded entirely and
    surfaced in skip_reasons (reason "manual_policy"). The human-triggered
    grid path keeps the default False: an explicit click generates for
    everything, exactly as before.

    Returns: { generated: int, skipped: int, errors: [...], entries: [...] }
    """
    from ..models import AccountingSchedule, ScheduleEntry, ProposedAction, ScheduleCreditMapping, QBOAccount
    from collections import defaultdict

    # PLAN-EXEMPT: B5-8 — never generate schedule JE entries INTO a locked (or
    # cascade-locked, lock-as-of) period; fail-closed 409 before any read or write.
    from .task_autonomy import assert_period_writable
    assert_period_writable(db, realm_id=realm_id, period=period)

    # Get all active schedules for this company
    schedules = db.query(AccountingSchedule).filter(
        AccountingSchedule.realm_id == realm_id,
        AccountingSchedule.status.in_(["ACTIVE", "FULLY_AMORTIZED"]),
    ).all()

    # AC-5: the beat path skips manual-policy schedules — surfaced, not silent.
    manual_skip_reasons: List[Dict[str, Any]] = []
    if skip_manual_policy:
        automated, manual = [], []
        for s in schedules:
            policy = getattr(s, "automation_policy", None) or "auto_propose"
            (manual if policy == "manual" else automated).append(s)
        schedules = automated
        manual_skip_reasons = [
            {
                "schedule_id": s.id,
                "name": s.name,
                "reason": "manual_policy",
                "detail": "automation_policy=manual — scheduled generation skips this schedule",
            }
            for s in manual
        ]

    # Bulk memory loading for resolving account IDs instantly
    all_accounts = db.query(QBOAccount).filter(QBOAccount.realm_id == realm_id).all()
    cached_maps = {
        "name": {a.name: a.qbo_id for a in all_accounts},
        "num": {a.acct_num: a.qbo_id for a in all_accounts if a.acct_num},
        "fqn": {a.fully_qualified_name: a.qbo_id for a in all_accounts if a.fully_qualified_name},
    }

    # Bulk fetch all relevant entries tied to these schedules
    sched_ids = [s.id for s in schedules]
    entries = db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id.in_(sched_ids),
        ScheduleEntry.period == period,
    ).all()
    entries_map = {e.schedule_id: e for e in entries}

    # Bulk fetch each schedule's already-committed amortization OUTSIDE this
    # period (POSTED/PENDING_APPROVAL/DRAFT — everything except SKIPPED).
    # This is the over-amortization guard's input: generation must never push
    # a schedule past its original_amount, no matter what mix of catch-up
    # imports, manual grid entries, and straight-line months came before.
    from sqlalchemy import func as sa_func

    amortized_sums = dict(
        db.query(ScheduleEntry.schedule_id, sa_func.sum(ScheduleEntry.amount))
        .filter(
            ScheduleEntry.schedule_id.in_(sched_ids),
            ScheduleEntry.status != "SKIPPED",
            ScheduleEntry.period != period,
        )
        .group_by(ScheduleEntry.schedule_id)
        .all()
    )

    def _remaining_amortizable(schedule) -> float:
        # Salvage-aware (Phase-B AC-1): the amortizable pool is the depreciable
        # BASE (original − salvage) — the clamp, corrective, and final-plug
        # semantics below are all expressed against this remaining, so they
        # stop/plug at the SALVAGE FLOOR instead of zero. salvage_value() is 0
        # for every pre-Phase-B row → unchanged legacy behavior.
        return round(
            float(schedule.original_amount or 0)
            - salvage_value(schedule)
            - float(schedule.accumulated_amount or 0)
            - float(amortized_sums.get(schedule.id) or 0),
            2,
        )

    # Bulk fetch all relevant linked proposals
    prop_ids = [e.proposed_action_id for e in entries if e.proposed_action_id]
    proposals = db.query(ProposedAction).filter(ProposedAction.id.in_(prop_ids)).all() if prop_ids else []
    proposals_map = {p.id: p for p in proposals}

    # Load credit mappings for this realm (AC-3). A row with
    # source_account_id NULL is the TYPE-LEVEL default — the pre-AC-3 shape,
    # and what every legacy row still is. A row with source_account_id set is
    # an ACCOUNT-LEVEL override that applies only to schedules whose BS
    # account (AccountingSchedule.qbo_account_id) matches it. Resolution
    # order per schedule: account-level → type-level → the group fallback
    # below (the first schedule's own BS account — unchanged).
    type_default_mappings: Dict[str, Dict[str, Any]] = {}
    account_level_mappings: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for m in (
        db.query(ScheduleCreditMapping)
        .filter(ScheduleCreditMapping.realm_id == realm_id)
        .order_by(ScheduleCreditMapping.id)
        .all()
    ):
        mapping_payload = {
            "mapping_id": m.id,
            "account_id": m.qbo_account_id,
            "account_name": m.qbo_account_name,
        }
        if m.source_account_id:
            account_level_mappings.setdefault(
                (m.schedule_type, str(m.source_account_id)), mapping_payload
            )
        else:
            type_default_mappings.setdefault(m.schedule_type, mapping_payload)

    def _resolve_credit_mapping(stype: str, schedule) -> Optional[Dict[str, Any]]:
        if schedule.qbo_account_id:
            hit = account_level_mappings.get((stype, str(schedule.qbo_account_id)))
            if hit:
                return hit
        return type_default_mappings.get(stype)

    skipped = len(manual_skip_reasons)
    errors = []
    entries_created = []
    skip_reasons = list(manual_skip_reasons)   # diagnostic: why each schedule was skipped

    # Phase 1: Collect all eligible debit lines, grouped by schedule_type
    type_groups = defaultdict(list)

    for schedule in schedules:
        try:
            # Check map (O(1)) instead of DB query
            existing = entries_map.get(schedule.id)

            if existing:
                if existing.proposed_action_id:
                    # Check map (O(1)) instead of DB query
                    linked_proposal = proposals_map.get(existing.proposed_action_id)
                    if linked_proposal and linked_proposal.status in ("PENDING", "APPROVED"):
                        skipped += 1
                        skip_reasons.append({
                            "schedule_id": schedule.id,
                            "name": schedule.name,
                            "reason": "existing_entry",
                            "detail": f"ScheduleEntry id={existing.id} already exists for period {period} (proposal {linked_proposal.id} status={linked_proposal.status})",
                        })
                        continue
                    else:
                        # Linked proposal was rejected or deleted — remove stale entry
                        db.delete(existing)
                        db.flush()
                        # Fall through to normal generation below
                else:
                    # Manual entry (no proposal link) — INCLUDE it in the JE
                    # Use the manual amount instead of the computed straight-line amount
                    manual_amount = float(existing.amount or 0)
                    if manual_amount == 0:
                        skipped += 1
                        skip_reasons.append({
                            "schedule_id": schedule.id,
                            "name": schedule.name,
                            "reason": "manual_zero",
                            "detail": f"Manual ScheduleEntry id={existing.id} has amount=0",
                        })
                        continue

                    # Over-amortization guard for manual entries: never post
                    # more than the schedule has left. SIGN-AWARE mirror: for a
                    # negative (credit-adjustment) schedule the amortization
                    # direction is negative, so a manual NEGATIVE amount is the
                    # one that must never step past zero. A manual amount in
                    # the OPPOSITE direction is a deliberate user correction
                    # and is left un-clamped. The capped amount is written
                    # back to the entry so the ledger matches the JE.
                    direction = 1.0 if float(schedule.original_amount or 0) >= 0 else -1.0
                    if manual_amount * direction > 0:
                        remaining = _remaining_amortizable(schedule)
                        if remaining * direction <= 0.005:
                            skipped += 1
                            skip_reasons.append({
                                "schedule_id": schedule.id,
                                "name": schedule.name,
                                "reason": "fully_amortized",
                                "detail": (
                                    f"Manual ScheduleEntry id={existing.id} skipped: "
                                    f"remaining={remaining} — schedule is fully amortized"
                                ),
                            })
                            continue
                        if abs(manual_amount) > abs(remaining) + 0.005:
                            skip_reasons.append({
                                "schedule_id": schedule.id,
                                "name": schedule.name,
                                "reason": "manual_amount_capped",
                                "detail": (
                                    f"Manual amount {manual_amount} capped to remaining "
                                    f"{remaining} for entry id={existing.id}"
                                ),
                            })
                            manual_amount = remaining
                            existing.amount = remaining

                    # Resolve expense account for this manual entry
                    expense_acct_id = _resolve_account_id(
                        db, realm_id,
                        schedule.expense_account_name,
                        schedule.expense_account_id,
                        cached_maps,
                    )
                    if not expense_acct_id:
                        errors.append({
                            "schedule_id": schedule.id,
                            "name": schedule.name,
                            "reason": f"Missing expense account ID. Name: '{schedule.expense_account_name}'",
                        })
                        skipped += 1
                        continue

                    stype = schedule.schedule_type or DEFAULT_SCHEDULE_TYPE
                    credit_mapping = _resolve_credit_mapping(stype, schedule)
                    type_groups[
                        (stype, credit_mapping["mapping_id"] if credit_mapping else None)
                    ].append({
                        "schedule": schedule,
                        "monthly": manual_amount,
                        "expense_acct_id": expense_acct_id,
                        "credit_mapping": credit_mapping,
                        "existing_entry": existing,  # Track so we can link it later
                    })
                    continue

            # Skip if the period is outside this schedule's active date range —
            # EXCEPT a schedule PAST its window that still carries an unamortized
            # balance (B5-7): without a true-up the leftover lingers on the BS
            # forever (schedule=$X vs QBO=$Y delta). Post the entire remaining as a
            # one-time CATCH-UP in this (current) period so the prepaid/asset clears.
            # SIGN-AWARE: remaining is posted SIGNED — a negative remaining (an
            # over-amortized positive schedule, or an unfinished negative
            # credit-adjustment schedule) emits a corrective NEGATIVE catch-up,
            # which the signed-line builder below flips to credit-expense /
            # debit-BS (mirrors the over-amortization guard further down).
            if not period_in_schedule_range(schedule, period):
                # PLAN-EXEMPT: B5-7 P1 field bug (live-prod-corroborated 2026-06-24).
                period_date = date(int(period[:4]), int(period[5:7]), 1)
                remaining = _remaining_amortizable(schedule)
                if period_date > _schedule_end_month(schedule) and abs(remaining) > 0.005:
                    expense_acct_id = _resolve_account_id(
                        db, realm_id,
                        schedule.expense_account_name,
                        schedule.expense_account_id,
                        cached_maps,
                    )
                    if not expense_acct_id:
                        errors.append({
                            "schedule_id": schedule.id,
                            "name": schedule.name,
                            "reason": f"Missing expense account ID. Name: '{schedule.expense_account_name}'",
                        })
                        skipped += 1
                        continue
                    stype = schedule.schedule_type or DEFAULT_SCHEDULE_TYPE
                    credit_mapping = _resolve_credit_mapping(stype, schedule)
                    type_groups[
                        (stype, credit_mapping["mapping_id"] if credit_mapping else None)
                    ].append({
                        "schedule": schedule,
                        # catch-up: post the whole lingering balance once,
                        # SIGNED (negative = corrective reversal past 100%)
                        "monthly": remaining,
                        "expense_acct_id": expense_acct_id,
                        "credit_mapping": credit_mapping,
                        "_trueup": True,
                        "corrective": remaining < 0
                        if float(schedule.original_amount or 0) > 0
                        else remaining > 0,
                    })
                    continue
                skipped += 1
                skip_reasons.append({
                    "schedule_id": schedule.id,
                    "name": schedule.name,
                    "reason": "out_of_range",
                    "detail": f"Period {period} not in range {schedule.start_date} to {schedule.end_date or 'computed'}",
                })
                continue

            # Compute monthly amount (may be negative for credit adjustments)
            monthly = compute_monthly_amount(schedule)
            if monthly == 0:
                skipped += 1
                skip_reasons.append({
                    "schedule_id": schedule.id,
                    "name": schedule.name,
                    "reason": "no_monthly",
                    "detail": f"monthly={monthly}, original={schedule.original_amount}, life_months={schedule.useful_life_months}",
                })
                continue

            # Over-amortization guard — SIGN-AWARE (the mirror applies to
            # negative credit-adjustment schedules too, and to schedules that
            # are ALREADY past 100%):
            # - |remaining| within a rounding cent → generate nothing
            # - remaining has the OPPOSITE sign of the monthly direction (the
            #   schedule is already over-amortized — e.g. Definitive
            #   Healthcare's −1,132.29 — or an over-reversed credit
            #   adjustment) → emit ONE corrective month equal to remaining:
            #   a signed reversal that brings the schedule to exactly 0. The
            #   signed-line builder below flips the JE (credit expense /
            #   debit BS for a negative amount). Never a skip, never an
            #   unclamped repeat of ±monthly.
            # - mid-schedule → cap the MAGNITUDE at what's left (catch-up
            #   imports or manual months may have outpaced straight-line)
            # - final in-range period → plug to EXACTLY what's left, so the
            #   series sums to original_amount (kills the cent-rounding
            #   overshoot/undershoot from round(original/life, 2))
            corrective = False
            remaining = _remaining_amortizable(schedule)
            if abs(remaining) <= 0.005:
                skipped += 1
                skip_reasons.append({
                    "schedule_id": schedule.id,
                    "name": schedule.name,
                    "reason": "fully_amortized",
                    "detail": (
                        f"remaining={remaining} — nothing left to amortize "
                        f"(original={schedule.original_amount})"
                    ),
                })
                continue
            if (monthly > 0) != (remaining > 0):
                # One-time corrective reversal: the schedule is already past
                # zero in the amortization direction; plug back to exactly 0.
                monthly = remaining
                corrective = True
            elif is_final_schedule_period(schedule, period):
                monthly = remaining
            else:
                # Same-sign cap: never step past zero in either direction.
                monthly = min(monthly, remaining) if monthly > 0 else max(monthly, remaining)

            # Resolve expense account ID
            expense_acct_id = _resolve_account_id(
                db, realm_id,
                schedule.expense_account_name,
                schedule.expense_account_id,
                cached_maps,
            )

            if not expense_acct_id:
                errors.append({
                    "schedule_id": schedule.id,
                    "name": schedule.name,
                    "reason": f"Missing expense account ID. Name: '{schedule.expense_account_name}'",
                })
                skipped += 1
                continue

            stype = schedule.schedule_type or DEFAULT_SCHEDULE_TYPE
            credit_mapping = _resolve_credit_mapping(stype, schedule)
            type_groups[
                (stype, credit_mapping["mapping_id"] if credit_mapping else None)
            ].append({
                "schedule": schedule,
                "monthly": monthly,
                "expense_acct_id": expense_acct_id,
                "credit_mapping": credit_mapping,
                "corrective": corrective,
            })

        except Exception as e:
            logger.error(
                "Error processing schedule %s: %s",
                schedule.id,
                redact_to_string(str(e), max_length=500),
            )
            errors.append({
                "schedule_id": schedule.id,
                "name": getattr(schedule, 'name', '?'),
                "reason": "schedule_processing_failed",
                "error": safe_error_detail(
                    error_code="schedule_processing_failed",
                    user_message="The schedule could not be processed for this period.",
                    exc=e,
                    retryable=False,
                    log_context="schedule_processing_failed",
                ),
            })

    # Phase 2: Build one consolidated JE per (schedule_type, resolved credit
    # account) group. With only type-level mappings (every legacy realm) this
    # degenerates to exactly one group per type — the pre-AC-3 behavior.
    from .schedule_types import SCHEDULE_TYPES

    generated = 0
    for (stype, _credit_key), items in type_groups.items():
        if not items:
            continue

        descriptor = SCHEDULE_TYPES.get(stype)
        desc = descriptor.je_description if descriptor else "Schedule entry"
        # credit_pl types (e.g. DEFERRED_REVENUE) recognize INTO the P&L: a
        # positive month CREDITS the P&L account and DEBITS the BS account.
        is_credit_pl = bool(
            descriptor and descriptor.amortization_direction == DIRECTION_CREDIT_PL
        )
        memo = f"Consolidated {stype.lower()} amortization — {period}"

        # Determine credit account from the per-group resolved mapping
        # (account-level → type-level) or fall back to first item's BS account
        credit_mapping = items[0]["credit_mapping"]
        if credit_mapping:
            credit_acct_id = credit_mapping["account_id"]
            credit_acct_name = credit_mapping.get("account_name", "")
        else:
            # Fallback: use the BS account from the first schedule row
            first_sched = items[0]["schedule"]
            credit_acct_id = _resolve_account_id(
                db, realm_id,
                first_sched.qbo_account_name,
                first_sched.qbo_account_id,
                cached_maps,
            )
            credit_acct_name = first_sched.qbo_account_name or ""

        if not credit_acct_id:
            for item in items:
                errors.append({
                    "schedule_id": item["schedule"].id,
                    "name": item["schedule"].name,
                    "reason": f"No credit account mapping found for schedule type '{stype}'",
                })
            continue

        # Build debit lines (one per schedule row, with vendor_name)
        debit_lines = []
        total_debit = 0.0
        total_credit_from_neg = 0.0
        for item in items:
            sched = item["schedule"]
            monthly = item["monthly"]

            # Resolve vendor name: use the DB field, or extract from schedule name
            vendor_name = sched.vendor_name or ""
            if not vendor_name or vendor_name.strip().lower() in ("", "vendor"):
                # Try to extract from schedule name (e.g. "Prepaid Insurance – Travelers 2026")
                sname = sched.name or ""
                for sep in [" – ", " - ", ": "]:
                    if sep in sname:
                        vendor_name = sname.split(sep, 1)[-1].strip()
                        break
                else:
                    vendor_name = sname  # Use the full name as fallback

            # Which side does the P&L line take? For debit_pl types a positive
            # month DEBITS the P&L (classic expense recognition); for
            # credit_pl types a positive month CREDITS the P&L (revenue
            # recognition / liability paydown). Negative (corrective) amounts
            # flip whichever side the direction selects. For the four legacy
            # debit_pl types this is byte-identical to the pre-registry code
            # (golden-locked).
            pl_line_is_credit = (monthly > 0) if is_credit_pl else (monthly < 0)
            if monthly > 0:
                description = f"{desc}: {sched.name}"
            else:
                description = (
                    f"{desc} ({'credit' if pl_line_is_credit else 'debit'}): {sched.name}"
                )

            abs_amount = abs(monthly)
            if not pl_line_is_credit:
                debit_lines.append({
                    "account_id": item["expense_acct_id"],
                    "account_name": sched.expense_account_name or "",
                    "vendor_name": vendor_name,
                    "debit": round(abs_amount, 2),
                    "credit": 0,
                    "description": description,
                })
                total_debit += abs_amount
            else:
                debit_lines.append({
                    "account_id": item["expense_acct_id"],
                    "account_name": sched.expense_account_name or "",
                    "vendor_name": vendor_name,
                    "debit": 0,
                    "credit": round(abs_amount, 2),
                    "description": description,
                })
                total_credit_from_neg += abs_amount

        # Net credit = total_debit - total_credit_from_neg
        net_credit = round(total_debit - total_credit_from_neg, 2)

        # Single credit line for the BS account (net amount)
        if net_credit > 0:
            credit_line = {
                "account_id": credit_acct_id,
                "account_name": credit_acct_name,
                "vendor_name": "",
                "debit": 0,
                "credit": round(net_credit, 2),
                "description": f"Consolidated {stype.lower()} credit — {period}",
            }
        else:
            # Net is zero or negative — debit the BS account
            credit_line = {
                "account_id": credit_acct_id,
                "account_name": credit_acct_name,
                "vendor_name": "",
                "debit": round(abs(net_credit), 2),
                "credit": 0,
                "description": f"Consolidated {stype.lower()} debit (net) — {period}",
            }

        je_payload = {
            "date": f"{period}-01",
            "memo": memo,
            "lines": debit_lines + [credit_line],
        }

        # Create one ProposedAction for the consolidated JE
        schedule_names = ", ".join(item["schedule"].name or "?" for item in items)
        proposal = ProposedAction(
            **human_provenance(human_preparer_user_id),
            company_id=realm_id,
            action_type="JOURNAL_ENTRY",
            payload=je_payload,
            reasoning=(
                f"Consolidated {stype} amortization for {period}. "
                f"{len(items)} items: {schedule_names}. "
                f"Total: ${total_debit:,.2f}."
            ),
            confidence_score=0.99,
            status="PENDING",
        )
        db.add(proposal)
        db.flush()  # Get proposal.id

        # Create ScheduleEntry for each schedule row, all linked to same proposal
        for item in items:
            if item.get("existing_entry"):
                # Manual entry already exists — DO NOT link it to the proposal.
                # The JE payload already includes this amount from the type_groups.
                # Leaving it unlinked means reject/cleanup won't delete it.
                pass
            else:
                # New entry — create it linked to the proposal
                entry = ScheduleEntry(
                    schedule_id=item["schedule"].id,
                    realm_id=realm_id,
                    period=period,
                    amount=item["monthly"],
                    proposed_action_id=proposal.id,
                    status="PENDING_APPROVAL",
                )
                db.add(entry)

            generated += 1
            entries_created.append({
                "schedule_id": item["schedule"].id,
                "schedule_name": item["schedule"].name,
                "schedule_type": stype,
                "vendor_name": item["schedule"].vendor_name or "",
                "amount": item["monthly"],
                "proposal_id": proposal.id,
                "expense_account": item["schedule"].expense_account_name,
                # True for a one-time reversal that plugs an over-amortized
                # schedule back to exactly zero (negative amount → the JE
                # credits the expense account and debits the BS account).
                "corrective": bool(item.get("corrective")),
                # AC-5 auto-post eligibility inputs: a manual grid override
                # folded into the consolidated JE, and a B5-7 past-window
                # catch-up true-up. Either disqualifies the WHOLE batch from
                # auto_post_deterministic (only plain unmodified straight-line
                # periods may auto-post).
                "manual": bool(item.get("existing_entry")),
                "trueup": bool(item.get("_trueup")),
            })

    try:
        db.commit()
    except IntegrityError:
        # A concurrent run inserted a ScheduleEntry for the same
        # (schedule_id, period) between our bulk fetch and this commit.
        # Roll back the whole batch (entries + proposals) — the winning run
        # already produced the consolidated proposal for this period.
        db.rollback()
        logger.warning(
            f"[ScheduleEngine] Concurrent generate_month_entries run detected for "
            f"{realm_id}/{period} — rolled back duplicate batch"
        )
        return {
            "period": period,
            "generated": 0,
            "skipped": skipped,
            "errors": errors,
            "entries": [],
            "skip_reasons": [{"reason": "concurrent_run"}],
        }

    return {
        "period": period,
        "generated": generated,
        "skipped": skipped,
        "errors": errors,
        "entries": entries_created,
        "skip_reasons": skip_reasons,
    }


def _next_period(period: str) -> str:
    """The YYYY-MM month after ``period`` (which may carry a suffix)."""
    year, month = int(period[:4]), int(period[5:7])
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def _maybe_draft_auto_reversal(db: Session, entry, schedule) -> Optional[int]:
    """
    Wave 2 AC-6: when a REVERSAL-FLAVORED entry posts, auto-DRAFT the period+1
    opposite-sign reversal entry (linked via reversal_of_entry_id) and its JE
    proposal. The reversal is ALWAYS proposal-gated — a human approves it like
    any other JE; it is never auto-posted.

    Guards (each refuses, loudly logged, never raises):
      * type's registry reversal_flavor != "auto_reverse" → not a reversing type
      * entry.reversal_of_entry_id set → the posted row IS a reversal — never
        chain reversal-of-reversal
      * suffixed period (e.g. "YYYY-MM-REV") → a corrective/manual correction
        row, not a periodic accrual — reversing it would undo the correction
      * amount opposing the schedule's direction → an engine corrective
      * a (schedule, next-period "-REV") row already exists → idempotent skip
        (at-least-once Celery delivery / a re-approved proposal must not stack
        a second reversal)

    The reversal is keyed "YYYY-MM-REV" — NEVER the plain next period — so the
    next month's normal generation still finds its slot free, and the grid's
    existing "-REV" fold renders it netted into the next month's cell.

    Returns the created reversal ScheduleEntry id, or None.
    """
    from ..models import ScheduleEntry
    from .schedule_types import REVERSAL_AUTO_REVERSE, SCHEDULE_TYPES

    descriptor = SCHEDULE_TYPES.get(schedule.schedule_type or "")
    if not descriptor or descriptor.reversal_flavor != REVERSAL_AUTO_REVERSE:
        return None
    if entry.reversal_of_entry_id is not None:
        return None  # chain guard: a reversal never spawns another
    if len(entry.period or "") != 7:
        return None  # "-REV"/suffixed rows are corrections, not periodic accruals

    entry_amount = float(entry.amount or 0)
    direction = 1.0 if float(schedule.original_amount or 0) >= 0 else -1.0
    if entry_amount * direction <= 0:
        return None  # corrective (or zero) — reversing it would undo the fix

    reversal_period = f"{_next_period(entry.period)}-REV"
    reversal = ScheduleEntry(
        schedule_id=schedule.id,
        realm_id=schedule.realm_id,
        period=reversal_period,
        amount=-entry_amount,
        status="DRAFT",
        reversal_of_entry_id=entry.id,
    )
    try:
        with db.begin_nested():
            db.add(reversal)
            db.flush()
    except IntegrityError:
        # (schedule, period) already taken — a prior run's reversal or a
        # pending manual "-REV" correction. Skip idempotently, but LOUDLY.
        logger.warning(
            "[ScheduleEngine] Auto-reversal for schedule %s (%s) skipped: an entry "
            "already occupies period %s (prior reversal or pending manual correction)",
            schedule.id,
            schedule.name,
            reversal_period,
        )
        return None

    from .accounting import je_builder

    proposal = je_builder.build_je_for_entry(reversal, schedule, db, submitted_by="system")
    logger.info(
        "[ScheduleEngine] Auto-drafted reversing entry %s (period %s, amount %s) for "
        "posted accrual entry %s on schedule %s%s",
        reversal.id,
        reversal_period,
        -entry_amount,
        entry.id,
        schedule.id,
        f" — proposal {proposal.id} PENDING" if proposal else " — proposal NOT created",
    )
    return reversal.id


def mark_entry_posted(db: Session, proposal_id: int) -> bool:
    """
    After a proposal is approved and executed, mark ALL linked
    ScheduleEntry records as POSTED and update each schedule's remaining_amount.
    Called from the proposals approval flow.

    Handles negative amounts correctly: a negative entry (credit adjustment)
    increases remaining_amount rather than decreasing it.

    Wave 2 AC-6: posting a reversal-flavored (ACCRUAL) periodic entry also
    auto-DRAFTs the period+1 reversal + its proposal (see
    _maybe_draft_auto_reversal — always propose, never auto-post reversals).
    """
    from ..models import ScheduleEntry, AccountingSchedule

    entries = db.query(ScheduleEntry).filter(
        ScheduleEntry.proposed_action_id == proposal_id,
    ).all()

    if not entries:
        return False

    for entry in entries:
        if entry.status == "POSTED":
            # Already recognized (a re-run of the hook): the header already
            # carries this amount on the posted basis — never subtract twice.
            continue
        entry.status = "POSTED"
        entry.posted_at = date.today()

        # Update schedule remaining amount
        schedule = db.query(AccountingSchedule).filter(
            AccountingSchedule.id == entry.schedule_id,
        ).first()

        if schedule:
            entry_amount = float(entry.amount or 0)
            current_remaining = float(schedule.remaining_amount or 0)
            # Subtract the entry amount (works for both positive and negative):
            # Positive entry → remaining decreases (normal amortization)
            # Negative entry → remaining increases (credit adjustment)
            # No clamping: a negative remaining means the schedule is
            # over-amortized and MUST surface honestly (the grid computes the
            # same unclamped figure; clamping here hid real over-postings).
            new_remaining = round(current_remaining - entry_amount, 2)
            schedule.remaining_amount = new_remaining

            # Over-amortized means remaining crossed the schedule's FLOOR
            # AGAINST its direction. The floor is the salvage value for a
            # normal (positive-original) schedule — remaining_amount includes
            # salvage, so a healthy asset finishes AT salvage, not zero
            # (Phase-B AC-1; salvage is 0 pre-Phase-B → the legacy zero
            # floor) — and zero for a negative credit-adjustment schedule
            # (whose remaining lives at <= 0 for its whole healthy life).
            original = float(schedule.original_amount or 0)
            floor = salvage_value(schedule)
            over_amortized = (
                new_remaining < floor - 0.005 if original >= 0 else new_remaining > 0.005
            )
            if over_amortized:
                logger.warning(
                    "Schedule %s (%s) is OVER-AMORTIZED: remaining=%s (floor=%s) after posting %s",
                    schedule.id,
                    schedule.name,
                    new_remaining,
                    floor,
                    entry_amount,
                )
            if new_remaining - floor <= 0.005 and entry_amount > 0:
                schedule.status = "FULLY_AMORTIZED"
                logger.info(f"Schedule {schedule.id} ({schedule.name}) marked FULLY_AMORTIZED")
            elif abs(new_remaining - floor) <= 0.005 and entry_amount < 0:
                # A corrective reversal (or the final month of a negative
                # credit-adjustment schedule) landed the balance at exactly
                # zero — the schedule is complete.
                schedule.status = "FULLY_AMORTIZED"
                logger.info(
                    f"Schedule {schedule.id} ({schedule.name}) marked FULLY_AMORTIZED "
                    f"(corrective/negative entry brought remaining to {new_remaining})"
                )

            # AC-6: auto-reversing flavor — draft the period+1 reversal
            # (proposal-gated; internally guarded against chains/correctives).
            _maybe_draft_auto_reversal(db, entry, schedule)

    db.commit()
    return True


class RemediationDetail(TypedDict):
    """One schedule touched by ``remediate_future_posted_entries``."""

    schedule_id: int
    schedule_name: str | None
    future_posted_reset: int
    remaining_before: float
    remaining_after: float
    status: str | None


class RemediationResult(TypedDict):
    """Summary of a ``remediate_future_posted_entries`` run (B2-5b)."""

    realm_id: str
    current_period: str
    dry_run: bool
    schedules_affected: int
    entries_reset: int
    details: list[RemediationDetail]


# PLAN-EXEMPT: B2-5b remediation fn on an OWNED, RATIFIED path
# (app/services/schedule_engine.py IS in plan 20260702-qa-batch2 plan.md
# surface_scope); the active-plan.json state file shipped with an empty
# surface_scope[] (state-drift bug) so the plan-gate cannot see the coverage.
# Auditable one-off; the ratified plan authorizes this exact file.
def remediate_future_posted_entries(
    db: Session,
    realm_id: str,
    *,
    when: date | None = None,
    dry_run: bool = False,
) -> RemediationResult:
    """B2-5b remediation: downgrade every FUTURE-dated POSTED ScheduleEntry
    (period > the current period) to DRAFT, then recompute each affected
    schedule's ``remaining_amount``.

    This unwinds the damage the pre-clamp import/grid/assistant paths did — a
    whole amortization table written straight to POSTED, so a schedule read
    99%-done years early (Collective RCM: 180 POSTED rows through 2034). A
    DRAFT entry still populates the grid projection; it just stops asserting
    the future month has been recognized, so ``remaining_amount`` re-inflates
    to the real un-recognized balance.

    SAFE + NARROW by construction:
      * only entries with ``proposed_action_id IS NULL`` are touched — a
        POSTED entry linked to an executed proposal reflects a real QBO GL
        write and is NEVER silently reversed here (the prod forensics confirm
        the over-posted rows are all NULL-proposal, i.e. internal-ledger only).
      * only ``period > current_period`` — historical/current POSTED entries
        (genuine recognition) are left exactly as they are.
      * ``"-REV"``/suffixed correction periods are excluded (they are not
        forward amortization months).
      * IDEMPOTENT: a second run finds nothing to downgrade (the first run
        already moved them to DRAFT) → ``entries_reset=0``.

    Tenant-scoped on ``realm_id`` (the app-layer predicate; RLS is the
    backstop). ``dry_run=True`` reports what WOULD change without writing.
    Returns a per-schedule summary the caller surfaces in the API response.
    """
    from collections import Counter

    from sqlalchemy import func as sa_func

    from ..models import AccountingSchedule, ScheduleEntry

    cur = current_period(when)

    # Future-dated, unposted-to-QBO (NULL proposal), non-correction POSTED rows.
    future_posted = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.realm_id == realm_id,
            ScheduleEntry.status == "POSTED",
            ScheduleEntry.proposed_action_id.is_(None),
            ScheduleEntry.period > cur,
            ~ScheduleEntry.period.like("%-REV"),
        )
        .all()
    )

    # In-memory tally (Counter — a plain dict, NOT a DB round-trip per row).
    by_schedule: "Counter[int]" = Counter(e.schedule_id for e in future_posted)
    if not dry_run:
        for e in future_posted:
            e.status = "DRAFT"
        if future_posted:
            db.flush()

    affected_ids = sorted(by_schedule)

    # Batch-load every affected schedule in ONE query (iterated in-memory below
    # — no per-row DB lookup in any loop).
    schedules = (
        db.query(AccountingSchedule)
        .filter(
            AccountingSchedule.id.in_(affected_ids),
            AccountingSchedule.realm_id == realm_id,
        )
        .all()
        if affected_ids
        else []
    )

    # Batch the post-flush POSTED sums in ONE grouped query (the books' truth
    # AFTER the downgrade): remaining = original − accumulated − Σ(POSTED)
    # (the posted_basis_remaining rule), unclamped so an over-amortized
    # schedule still surfaces honestly.
    posted_sums: dict[int, float] = {}
    if not dry_run and affected_ids:
        posted_sums = {
            sid: float(total or 0)
            for sid, total in db.query(
                ScheduleEntry.schedule_id, sa_func.sum(ScheduleEntry.amount)
            )
            .filter(
                ScheduleEntry.realm_id == realm_id,
                ScheduleEntry.schedule_id.in_(affected_ids),
                ScheduleEntry.status == "POSTED",
            )
            .group_by(ScheduleEntry.schedule_id)
            .all()
        }

    details: list[RemediationDetail] = []
    # Pure in-memory loop over the pre-loaded schedule rows.
    for schedule in schedules:
        old_remaining = round(float(schedule.remaining_amount or 0), 2)
        new_remaining = old_remaining
        if not dry_run:
            total_posted = posted_sums[schedule.id] if schedule.id in posted_sums else 0.0
            remaining = (
                Decimal(str(schedule.original_amount or 0))
                - Decimal(str(schedule.accumulated_amount or 0))
                - Decimal(str(total_posted))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            new_remaining = float(remaining)
            schedule.remaining_amount = new_remaining
            # A schedule flipped to FULLY_AMORTIZED off the phantom future
            # postings returns to ACTIVE now that real balance remains. Leave
            # DISPOSED / other terminal states alone.
            _reopen_if_balance_remains(schedule, remaining)

        details.append(
            {
                "schedule_id": schedule.id,
                "schedule_name": schedule.name,
                "future_posted_reset": by_schedule[schedule.id],
                "remaining_before": old_remaining,
                "remaining_after": new_remaining,
                "status": schedule.status,
            }
        )

    if not dry_run:
        db.commit()

    return {
        "realm_id": realm_id,
        "current_period": cur,
        "dry_run": dry_run,
        "schedules_affected": len(details),
        "entries_reset": len(future_posted),
        "details": details,
    }


# Threshold below which a schedule is considered over-amortized (a negative
# remaining balance beyond a rounding cent). Mirrors the grid's over_amortized
# flag (accounting_schedules.py:928) and mark_entry_posted's warning gate.
OVER_AMORTIZED_THRESHOLD = -0.005


class CorrectionItem(TypedDict):
    """One drafted over-amortization reversing-JE proposal."""

    schedule_id: int
    schedule_name: str
    schedule_type: str
    over_amortized_by: float
    period: str
    proposal_id: int
    bs_account: str | None
    expense_account: str | None


class SkippedItem(TypedDict):
    """A schedule whose correction could not be drafted, with the reason."""

    schedule_id: int
    schedule_name: str
    reason: str


class CorrectionResult(TypedDict):
    """Summary of a propose_over_amortization_corrections run."""

    realm_id: str
    corrections_proposed: int
    corrections: list[CorrectionItem]
    skipped: list[SkippedItem]


def propose_over_amortization_corrections(
    db: Session,
    realm_id: str,
    *,
    schedule_type: str | None = None,
    submitted_by: str = "system",
    human_preparer_user_id: int | None = None,
) -> CorrectionResult:
    """
    For every ACTIVE schedule whose remaining_amount crossed zero AGAINST its
    amortization direction (over-amortized: negative remaining on a positive
    schedule, positive remaining on a negative credit-adjustment schedule),
    draft a PROPOSAL-ONLY reversing journal entry that restores the balance to
    zero. Schedules with a corrective already pending (a DRAFT/PENDING_APPROVAL
    "-REV" manual correction or an engine-emitted corrective for the current
    period) are SKIPPED with reason "corrective_already_pending" — never a
    second correction stacked on the first.

    The correction for the canonical over-amortized-positive case is a single
    reversing entry:
        DEBIT  <schedule BS/asset account>   abs(remaining)
        CREDIT <expense account>             abs(remaining)
    (the sign-aware je_builder mirrors it for an over-reversed negative
    schedule: DEBIT expense / CREDIT BS).

    It is represented as a ScheduleEntry with amount == remaining_amount (the
    NEGATIVE value) so that on approval mark_entry_posted re-zeros the balance:
        new_remaining = current_remaining - entry_amount
                      = remaining        - remaining      = 0
    (no double-counting — the entry amount IS the residual to unwind).

    The reversing JE is built by je_builder.build_je_for_entry, which is
    sign-aware: a negative entry amount swaps debit/credit and uses abs(), so
    the emitted lines are balanced and correctly-signed. NOTHING is posted to
    QBO here — each proposal is PENDING and requires human approval.
    """
    from ..models import AccountingSchedule, ScheduleEntry
    from .accounting import je_builder

    q = db.query(AccountingSchedule).filter(
        AccountingSchedule.realm_id == realm_id,
        AccountingSchedule.status == "ACTIVE",
    )
    if schedule_type:
        q = q.filter(AccountingSchedule.schedule_type == schedule_type)

    schedules = q.all()

    # Only schedules that are actually over-amortized need a correction.
    # SIGN-AWARE (mirrors mark_entry_posted's over_amortized definition):
    # over-amortized means remaining crossed zero AGAINST the schedule's
    # amortization direction — below zero for a positive-original schedule,
    # ABOVE zero for a negative credit-adjustment schedule (whose remaining
    # lives at <= 0 for its whole healthy life; a naive `remaining < 0` filter
    # would have "corrected" every healthy negative schedule by doubling it).
    def _is_over_amortized(s) -> bool:
        remaining = round(float(s.remaining_amount or 0), 2)
        if float(s.original_amount or 0) >= 0:
            # Salvage-aware floor (Phase-B AC-1): a positive schedule is
            # over-amortized when remaining fell BELOW its salvage value
            # (salvage is 0 pre-Phase-B → the legacy below-zero check).
            return remaining - salvage_value(s) < OVER_AMORTIZED_THRESHOLD
        return remaining > -OVER_AMORTIZED_THRESHOLD

    over_amortized = [s for s in schedules if _is_over_amortized(s)]

    corrections: list[CorrectionItem] = []
    skipped: list[SkippedItem] = []

    if not over_amortized:
        return {
            "realm_id": realm_id,
            "corrections_proposed": 0,
            "corrections": [],
            "skipped": [],
        }

    # Batch-fetch, in ONE query (avoids an N+1 collision-check inside the
    # loop): every entry already occupying the current period for these
    # schedules, plus every still-unposted (DRAFT/PENDING_APPROVAL) entry in
    # ANY period — the double-correction guard's input.
    # Scoped to realm_id — the app-layer tenant predicate on ScheduleEntry.
    from sqlalchemy import or_ as sa_or

    base_period = date.today().strftime("%Y-%m")
    over_ids = [s.id for s in over_amortized]
    period_entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.realm_id == realm_id,
            ScheduleEntry.schedule_id.in_(over_ids),
            sa_or(
                ScheduleEntry.period == base_period,
                ScheduleEntry.status.in_(["DRAFT", "PENDING_APPROVAL"]),
            ),
        )
        .all()
    )
    taken_current_period = {
        e.schedule_id for e in period_entries if e.period == base_period
    }

    # Double-correction guard (AC-7b): a schedule with a corrective already
    # IN FLIGHT must not get a second one stacked on top. In flight means a
    # DRAFT / PENDING_APPROVAL entry (any period — remaining_amount does not
    # reflect unposted entries, so an old pending corrective still corrects
    # the full residual) that is either
    #   (a) a manual "-REV" correction from a prior run of this endpoint, or
    #   (b) an engine-emitted corrective — an entry whose sign OPPOSES the
    #       schedule's amortization direction (generate_month_entries'
    #       sign-aware clamp and B5-7 true-up emit exactly that).
    # POSTED correctives never block: mark_entry_posted already folded them
    # into remaining_amount, so a still-over-amortized schedule genuinely
    # needs a fresh correction. SKIPPED entries never block (human declined).
    originals = {s.id: float(s.original_amount or 0) for s in over_amortized}
    pending_corrective_ids: set[int] = set()
    for e in period_entries:
        if e.status not in ("DRAFT", "PENDING_APPROVAL"):
            continue
        if e.period.endswith("-REV"):
            pending_corrective_ids.add(e.schedule_id)
            continue
        direction = 1.0 if originals.get(e.schedule_id, 0.0) >= 0 else -1.0
        if float(e.amount or 0) * direction < 0:
            pending_corrective_ids.add(e.schedule_id)

    for schedule in over_amortized:
        # The residual to unwind is measured against the salvage FLOOR: the
        # correction entry's amount is remaining − salvage (NEGATIVE for the
        # canonical over-amortized-positive case) so that on approval
        # mark_entry_posted re-lands the balance exactly AT salvage:
        #     new_remaining = remaining − (remaining − salvage) = salvage.
        # Salvage is 0 for every pre-Phase-B row → the legacy re-zeroing.
        remaining = round(
            float(schedule.remaining_amount or 0) - salvage_value(schedule), 2
        )

        if schedule.id in pending_corrective_ids:
            skipped.append({
                "schedule_id": schedule.id,
                "schedule_name": schedule.name,
                "reason": "corrective_already_pending",
            })
            continue

        # Correction period = current month. If an entry already occupies
        # (schedule_id, period), append a disambiguating suffix so the reversal
        # never collides with a periodic entry (the UniqueConstraint is on the
        # raw period string, so "YYYY-MM-REV" is a distinct key).
        period = base_period
        if schedule.id in taken_current_period:
            period = f"{base_period}-REV"

        # Insert the correction ScheduleEntry inside a SAVEPOINT so a concurrent
        # run racing the same (schedule_id, period) rolls back just the savepoint
        # and leaves the session usable. amount == remaining (NEGATIVE) is the
        # residual to unwind.
        entry = ScheduleEntry(
            schedule_id=schedule.id,
            realm_id=realm_id,
            period=period,
            amount=remaining,
            status="DRAFT",
        )
        try:
            with db.begin_nested():
                db.add(entry)
                db.flush()
        except IntegrityError:
            # A concurrent run already created a correction for this period.
            skipped.append({
                "schedule_id": schedule.id,
                "schedule_name": schedule.name,
                "reason": "correction_entry_exists",
            })
            continue
        db.commit()

        proposal = je_builder.build_je_for_entry(
            entry, schedule, db, submitted_by=submitted_by,
            human_preparer_user_id=human_preparer_user_id,
        )
        if proposal is None:
            skipped.append({
                "schedule_id": schedule.id,
                "schedule_name": schedule.name,
                "reason": "proposal_not_created",
            })
            continue

        corrections.append({
            "schedule_id": schedule.id,
            "schedule_name": schedule.name,
            "schedule_type": schedule.schedule_type,
            "over_amortized_by": round(abs(remaining), 2),
            "period": period,
            "proposal_id": proposal.id,
            "bs_account": schedule.qbo_account_name or schedule.qbo_account_id,
            "expense_account": schedule.expense_account_name or schedule.expense_account_id,
        })
        logger.info(
            "[ScheduleEngine] Drafted over-amortization correction for schedule %s (%s): "
            "reversing $%.2f, proposal_id=%s",
            schedule.id,
            schedule.name,
            abs(remaining),
            proposal.id,
        )

    return {
        "realm_id": realm_id,
        "corrections_proposed": len(corrections),
        "corrections": corrections,
        "skipped": skipped,
    }


def _finalize_disposal(db: Session, action, *, actor: str | None = None) -> None:
    """
    Phase-B AC-2 (plan 20260702-phase-b-fa-lifecycle): when an EXECUTED
    JOURNAL_ENTRY proposal carries the ``peregrine_disposal`` payload marker
    (written by je_builder.build_disposal_je), flip the schedule to DISPOSED
    and freeze it. Runs INSIDE run_post_execution_schedule_hook's loud-failure
    contract. Idempotent: an already-DISPOSED schedule is a no-op (at-least-
    once execution delivery must not double-audit).

    Freezing means: remaining_amount / accumulated_amount are left EXACTLY as
    they stand (the disposal JE removed cost + accum-dep in QBO; the schedule
    row keeps its history) and the disposal facts (date / proceeds /
    gain-loss / proposal id) are stamped with an audit row. Generation stops
    because DISPOSED is outside generate_month_entries' status filter
    (ACTIVE / FULLY_AMORTIZED) — the beat task inherits that filter.
    """
    from ..models import AccountingSchedule
    from . import audit

    disposal = (action.payload or {}).get("peregrine_disposal")
    if not disposal:
        return

    schedule = (
        db.query(AccountingSchedule)
        .filter(
            AccountingSchedule.id == disposal.get("schedule_id"),
            AccountingSchedule.realm_id == action.company_id,
        )
        .first()
    )
    if schedule is None:
        # Loud: the JE posted to QBO but the schedule row is gone — surface
        # through the hook's failure contract (raise → SCHEDULE_POST_HOOK_FAILED).
        raise RuntimeError(
            f"Disposal proposal {action.id} executed but schedule "
            f"{disposal.get('schedule_id')} was not found for realm {action.company_id}"
        )
    if schedule.status == "DISPOSED":
        return  # idempotent re-delivery

    schedule.status = "DISPOSED"
    schedule.disposal_date = date.fromisoformat(disposal["disposal_date"])
    schedule.disposal_proceeds = disposal["proceeds"]
    schedule.disposal_gain_loss = disposal["gain_loss"]
    schedule.disposal_proposed_action_id = action.id

    audit.log_action(
        db=db,
        realm_id=schedule.realm_id,
        action="SCHEDULE_DISPOSED",
        entity_type="accounting_schedule",
        entity_id=str(schedule.id),
        details={
            "proposal_id": action.id,
            "disposal_date": disposal["disposal_date"],
            "proceeds": disposal["proceeds"],
            "gain_loss": disposal["gain_loss"],
            "cost": disposal.get("cost"),
            "accumulated": disposal.get("accumulated"),
            "net_book_value": disposal.get("net_book_value"),
            "actor": actor,
        },
    )
    db.commit()
    logger.info(
        "[ScheduleEngine] Schedule %s (%s) DISPOSED via proposal %s "
        "(proceeds=%s, gain_loss=%s)",
        schedule.id,
        schedule.name,
        action.id,
        disposal["proceeds"],
        disposal["gain_loss"],
    )


def run_post_execution_schedule_hook(db: Session, action, *, actor: str | None = None):
    """
    After a proposal executes successfully, mark its linked ScheduleEntry rows
    POSTED (mark_entry_posted), then finalize a disposal if the payload
    carries the ``peregrine_disposal`` marker (_finalize_disposal). LOUD
    failure contract: a hook failure leaves the proposal EXECUTED but the
    schedule ledger inconsistent, so it must never be a silent warning — it
    writes a SCHEDULE_POST_HOOK_FAILED audit row, stamps a warning into the
    action's execution_result, and returns the warning dict so callers can
    surface it in their response. Returns None on success (including the
    no-linked-entries no-op).
    """
    from . import audit

    try:
        mark_entry_posted(db, action.id)
        _finalize_disposal(db, action, actor=actor)
        # PLAN-EXEMPT: ratified plan 20260702-phase-c-asc842 (AC-3/AC-4 — the
        # lease leg of the existing post-execution rail; a no-op for payloads
        # without a peregrine_lease marker. This hook is the single seam all
        # four executor call sites share, so the lease delegation lives here.)
        from .lease_engine import run_post_execution_lease_hook
        run_post_execution_lease_hook(db, action, actor=actor)
        return None
    except Exception as exc:
        # mark_entry_posted commits as it goes; the session may be dirty.
        db.rollback()
        error_category = redact_to_string(str(exc), max_length=300)
        logger.error(
            "SCHEDULE POST HOOK FAILED for proposal_id=%s company_id=%s: linked "
            "schedule entries were NOT marked POSTED and need manual reconciliation. %s",
            action.id,
            action.company_id,
            error_category,
        )
        warning = {
            "code": "schedule_post_hook_failed",
            "message": (
                "The QuickBooks write succeeded, but the linked schedule entries could "
                "not be marked POSTED. Schedule balances will not reflect this entry "
                "until reconciled."
            ),
        }
        audit.log_action(
            db=db,
            realm_id=action.company_id,
            action="SCHEDULE_POST_HOOK_FAILED",
            entity_type="proposed_action",
            entity_id=str(action.id),
            details={"error": error_category, "actor": actor},
        )
        action.execution_result = {
            **(action.execution_result or {}),
            "warnings": [warning],
        }
        db.commit()
        return warning


# ─────────────────────────────────────────────────────────────────────────────
# The ONE schedule tie-out (AC-D3, plan 20260716-five-domain-remediation;
# basis rebased to QBO-REFLECTED + AC-T2/T3 diagnostics by AC-T1..T3, plan
# 20260716-batch5-remediation)
#
# "Does the schedule sub-ledger tie to QuickBooks?" is answered in exactly ONE
# place: the reflected-basis decomposition + build_balance_check below. Both
# consumers — the workpaper grid (routers/accounting_schedules.py
# GET /schedule-grid) and the standalone validate_schedule_balance (the
# GET /schedule-balance-check endpoint + the assistant's schedule review) —
# delegate here. The former second implementation (stale remaining_amount
# sums, a today-capped single-column Balance Sheet, an ACTIVE+
# PENDING_APPROVAL schedule filter) is RETIRED; the registry's
# engine_bs_patterns field is now vestigial — the tie-out matches mapped
# credit accounts first, then grid_bs_patterns.
#
# NOTE: every dict access in the loop bodies below is a PLAIN DICT lookup on
# pre-batched inputs (never a DB query) — written bracket-style per the
# shared-walker idiom.
# ─────────────────────────────────────────────────────────────────────────────

# Schedule statuses whose recognition participates in the tie-out — exactly
# the engine's ACTIVE set (generate_month_entries queries these two): a
# DETECTED / PENDING_APPROVAL schedule is not recognizing yet, and a DISPOSED
# schedule left the GL via its disposal JE, so neither may contribute to any
# tie-out total.
TIE_OUT_RECOGNIZING_STATUSES = ("ACTIVE", "FULLY_AMORTIZED")

TIE_OUT_TOLERANCE = 1.00  # $1.00 rounding tolerance on the reflected-basis delta

# AC-T2 (plan 20260716-batch5-remediation): a header-vs-entry disagreement at
# or below a cent is rounding, not drift — anything above it is surfaced.
TIE_OUT_DRIFT_TOLERANCE = 0.01


class TieOutScheduleSide(TypedDict, total=False):
    """The reflected-basis schedule-side decomposition build_balance_check reads."""

    is_depreciable: bool
    schedule_total: float   # legacy display-basis total (grid rendering parity)
    posted_total: float     # QBO-REFLECTED basis (AC-T1) — the tie-out verdict side
    pending_total: float    # entry-backed recognition awaiting approval (DRAFT/PENDING)
    projected_total: float  # entry-less straight-line projection fill
    row_count: int
    # AC-T2/AC-T3 diagnostics (assemble_tie_out_diagnostics builds all three):
    unstamped_reflected_months: int   # nonzero reflected months w/o a posted_at stamp
    header_drift: dict | None         # header-vs-entry drift block (None when clean)
    unconfirmed_history: dict | None  # zero-entry sync schedules (None when none)


# The tie-out payload / report-walk context are heterogeneous JSON shapes for
# the API edge — typed as object-valued dicts (structural consumers).
BalanceCheckPayload = dict[str, object]
BSContext = dict[str, object]


def entry_is_qbo_reflected(posted_at, status) -> bool:
    """The QBO-REFLECTED predicate (AC-T1, plan 20260716-batch5-remediation):
    ``(posted_at IS NOT NULL) OR (status == "POSTED")``.

    The strict AC-D3 basis (``posted_at IS NOT NULL`` only) counted just the
    ProposedAction-executed lane — DB-wide 20 entries / $12,322 — while real
    recognition lives in catch-up / import / grid-save / assistant writes
    that stamp ``status="POSTED"`` with ``posted_at=None``
    (2,749 entries / $15.4M). On the pilot realm that read 0.08% of real
    recognition and produced the false $579,196.37 alarm.

    Why status="POSTED" is trustworthy as a QBO-GL claim (the R-Dim-1 writer
    census, refuter-verified):
      * every path that can write status="POSTED" runs resolve_written_status
        (grid save, /confirm-import, assistant), which grants POSTED only for
        periods <= the current month — a POSTED/no-stamp row is exclusively a
        claim that the recognition already exists in QBO's GL;
      * all service-side constructors write DRAFT or PENDING_APPROVAL;
        disposal/lease-termination write SKIPPED only;
      * the ONLY writer of ``posted_at`` is mark_entry_posted (after the
        linked ProposedAction's JE EXECUTED in QBO), and
        posted_at IS NOT NULL AND status <> 'POSTED' is zero rows DB-wide —
        the OR is a strict widening, never a re-bucketing.

    A FALSE claim (a user-typed month whose JE never existed in QBO) now
    SURFACES as a real delta instead of hiding in "pending" — the
    Definitive-Healthcare-class flip ratification explicitly owns (plan §6).
    DRAFT / PENDING_APPROVAL entries stay in the pending bucket; SKIPPED
    lands in no bucket.
    """
    return posted_at is not None or status == "POSTED"


def _life_month_keys(schedule) -> set:
    """Every ``YYYY-MM`` inside the schedule's straight-line life window
    (start_date → end_date, or start + useful_life − 1). The display walk only
    projects nonzero values inside this window, so it is exactly the set of
    months that can carry a projection echo the save path must recognize."""
    if not schedule.start_date or not schedule.useful_life_months:
        return set()
    from dateutil.relativedelta import relativedelta

    start = date(schedule.start_date.year, schedule.start_date.month, 1)
    end = schedule.end_date or (
        start + relativedelta(months=schedule.useful_life_months - 1)
    )
    end = date(end.year, end.month, 1)
    keys = set()
    cursor = start
    while cursor <= end:
        keys.add(cursor.strftime("%Y-%m"))
        cursor += relativedelta(months=1)
    return keys


def _display_month_walk(schedule, months, base_amounts, rev_info):
    """The ONE per-schedule month walk (single shared walker — no divergent
    copies): given the base-entry amounts and the non-SKIPPED "-REV" fold
    metadata for a schedule, produce the exact per-month DISPLAY values the
    workpaper grid renders — a real entry wins, straight-line projection fills
    entry-less months inside the life window (salvage floor, sign-aware
    over-amortization clamp, final-period plug), and "-REV" corrections fold
    into their base month. Consumed by ``GET /schedule-grid`` (rendering),
    ``POST /save-schedule-rows`` (the mutation-only lock guard: an incoming
    cell that equals its display value is a no-op echo, not an edit), AND the
    tie-out decomposition above (AC-D3) — moved here from the router so the
    service-side tie-out shares it without re-implementation.

    ``months`` must be an ASCENDING month list starting at-or-before the
    schedule's first value-bearing month — the running-remaining recurrence is
    order-sensitive. Returns ``(month_values, rev_folded, salvage,
    monthly_amount)``.
    """
    from dateutil.relativedelta import relativedelta

    # Phase-B AC-1: the depreciable base excludes salvage (0 for every
    # pre-Phase-B row and for negative credit-adjustment schedules — mirrors
    # schedule_engine.salvage_value).
    salvage = (
        round(float(getattr(schedule, "salvage_value", 0) or 0), 2)
        if float(schedule.original_amount or 0) > 0
        else 0.0
    )
    monthly_amount = (
        (float(schedule.original_amount) - salvage) / schedule.useful_life_months
        if schedule.useful_life_months and schedule.useful_life_months > 0
        else 0.0
    )
    month_values = {}
    # Remaining-aware straight-line projection — mirrors the engine's
    # sign-aware over-amortization clamp (schedule_engine.generate_month_entries):
    # a projected month never steps the running balance past zero, the final
    # in-range month plugs to exactly what's left, and an over-amortized
    # schedule projects NOTHING (0) for future months. The corrective
    # negative month is emitted by the engine as a REAL ScheduleEntry (which
    # then shows here signed); projecting it would cosmetically zero the
    # OVER state before the corrective exists.
    running_remaining = round(float(schedule.original_amount), 2)
    rev_folded = {}  # base month -> folded "-REV" correction metadata
    for m in months:
        # Plain dict lookups (not DB queries) — the entries were batched into
        # base_amounts / rev_info by the caller in ONE query.
        rev = rev_info[m] if m in rev_info else None
        # A folded "-REV" correction counts as a REAL entry for this month:
        # never overlay a straight-line projection on top of it.
        has_entry = m in base_amounts or rev is not None
        val = base_amounts[m] if m in base_amounts else 0.0
        # Only fall back to straight-line if NO imported entry exists
        if not has_entry and schedule.useful_life_months:
            m_date = date(int(m[:4]), int(m[5:7]), 1)
            s_start = date(schedule.start_date.year, schedule.start_date.month, 1)
            s_end = schedule.end_date or (
                s_start + relativedelta(months=schedule.useful_life_months - 1)
            )
            s_end = date(s_end.year, s_end.month, 1)
            if s_start <= m_date <= s_end:
                projected = round(monthly_amount, 2)
                # Salvage-aware floor (Phase-B AC-1): the projection stops
                # AT salvage, never at zero — remaining_above_floor is the
                # still-projectable depreciable balance. Salvage is 0 for
                # every pre-Phase-B row → byte-identical legacy math.
                remaining_above_floor = round(running_remaining - salvage, 2)
                if abs(remaining_above_floor) <= 0.005:
                    val = 0.0  # fully consumed — nothing left to project
                elif (projected > 0) != (remaining_above_floor > 0):
                    val = 0.0  # over-amortized — the engine emits the corrective entry
                elif m_date == s_end:
                    val = remaining_above_floor  # final-period plug: land EXACTLY on the salvage floor
                else:
                    # Same-sign cap: never project past the floor in either direction
                    val = (
                        min(projected, remaining_above_floor)
                        if projected > 0
                        else max(projected, remaining_above_floor)
                    )
        if rev is not None:
            # Fold the correction into the base month: NET of the regular
            # entry (or 0 when only the corrective exists) + the reversal.
            val = round(val + rev["amount"], 2)
            rev_folded[m] = rev
        month_values[m] = val
        running_remaining = round(running_remaining - val, 2)
    return month_values, rev_folded, salvage, monthly_amount


def find_duplicate_schedules(
    schedules, monthly_amounts: dict[int, float]
) -> dict[int, list[dict[str, object]]]:
    """AC-D4 duplicate surfacing: flag schedules sharing a vendor + monthly
    amount + OVERLAPPING amortization ranges — surfaced as a warning, NEVER
    auto-netted (disposition is a human call).

    ``monthly_amounts`` is {schedule_id: walker monthly_amount} — reuse the
    shared walker's output, never a re-derivation. Matching is cent-precision
    (amounts compared at round-2); schedules with no vendor or a zero monthly
    never flag (too noisy to be a signal); DISPOSED schedules are dead and
    excluded. The prod Definitive Healthcare pair (same vendor + start,
    monthlies 732.88 vs 1132.29) must NOT flag — different amounts.

    Returns {schedule_id: [{"id", "name"}, ...]} naming each counterpart.
    """
    from collections import defaultdict

    by_key: dict = defaultdict(list)
    for s in schedules:
        vendor = (s.vendor_name or "").strip().lower()
        if not vendor or (s.status or "") == "DISPOSED" or not s.start_date:
            continue
        # Plain dict lookup (not a DB query) — walker outputs batched by caller.
        monthly = round(monthly_amounts[s.id] if s.id in monthly_amounts else 0.0, 2)
        if abs(monthly) <= 0.005:
            continue
        start = date(s.start_date.year, s.start_date.month, 1)
        end = _schedule_end_month(s)
        by_key[(vendor, monthly)].append((s, start, end))

    dup_map: dict[int, list[dict[str, object]]] = {}
    for group in by_key.values():
        if len(group) < 2:
            continue
        for i, (s, s_start, s_end) in enumerate(group):
            for s2, s2_start, s2_end in group[i + 1:]:
                if s_start <= s2_end and s2_start <= s_end:  # ranges overlap
                    dup_map.setdefault(s.id, []).append({"id": s2.id, "name": s2.name})
                    dup_map.setdefault(s2.id, []).append({"id": s.id, "name": s.name})
    return dup_map


def fold_entry_maps(entries) -> tuple[dict, dict, dict]:
    """The ONE entry fold: split a batch of ScheduleEntry rows into the three
    per-schedule lookups every display/tie-out consumer reads.

    Manual over-amortization corrections are stored under a "YYYY-MM-REV"
    period (a DELIBERATE dedup key — the UniqueConstraint is on the raw
    period string, so the reversal never collides with the periodic entry).
    They FOLD into their base "YYYY-MM" month; SKIPPED corrections (human
    declined) are NOT folded. Returns ``(base_by_sched, rev_by_sched,
    meta_by_sched)``; meta carries the reflected-basis classification inputs
    decompose_recognition reads.
    """
    base_by_sched: dict = {}
    rev_by_sched: dict = {}
    meta_by_sched: dict = {}
    for e in entries:
        if e.period.endswith("-REV"):
            if e.status == "SKIPPED":
                continue
            base = e.period[: -len("-REV")]
            # EXACTLY these four keys: the rev dict rides the grid payload
            # (rows[].rev_folded — a public shape the FE correction badge and
            # the manual-correction goldens pin). "qbo_posted" keeps its
            # original STAMP meaning (a QBO JE executed via mark_entry_posted);
            # decompose_recognition derives the widened REFLECTED bucket from
            # status + stamp (AC-T1) without changing this shape.
            rev_by_sched.setdefault(e.schedule_id, {})[base] = {
                "amount": float(e.amount or 0),
                "status": e.status,
                "pending": e.status in ("DRAFT", "PENDING_APPROVAL"),
                "qbo_posted": e.posted_at is not None,
            }
        else:
            base_by_sched.setdefault(e.schedule_id, {})[e.period] = float(e.amount or 0)
            meta_by_sched.setdefault(e.schedule_id, {})[e.period] = {
                "status": e.status,
                # The AC-T1 tie-out bucket predicate (stamped OR status
                # POSTED) plus the raw stamp — the stamp feeds the AC-T2
                # unstamped-claim count. This meta map is internal to the
                # decomposition (never serialized to the grid payload).
                "qbo_reflected": entry_is_qbo_reflected(e.posted_at, e.status),
                "qbo_stamped": e.posted_at is not None,
            }
    return base_by_sched, rev_by_sched, meta_by_sched


def decompose_recognition(
    months,
    view_period: str,
    month_values: dict[str, float],
    rev_folded: dict[str, dict],
    base_amounts: dict[str, float],
    base_meta: dict[str, dict],
) -> tuple[float, float, float, int]:
    """Split ONE schedule's displayed recognition through ``view_period`` into
    the tie-out buckets ``(reflected, pending, projected,
    unstamped_reflected_months)``.

    Rides the shared _display_month_walk outputs — never re-walks:
      * reflected — entry-backed months the QBO-REFLECTED predicate accepts
                    (entry_is_qbo_reflected: a posted_at stamp OR
                    status=="POSTED"), incl. folded reflected correctives.
                    Feeds the payload's ``posted_total`` side (AC-T1).
      * pending   — entry-backed DRAFT / PENDING_APPROVAL months awaiting
                    approval, incl. folded pending "-REV" corrections.
      * projected — entry-less straight-line projection fill.
      * unstamped_reflected_months — how many reflected months carry a
                    NONZERO recognition claim with NO posted_at stamp (a
                    status-only POSTED claim). This is the count the DH-flip
                    banner teaches as "N months are recognized in Peregrine
                    but have no QuickBooks journal entry" (AC-T2, ratified
                    wording); zero-amount padding rows never inflate it.
    A SKIPPED base entry (human declined the period) lands in NO bucket: it
    is neither reflected nor awaiting posting, and any QBO-side reality it
    papered over surfaces in the reflected delta instead of being silently
    absorbed.
    """
    reflected = pending = projected = 0.0
    unstamped_months = 0
    for m in months:
        if m > view_period:
            continue
        # Plain dict lookups (not DB queries) — inputs were batched by the caller.
        val = month_values[m] if m in month_values else 0.0
        rev = rev_folded[m] if m in rev_folded else None
        base_component = round(val - rev["amount"], 2) if rev else val
        month_has_unstamped_claim = False
        if m in base_amounts:
            meta = base_meta[m] if m in base_meta else None
            if meta is not None and meta["qbo_reflected"]:
                reflected += base_component
                if not meta["qbo_stamped"] and abs(base_component) > 0.005:
                    month_has_unstamped_claim = True
            elif meta is None or meta["status"] != "SKIPPED":
                pending += base_component
        elif base_component:
            projected += base_component
        if rev is not None:
            # The rev dict carries the PUBLIC 4-key grid-payload shape (see
            # fold_entry_maps): "qbo_posted" is the STAMP; the widened
            # REFLECTED predicate is derived here (stamp OR status POSTED —
            # AC-T1) so the payload shape stays byte-stable.
            if rev["qbo_posted"] or rev["status"] == "POSTED":
                reflected += rev["amount"]
                if not rev["qbo_posted"] and abs(rev["amount"]) > 0.005:
                    month_has_unstamped_claim = True
            else:
                pending += rev["amount"]
        if month_has_unstamped_claim:
            unstamped_months += 1
    return round(reflected, 2), round(pending, 2), round(projected, 2), unstamped_months


def compute_header_drift_row(schedule, reflected_rec: float) -> dict | None:
    """AC-T2 (plan 20260716-batch5-remediation): ONE schedule's
    header-vs-entry drift row, or None when the two derivations agree.

    Every header writer now shares the posted basis (posted_basis_remaining:
    grid-save, /confirm-import, recompute and the remediation recompute it;
    mark_entry_posted subtracts each entry exactly once as it posts), so the
    header should equal the entry derivation. Historical rows written under
    the old mixed bases (grid-save once subtracted future DRAFT entries) can
    still disagree — on the pilot realm, 3 schedules' entries ran 2 months
    AHEAD of their headers, $13,294.35 total. The tie-out's verdict rides the ENTRY
    derivation (original − accumulated − reflected recognition); the header
    is COMPARED against it and any disagreement is surfaced as this named
    diagnostic — never silently absorbed into either side.
    """
    entry_remaining = round(
        float(schedule.original_amount or 0)
        - float(getattr(schedule, "accumulated_amount", 0) or 0)
        - reflected_rec,
        2,
    )
    header_remaining = round(float(schedule.remaining_amount or 0), 2)
    drift = round(header_remaining - entry_remaining, 2)
    if abs(drift) <= TIE_OUT_DRIFT_TOLERANCE:
        return None
    return {
        "id": schedule.id,
        "name": schedule.name,
        "header_remaining": header_remaining,
        "entry_remaining": entry_remaining,
        "drift": drift,
    }


def schedule_history_is_unconfirmed(schedule, has_entries: bool, view_period: str) -> bool:
    """AC-T3: True for a recognizing schedule with ZERO entries whose life
    began before the view month — the /sync-prepaids / account-type-sync
    population (header-only schedules with no entry history). Their past
    recognition is UNKNOWN: the straight-line fill is a projection, not
    evidence, so the tie-out reports them as "history unconfirmed" rather
    than presenting a confident tie or a false alarm. Zero-entry schedules
    are excluded from the AC-T2 drift diagnostic for the same reason — there
    is no entry derivation to compare the header against."""
    if has_entries or not schedule.start_date:
        return False
    return schedule.start_date.strftime("%Y-%m") < view_period


def assemble_tie_out_diagnostics(
    drift_rows: list, unconfirmed_rows: list, unstamped_months: int
) -> dict:
    """The ONE assembler both tie-out paths (the grid row loop and
    compute_tie_out_schedule_side) use for the AC-T2/AC-T3 diagnostic keys.
    Empty diagnostics yield None so a clean realm's payload carries no
    drift/history block (build_balance_check omits None-valued blocks)."""
    return {
        "unstamped_reflected_months": unstamped_months,
        "header_drift": (
            {
                "total": round(sum(r["drift"] for r in drift_rows), 2),
                "schedules": drift_rows,
            }
            if drift_rows
            else None
        ),
        "unconfirmed_history": (
            {"count": len(unconfirmed_rows), "schedules": unconfirmed_rows}
            if unconfirmed_rows
            else None
        ),
    }


def compute_tie_out_schedule_side(
    db: Session, realm_id: str, schedule_type: str, view_period: str
) -> TieOutScheduleSide:
    """Standalone schedule-side assembler for the tie-out — the same fold +
    shared walker + decomposition the grid applies to its already-walked
    outputs, for callers with no grid context (validate_schedule_balance).
    Only TIE_OUT_RECOGNIZING_STATUSES schedules participate."""
    from ..models import AccountingSchedule, ScheduleEntry
    from .schedule_types import SCHEDULE_TYPES, TIE_OUT_CUMULATIVE_VS_CONTRA

    descriptor = SCHEDULE_TYPES.get(schedule_type)
    is_depreciable = bool(
        descriptor and descriptor.tie_out_mode == TIE_OUT_CUMULATIVE_VS_CONTRA
    )

    schedules = db.query(AccountingSchedule).filter(
        AccountingSchedule.realm_id == realm_id,
        AccountingSchedule.schedule_type == schedule_type,
        AccountingSchedule.status.in_(list(TIE_OUT_RECOGNIZING_STATUSES)),
    ).all()

    sched_ids = [s.id for s in schedules]
    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.realm_id == realm_id,
            ScheduleEntry.schedule_id.in_(sched_ids),
        )
        .all()
        if sched_ids
        else []
    )
    base_by_sched, rev_by_sched, meta_by_sched = fold_entry_maps(entries)

    reflected_rec = pending_rec = projected_rec = display_rec = 0.0
    original_as_of_view = 0.0
    accumulated_as_of_view = 0.0
    unstamped_months = 0
    drift_rows: list = []
    unconfirmed_rows: list = []
    for s in schedules:
        if s.start_date and s.start_date.strftime("%Y-%m") <= view_period:
            original_as_of_view += float(s.original_amount or 0)
            # AC-T1: pre-Peregrine recognition (accumulated_amount) joins the
            # reflected side in BOTH modes — same eligibility gate as original.
            accumulated_as_of_view += float(getattr(s, "accumulated_amount", 0) or 0)
        # Plain dict lookups (not DB queries) — entries batched in ONE query above.
        base = base_by_sched[s.id] if s.id in base_by_sched else {}
        rev = rev_by_sched[s.id] if s.id in rev_by_sched else {}
        meta = meta_by_sched[s.id] if s.id in meta_by_sched else {}
        has_entries = bool(base) or bool(rev)
        if schedule_history_is_unconfirmed(s, has_entries, view_period):
            unconfirmed_rows.append({"id": s.id, "name": s.name})
        walk_months = sorted(set(base) | set(rev) | _life_month_keys(s))
        if not walk_months:
            continue
        month_values, rev_folded, _salvage, _monthly = _display_month_walk(
            s, walk_months, base, rev
        )
        p_rec, pe_rec, pr_rec, p_unstamped = decompose_recognition(
            walk_months, view_period, month_values, rev_folded, base, meta
        )
        reflected_rec += p_rec
        pending_rec += pe_rec
        projected_rec += pr_rec
        unstamped_months += p_unstamped
        display_rec += sum(v for m, v in month_values.items() if m <= view_period)
        if has_entries:
            # AC-T2: header-vs-entry drift, entry-bearing schedules only
            # (zero-entry schedules have no entry derivation — they surface
            # via unconfirmed_history above instead).
            drift_row = compute_header_drift_row(s, p_rec)
            if drift_row is not None:
                drift_rows.append(drift_row)

    if is_depreciable:
        # cumulative_vs_contra: the totals ARE cumulative recognition;
        # pre-Peregrine accumulated joins the reflected side (AC-T1).
        schedule_total = round(display_rec, 2)
        posted_total = round(accumulated_as_of_view + reflected_rec, 2)
    else:
        # remaining_vs_bs: original (for schedules acquired by the view
        # period) minus pre-Peregrine accumulated minus reflected recognition
        # (AC-T1); the display total keeps the legacy walk-only form.
        schedule_total = round(original_as_of_view - display_rec, 2)
        posted_total = round(
            original_as_of_view - accumulated_as_of_view - reflected_rec, 2
        )

    return {
        "is_depreciable": is_depreciable,
        "schedule_total": schedule_total,
        "posted_total": posted_total,
        "pending_total": round(pending_rec, 2),
        "projected_total": round(projected_rec, 2),
        "row_count": len(schedules),
        **assemble_tie_out_diagnostics(drift_rows, unconfirmed_rows, unstamped_months),
    }


def build_balance_check(
    db: Session,
    realm_id: str,
    schedule_type: str,
    *,
    qbo_client,
    view_period: str,
    schedule_side: TieOutScheduleSide,
    row_count: int,
    qbo_client_factory=None,
    no_client_ok: bool = False,
) -> tuple[BalanceCheckPayload, BSContext | None]:
    """Assemble THE tie-out verdict: the QBO-REFLECTED schedule side (AC-T1)
    vs the QBO Balance Sheet. Returns ``(balance_check, bs_context)`` —
    bs_context feeds the grid's per-contra-account breakdown (None on the
    no-client / error branches).

    The QBO side is the month-columned Balance Sheet (summarize_column_by=
    Month) so future-dated transactions inside the view month are included —
    the retired standalone implementation's today-capped single-column report
    missed them. ``delta = posted_total − qbo_balance`` (magnitude of qbo for
    cumulative-vs-contra types, whose contra balance is negative on the BS);
    ``in_balance`` rides the REFLECTED delta only — a pending or projected
    month can never make the tie-out read as off. The AC-T2/AC-T3 diagnostics
    (unstamped claim count, header-vs-entry drift, unconfirmed sync history)
    ride EVERY branch including no-client/error: they are schedule-side
    facts, independent of QBO availability.
    """
    import calendar as _calendar

    from .schedule_types import SCHEDULE_TYPES

    type_descriptor = SCHEDULE_TYPES.get(schedule_type)
    check_label = (
        type_descriptor.tie_out_label if type_descriptor else "remaining balance"
    )
    is_depreciable = bool(schedule_side.get("is_depreciable"))
    posted_total = schedule_side["posted_total"]
    unstamped_months = int(schedule_side.get("unstamped_reflected_months") or 0)
    header_drift = schedule_side.get("header_drift")
    unconfirmed_history = schedule_side.get("unconfirmed_history")
    base_payload: BalanceCheckPayload = {
        "schedule_total": schedule_side["schedule_total"],
        "posted_total": posted_total,
        "pending_total": schedule_side["pending_total"],
        "projected_total": schedule_side["projected_total"],
        "row_count": row_count,
        "check_type": check_label,
        # AC-T2: the DH-flip teaching count — reflected months carrying a
        # nonzero claim with no posted_at stamp.
        "unstamped_reflected_months": unstamped_months,
    }
    # AC-T2/AC-T3 named diagnostic blocks — ABSENT when clean ("no block"),
    # never silently preferring the header or the entry derivation.
    if header_drift:
        base_payload["header_drift"] = header_drift
    if unconfirmed_history:
        base_payload["unconfirmed_history"] = unconfirmed_history

    if qbo_client is None and qbo_client_factory is None and no_client_ok:
        return (
            {
                **base_payload,
                "qbo_balance": None,
                "delta": None,
                "in_balance": True,  # can't verify without QBO
                "details": (
                    f"Schedule QBO-reflected total: ${posted_total:,.2f} "
                    f"({row_count} rows). No QBO client to verify BS balance."
                ),
            },
            None,
        )

    try:
        if qbo_client is None and qbo_client_factory is not None:
            # A factory raise (no QBO token / connection) flows to the error
            # payload below — the grid's legacy semantics for a missing client.
            qbo_client = qbo_client_factory()
        from ..models import ScheduleCreditMapping

        # Credit-account mapping(s): multiple rows may exist per type
        # (account-level overrides + the type-level default) — the tie-out
        # matches the UNION of their credit accounts, so per-account contra
        # mappings still sum into one check.
        mapping_rows = (
            db.query(ScheduleCreditMapping)
            .filter(
                ScheduleCreditMapping.realm_id == realm_id,
                ScheduleCreditMapping.schedule_type == schedule_type,
            )
            .order_by(ScheduleCreditMapping.id)
            .all()
        )

        # Month-columned BS so future-dated transactions inside the view
        # month are included (a single-column BS caps at today).
        vp_year, vp_month = int(view_period[:4]), int(view_period[5:7])
        end_day = _calendar.monthrange(vp_year, vp_month)[1]
        bs_end_date = f"{vp_year}-{vp_month:02d}-{end_day:02d}"
        report = qbo_client.get_report("BalanceSheet", params={
            "accounting_method": "Accrual",
            "start_date": f"{vp_year}-01-01",
            "end_date": bs_end_date,
            "summarize_column_by": "Month",
        })

        # Which column index holds the view_period month (default: the LAST
        # Money column = the latest month). Dict accesses below are plain
        # lookups on the parsed report JSON — no DB queries in these loops.
        bs_columns = report.get("Columns", {}).get("Column", [])
        target_col_idx = max(len(bs_columns) - 1, 1)
        target_label = f"{_calendar.month_abbr[vp_month]} {vp_year}"  # "Mar 2026"
        for i, bs_col in enumerate(bs_columns):
            title = bs_col["ColTitle"] if "ColTitle" in bs_col else ""
            if target_label.lower() in (title or "").lower():
                target_col_idx = i
                break

        bs_rows = report.get("Rows", {}).get("Row", [])
        qbo_balance = 0.0
        matched_accounts: list[dict[str, object]] = []

        mapped_account_ids = {
            str(m.qbo_account_id) for m in mapping_rows if m.qbo_account_id
        }
        mapped_account_names = {
            (m.qbo_account_name or "").lower()
            for m in mapping_rows
            if m.qbo_account_name
        }
        patterns = list(type_descriptor.grid_bs_patterns) if type_descriptor else []

        def _is_acct_match(acct_name: str, acct_id: str) -> bool:
            """Match by mapped ID, mapped name, or per-type pattern."""
            if mapped_account_ids and str(acct_id) in mapped_account_ids:
                return True
            for mapped_name in mapped_account_names:
                if mapped_name and mapped_name in acct_name:
                    return True
            if not mapped_account_ids:
                for pattern in patterns:
                    if pattern in acct_name:
                        return True
            return False

        def _search_bs(rows_list):
            nonlocal qbo_balance
            for row in rows_list:
                # FIRST: a Section whose Header matches — take the Summary
                # total (parent account total) and DO NOT recurse into
                # sub-accounts (avoids partial-sum bugs when sub-accounts
                # don't all contain the pattern keyword).
                header = row["Header"] if "Header" in row else {}
                header_cols = header["ColData"] if "ColData" in header else []
                if len(header_cols) >= 1:
                    first = header_cols[0]
                    h_name = ((first["value"] if "value" in first else "") or "").lower()
                    h_id = first["id"] if "id" in first else ""
                    if _is_acct_match(h_name, h_id):
                        summary = row["Summary"] if "Summary" in row else {}
                        summary_cols = summary["ColData"] if "ColData" in summary else []
                        if len(summary_cols) > target_col_idx:
                            try:
                                cell = summary_cols[target_col_idx]
                                bal = float((cell["value"] if "value" in cell else "0") or "0")
                                qbo_balance += bal
                                matched_accounts.append({
                                    "name": first["value"] if "value" in first else "",
                                    "balance": bal,
                                })
                            except (ValueError, TypeError):
                                pass
                        continue  # Skip recursion — we have the parent total

                # Data rows (leaf accounts with no sub-accounts)
                col_data = row["ColData"] if "ColData" in row else []
                if len(col_data) > target_col_idx:
                    first = col_data[0]
                    acct_name = ((first["value"] if "value" in first else "") or "").lower()
                    acct_id = first["id"] if "id" in first else ""
                    if _is_acct_match(acct_name, acct_id):
                        try:
                            cell = col_data[target_col_idx]
                            bal = float((cell["value"] if "value" in cell else "0") or "0")
                            qbo_balance += bal
                            matched_accounts.append({
                                "name": first["value"] if "value" in first else "",
                                "balance": bal,
                            })
                        except (ValueError, TypeError):
                            pass

                # Recurse into unmatched sections
                nested = row["Rows"] if "Rows" in row else {}
                sub_rows = nested["Row"] if "Row" in nested else []
                if sub_rows:
                    _search_bs(sub_rows)

        _search_bs(bs_rows)
        qbo_balance = round(qbo_balance, 2)

        # Accumulated depreciation/amortization is negative on the BS; the
        # schedule tracks positive cumulative credits → compare magnitudes.
        if is_depreciable:
            delta = round(posted_total - abs(qbo_balance), 2)
        else:
            delta = round(posted_total - qbo_balance, 2)
        in_balance = abs(delta) <= TIE_OUT_TOLERANCE

        logger.info(
            f"[BalanceCheck] {schedule_type} ({check_label}) as_of={bs_end_date}: "
            f"posted={posted_total}, pending={schedule_side['pending_total']}, "
            f"projected={schedule_side['projected_total']}, qbo={qbo_balance}, "
            f"delta={delta}, matched={matched_accounts}"
        )

        details = (
            f"Posted: ${posted_total:,.2f} | "
            f"Pending: ${schedule_side['pending_total']:,.2f} | "
            f"Projected: ${schedule_side['projected_total']:,.2f} | "
            f"QBO BS: ${qbo_balance:,.2f} | "
            f"Delta (posted − QBO): ${delta:,.2f} "
            f"{'✅' if in_balance else '❌ OUT OF BALANCE'}"
        )
        # AC-T2 teaching lines (assistant passthrough reads this string too):
        # the DH-flip explanation on a mismatch, and the header-vs-entry /
        # unconfirmed-history diagnostics in every verdict state.
        if not in_balance and unstamped_months:
            details += (
                f" | {unstamped_months} months are recognized in Peregrine "
                f"but have no QuickBooks journal entry"
            )
        if header_drift:
            details += (
                f" | Header-vs-entry drift on {len(header_drift['schedules'])} "
                f"schedule(s): ${header_drift['total']:,.2f}"
            )
        if unconfirmed_history:
            details += (
                f" | {unconfirmed_history['count']} schedule(s) with history "
                f"unconfirmed (no recognition entries yet)"
            )

        balance_check: BalanceCheckPayload = {
            **base_payload,
            "qbo_balance": qbo_balance,
            "delta": delta,
            "in_balance": in_balance,
            "matched_accounts": matched_accounts,
            "as_of_date": bs_end_date,
            "details": details,
        }
        bs_context: BSContext = {
            "bs_rows": bs_rows,
            "target_col_idx": target_col_idx,
            "patterns": patterns,
            "mapping_rows": mapping_rows,
        }
        return balance_check, bs_context

    except Exception as e:
        logger.warning(
            "Balance check failed for %s: %s",
            schedule_type,
            redact_to_string(str(e), max_length=500),
            exc_info=True,
        )
        return (
            {
                **base_payload,
                "qbo_balance": None,
                "delta": None,
                "in_balance": False,
                "details": (
                    f"Schedule QBO-reflected total: ${posted_total:,.2f} "
                    f"({row_count} rows). QuickBooks balance check unavailable."
                ),
                "error": safe_error_detail(
                    error_code="schedule_balance_check_unavailable",
                    user_message="The QuickBooks balance check could not be completed.",
                    exc=e,
                    retryable=True,
                    log_context="schedule_balance_check_unavailable",
                ),
                "data_quality": {
                    "is_partial": True,
                    "unavailable_sections": ["schedule_balance_check_unavailable"],
                },
            },
            None,
        )


def build_contra_groups(
    schedules,
    bs_context: BSContext,
    *,
    sched_display_cum: dict[int, float],
    sched_posted_cum: dict[int, float],
) -> list[dict[str, object]] | None:
    """Phase-B AC-4 multi-contra breakdown of the ONE tie-out: group schedules
    by their RESOLVED contra account (the same account-level → type-level
    ScheduleCreditMapping resolution monthly generation uses) and check each
    group against ITS account. Returns None unless ≥2 distinct groups exist
    (today's single-contra world keeps the flat balance_check shape). Group
    verdicts ride the QBO-REFLECTED basis (AC-T1) — the caller's per-schedule
    ``sched_posted_cum`` values include pre-Peregrine ``accumulated_amount``
    plus reflected recognition (AC-T3, the same accumulated-joins-the-posted-
    side rule as the aggregate); the display-basis cumulative is kept per
    group as ``schedule_total`` for the register columns.
    """
    mapping_rows = bs_context["mapping_rows"]
    bs_rows = bs_context["bs_rows"]
    target_col_idx = bs_context["target_col_idx"]
    patterns = bs_context["patterns"]

    account_level_by_source: dict[str, object] = {}
    type_default_row = None
    for m in mapping_rows:
        if m.source_account_id:
            account_level_by_source.setdefault(str(m.source_account_id), m)
        elif type_default_row is None:
            type_default_row = m

    groups: dict[str, dict[str, object]] = {}
    for s in schedules:
        resolved = None
        # Plain dict lookups (not DB queries) — maps built above / by the caller.
        if s.qbo_account_id and str(s.qbo_account_id) in account_level_by_source:
            resolved = account_level_by_source[str(s.qbo_account_id)]
        resolved = resolved or type_default_row
        key = str(resolved.qbo_account_id) if resolved else "__pattern__"
        g = groups.setdefault(
            key,
            {
                "contra_account_id": str(resolved.qbo_account_id) if resolved else None,
                "contra_account_name": (resolved.qbo_account_name or "") if resolved else "",
                "schedule_total": 0.0,
                "posted_total": 0.0,
                "schedule_count": 0,
            },
        )
        g["schedule_total"] = round(
            g["schedule_total"]
            + (sched_display_cum[s.id] if s.id in sched_display_cum else 0.0),
            2,
        )
        g["posted_total"] = round(
            g["posted_total"]
            + (sched_posted_cum[s.id] if s.id in sched_posted_cum else 0.0),
            2,
        )
        g["schedule_count"] += 1

    if len(groups) < 2:
        return None

    def _collect_bs_for(match_fn):
        """Per-group BS walk — the same Header/Summary parent-total logic as
        build_balance_check's aggregate walk, parameterized matcher. Dict
        accesses are plain lookups on parsed report JSON — no DB queries."""
        total = 0.0
        matched: list[dict[str, object]] = []

        def _walk(rows_list):
            nonlocal total
            for row in rows_list:
                header = row["Header"] if "Header" in row else {}
                header_cols = header["ColData"] if "ColData" in header else []
                if len(header_cols) >= 1:
                    first = header_cols[0]
                    h_name = ((first["value"] if "value" in first else "") or "").lower()
                    h_id = first["id"] if "id" in first else ""
                    if match_fn(h_name, h_id):
                        summary = row["Summary"] if "Summary" in row else {}
                        summary_cols = summary["ColData"] if "ColData" in summary else []
                        if len(summary_cols) > target_col_idx:
                            try:
                                cell = summary_cols[target_col_idx]
                                bal = float((cell["value"] if "value" in cell else "0") or "0")
                                total += bal
                                matched.append({
                                    "name": first["value"] if "value" in first else "",
                                    "balance": bal,
                                })
                            except (ValueError, TypeError):
                                pass
                        continue
                col_data = row["ColData"] if "ColData" in row else []
                if len(col_data) > target_col_idx:
                    first = col_data[0]
                    acct_name = ((first["value"] if "value" in first else "") or "").lower()
                    acct_id = first["id"] if "id" in first else ""
                    if match_fn(acct_name, acct_id):
                        try:
                            cell = col_data[target_col_idx]
                            bal = float((cell["value"] if "value" in cell else "0") or "0")
                            total += bal
                            matched.append({
                                "name": first["value"] if "value" in first else "",
                                "balance": bal,
                            })
                        except (ValueError, TypeError):
                            pass
                nested = row["Rows"] if "Rows" in row else {}
                sub_rows = nested["Row"] if "Row" in nested else []
                if sub_rows:
                    _walk(sub_rows)

        _walk(bs_rows)
        return round(total, 2), matched

    contra_groups = []
    for key, g in sorted(groups.items()):
        if key == "__pattern__":
            def match_fn(name: str, acct_id, _patterns=patterns) -> bool:
                return any(p in name for p in _patterns)
        else:
            _gid = g["contra_account_id"]
            _gname = (g["contra_account_name"] or "").lower()

            def match_fn(name: str, acct_id, _gid=_gid, _gname=_gname) -> bool:
                return str(acct_id) == _gid or bool(_gname and _gname in name)
        g_balance, g_matched = _collect_bs_for(match_fn)
        g_delta = round(g["posted_total"] - abs(g_balance), 2)
        contra_groups.append({
            **g,
            "qbo_balance": g_balance,
            "delta": g_delta,
            "in_balance": abs(g_delta) <= TIE_OUT_TOLERANCE,
            "matched_accounts": g_matched,
        })
    return contra_groups


def validate_schedule_balance(
    db: Session,
    realm_id: str,
    schedule_type: str = "PREPAID",
    qbo_client=None,
) -> BalanceCheckPayload:
    """ALIGNED delegate: the standalone tie-out entrypoint — the
    GET /schedule-balance-check endpoint + the assistant's schedule review —
    routes through the ONE implementation above. Compares the QBO-REFLECTED
    schedule side (entry_is_qbo_reflected, AC-T1 plan
    20260716-batch5-remediation) as of the current period to the
    month-columned QBO Balance Sheet; pending / projected recognition is
    reported separately in pending_total / projected_total, never mixed into
    the verdict, and the AC-T2/AC-T3 diagnostics (unstamped_reflected_months,
    header_drift, unconfirmed_history) ride the same payload.

    Returns the balance_check payload: {schedule_total (display basis),
    posted_total (reflected basis), pending_total, projected_total,
    qbo_balance, delta = posted − qbo, in_balance, details, row_count, ...}.
    With no qbo_client, reports the schedule side only (in_balance True —
    nothing to verify against).
    """
    view_period = current_period()
    schedule_side = compute_tie_out_schedule_side(
        db, realm_id, schedule_type, view_period
    )
    balance_check, _bs_context = build_balance_check(
        db,
        realm_id,
        schedule_type,
        qbo_client=qbo_client,
        view_period=view_period,
        schedule_side=schedule_side,
        row_count=schedule_side["row_count"],
        no_client_ok=True,
    )
    return balance_check


# ─────────────────────────────────────────────────────────────────────────────
# Full-life schedule table generation
# (consolidated from the RETIRED app/services/accounting/schedule_engine.py —
#  Wave 3 AC-8, plan 20260701-phase-a-generic-schedules)
#
# These functions own a DIFFERENT job than generate_month_entries above.
# generate_month_entries drafts ONE period's consolidated JE ProposedAction —
# it is the monthly-generation authority (the app/tasks/schedules.py day-1
# beat + the grid's generate button). The functions below pre-populate or
# recompute a schedule's FULL amortization table as plain DRAFT ScheduleEntry
# rows (NO proposals, NO QBO writes) when a schedule is created, approved, or
# has its dates/useful-life overridden (routers/accounting_schedules.py
# create/approve/recompute), plus the shared savepoint-guarded insert helper
# the grid save + assistant month-value paths use. Bodies are byte-identical
# to the retired module (behavior locked by
# tests/test_schedule_amortization_methods.py,
# tests/test_schedule_over_amortization_guard.py,
# tests/test_schedule_amortization_property.py,
# tests/test_schedule_entry_dedupe.py).
# ─────────────────────────────────────────────────────────────────────────────


def add_entry_or_skip(db: Session, **kwargs) -> "ScheduleEntry | None":  # noqa: F821 — lazy model import below
    """
    Insert a ScheduleEntry inside a SAVEPOINT; on IntegrityError (duplicate
    (schedule_id, period) from a concurrent run, or FK violation) roll back
    just the savepoint and return None, leaving the session usable.
    """
    from ..models import ScheduleEntry

    entry = ScheduleEntry(**kwargs)
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
    except IntegrityError:
        logger.info(
            "[ScheduleEngine] Skipped duplicate ScheduleEntry "
            f"(schedule_id={kwargs.get('schedule_id')}, period={kwargs.get('period')})"
        )
        return None
    return entry


def _period_str(d: date) -> str:
    """Convert a date to YYYY-MM period string."""
    return d.strftime("%Y-%m")


def generate_entries(schedule, db: Session) -> list:
    """
    Generate all DRAFT ScheduleEntry rows for a schedule from start_date to end_date.
    Skips periods that already have an entry.
    Returns the list of newly created entries.
    """
    from dateutil.relativedelta import relativedelta

    from ..models import ScheduleEntry

    if not schedule.useful_life_months and not schedule.end_date:
        logger.warning(f"[ScheduleEngine] Schedule {schedule.id} has no useful_life_months or end_date — cannot generate entries")
        return []

    # Compute end_date from useful_life if not set
    end_date = schedule.end_date
    if not end_date and schedule.useful_life_months:
        end_date = schedule.start_date + relativedelta(months=schedule.useful_life_months) - relativedelta(days=1)

    total_months = schedule.useful_life_months or (
        (end_date.year - schedule.start_date.year) * 12 +
        (end_date.month - schedule.start_date.month) + 1
    )
    if total_months <= 0:
        return []

    # Depreciable BASE = original − salvage (Phase-B AC-1; salvage is 0 for
    # non-asset types and pre-Phase-B rows → the legacy full-original base).
    # The whole-life table sums to the BASE, leaving salvage on the books.
    original = Decimal(str(schedule.original_amount)) - Decimal(
        str(salvage_value(schedule))
    )

    # Compute monthly amounts based on method. An unimplemented method raises
    # (ValueError) rather than being computed as some other method.
    from .schedule_types import DEPRECIATION_STRAIGHT_LINE, normalize_depreciation_method

    method = normalize_depreciation_method(schedule.depreciation_method)
    if method == DEPRECIATION_STRAIGHT_LINE:
        monthly_amount = (original / total_months).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        monthly_amount = None  # declining balance — computed per period below

    # Fetch existing entries for this schedule: never regenerate a period that
    # already has one, and decrement the running balance by each period's
    # ACTUAL amount (catch-up imports and manual edits are routinely not
    # straight-line — decrementing by the computed amount instead was the
    # over-amortization bug). SKIPPED periods exist but consume no balance.
    existing_rows = db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id == schedule.id
    ).all()
    existing_amounts = {
        e.period: (Decimal(str(e.amount or 0)) if e.status != "SKIPPED" else Decimal("0"))
        for e in existing_rows
    }

    new_entries: list = []
    # Salvage-aware running pool: the table never generates past the salvage
    # floor (original here is already the depreciable base).
    remaining = original - Decimal(str(schedule.accumulated_amount or 0))
    current = schedule.start_date.replace(day=1)
    period_num = 0
    switched_amount: Decimal | None = None  # declining balance: the held SL month

    while current <= end_date and remaining > Decimal("0.005"):
        period = _period_str(current)
        period_num += 1

        if period in existing_amounts:
            remaining -= existing_amounts[period]
            current += relativedelta(months=1)
            continue

        if period_num == total_months:
            # Last period: plug to remaining so the series sums EXACTLY to
            # the depreciable base (no cent-rounding drift, and no residual
            # left for a post-life true-up).
            amount = remaining
        elif method == DEPRECIATION_STRAIGHT_LINE:
            amount = monthly_amount
        elif switched_amount is not None:
            amount = switched_amount
        else:
            # 200% declining balance on the current book value, switching to
            # straight-line over the remaining periods once that is at least
            # as large (the SL month is then held, the last period plugs) —
            # without the switch the table stops ~11-13% short of the base.
            # The SL month is at least one cent: a sub-cent month rounded to
            # 0.00 stopped a small base or long life short of the base.
            periods_left = Decimal(max(total_months - period_num + 1, 1))
            declining = (remaining * Decimal("2") / Decimal(total_months)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            straight = max(
                (remaining / periods_left).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                Decimal("0.01"),
            )
            if straight >= declining:
                switched_amount = straight
                amount = straight
            else:
                amount = declining

        amount = min(amount, remaining)  # never exceed remaining balance
        if amount <= Decimal("0.005"):
            break

        entry = add_entry_or_skip(
            db,
            schedule_id=schedule.id,
            realm_id=schedule.realm_id,
            period=period,
            amount=float(amount),
            status="DRAFT",
        )
        if entry is not None:
            new_entries.append(entry)

        remaining -= amount
        current += relativedelta(months=1)

    db.commit()
    logger.info(f"[ScheduleEngine] Generated {len(new_entries)} new entries for schedule {schedule.id}")
    return new_entries


def posted_basis_remaining(db: Session, schedule) -> Decimal:
    """The ONE basis every ``remaining_amount`` writer uses:

        original − accumulated (pre-Peregrine) − Σ POSTED entries

    POSTED "-REV" correctives are included (they are recognition too).
    DRAFT / PENDING_APPROVAL entries are unrecognized projections awaiting
    approval — they never reduce the header; mark_entry_posted subtracts
    each one at the moment it posts, which keeps this identity true."""
    from sqlalchemy import func as sa_func

    from ..models import ScheduleEntry

    total_posted = (
        db.query(sa_func.coalesce(sa_func.sum(ScheduleEntry.amount), 0))
        .filter(
            ScheduleEntry.realm_id == schedule.realm_id,
            ScheduleEntry.schedule_id == schedule.id,
            ScheduleEntry.status == "POSTED",
        )
        .scalar()
    )
    return (
        Decimal(str(schedule.original_amount or 0))
        - Decimal(str(schedule.accumulated_amount or 0))
        - Decimal(str(total_posted or 0))
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _reopen_if_balance_remains(schedule, remaining: Decimal) -> None:
    """A FULLY_AMORTIZED schedule whose posted-basis remaining is back above
    its floor (a corrected header, an edited posted month) has balance left
    to amortize — return it to ACTIVE so the monthly engine sees it again.
    Sign-aware: a negative credit-adjustment schedule is incomplete while its
    remaining is still below zero. DISPOSED / other states are left alone."""
    if schedule.status != "FULLY_AMORTIZED":
        return
    if float(schedule.original_amount or 0) >= 0:
        still_open = remaining - Decimal(str(salvage_value(schedule))) > Decimal("0.005")
    else:
        still_open = remaining < Decimal("-0.005")
    if still_open:
        schedule.status = "ACTIVE"


def refresh_remaining_from_posted(db: Session, schedule) -> Decimal:
    """Rewrite ``schedule.remaining_amount`` on the posted basis (no commit)
    and reopen a wrongly-completed schedule. Returns the new remaining."""
    remaining = posted_basis_remaining(db, schedule)
    schedule.remaining_amount = float(remaining)
    _reopen_if_balance_remains(schedule, remaining)
    return remaining


def recompute(schedule, db: Session) -> list:
    """
    Delete all DRAFT/PENDING_APPROVAL entries and regenerate.
    POSTED entries are preserved (they're already in QBO).
    """
    from ..models import ScheduleEntry

    deleted = db.query(ScheduleEntry).filter(
        ScheduleEntry.schedule_id == schedule.id,
        ScheduleEntry.status.in_(["DRAFT", "PENDING_APPROVAL"]),
    ).delete(synchronize_session=False)
    db.commit()
    logger.info(f"[ScheduleEngine] Deleted {deleted} unposted entries before recompute for schedule {schedule.id}")

    # Recompute remaining_amount from posted entries AND the pre-Peregrine
    # accumulated_amount (AC-T3, plan 20260716-batch5-remediation): the
    # legacy form dropped accumulated, restating an imported schedule's
    # remaining UP by its pre-Peregrine recognition on every recompute —
    # generate_entries below already seeds its pool with the same subtraction.
    refresh_remaining_from_posted(db, schedule)
    db.commit()

    return generate_entries(schedule, db)


def mark_fully_amortized(schedule, db: Session) -> None:
    """Check if schedule is complete and update its status.

    Does NOT zero out remaining_amount: a negative remaining means the
    schedule is over-amortized and must stay visible as such.
    """
    remaining = Decimal(str(schedule.remaining_amount))
    # Salvage-aware floor: a positive schedule is complete AT salvage.
    if remaining - Decimal(str(salvage_value(schedule))) <= Decimal("0.01"):
        schedule.status = "FULLY_AMORTIZED"
        db.commit()
        logger.info(f"[ScheduleEngine] Schedule {schedule.id} marked FULLY_AMORTIZED")


# ─────────────────────────────────────────────────────────────────────────────
# Prospective useful-life change (ASC 250 change-in-estimate)
# Phase-B AC-3, plan 20260702-phase-b-fa-lifecycle
# ─────────────────────────────────────────────────────────────────────────────


def apply_prospective_life_change(
    db: Session,
    schedule,
    *,
    new_useful_life_months: int,
    effective_period: str,  # YYYY-MM — the FIRST period the new run-rate applies to
    actor: str | None = None,
) -> Dict[str, Any]:
    """
    ASC 250 change in accounting estimate: PROSPECTIVE ONLY. Depreciation
    already recognized (POSTED entries, and any period before
    ``effective_period``) is NEVER restated; from ``effective_period`` forward
    each month carries

        remaining depreciable base ÷ remaining new life

    where remaining base = (original − salvage) − accumulated_amount −
    Σ(non-SKIPPED entries with period < effective_period), and remaining new
    life = new_useful_life_months − months elapsed from the schedule start
    month to ``effective_period`` (exclusive).

    Mechanics: DRAFT rows with period ≥ effective_period are deleted and
    rewritten at the new run-rate with a final-month plug (Decimal math, the
    series lands the balance exactly at the salvage floor). The CALLER (the
    router) is responsible for the guards — effective period unlocked, no
    POSTED / PENDING_APPROVAL / "-REV" rows at or after effective_period —
    this function re-checks the fatal ones and raises ValueError as the
    fail-closed backstop. An audit row (SCHEDULE_LIFE_CHANGED) records
    old/new life, effective period, and the actor.

    Returns a summary dict for the API response.
    """
    from dateutil.relativedelta import relativedelta

    from ..models import ScheduleEntry
    from . import audit

    if len(effective_period) != 7 or effective_period[4] != "-":
        raise ValueError("effective_period must be YYYY-MM")
    if new_useful_life_months <= 0:
        raise ValueError("new_useful_life_months must be positive")

    start_month = date(schedule.start_date.year, schedule.start_date.month, 1)
    eff = date(int(effective_period[:4]), int(effective_period[5:7]), 1)
    if eff < start_month:
        raise ValueError("effective_period is before the schedule starts")

    elapsed_months = (eff.year - start_month.year) * 12 + (eff.month - start_month.month)
    remaining_life = new_useful_life_months - elapsed_months

    entries = (
        db.query(ScheduleEntry)
        .filter(
            ScheduleEntry.schedule_id == schedule.id,
            ScheduleEntry.realm_id == schedule.realm_id,
        )
        .all()
    )

    # Fail-closed backstop for the never-restate invariant: nothing POSTED /
    # in-flight (or any live "-REV" correction) may sit at/after the effective
    # period. The router 409s these with a teaching message first.
    for e in entries:
        base_period = e.period[:7]
        if base_period >= effective_period and e.status not in ("DRAFT", "SKIPPED"):
            raise ValueError(
                f"entry {e.id} ({e.period}, {e.status}) is at/after the effective "
                f"period — posted/in-flight periods are never restated (ASC 250)"
            )
        if (
            base_period >= effective_period
            and e.period.endswith("-REV")
            and e.status != "SKIPPED"
        ):
            raise ValueError(
                f"pending correction entry {e.id} ({e.period}) is at/after the "
                f"effective period — resolve it before changing the life"
            )

    # Remaining depreciable base at the effective period: base minus
    # everything consumed BEFORE it (POSTED and still-DRAFT pre-effective
    # months both stand — prospective means the past is taken as-is).
    consumed = sum(
        Decimal(str(e.amount or 0))
        for e in entries
        if e.period[:7] < effective_period and e.status != "SKIPPED"
    )
    base = Decimal(str(schedule.original_amount or 0)) - Decimal(
        str(salvage_value(schedule))
    )
    remaining_base = base - Decimal(str(schedule.accumulated_amount or 0)) - consumed

    if remaining_life <= 0 and abs(remaining_base) > Decimal("0.005"):
        raise ValueError(
            "new useful life is not longer than the months already elapsed and a "
            "depreciable balance remains — the new life must extend past the "
            "effective period"
        )

    # Rewrite the future: delete plain DRAFT rows ≥ effective, regenerate.
    old_life = schedule.useful_life_months
    deleted = 0
    for e in entries:
        if (
            e.period[:7] >= effective_period
            and e.status == "DRAFT"
            and not e.period.endswith("-REV")
        ):
            db.delete(e)
            deleted += 1
    db.flush()

    new_monthly = (
        (remaining_base / remaining_life).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if remaining_life > 0
        else Decimal("0")
    )

    created = 0
    pool = remaining_base
    cursor = eff
    for i in range(max(remaining_life, 0)):
        amount = new_monthly if i < remaining_life - 1 else pool  # final-month plug
        # Same-direction cap: never step the pool past the floor.
        if base >= 0:
            amount = min(amount, pool)
        else:
            amount = max(amount, pool)
        if abs(amount) <= Decimal("0.005"):
            break
        entry = add_entry_or_skip(
            db,
            schedule_id=schedule.id,
            realm_id=schedule.realm_id,
            period=_period_str(cursor),
            amount=float(amount),
            status="DRAFT",
        )
        if entry is not None:
            created += 1
            pool -= amount
        cursor += relativedelta(months=1)

    schedule.useful_life_months = new_useful_life_months
    schedule.end_date = schedule.start_date + relativedelta(
        months=new_useful_life_months
    ) - relativedelta(days=1)
    if remaining_life <= 0 and abs(remaining_base) <= Decimal("0.005"):
        # The shortened life exactly zeroes out at the effective period.
        schedule.status = "FULLY_AMORTIZED"

    audit.log_action(
        db=db,
        realm_id=schedule.realm_id,
        action="SCHEDULE_LIFE_CHANGED",
        entity_type="accounting_schedule",
        entity_id=str(schedule.id),
        details={
            "old_useful_life_months": old_life,
            "new_useful_life_months": new_useful_life_months,
            "effective_period": effective_period,
            "remaining_depreciable_base": float(remaining_base),
            "remaining_life_months": remaining_life,
            "new_monthly_amount": float(new_monthly),
            "draft_entries_deleted": deleted,
            "draft_entries_created": created,
            "actor": actor,
        },
    )
    db.commit()

    logger.info(
        "[ScheduleEngine] Prospective life change on schedule %s (%s): %s → %s months "
        "effective %s; new monthly %s over %s remaining months",
        schedule.id,
        schedule.name,
        old_life,
        new_useful_life_months,
        effective_period,
        new_monthly,
        remaining_life,
    )
    return {
        "schedule_id": schedule.id,
        "old_useful_life_months": old_life,
        "new_useful_life_months": new_useful_life_months,
        "effective_period": effective_period,
        "remaining_depreciable_base": round(float(remaining_base), 2),
        "remaining_life_months": remaining_life,
        "new_monthly_amount": float(new_monthly),
        "draft_entries_deleted": deleted,
        "draft_entries_created": created,
        "status": schedule.status,
    }

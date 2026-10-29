"""Per-tenant, per-task autonomy state + the auto-execute gate (Phase 2 PR2).

(Distinct from the legacy, unwired ``autonomy.py`` Autopilot cycle.) The
autonomy *level* for a (tenant, task) decides how AI output is handled:

- ``proposal_first`` — human approves everything (the classic path).
- ``shadow`` — measure would-be-auto but still route everything to the human.
  This is the default except for the routine internal-write lane, whose
  default is ``proposal_first`` (a tenant admin must opt in explicitly).
- ``supervised_l3`` — gated auto-execution for reversible actions within caps.

``gate_decision`` is the single chokepoint for auto-execution: it returns
``auto=True`` only when EVERY condition holds (level is supervised_l3, calibrated
confidence meets the threshold, the action is reversible and not in a HARD-locked
period, and we're within the caps). Otherwise the existing proposal-first path
runs unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

# PLAN-EXEMPT: B5-8 P1 field bug (live-prod-corroborated 2026-06-24) — make the period
# lock cascade (lock-as-of) and add a writable-guard reused by the schedule write paths.
from sqlalchemy import func as sa_func, or_ as sa_or
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AICorrectionEvent, FinancialCalendar, TenantTaskAutonomy
from ..tenant_rls import tenant_scope_for_realm
from .confidence_calibration import calibrated_confidence
from .errors import user_safe_http_exception
from .redaction import redact_to_string

logger = logging.getLogger(__name__)

DEFAULT_LEVEL = "shadow"
LEVELS = ("proposal_first", "shadow", "supervised_l3")

# Tier-3 autonomy lane: the task_type under which INTERNAL_ROUTINE ProposedActions
# (app/services/action_tiers.py) consult this gate. The ABSENCE of a TenantTaskAutonomy
# row reads as ``proposal_first`` (OFF): routine internal-DB drafts wait for a human
# unless a tenant admin opted in by setting the row's level to ``supervised_l3``
# (``set_autonomy`` / PUT task-autonomy). Even then the auto lane re-checks the
# drafting user's role (action_tiers.ROUTINE_ACTION_REQUIRED_ROLES).
INTERNAL_ROUTINE_TASK_TYPE = "internal_routine_write"
INTERNAL_ROUTINE_DEFAULT_LEVEL = "proposal_first"

# Task types whose actions are reversible enough to be auto-execution candidates.
_REVERSIBLE_TASK_TYPES = frozenset({"categorization", "reclassification"})


def get_autonomy(db: Session, organization_id: int, task_type: str) -> Optional[TenantTaskAutonomy]:
    return (
        db.query(TenantTaskAutonomy)
        .filter(
            TenantTaskAutonomy.organization_id == organization_id,
            TenantTaskAutonomy.task_type == task_type,
        )
        .first()
    )


def effective_policy(db: Session, organization_id: int, task_type: str) -> dict[str, Any]:
    """Describe policy, without evaluating eligibility of a particular action."""
    row = get_autonomy(db, organization_id, task_type)
    default = INTERNAL_ROUTINE_DEFAULT_LEVEL if task_type == INTERNAL_ROUTINE_TASK_TYPE else DEFAULT_LEVEL
    return {"level": row.level if row is not None else default,
            "origin": "explicit" if row is not None else "default"}


def effective_level(db: Session, organization_id: int, task_type: str) -> str:
    return effective_policy(db, organization_id, task_type)["level"]


def effective_threshold(db: Session, organization_id: int, task_type: str) -> float:
    """Per-tenant threshold override, falling back to the config default."""
    row = get_autonomy(db, organization_id, task_type)
    if row is not None and row.confidence_threshold is not None:
        return float(row.confidence_threshold)
    return settings.autonomy_categorization_confidence_threshold


def set_autonomy(
    db: Session,
    *,
    organization_id: int,
    task_type: str,
    level: str,
    confidence_threshold: Optional[float] = None,
    updated_by: Optional[str] = None,
    commit: bool = True,
) -> TenantTaskAutonomy:
    """Upsert the autonomy level/threshold for a (tenant, task). Validates level."""
    if level not in LEVELS:
        raise ValueError(f"invalid autonomy level: {level!r}")
    row = get_autonomy(db, organization_id, task_type)
    if row is None:
        row = TenantTaskAutonomy(organization_id=organization_id, task_type=task_type)
        db.add(row)
    row.level = level
    if confidence_threshold is not None:
        row.confidence_threshold = confidence_threshold
    row.updated_by = updated_by
    if commit:
        db.commit()
        db.refresh(row)
    return row


# PLAN-EXEMPT: B5-8 P1 field bug (live-prod-corroborated 2026-06-24); B3-1
# (plan 20260703-qa-batch3) split HARD lock from SOFT close — see docstring.
def latest_locked_period(db: Session, *, realm_id: str, hard_only: bool = False) -> str | None:
    """The most recent locked/closed period (``YYYY-MM``) for the realm, or None —
    the LOCK FLOOR.

    Locking is stored per-period (``FinancialCalendar``), but locking is meant to be
    lock-AS-OF: locking May implicitly closes every EARLIER period too. This returns the
    max locked/closed period so callers can treat any period ``<= floor`` as locked.

    TWO lock notions exist and must not be conflated (B3-1, roots B2-6 —
    prod-corroborated 2026-07-02: the month-rollover beat
    ``accounting.run_month_end_workflow`` sets ``status='CLOSED'`` on the prior
    month for EVERY realm, unattended, ``locked`` stays false):

    - **HARD lock** (``hard_only=True``): ``locked == True`` (the break-glass
      human lock, ``POST /accounting/calendar/{period}/lock``) or a literal
      ``status='LOCKED'``. This is the EDITABILITY floor — the only thing that
      may refuse a human's schedule-grid write. The close cockpit's own
      "period_locked" reads (close_tasks.py) use exactly this bit.
    - **SOFT close** (``hard_only=False``, default): additionally counts
      ``status='CLOSED'`` — the observational auto-close. Conservative floor
      for the AI proposal/auto-execution gates (``period_is_closed``): never
      propose into a month a close workflow has swept, even if no human locked it.

    Reads under an explicit realm pin (``financial_calendar`` is realm-keyed under
    FORCE RLS — an unscoped read fails OPEN to zero rows). Raises on read error; callers
    that must fail closed (``period_is_closed`` / ``assert_period_writable``) catch it.
    """
    if not realm_id:
        return None
    statuses = ["LOCKED"] if hard_only else ["CLOSED", "LOCKED"]
    with tenant_scope_for_realm(db, realm_id):
        return (
            db.query(sa_func.max(FinancialCalendar.period))
            .filter(
                FinancialCalendar.realm_id == realm_id,
                sa_or(
                    FinancialCalendar.locked == True,  # noqa: E712
                    sa_func.upper(sa_func.coalesce(FinancialCalendar.status, "")).in_(statuses),
                ),
            )
            .scalar()
        )


def _period_label(period: str) -> str:
    """``2026-06`` → ``Jun 2026`` (falls back to the raw string on any malformation)."""
    try:
        import calendar as _cal

        return f"{_cal.month_abbr[int(period[5:7])]} {int(period[:4])}"
    except (ValueError, IndexError):
        return period


# PLAN-EXEMPT: B2-6 P1 field bug (live-prod-corroborated 2026-07-02, plan
# 20260702-qa-batch2) — a HARD-lock floor for the schedules GRID that treats
# soft-CLOSED as editable.
def latest_hard_locked_period(db: Session, *, realm_id: str) -> str | None:
    """The most recent HARD-locked period (``YYYY-MM``) for the realm, or None —
    the GRID's edit-lock floor.

    Distinct from :func:`latest_locked_period`'s default SOFT floor (the JE-WRITE
    lock floor, which also counts soft ``status='CLOSED'``). A period auto-rolls
    to ``status='CLOSED', locked=false`` at month rollover across ~20 realms —
    that soft-close means "books rolled forward," NOT "editing forbidden." The
    schedules grid must let a user still adjust a soft-CLOSED month (the B2-6
    bug: June soft-CLOSED cascaded edit-locked). So this floor counts ONLY a
    HARD lock:

        ``locked = TRUE``  OR  ``status = 'LOCKED'``

    — an explicit, intentional period lock — and deliberately NOT
    ``status='CLOSED'``. Same lock-AS-OF cascade semantics (locking May hard-
    locks every earlier month) and the same realm-pin + fail-on-error contract
    as ``latest_locked_period`` — it IS that query with ``hard_only=True`` (one
    implementation, two named floors; the merge reconciled the two
    independently-landed spellings). Backs BOTH the schedules grid ``is_locked``
    AND the schedule/lease edit guard ``assert_period_writable`` (directly, and
    via ``period_is_hard_locked``) — edits are gated on the HARD lock only. As of
    D-B6-1 (2026-07-16) the JE/QBO-EXECUTION guard (qbo_actions/executor.py) gates
    on this SAME hard-lock floor via ``period_is_hard_locked`` — a soft-CLOSED month
    is books-rolled, not write-blocked. (The soft ``period_is_closed`` floor now
    backs only the proactive accrual-proposal gate; the DISPLAY/alerts readers read
    ``latest_locked_period(hard_only=False)`` directly.)
    """
    return latest_locked_period(db, realm_id=realm_id, hard_only=True)


# PLAN-EXEMPT: B5-8 P1 field bug (live-prod-corroborated 2026-06-24) wrote this guard;
# B2-6 (plan 20260702-qa-batch2, Steven 2026-07-02) re-floored it to the HARD lock only;
# B3-1 (plan 20260703-qa-batch3) added the explicit fail-CLOSED verification branch and
# the full two-variant lock teaching message.
def assert_period_writable(db: Session, *, realm_id: str, period: str) -> None:
    """Raise 409 ``period_locked`` if ``period`` is on or before the HARD-lock floor.

    The single guard the schedule/lease write paths (save/reset/generate/edit/import/
    disposal/life-change + lease commencement/effective/termination) call before mutating a
    period. Gates on the HARD-lock floor (``latest_hard_locked_period``: ``locked=TRUE`` /
    ``status='LOCKED'`` only, lock-as-of cascade — the same floor ``period_is_hard_locked``
    reads) — NOT soft ``status='CLOSED'``, which the month-rollover auto-workflow sets and
    which must NOT block editing (B2-6; Steven 2026-07-02: a month is closed only by
    explicit team action). Fail-CLOSED, and EXPLICITLY so (B3-1): an indeterminate calendar
    read is refused with its own "couldn't be verified" 409 rather than silently allowing
    the write — belt-and-braces over ``period_is_hard_locked``'s swallow-to-locked contract,
    and it keeps a read error distinguishable from a genuine lock. As of D-B6-1
    (2026-07-16) the JE/QBO-EXECUTION guard (``period_is_hard_locked``,
    qbo_actions/executor.py) gates on this SAME hard-lock floor — a schedule EDIT and
    its resulting JE are both refused only by an explicit hard lock; a soft-CLOSED
    month stays editable AND postable (books-rolled, not write-blocked).
    """
    try:
        floor = latest_hard_locked_period(db, realm_id=realm_id)
        locked = floor is not None and period <= floor
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED on ANY read error
        logger.warning(
            "assert_period_writable calendar read failed for realm; failing closed: %s",
            redact_to_string(exc),
        )
        raise user_safe_http_exception(
            status_code=409,
            error_code="period_locked",
            user_message=(
                "The period-lock state couldn't be verified, so this edit was refused "
                "to protect closed books. Retry in a moment; if it persists, contact support."
            ),
        )
    if locked:
        if period == floor:
            why = f"{_period_label(period)} was closed and locked"
        else:
            why = (
                f"{_period_label(floor)} is locked, and locking a month locks every "
                f"earlier month with it (lock-as-of), so {_period_label(period)} is locked too"
            )
        raise user_safe_http_exception(
            status_code=409,
            error_code="period_locked",
            user_message=(
                f"{_period_label(period)} can't be edited: {why}. "
                "Locked months are part of the closed books, so this change was not saved. "
                "To fix an amount that belongs in a locked month, enter the corrective "
                "amount in an open month instead — the schedule keeps the running balance "
                "tied out. An admin can break-glass unlock the period if the books truly "
                "need restating."
            ),
        )


def period_is_closed(
    db: Session, *, realm_id: str, when: datetime | None = None, period: str | None = None
) -> bool:
    """The SOFT close floor predicate for ``realm_id`` — ``locked`` OR ``status``
    CLOSED/LOCKED.

    The authoritative lock is ``FinancialCalendar`` keyed by ``(realm_id,
    period="YYYY-MM")`` (set via ``POST /accounting/calendar/{period}/lock``,
    accounting.py:431); a period is SOFT-closed when the row is ``locked`` OR its
    ``status`` is CLOSED/LOCKED — the latter set unattended by the month-rollover
    auto-workflow. As of D-B6-1 (2026-07-16) this SOFT floor NO LONGER gates any
    WRITE/EXECUTE/proposal path — every one of those swapped to
    ``period_is_hard_locked`` (a soft-CLOSED month is books-rolled, not
    write-blocked). Its one remaining production caller is the proactive
    accrual-proposal gate (tasks/proactive.py); the DISPLAY/alerts readers read
    ``latest_locked_period(hard_only=False)`` directly. For any write gate use
    ``period_is_hard_locked``, never this.

    FAIL-CLOSED, not fail-open. ``financial_calendar`` is in the FORCE-RLS allowlist
    (migration e7f1a9c3d2b5:82), so an UNPINNED read returns ZERO rows → would read
    as "not locked" → the gate would propose into a LOCKED period (the capstone's
    worst failure). So the read runs under an EXPLICIT realm scope, and every
    indeterminate path (no realm, read error) returns ``True`` (treat as locked).
    A genuinely-absent calendar row — seen UNDER the pinned scope — is OPEN (False):
    not a fail-open, because the scope-pin guarantees a real lock would be visible.

    ``period`` (``YYYY-MM``) wins; else it is derived from ``when``. With neither
    (no temporal context) the period is unresolvable → OPEN (False); the close
    driver always passes a period, so only the proactive gate's no-date path hits it.
    """
    # No realm → cannot resolve a lock at all. Fail CLOSED, not open.
    if not realm_id:
        return True
    if period is None and when is not None:
        period = when.strftime("%Y-%m")
    if period is None:
        return False
    try:
        # Read under an EXPLICIT realm PIN (NOT bypass): financial_calendar is realm-keyed
        # (e7f1a9c3d2b5:82), so tenant_scope_for_realm pins app.current_realm_id and the
        # policy admits the locked row — least-privilege, no cross-tenant bypass. An
        # unscoped read would fail-OPEN (zero rows → "not locked"); the realm pin closes
        # that. Restores the ratified C1 design a later merge reconciled to bypass=True.
        # PLAN-EXEMPT: B5-8 — lock-as-of CASCADE: a period is closed when it is on or
        # before the latest locked/closed period (locking May implicitly locks Feb).
        floor = latest_locked_period(db, realm_id=realm_id)
        return floor is not None and period <= floor
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED on ANY error (see docstring)
        # An exception here is indeterminate, not "open". Returning False would be a
        # fail-OPEN that lets the gate propose into a possibly-locked period.
        logger.warning(
            "period_is_closed read failed for realm; failing closed (locked): %s",
            redact_to_string(exc),
        )
        return True


def period_is_hard_locked(
    db: Session, *, realm_id: str, when: datetime | None = None, period: str | None = None
) -> bool:
    """Whether ``period`` is HARD-locked (an explicit, intentional lock) for ``realm_id``.

    The WRITE/EXECUTE-guard predicate. Counts ONLY a HARD lock (``locked=TRUE`` OR
    ``status='LOCKED'``, via ``latest_hard_locked_period``) — the deliberate
    ``POST /accounting/calendar/{period}/lock``. As of D-B6-1 (2026-07-16) this is the
    single WRITE floor: JE/QBO EXECUTION (qbo_actions/executor.py), the reclassify push
    (classification.py), agent/close/lifecycle/review-check proposal generation, the
    tier-3 auto-execute gate, AND schedule/lease EDITS all gate on it. Its sibling
    ``period_is_closed`` also counts soft ``status='CLOSED'`` (the month-rollover
    auto-workflow's "books rolled forward", NOT "editing forbidden") and now backs ONLY
    the proactive accrual-proposal gate — so a soft-CLOSED (auto-rolled) month stays
    editable AND postable (B2-6; Steven 2026-07-02: a month is closed only by explicit
    team action, never at rollover).

    Same FAIL-CLOSED contract as ``period_is_closed``: no realm / an unresolvable period is
    handled identically, and any read error returns ``True`` (treat as locked).
    """
    if not realm_id:
        return True
    if period is None and when is not None:
        period = when.strftime("%Y-%m")
    if period is None:
        return False
    try:
        # PLAN-EXEMPT: B2-6 (Steven 2026-07-02) — HARD-lock-only edit gate, lock-AS-OF cascade.
        floor = latest_hard_locked_period(db, realm_id=realm_id)
        return floor is not None and period <= floor
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED on ANY error (mirror period_is_closed)
        logger.warning(
            "period_is_hard_locked read failed for realm; failing closed (locked): %s",
            redact_to_string(exc),
        )
        return True


def is_reversible(db: Session, *, task_type: str, realm_id: str, when: Optional[datetime] = None) -> bool:
    """Reclassification/categorization are reversible — unless the period is HARD-locked
    (D-B6-1, 2026-07-16: the WRITE/auto-execute floor is the explicit hard lock; a
    soft-CLOSED month is books-rolled, not write-blocked)."""
    if task_type not in _REVERSIBLE_TASK_TYPES:
        return False
    if period_is_hard_locked(db, realm_id=realm_id, when=when):
        return False
    return True


def autoexec_count_today(db: Session, *, organization_id: int, task_type: str) -> int:
    """Count auto-executed corrections for this tenant/task so far today (UTC).

    Auto-executed rows are written by the engine (actor_user_id IS NULL) and
    marked would_autoexecute=True; used to enforce the per-run cap.
    """
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(AICorrectionEvent)
        .filter(
            AICorrectionEvent.organization_id == organization_id,
            AICorrectionEvent.task_type == task_type,
            AICorrectionEvent.would_autoexecute.is_(True),
            AICorrectionEvent.actor_user_id.is_(None),
            AICorrectionEvent.created_at >= day_start,
        )
        .count()
    )


def gate_decision(
    db: Session,
    *,
    organization_id: int,
    task_type: str,
    realm_id: str,
    raw_confidence: Optional[float],
    when: Optional[datetime] = None,
    per_action_count: int = 1,
) -> dict[str, Any]:
    """Decide whether one categorization action may auto-execute.

    ``auto=True`` only when ALL hold: level is supervised_l3, within the per-
    action cap, the action is reversible and not in a closed period, the
    calibrated confidence is calibrated and >= the tenant's threshold, and we're
    within the per-run cap. Otherwise ``auto=False`` with a ``reason`` and the
    existing proposal-first path runs unchanged.

    For the tier-3 lane, ``task_type == INTERNAL_ROUTINE_TASK_TYPE`` branches to the
    routine-write gate — OFF unless the tenant opted in (a TenantTaskAutonomy row at
    ``supervised_l3``; no row reads as ``proposal_first``), the period guard honored
    via the fail-closed ``period_is_hard_locked`` predicate (D-B6-1, 2026-07-16 — the
    WRITE/auto-execute floor is the explicit hard lock; a soft-CLOSED month is
    books-rolled, not write-blocked). Confidence calibration does not apply to
    this lane (routine drafts carry a fixed proposer score; the material-vs-
    routine split IS the risk decision, made in the frozen tier map, and the
    caller re-checks the drafting user's role). Every other task_type keeps the
    existing behavior byte-for-byte.
    """
    policy = effective_policy(db, organization_id, task_type)
    level = policy["level"]
    if task_type == INTERNAL_ROUTINE_TASK_TYPE:
        if level != "supervised_l3":
            reason = "not_enabled" if policy["origin"] == "default" else "tenant_opt_out"
            return {"auto": False, "reason": reason, "level": level, "origin": policy["origin"]}
        # Hard-lock guard: never auto-execute into a HARD-locked CURRENT period
        # (D-B6-1, 2026-07-16 — a soft-CLOSED month is books-rolled, not write-blocked;
        # the write floor is the explicit hard lock only). Payload-derived target
        # periods are additionally refused by the execution core's hard-lock gate
        # (gate_refusal_reason, internal_action_execution.py — already hard).
        if period_is_hard_locked(
            db, realm_id=realm_id, when=when or datetime.now(timezone.utc)
        ):
            return {"auto": False, "reason": "closed_period", "level": level, "origin": policy["origin"]}
        return {"auto": True, "reason": "ok", "level": level, "origin": policy["origin"]}

    if level != "supervised_l3":
        return {"auto": False, "reason": "not_l3", "level": level, "origin": policy["origin"]}

    if per_action_count > settings.autonomy_max_autoexec_per_action:
        return {"auto": False, "reason": "action_cap", "level": level, "origin": policy["origin"]}

    if not is_reversible(db, task_type=task_type, realm_id=realm_id, when=when):
        return {"auto": False, "reason": "not_reversible", "level": level, "origin": policy["origin"]}

    threshold = effective_threshold(db, organization_id, task_type)
    cal = calibrated_confidence(
        db,
        organization_id=organization_id,
        raw_score=raw_confidence,
        task_type=task_type,
    )
    if cal["uncalibrated"] or cal["calibrated"] is None or cal["calibrated"] < threshold:
        return {
            "auto": False,
            "reason": "below_threshold",
            "level": level, "origin": policy["origin"],
            "calibrated": cal["calibrated"],
            "uncalibrated": cal["uncalibrated"],
            "threshold": threshold,
        }

    if autoexec_count_today(db, organization_id=organization_id, task_type=task_type) >= settings.autonomy_max_autoexec_per_run:
        return {"auto": False, "reason": "run_cap", "level": level, "origin": policy["origin"]}

    return {
        "auto": True,
        "reason": "ok",
        "level": level, "origin": policy["origin"],
        "calibrated": cal["calibrated"],
        "threshold": threshold,
    }

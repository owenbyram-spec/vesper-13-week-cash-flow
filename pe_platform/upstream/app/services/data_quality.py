# Plan 20260712-agentic-finance-os W6 (GAP-9, directive §24) — data-quality reports.
"""A typed report of the quality + coverage of a company's synced data, so a
workflow knows whether it may continue, must disclose limitations, should refresh,
or must stop — and so the final response NEVER presents partial/stale data as a
complete current-period conclusion.

Reads the SyncEvent ledger (per-company, per-period, status PENDING|PROCESSING|
DONE|FAILED|SUPERSEDED). A period is COVERED when its latest event is DONE; STALE
when the most recent DONE is older than the freshness horizon; FAILED when the
latest event failed. All deterministic — no model involvement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import SyncEvent

# A DONE sync older than this is treated as stale for a current-period conclusion.
_DEFAULT_STALE_AFTER_HOURS = 48

# Report-level sync status (worst-case across the requested scope).
SYNC_OK = "ok"           # fresh DONE coverage
SYNC_STALE = "stale"     # DONE but older than the freshness horizon
SYNC_MISSING = "missing"  # no DONE sync for a required period
SYNC_FAILED = "failed"   # the latest sync failed
SYNC_IN_PROGRESS = "in_progress"  # a sync is PENDING/PROCESSING (computing)

# Planning dispositions (directive §24).
CONTINUE = "continue"
CONTINUE_WITH_LIMITATIONS = "continue_with_limitations"
REFRESH = "refresh"
ESCALATE = "escalate"
STOP = "stop"


@dataclass(frozen=True)
class DataQualityReport:
    company_id: str
    as_of: datetime
    sync_status: str                       # SYNC_* — worst-case across the scope
    latest_sync_at: datetime | None
    covered_periods: tuple[str, ...]        # periods with a fresh DONE sync
    stale_periods: tuple[str, ...]          # DONE but older than the horizon
    missing_periods: tuple[str, ...]        # required but never DONE
    failed_periods: tuple[str, ...]         # latest event FAILED
    in_progress_periods: tuple[str, ...]    # latest event PENDING/PROCESSING
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_fresh(self) -> bool:
        completed = _aware(self.latest_sync_at)
        return bool(self.sync_status == SYNC_OK and self.covered_periods and completed
                    and completed <= _aware(self.as_of) and not (self.stale_periods or self.missing_periods
                    or self.failed_periods or self.in_progress_periods))

    def to_payload(self) -> dict:  # CRAFT-OK: data-quality report DTO — explicit typed fields below
        return {
            "company_id": self.company_id,
            "as_of": self.as_of.isoformat(),
            "sync_status": self.sync_status,
            "latest_sync_at": self.latest_sync_at.isoformat() if self.latest_sync_at else None,
            "covered_periods": list(self.covered_periods),
            "stale_periods": list(self.stale_periods),
            "missing_periods": list(self.missing_periods),
            "failed_periods": list(self.failed_periods),
            "in_progress_periods": list(self.in_progress_periods),
            "issues": list(self.issues),
            "is_fresh": self.is_fresh,
        }


def _aware(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def build_data_quality_report(
    db: Session,
    company_id: str,
    *,
    required_periods: list[str] | None = None,
    stale_after_hours: int = _DEFAULT_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> DataQualityReport:
    """The data-quality report for a company. ``required_periods`` (YYYY-MM) are the
    periods the workflow needs; any without a fresh DONE sync becomes missing/stale/
    failed/in-progress. With no required_periods, the report reflects whatever synced
    periods exist (coverage-only)."""
    now = _aware(now) or datetime.now(timezone.utc)
    horizon = now.timestamp() - stale_after_hours * 3600

    required = list(dict.fromkeys(required_periods or []))
    query = db.query(SyncEvent).filter(SyncEvent.company_id == company_id)
    if required:
        query = query.filter(SyncEvent.period.in_(required))
    events = query.order_by(SyncEvent.period, SyncEvent.created_at, SyncEvent.id).all()
    # Latest event per period (events already ordered by created_at asc → last wins).
    latest_by_period: dict[str, SyncEvent] = {}
    for ev in events:
        latest_by_period[ev.period] = ev

    covered: list[str] = []
    stale: list[str] = []
    failed: list[str] = []
    in_progress: list[str] = []
    latest_done_at: datetime | None = None
    missing: list[str] = []

    for period, ev in latest_by_period.items():
        status = (ev.status or "").upper()
        completed = _aware(ev.completed_at)
        if status == "DONE":
            if completed is None or completed > now:
                missing.append(period)
                continue
            if completed.timestamp() >= horizon:
                covered.append(period)
            else:
                stale.append(period)
            if completed and (latest_done_at is None or completed > latest_done_at):
                latest_done_at = completed
        elif status == "FAILED":
            failed.append(period)
        elif status in ("PENDING", "PROCESSING"):
            in_progress.append(period)
        else:
            missing.append(period)

    missing.extend(p for p in required if p not in latest_by_period)
    # A required period whose latest event isn't a fresh DONE is a gap too.
    for p in required:
        if p in stale or p in failed or p in in_progress:
            continue
        if p not in covered and p not in missing:
            missing.append(p)

    issues: list[str] = []
    if not events:
        issues.append("No completed source synchronization evidence is available.")
    if missing:
        issues.append(f"no completed sync for period(s): {', '.join(sorted(set(missing)))}")
    if failed:
        issues.append(f"latest sync FAILED for period(s): {', '.join(sorted(set(failed)))}")
    if stale:
        issues.append(f"sync is stale (> {stale_after_hours}h) for period(s): {', '.join(sorted(set(stale)))}")
    if in_progress:
        issues.append(f"sync in progress for period(s): {', '.join(sorted(set(in_progress)))}")

    # Worst-case status across the scope (failed > missing > in_progress > stale > ok).
    if failed:
        status = SYNC_FAILED
    elif missing or not events:
        status = SYNC_MISSING
    elif in_progress:
        status = SYNC_IN_PROGRESS
    elif stale:
        status = SYNC_STALE
    else:
        status = SYNC_OK

    return DataQualityReport(
        company_id=company_id,
        as_of=now,
        sync_status=status,
        latest_sync_at=latest_done_at,
        covered_periods=tuple(sorted(set(covered))),
        stale_periods=tuple(sorted(set(stale))),
        missing_periods=tuple(sorted(set(missing))),
        failed_periods=tuple(sorted(set(failed))),
        in_progress_periods=tuple(sorted(set(in_progress))),
        issues=tuple(issues),
    )


def planning_disposition(report: DataQualityReport) -> str:
    """What a workflow should do given the data quality (directive §24). Never let
    partial/stale data be presented as a complete current-period conclusion:
      * ok            → CONTINUE
      * stale         → REFRESH (re-sync before concluding)
      * in_progress   → CONTINUE_WITH_LIMITATIONS (disclose 'computing')
      * missing       → CONTINUE_WITH_LIMITATIONS (disclose the gap) — or ESCALATE if total
      * failed        → ESCALATE (a failed sync is not a partial-data situation)
    """
    if report.sync_status == SYNC_OK:
        return CONTINUE
    if report.sync_status == SYNC_FAILED:
        return ESCALATE
    if report.sync_status == SYNC_STALE:
        return REFRESH
    if report.sync_status == SYNC_MISSING:
        # No coverage at all → nothing to work with; escalate. Partial gap → disclose.
        return ESCALATE if not report.covered_periods else CONTINUE_WITH_LIMITATIONS
    if report.sync_status == SYNC_IN_PROGRESS:
        return CONTINUE_WITH_LIMITATIONS
    return CONTINUE

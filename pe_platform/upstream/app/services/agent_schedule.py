from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SUPPORTED_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY"}


def normalize_objective_schedule(schedule: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize the simple UTC schedule format used by AgentObjective."""
    raw = dict(schedule or {})
    frequency = str(raw.get("frequency", "DAILY")).upper()
    if frequency not in SUPPORTED_FREQUENCIES:
        raise ValueError(f"Unsupported frequency '{frequency}'. Use DAILY, WEEKLY, or MONTHLY.")

    hour_utc = int(raw.get("hour_utc", 8))
    minute_utc = int(raw.get("minute_utc", 0))
    if hour_utc < 0 or hour_utc > 23:
        raise ValueError("hour_utc must be between 0 and 23.")
    if minute_utc < 0 or minute_utc > 59:
        raise ValueError("minute_utc must be between 0 and 59.")

    normalized: Dict[str, Any] = {
        "frequency": frequency,
        "hour_utc": hour_utc,
        "minute_utc": minute_utc,
    }
    tz_name = str(raw.get("timezone", "UTC") or "UTC")
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unsupported timezone '{tz_name}'.") from exc
    normalized["timezone"] = tz_name

    if frequency == "WEEKLY":
        day_of_week = int(raw.get("day_of_week", 0))
        if day_of_week < 0 or day_of_week > 6:
            raise ValueError("day_of_week must be 0-6, where 0 is Monday.")
        normalized["day_of_week"] = day_of_week

    if frequency == "MONTHLY":
        day_of_month = int(raw.get("day_of_month", 1))
        if day_of_month < 1 or day_of_month > 28:
            raise ValueError("day_of_month must be 1-28.")
        normalized["day_of_month"] = day_of_month

    return normalized


def compute_next_run_at(schedule: Dict[str, Any], after: Optional[datetime] = None) -> datetime:
    """Return the next UTC fire time strictly after `after`.

    `after` is treated as UTC. The returned value is a naive UTC datetime to
    match the rest of the app's existing scheduler code.
    """
    normalized = normalize_objective_schedule(schedule)
    after_utc = after or datetime.utcnow()
    if after_utc.tzinfo is None:
        after_utc = after_utc.replace(tzinfo=timezone.utc)
    else:
        after_utc = after_utc.astimezone(timezone.utc)

    local_tz = ZoneInfo(normalized["timezone"])
    cursor = after_utc.astimezone(local_tz).replace(second=0, microsecond=0)
    hour = normalized["hour_utc"]
    minute = normalized["minute_utc"]
    frequency = normalized["frequency"]

    if frequency == "DAILY":
        candidate = cursor.replace(hour=hour, minute=minute)
        if candidate <= cursor:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc).replace(tzinfo=None)

    if frequency == "WEEKLY":
        target_dow = normalized["day_of_week"]
        days_ahead = (target_dow - cursor.weekday()) % 7
        candidate = (cursor + timedelta(days=days_ahead)).replace(hour=hour, minute=minute)
        if candidate <= cursor:
            candidate += timedelta(days=7)
        return candidate.astimezone(timezone.utc).replace(tzinfo=None)

    day_of_month = normalized["day_of_month"]
    year = cursor.year
    month = cursor.month
    candidate = cursor.replace(day=min(day_of_month, 28), hour=hour, minute=minute)
    if candidate <= cursor:
        month += 1
        if month == 13:
            month = 1
            year += 1
        candidate = candidate.replace(year=year, month=month, day=min(day_of_month, 28))
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)

"""Autonomy circuit breaker (Phase 2 PR2).

Fails CLOSED: it only ever DEMOTES a supervised_l3 task back to proposal_first —
it never promotes. When an L3 categorization task shows degradation (ANY
high-severity miss on a would-be-auto item, or a human-override-rate spike over
the recent window), the breaker revokes autonomy immediately and alerts.

Wired to run on correction capture for L3 tasks (see correction_capture). Best-
effort: a breaker failure must never break the human's action.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AICorrectionEvent
from .task_autonomy import effective_level, set_autonomy

logger = logging.getLogger(__name__)

_AGREEING_DECISIONS = {"accepted", "confirmed"}


def maybe_trip_breaker(
    db: Session,
    *,
    organization_id: int,
    task_type: str,
    window_days: int = 7,
    commit: bool = False,
) -> dict[str, Any]:
    """Demote an L3 task to proposal_first on degradation. No-op otherwise.

    Triggers:
      - ANY high-severity miss: a would-be-auto item the human overrode
        (would_autoexecute is True AND matched_human is not True).
      - Override-rate spike: over >= ``autonomy_breaker_min_samples`` recent
        corrections, the human-override rate exceeds ``autonomy_breaker_override_rate``.

    Only acts when the current level is supervised_l3 (fail-closed: never
    promotes). Returns ``{tripped, reason, ...}``.
    """
    try:
        level = effective_level(db, organization_id, task_type)
        if level != "supervised_l3":
            return {"tripped": False, "reason": "not_l3", "level": level}

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=window_days)
        rows = (
            db.query(AICorrectionEvent)
            .filter(
                AICorrectionEvent.organization_id == organization_id,
                AICorrectionEvent.task_type == task_type,
                AICorrectionEvent.created_at >= window_start,
            )
            .all()
        )

        high_sev_misses = sum(
            1 for r in rows if r.would_autoexecute is True and r.matched_human is not True
        )
        total = len(rows)
        overrides = sum(1 for r in rows if r.human_decision not in _AGREEING_DECISIONS)
        override_rate = (overrides / total) if total else 0.0

        trip_reason = None
        if high_sev_misses > 0:
            trip_reason = f"high_severity_miss ({high_sev_misses})"
        elif total >= settings.autonomy_breaker_min_samples and override_rate > settings.autonomy_breaker_override_rate:
            trip_reason = f"override_rate_spike ({override_rate:.2f} > {settings.autonomy_breaker_override_rate})"

        if trip_reason is None:
            return {
                "tripped": False,
                "reason": "healthy",
                "level": level,
                "high_severity_miss_count": high_sev_misses,
                "override_rate": override_rate,
                "samples": total,
            }

        set_autonomy(
            db,
            organization_id=organization_id,
            task_type=task_type,
            level="proposal_first",
            updated_by="circuit_breaker",
            commit=commit,
        )
        logger.warning(
            "[autonomy_guard] CIRCUIT BREAKER tripped for org=%s task=%s: %s — demoted to proposal_first",
            organization_id,
            task_type,
            trip_reason,
        )
        return {
            "tripped": True,
            "reason": trip_reason,
            "demoted_to": "proposal_first",
            "high_severity_miss_count": high_sev_misses,
            "override_rate": override_rate,
            "samples": total,
        }
    except Exception as exc:  # pragma: no cover - defensive; never break capture
        logger.debug("[autonomy_guard] breaker evaluation failed: %s", exc)
        return {"tripped": False, "reason": "error"}

"""Canonical reconciliation-session status semantics.

The durable schema stores ``OPEN`` explicitly, but a downgraded or legacy row
may still carry ``NULL``.  Every runtime boundary treats that historical value
as ``OPEN``; no caller gets to invent a separate compatibility rule.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func

STATUS_OPEN = "OPEN"
STATUS_COMPLETE = "COMPLETE"
STATUS_ABANDONED = "ABANDONED"


def effective_session_status(status: str | None) -> str:
    """Return the canonical status, mapping the historical NULL state to OPEN."""
    return STATUS_OPEN if status is None else status


def is_open_session_status(status: str | None) -> bool:
    """Whether an instance status authorizes an OPEN-session action."""
    return effective_session_status(status) == STATUS_OPEN


def open_session_status_clause(status_column: Any):
    """SQL predicate matching the Python NULL=>OPEN instance rule."""
    return func.coalesce(status_column, STATUS_OPEN) == STATUS_OPEN


def live_session_status_clause(status_column: Any):
    """SQL predicate for an exact-period row not released by abandonment."""
    return func.coalesce(status_column, STATUS_OPEN) != STATUS_ABANDONED


__all__ = [
    "STATUS_ABANDONED",
    "STATUS_COMPLETE",
    "STATUS_OPEN",
    "effective_session_status",
    "is_open_session_status",
    "live_session_status_clause",
    "open_session_status_clause",
]

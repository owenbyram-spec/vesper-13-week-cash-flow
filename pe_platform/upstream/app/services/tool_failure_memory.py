"""Best-effort RECORD / EXPIRE for ToolFailureMemory rows (audit LG-5, Wave 3.3).

A per-(org, realm, tool, error_class) health aggregate the agent reads as an
always-on caveat ("tool X has been failing"). Two entry points, both called FROM
``app/agent/runtime/tool_executor.py``:

  * ``record_tool_failure`` — on a tool RUNTIME failure, UPSERT the aggregate
    (increment ``failure_count``, slide the ``expires_at`` TTL, refresh the redacted
    error, and re-open a previously-resolved row).
  * ``resolve_tool_failure`` — on that tool's next SUCCESS, soft-resolve
    (``resolved_at = now``) the realm's open rows for the tool so the caveat clears.

Both are FAIL-OPEN (a memory-write failure never breaks the tool call) and run on
their OWN short-lived, committed ``SessionLocal`` — the record seam has already
``db.rollback()``'d the failing tool transaction, so the write cannot ride it (the
``record_llm_usage`` pattern). Skip entirely when ``organization_id`` is None (no
NULL-org orphan — the ``AICorrectionEvent`` / ``correction_capture`` convention).

**Load-bearing (E-B/E-C):** the writes use the SQLAlchemy **Core** form
(``pg_insert`` / ``sqlite_insert`` ``.on_conflict_do_update`` for RECORD,
``update(...)`` for EXPIRE) — NEVER an ORM ``.add(ToolFailureMemory(...))``. An ORM
construction of a ``*Memory`` object at the ``app/agent/runtime/*`` executor seam
trips the AWB-06 ``pre-write-untrusted-content-authority`` structural no-escape wall;
this service lives in ``app/services/`` (outside that path scope) and the Core form
constructs no ORM object.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..config import settings
from ..db import SessionLocal
from ..models import ToolFailureMemory
from ..tenant_rls import tenant_scope_live

logger = logging.getLogger(__name__)

# The UPSERT conflict target — mirrors the UNIQUE(org, realm, tool, error_class).
_CONFLICT_COLS = ["organization_id", "company_id", "tool_name", "error_class"]


def record_tool_failure(
    *,
    organization_id: int | None,
    company_id: str,
    tool_name: str,
    error_class: str,
    last_error_redacted: str | None = None,
    organization_company_id: int | None = None) -> None:
    """UPSERT the (org, realm, tool, error_class) failure aggregate. Never raises.

    A retried failure bumps ``failure_count`` (correct — it failed again) but never
    creates a duplicate row (the UNIQUE key + ON CONFLICT DO UPDATE); it also
    re-opens a previously-resolved row (``resolved_at = NULL``) — a fresh failure
    means the tool is broken again.
    """
    if organization_id is None:
        return  # no NULL-org orphan (correction_capture refuse-to-write convention)
    if not company_id or not tool_name or not error_class:
        return
    try:
        now = datetime.utcnow()
        expires_at = now + timedelta(days=max(1, settings.tool_failure_ttl_days))
        session = SessionLocal()
        try:
            # org-keyed FORCE-RLS: pin the real org+realm scope so the WITH CHECK
            # (organization_id == app.current_org_id) passes. No-op on SQLite.
            with tenant_scope_live(session, org_id=organization_id, realm_id=company_id):
                insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
                stmt = insert(ToolFailureMemory).values(
                    organization_id=organization_id,
                    organization_company_id=organization_company_id,
                    company_id=company_id,
                    tool_name=tool_name,
                    error_class=error_class,
                    failure_count=1,
                    first_seen=now,
                    last_seen=now,
                    resolved_at=None,
                    expires_at=expires_at,
                    last_error_redacted=last_error_redacted)
                stmt = stmt.on_conflict_do_update(
                    index_elements=_CONFLICT_COLS,
                    set_={
                        "failure_count": ToolFailureMemory.failure_count + 1,
                        "last_seen": now,
                        "expires_at": expires_at,
                        "resolved_at": None,
                        "last_error_redacted": last_error_redacted})
                session.execute(stmt)
                session.commit()
        finally:
            session.close()
    except Exception as exc:  # fail-open: a memory-write failure never breaks the tool call
        logger.warning(
            "[ToolFailureMemory] failed to record tool failure (tool=%s class=%s): %s",
            tool_name, error_class, exc)


def resolve_tool_failure(
    *,
    organization_id: int | None,
    company_id: str,
    tool_name: str) -> None:
    """Soft-resolve the realm's OPEN rows for a tool on its next SUCCESS. Never raises.

    Idempotent + realm-scoped (a success in tenant A never clears tenant B's caveat):
    sets ``resolved_at = now`` on every ``resolved_at IS NULL`` row for
    (org, realm, tool), so retrieval (``resolved_at IS NULL AND expires_at > now``)
    stops surfacing it. A success on a tool with no open row is a no-op UPDATE.
    """
    if organization_id is None:
        return
    if not company_id or not tool_name:
        return
    try:
        now = datetime.utcnow()
        session = SessionLocal()
        try:
            with tenant_scope_live(session, org_id=organization_id, realm_id=company_id):
                session.execute(
                    update(ToolFailureMemory)
                    .where(
                        ToolFailureMemory.organization_id == organization_id,
                        ToolFailureMemory.company_id == company_id,
                        ToolFailureMemory.tool_name == tool_name,
                        ToolFailureMemory.resolved_at.is_(None))
                    .values(resolved_at=now))
                session.commit()
        finally:
            session.close()
    except Exception as exc:  # fail-open: a memory-write failure never breaks the tool call
        logger.warning("[ToolFailureMemory] failed to resolve tool failure (tool=%s): %s", tool_name, exc)

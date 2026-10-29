from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from ..models import SystemAuditLog
import logging

logger = logging.getLogger(__name__)

# Merged-read imports (kept off the legacy `Any` import line above to avoid
# re-tripping the diff-vs-HEAD bare-Any ratchet on an otherwise-unchanged line).
from sqlalchemy import desc  # noqa: E402
from ..models import AuditLog  # noqa: E402

def log_audit_event(
    db: Session,
    company_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    user_id: Optional[int] = None,
    previous_state: Optional[Dict[str, Any]] = None,
    new_state: Optional[Dict[str, Any]] = None
) -> Optional[SystemAuditLog]:
    """
    Writes an immutable audit log entry.
    """
    try:
        audit_log = SystemAuditLog(
            company_id=company_id,
            user_id=user_id,
            action=action.upper(),
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else "",
            previous_state=previous_state,
            new_state=new_state,
            timestamp=datetime.utcnow()
        )
        db.add(audit_log)
        # We do not commit here, we rely on the parent transaction to commit it
        # guaranteeing the change and the audit log succeed or fail together.
        return audit_log
    except Exception as e:
        logger.error(f"[Audit] Failed to log event {action} for {entity_type} {entity_id}: {e}")
        return None


def log_action(
    db: Session,
    realm_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: Optional[Dict[str, Any]] = None,
    user_id: Optional[int | str] = None,
) -> Optional[SystemAuditLog]:
    """
    Backward-compatible audit helper used by older write-back routes.
    """
    numeric_user_id = user_id if isinstance(user_id, int) else None
    return log_audit_event(
        db=db,
        company_id=realm_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=numeric_user_id,
        new_state=details,
    )


def _normalize_audit_log(row: AuditLog) -> dict:
    """Normalize a legacy AuditLog row (lock/unlock, proactive-review,
    close-orchestration) into the one merged audit shape."""
    return {
        "id": f"audit:{row.id}",
        "source": "audit_log",
        "realm_id": row.realm_id,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "user_id": str(row.user_id) if row.user_id is not None else None,
        "details": row.details,
    }


def _normalize_system_audit_log(row: SystemAuditLog) -> dict:
    """Normalize a SystemAuditLog row (JE create, classify-approve, schedule,
    teach, and the other real financial mutations) into the one merged shape.

    The merged `details` surfaces the write snapshot (`new_state`), folding in
    `previous_state` when present so the audit page renders a single column."""
    if row.previous_state is not None:
        details = {"previous_state": row.previous_state, "new_state": row.new_state}
    else:
        details = row.new_state
    return {
        "id": f"system:{row.id}",
        "source": "system_audit_log",
        # company_id IS the realm on this table — expose it under realm_id so the
        # merged shape has one tenant key the client filters/searches on.
        "realm_id": row.company_id,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "user_id": str(row.user_id) if row.user_id is not None else None,
        "details": details,
    }


def fetch_merged_audit_logs(
    db: Session,
    *,
    realm_id: str | None = None,
    limit: int = 100,
    user_id: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> list[dict]:
    """Read EVERY recorded write for the (already-authorized) realm by UNIONing
    AuditLog + SystemAuditLog, normalizing both to one shape, and merging them
    ordered by timestamp desc.

    Tenant scoping is the CALLER's responsibility — the router validates realm
    access (the priority-admin vs ORG_ADMIN/VIEWER gates) BEFORE calling this and
    passes the verified `realm_id`. When `realm_id` is None (priority-admin, all
    realms), no realm predicate is applied; otherwise BOTH tables are filtered to
    the realm (AuditLog.realm_id and SystemAuditLog.company_id — both ARE the
    realm). The user_id/action/entity_type/period filters apply to BOTH tables.
    """
    audit_q = db.query(AuditLog)
    system_q = db.query(SystemAuditLog)

    if realm_id:
        audit_q = audit_q.filter(AuditLog.realm_id == realm_id)
        system_q = system_q.filter(SystemAuditLog.company_id == realm_id)

    if action:
        audit_q = audit_q.filter(AuditLog.action == action)
        system_q = system_q.filter(SystemAuditLog.action == action)
    if entity_type:
        audit_q = audit_q.filter(AuditLog.entity_type == entity_type)
        system_q = system_q.filter(SystemAuditLog.entity_type == entity_type)
    if user_id:
        # AuditLog.user_id is a String column; SystemAuditLog.user_id is an Integer
        # (NULL for AI-agent actions). Match each table on its own column type.
        audit_q = audit_q.filter(AuditLog.user_id == user_id)
        if user_id.isdigit():
            system_q = system_q.filter(SystemAuditLog.user_id == int(user_id))
        else:
            # A non-numeric user filter can never match the integer column — return
            # zero system rows for it (a non-matchable id) rather than cast-crash.
            system_q = system_q.filter(SystemAuditLog.id == None).filter(SystemAuditLog.id != None)  # noqa: E711

    if period_start is not None and period_end is not None:
        audit_q = audit_q.filter(
            AuditLog.timestamp >= period_start, AuditLog.timestamp < period_end
        )
        system_q = system_q.filter(
            SystemAuditLog.timestamp >= period_start,
            SystemAuditLog.timestamp < period_end,
        )

    # Pull `limit` from each table, merge, then trim to `limit` — so the final
    # window is the true newest-N across BOTH tables (not N-from-one).
    audit_rows = audit_q.order_by(desc(AuditLog.timestamp)).limit(limit).all()
    system_rows = system_q.order_by(desc(SystemAuditLog.timestamp)).limit(limit).all()

    merged = [_normalize_audit_log(r) for r in audit_rows]
    merged += [_normalize_system_audit_log(r) for r in system_rows]

    # Order by timestamp desc; None timestamps sort last (treated as epoch).
    merged.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return merged[:limit]

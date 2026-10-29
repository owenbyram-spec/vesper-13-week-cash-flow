"""Celery entrypoints for the QBO write-back recovery lane.

``sweep_stranded_qbo_actions`` (the periodic entry point) finds every realm with
a stale ``running`` execution row or an APPROVED action whose QuickBooks outcome
is ``retryable_failed`` / ``uncertain``, and fans out one
``recover_qbo_actions_for_realm`` child per realm. Each child pins that realm's
tenant scope and runs ``app.qbo_actions.recovery.recover_realm``: reap stale
``running`` rows to ``uncertain``, then re-run each recoverable action under its
own idempotency key so the reconcile probe decides before any re-post.

Idempotent under acks_late redelivery: the reaper flips a row only from the
observed ``running`` attempt, and every re-run claims the ledger row through the
executor's compare-and-set, so two concurrent children cannot both post.
"""
from __future__ import annotations

import logging
from typing import Any

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)

SWEEP_TASK_NAME = "app.tasks.qbo_action_recovery.sweep_stranded_qbo_actions"
REALM_TASK_NAME = "app.tasks.qbo_action_recovery.recover_qbo_actions_for_realm"


@celery_app.task(bind=True, max_retries=2, name=SWEEP_TASK_NAME)
def sweep_stranded_qbo_actions(self) -> dict[str, Any]:
    """Fan out one recovery child per realm with stranded QBO write-backs."""
    from ..qbo_actions.recovery import realms_needing_recovery

    db = SessionLocal()
    try:
        # SYSTEM census across tenants (read-only); each child re-pins its realm.
        with tenant_scope(bypass=True):
            realms = realms_needing_recovery(db)
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()

    for realm_id in realms:
        recover_qbo_actions_for_realm.delay(realm_id)
    if realms:
        logger.info("[qbo_action_recovery] dispatched recovery for %s realm(s)", len(realms))
    return {"status": "dispatched", "realms": len(realms)}


@celery_app.task(bind=True, max_retries=2, name=REALM_TASK_NAME)
def recover_qbo_actions_for_realm(self, realm_id: str) -> dict[str, Any]:
    """Reap and re-run one realm's stranded QBO write-backs under its tenant scope."""
    from ..qbo_actions.recovery import recover_realm

    db = SessionLocal()
    try:
        with tenant_scope_for_realm(db, realm_id):
            result = recover_realm(db, realm_id)
        return {"status": "done", **result.as_dict()}
    except Exception as exc:
        db.rollback()
        logger.warning(
            "[qbo_action_recovery] realm %s recovery failed: %s",
            realm_id,
            redact_to_string(str(exc), max_length=500),
        )
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()

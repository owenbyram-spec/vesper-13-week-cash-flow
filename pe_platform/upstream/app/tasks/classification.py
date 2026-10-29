# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel chunk C1 (marker
# RATIFICATION-2026-07-13.md on disk; active-plan.json sidecar absent in this
# dispatched session — Wave-0 precedent).
"""Daily categorization sweep task (Wave-1 C1, WF-1).

The old body of this task classified pending transactions and then DISCARDED
the output (a rule hit fell into a ``pass`` branch; the module's own comment
called it "theoretical"). It now calls
``app/services/categorization_sweep.run_categorization_sweep`` per realm, which
lands executable PENDING ``RECLASSIFY_TXN`` proposals (pinned payload contract,
execution-time SyncToken resolution) with gated L3 auto-execution — shadow
default routes 100% to the human inbox.
"""
import logging

from sqlalchemy import desc

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models import QBOTransaction
from ..services.categorization_sweep import (
    CATEGORIZATION_ENTITY_TYPES,
    run_categorization_sweep,
)
from ..services.redaction import redact_to_string
from ..tenant_rls import tenant_scope, tenant_scope_for_realm

logger = logging.getLogger(__name__)


# IDEMPOTENT-OK: the sweep dedups before any insert — one batched
# inputs_fingerprint IN query per run against still-PENDING reclassification
# proposals (AC-3), and disposed predictions are filtered out — so an
# acks_late at-least-once redelivery re-runs to zero new rows; the QBO write
# side is additionally guarded by the QBOActionExecution SHA-256 ledger.
@celery_app.task(bind=True)
def classify_pending_transactions_task(self, limit: int = 50):
    """Sweep every realm's pending (uncategorized) transactions into
    executable PENDING reclassification proposals. ``limit`` is per realm."""
    db = SessionLocal()
    try:
        # CROSS-TENANT SWEEP: the realm-enumeration query spans ALL tenants, so
        # the orchestration runs under bypass; the actual sweep work is
        # re-scoped per realm below (no unit runs unscoped).
        with tenant_scope(bypass=True):
            realm_rows = (
                db.query(QBOTransaction.realm_id)  # CROSS-TENANT-OK: all-tenants pending-classification realm enumeration; each realm re-scoped under tenant_scope_for_realm below
                .filter(
                    QBOTransaction.account_ref.is_(None),
                    QBOTransaction.entity_type.in_(CATEGORIZATION_ENTITY_TYPES),
                )
                .order_by(desc(QBOTransaction.date))
                .distinct()
                .all()
            )
        realm_ids = [row[0] for row in realm_rows if row[0]]

        results = []
        for realm_id in realm_ids:  # SA-OK: bounded-loop realm fan — each realm must run under its own RLS GUC + LLM-attribution binding; realm count is bounded by connected-tenant count
            try:
                results.append(_sweep_one_realm(db, realm_id, limit))
            except Exception as exc:
                # One realm's failure must not abort the remaining realms —
                # roll back its partial state and continue.
                db.rollback()
                logger.error(
                    "[categorization_sweep] realm %s sweep failed: %s",
                    realm_id,
                    redact_to_string(str(exc), max_length=500),
                )
                results.append({"realm_id": realm_id, "error": "sweep_failed"})

        return {"realms": len(realm_ids), "results": results}
    finally:
        db.close()


def _sweep_one_realm(db, realm_id: str, limit: int) -> dict:
    """Run one realm's sweep under its own tenant scope + LLM attribution."""
    from ..services.tenant_companies import bind_tenant_tag_for_realm

    # Bind org+company+realm so the LLM-classifier fallback calls inside the
    # sweep are attributed (a background task is NULL-realm otherwise).
    with tenant_scope_for_realm(db, realm_id), bind_tenant_tag_for_realm(db, realm_id):
        return run_categorization_sweep(db, realm_id, limit=limit)

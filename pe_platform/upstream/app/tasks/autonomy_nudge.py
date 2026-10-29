# PLAN-EXEMPT: ratified plan 20260713-wave1-learning-flywheel (marker
# RATIFICATION-2026-07-13.md) — C4 / AC-9 (WF-11): once-per-transition autonomy READY nudge.
"""Once-per-transition autonomy READY nudge (WF-11, plan decision 7).

The promotion gate (``promotion_gate_summary`` — the SAME service the
``GET /organizations/{id}/ai-promotion-gate`` endpoint calls) computes a
READY / NOT_READY verdict per tenant + task. Until now that verdict lived
behind a GET nobody polls. This beat-scheduled sweep surfaces it: when a
tenant's gate transitions to READY, ONE ``WatchdogAlert`` per realm of the org
is created carrying the gate's evidence fields (volume / precision / misses /
cycles / verdict), so an admin sees "this task has earned promotion — go
review it".

Transition state is verdict-in-context (migration-free): each nudge alert
stamps the gate verdict into its ``context`` JSON, and every run compares the
LATEST nudge alert's stamped verdict against the current one —

- READY persisting  → no new alert (the stamped READY suppresses re-nudging);
- READY → NOT_READY → the latest alert's stamped verdict is updated to
  NOT_READY (the re-arm), no new alert;
- drop → regain     → the stamped NOT_READY no longer matches → ONE new alert.

The existing 90-day-sticky fingerprint machinery
(``app/services/alert_fingerprints.py``) is deliberately NOT used for
suppression: its dismiss-and-stay-quiet semantics cannot express
re-arm-on-transition. The stable fingerprint is still stamped on the alert so
the feedback loop (LG-6) can key on it.

NEVER promotes: this module reads the gate and writes evidence alerts only.
The promotion mutation (``set_autonomy`` / the task-autonomy PUT) is
deliberately not imported — promotion stays a human keyboard action
(grep-asserted by ``tests/test_autonomy_nudge.py``).

Org → realm mapping: the gate is org-keyed (AICorrectionEvent) while
WatchdogAlert is realm-keyed and served per company, so a READY org nudges
once per ACTIVE mapped realm (``OrganizationCompany.status == "active"``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..celery_app import celery_app
from ..db import SessionLocal
from ..services.redaction import redact_to_string
from ..services.shadow_scoring import default_window, promotion_gate_summary
from ..tenant_rls import tenant_scope, tenant_scope_live

logger = logging.getLogger(__name__)

# The nudge alert's type — a new WatchdogAlert vocabulary entry, additive only.
AUTONOMY_READY_ALERT_TYPE = "AUTONOMY_PROMOTION_READY"

# Task types the nudge evaluates. Wave 1 promotes exactly the categorization
# lane (the C1 sweep's autonomy lane); extend the tuple when another task type
# earns a shadow-scored gate.
NUDGE_TASK_TYPES = ("categorization",)

# How many recent nudge-type alerts to scan when resolving "the latest nudge
# alert for this task type" (the context task_type match happens in Python —
# portable across the PG/sqlite JSON dialects). Nudges are rare (one per
# transition), so a small bound is generous.
_LATEST_NUDGE_SCAN_LIMIT = 20


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_or_none(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _latest_nudge_alert(db: Session, realm_id: str, task_type: str):
    """Newest AUTONOMY_PROMOTION_READY alert for this realm + task type (or None)."""
    from ..models import WatchdogAlert

    rows = (
        db.query(WatchdogAlert)
        .filter(
            WatchdogAlert.company_id == realm_id,
            WatchdogAlert.alert_type == AUTONOMY_READY_ALERT_TYPE,
        )
        .order_by(WatchdogAlert.id.desc())
        .limit(_LATEST_NUDGE_SCAN_LIMIT)
        .all()
    )
    for row in rows:
        if (row.context or {}).get("task_type") == task_type:
            return row
    return None


def _create_ready_alert(db: Session, *, realm_id: str, organization_id: int,
                        task_type: str, gate: dict) -> None:
    """One evidence-carrying READY nudge alert for one realm. Verdict stamped
    into the context JSON — the transition state the next run compares against.

    Idempotency: every caller (``_nudge_realm``) runs the verdict-compare
    existence check FIRST — a redelivered run sees the stamped READY and never
    reaches this create.
    """
    from ..models import WatchdogAlert
    from ..services.alert_fingerprints import compute_fingerprint

    precision = gate.get("precision")
    evidence = {
        "would_autoexecute_volume": gate.get("would_autoexecute_volume"),
        "precision": precision,
        "high_severity_miss_count": gate.get("high_severity_miss_count"),
        "cycles": gate.get("cycles"),
        "bar": gate.get("bar"),
        "verdict": gate.get("verdict"),
        "window_start": _iso_or_none(gate.get("start")),
        "window_end": _iso_or_none(gate.get("end")),
    }
    alert = WatchdogAlert(
        company_id=realm_id,
        alert_type=AUTONOMY_READY_ALERT_TYPE,
        severity="info",
        title=f"AI {task_type} has earned supervised autonomy — review the promotion gate",
        description=(
            f"The promotion gate for '{task_type}' is READY: "
            f"{gate.get('would_autoexecute_volume')} would-be-auto predictions over "
            f"{gate.get('cycles')} activity days at {float(precision or 0.0):.1%} precision "
            f"with {gate.get('high_severity_miss_count')} high-severity misses. "
            "Review the evidence and promote the task under AI autonomy settings — "
            "Peregrine never promotes on its own."
        ),
        metric_value=precision,
        metric_unit="score",
        context={
            "source": "autonomy_nudge",
            "task_type": task_type,
            "organization_id": organization_id,
            "gate_verdict": str(gate.get("verdict")),
            "gate_verdict_updated_at": _utcnow_iso(),
            "evidence": evidence,
        },
        # Stable identity for the feedback loop (LG-6). Suppression does NOT run
        # through the 90-day-sticky machinery — transition state is the stamped
        # verdict above, which re-arms on drop→regain.
        fingerprint=compute_fingerprint("watchdog", realm_id, AUTONOMY_READY_ALERT_TYPE, None, task_type),
    )
    db.add(alert)  # IDEMPOTENT-OK: guarded by the caller's verdict-compare existence check (_nudge_realm reads the latest stamped verdict first; READY-persisting never reaches this create)
    db.commit()


def _nudge_realm(db: Session, *, realm_id: str, organization_id: int,
                 task_type: str, gate: dict) -> str:
    """Apply the verdict-transition rule for ONE realm. Returns the outcome label.

    Idempotent by construction: the compare against the latest stamped verdict is
    the pre-write existence check — a redelivered run re-reads the same state and
    writes nothing new.
    """
    latest = _latest_nudge_alert(db, realm_id, task_type)
    stamped = (latest.context or {}).get("gate_verdict") if latest is not None else None
    current = str(gate.get("verdict"))

    if current == "READY":
        if stamped == "READY":
            return "ready_already_nudged"
        _create_ready_alert(
            db, realm_id=realm_id, organization_id=organization_id,
            task_type=task_type, gate=gate,
        )
        return "nudged"

    if latest is not None and stamped == "READY":
        # READY → NOT_READY: re-arm by stamping the drop onto the latest nudge
        # alert (JSON column — reassign, never mutate in place, so the change flushes).
        latest.context = {
            **(latest.context or {}),
            "gate_verdict": current,
            "gate_verdict_updated_at": _utcnow_iso(),
        }
        db.commit()  # IDEMPOTENT-OK: set-to-fixed-state re-arm stamp — a redelivery re-writes the same NOT_READY verdict onto the same latest alert
        return "re_armed"
    return "not_ready"


def _nudge_org(db: Session, organization_id: int) -> dict:
    """Per-tenant unit of work: evaluate the gate once per task type and apply the
    transition rule to every ACTIVE realm of the org. Pure unit — the task wraps it;
    tests drive it directly (the ``_probe_realm_health`` pattern)."""
    from ..models import OrganizationCompany
    # Read-only level check. The promotion MUTATION (set_autonomy / the PUT) is
    # deliberately NOT imported anywhere in this module.
    from ..services.task_autonomy import effective_level

    realms = [
        row.realm_id
        for row in (
            db.query(OrganizationCompany)
            .filter(
                OrganizationCompany.organization_id == organization_id,
                OrganizationCompany.status == "active",
                OrganizationCompany.realm_id.isnot(None),
            )
            .all()
        )
    ]

    results: dict[str, dict] = {}
    for task_type in NUDGE_TASK_TYPES:
        if effective_level(db, organization_id, task_type) == "supervised_l3":
            # Already promoted — a READY nudge would be noise; nothing to arm.
            results[task_type] = {"status": "already_promoted"}
            continue
        window_start, window_end = default_window()
        # The SAME service the GET /ai-promotion-gate endpoint calls (AC-9).
        gate = promotion_gate_summary(
            db,
            organization_id=organization_id,
            task_type=task_type,
            start=window_start,
            end=window_end,
        )
        per_realm: dict[str, str] = {}
        # SA-OK: bounded-loop — a tenant maps a handful of realms, and each realm's
        # WatchdogAlert read/write must run under its OWN realm pin (realm-keyed RLS).
        for realm_id in realms:
            with tenant_scope_live(db, org_id=organization_id, realm_id=realm_id):
                per_realm[realm_id] = _nudge_realm(
                    db, realm_id=realm_id, organization_id=organization_id,
                    task_type=task_type, gate=gate,
                )
        results[task_type] = {"verdict": gate.get("verdict"), "realms": per_realm}
    return {"organization_id": organization_id, "results": results}


@celery_app.task(bind=True, max_retries=2, name="app.tasks.autonomy_nudge.nudge_autonomy_ready_for_org")
def nudge_autonomy_ready_for_org(self, organization_id: int | None = None) -> dict:
    """READY-nudge check for ONE tenant. Strictly org-scoped.

    IDEMPOTENT-OK: the verdict-compare against the latest stamped nudge alert is the
    pre-write existence check — a redelivered/duplicate run re-reads the stamped
    verdict and creates nothing new (READY-persisting is a no-op by design)."""
    if not organization_id:
        logger.warning("[autonomy_nudge] missing organization_id; skipping")
        return {"status": "skipped", "reason": "missing_organization_id"}

    db = SessionLocal()
    try:
        # Pin the org scope for the org-keyed reads (AICorrectionEvent,
        # TenantTaskAutonomy); each realm's alert write re-pins org+realm inside.
        with tenant_scope_live(db, org_id=organization_id):
            result = _nudge_org(db, int(organization_id))
        logger.info("[autonomy_nudge] org=%s results=%s", organization_id, result.get("results"))
        return result
    except Exception as exc:  # noqa: BLE001 — one org's failure must never crash the beat sweep
        db.rollback()
        logger.error(
            "[autonomy_nudge] org=%s nudge failed: %s",
            organization_id, redact_to_string(str(exc), max_length=300),
        )
        # Capped exponential backoff (AP-CEL-4): 120s, 240s, bounded at 480s.
        raise self.retry(exc=exc, countdown=min(120 * (2 ** self.request.retries), 480)) from exc
    finally:
        db.close()


@celery_app.task(name="app.tasks.autonomy_nudge.check_all_autonomy_gates")
def check_all_autonomy_gates() -> dict:
    """Beat-scheduled fan-out (AP-CEL-9): one org-scoped child per active tenant, so
    each child stays small and under the broker visibility timeout. Mirrors
    ``check_all_qbo_connections``.

    IDEMPOTENT-OK: dispatch-only — the side-effect guard (verdict-compare existence
    check) lives in each child; re-dispatching converges to the same alerts."""
    from ..models import Organization, OrganizationCompany

    db = SessionLocal()
    try:
        # SWEEP orchestration: the cross-tenant enumeration runs under bypass over the
        # RLS-excluded org/company mapping; each fanned-out child re-pins its own org.
        with tenant_scope(bypass=True):  # CROSS-TENANT-OK: vetted sweep enumeration over the RLS-excluded organizations/organization_companies mapping; each child re-pins its own org scope
            org_ids = [
                row[0]
                for row in (
                    db.query(OrganizationCompany.organization_id)
                    .join(Organization, Organization.id == OrganizationCompany.organization_id)
                    .filter(
                        OrganizationCompany.status == "active",
                        OrganizationCompany.realm_id.isnot(None),
                        Organization.is_active.is_(True),
                    )
                    .distinct()
                    .all()
                )
            ]
    finally:
        db.close()

    for org_id in org_ids:
        nudge_autonomy_ready_for_org.delay(organization_id=org_id)
    logger.info("[autonomy_nudge] dispatched=%s org nudge checks", len(org_ids))
    return {"dispatched": len(org_ids)}
